"""Deterministic data functions for the Market Fragility Dashboard fast variables.

The fetchers deliberately keep upstream failures local: callers receive ``None``
or an unavailable metric rather than a partially current-looking value.  Pure
transformations live here so calendar, freshness, status, and parser behavior
can be unit tested without network access.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any, Iterable
import json
import time

import numpy as np
import pandas as pd
import requests

CBOE_VIX_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
CBOE_VIX3M_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX3M_History.csv"
CBOE_VIX9D_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX9D_History.csv"
NYFED_SOFR_URL = "https://markets.newyorkfed.org/api/rates/secured/sofr/last/800.json"
NYFED_REPO_URL = "https://markets.newyorkfed.org/api/rp/repo/all/results/last/500.json"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}&observation_start={start}"
SSGA_SPY_HOLDINGS_URL = "https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx"
TREASURY_AUCTIONS_URL = (
    "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query"
)
FRED_CACHE_DIR = Path(__file__).resolve().parent / "data" / "fred-cache"

AI_BASKET = [
    "NVDA", "MU", "MSFT", "GOOGL", "AMZN", "META", "AVGO", "ORCL", "VRT", "ETN",
    "PWR", "CEG", "TSM", "ANET", "DELL", "SMCI", "KLAC", "AMAT", "LRCX",
]


@dataclass(frozen=True)
class Freshness:
    observation_date: str | None
    fetched_at: str
    cadence: str
    stale: bool
    age_days: int | None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().strftime("%Y-%m-%d %H:%M UTC")


def _as_date(value: date | datetime | str | pd.Timestamp | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return pd.Timestamp(value).date()
    except (TypeError, ValueError):
        return None


def freshness(observation_date: date | datetime | str | None, cadence: str,
              max_age_days: int, now: datetime | None = None) -> Freshness:
    """Separate observation date from fetch timestamp and make stale explicit."""
    current = now or utc_now()
    obs = _as_date(observation_date)
    age = (current.date() - obs).days if obs else None
    return Freshness(
        observation_date=obs.isoformat() if obs else None,
        fetched_at=current.strftime("%Y-%m-%d %H:%M UTC"),
        cadence=cadence,
        stale=(obs is None or age is None or age > max_age_days),
        age_days=age,
    )


def _get(url: str, *, params: dict[str, Any] | None = None, timeout: int = 40,
         retries: int = 3, session: requests.Session | None = None) -> requests.Response:
    http = session or requests.Session()
    headers = {"User-Agent": "Market-Fragility-Dashboard/1.0 (public-data dashboard)"}
    last: Exception | None = None
    for attempt in range(retries):
        try:
            response = http.get(url, params=params, timeout=timeout, headers=headers)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"request failed for {url}: {last}")


def _parse_fred_csv(text: str, series: str) -> pd.Series:
    df = pd.read_csv(StringIO(text))
    if len(df.columns) < 2:
        raise ValueError(f"FRED {series} returned no value column")
    df = df.iloc[:, :2]
    df.columns = ["date", "value"]
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    result = df.dropna().drop_duplicates("date", keep="last").set_index("date")["value"].sort_index()
    if result.empty:
        raise ValueError(f"FRED {series} returned no valid observations")
    return result


def fetch_fred_series(series: str, start: str = "2018-01-01", *, session=None) -> pd.Series:
    """Read the official FRED endpoint, falling back to an archived official CSV.

    The cache is refreshed after a successful live request and only protects the
    dashboard when FRED is temporarily slow or unavailable. Freshness is always
    evaluated from the returned observation date, so a cache can never look
    current once its data is old.
    """
    cache = FRED_CACHE_DIR / f"{series}.csv"
    try:
        response = _get(FRED_URL.format(series=series, start=start), timeout=10, retries=1, session=session)
        series_data = _parse_fred_csv(response.text, series)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(response.text, encoding="utf-8")
        series_data.attrs["source"] = "FRED live"
        return series_data
    except Exception as live_error:
        if not cache.exists():
            raise live_error
        series_data = _parse_fred_csv(cache.read_text(encoding="utf-8"), series)
        series_data.attrs["source"] = "FRED cached official CSV"
        return series_data


def fetch_cboe_history(url: str, *, session=None) -> pd.Series:
    response = _get(url, session=session)
    df = pd.read_csv(StringIO(response.text))
    df.columns = [str(c).strip().upper() for c in df.columns]
    if "DATE" not in df or "CLOSE" not in df:
        raise ValueError("Cboe history is missing DATE/CLOSE")
    df["DATE"] = pd.to_datetime(df["DATE"], errors="coerce")
    df["CLOSE"] = pd.to_numeric(df["CLOSE"], errors="coerce")
    result = df.dropna(subset=["DATE", "CLOSE"]).set_index("DATE")["CLOSE"].sort_index()
    if result.empty:
        raise ValueError("Cboe history returned no valid closes")
    return result


def vix_term_structure(spot: pd.Series, three_month: pd.Series, nine_day: pd.Series,
                       now: datetime | None = None) -> dict[str, Any]:
    """Validate exact-date alignment; never silently calculate a stale ratio."""
    last_spot = spot.dropna().index.max() if len(spot) else None
    last_3m = three_month.dropna().index.max() if len(three_month) else None
    last_9d = nine_day.dropna().index.max() if len(nine_day) else None
    dates = {d.date().isoformat() if d is not None else None for d in (last_spot, last_3m, last_9d)}
    aligned = len(dates) == 1 and None not in dates
    latest_date = last_spot.date() if last_spot is not None else None
    f = freshness(latest_date, "daily business day", 4, now)
    available = bool(aligned and not f.stale)
    if not available:
        return {
            "available": False, "reason": "date mismatch" if not aligned else "stale",
            "spot_date": str(last_spot.date()) if last_spot is not None else None,
            "vix3m_date": str(last_3m.date()) if last_3m is not None else None,
            "vix9d_date": str(last_9d.date()) if last_9d is not None else None,
            "freshness": f.__dict__, "dates": [], "spot_history": [],
        }
    current_date = pd.Timestamp(latest_date)
    return {
        "available": True,
        "spot": float(spot.loc[current_date]),
        "vix3m": float(three_month.loc[current_date]),
        "vix9d": float(nine_day.loc[current_date]),
        "ratio": float(spot.loc[current_date] / three_month.loc[current_date]),
        "observation_date": current_date.strftime("%Y-%m-%d"),
        "freshness": f.__dict__,
        "dates": [d.strftime("%Y-%m-%d") for d in spot.tail(30).index],
        "spot_history": [round(float(v), 2) for v in spot.tail(30).values],
    }


def _business_day_on_or_after(day: date) -> date:
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def _last_business_day(day: date) -> date:
    # day may be any date in target month; find calendar month end first.
    next_month = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    candidate = next_month - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def tax_dates(year: int) -> set[date]:
    """Major US corporate estimated-tax dates adjusted to the next business day."""
    return {_business_day_on_or_after(date(year, month, 15)) for month in (4, 6, 9, 12)}


def calendar_flags(day: date | datetime | str, treasury_settlements: Iterable[date] | None = None) -> list[str]:
    d = _as_date(day)
    if d is None:
        return []
    flags: list[str] = []
    if d == _last_business_day(d):
        flags.append("month-end")
        if d.month in (3, 6, 9, 12):
            flags.append("quarter-end")
    if d in tax_dates(d.year):
        flags.append("corporate tax date")
    settlement_set = {_as_date(x) for x in (treasury_settlements or [])}
    if d in settlement_set:
        flags.append("large Treasury settlement")
    return flags


def parse_treasury_large_settlements(rows: Iterable[dict[str, Any]], threshold_billions: float = 30.0) -> set[date]:
    """Return official Treasury auction issue dates with large offering amounts."""
    result: set[date] = set()
    for row in rows:
        raw_date = row.get("issue_date") or row.get("settlement_date")
        raw_amount = row.get("offering_amt") or row.get("offering_amount")
        d = _as_date(raw_date)
        try:
            amount = float(raw_amount)
        except (TypeError, ValueError):
            continue
        if d and amount >= threshold_billions * 1e9:
            result.add(d)
    return result


def filtered_sofr_iorb(sofr: pd.Series, iorb: pd.Series,
                       treasury_settlements: Iterable[date] | None = None) -> pd.DataFrame:
    """Build raw bps spread and the emphasized median of 5 non-calendar readings."""
    data = pd.concat([sofr.rename("sofr"), iorb.rename("iorb")], axis=1, join="inner").dropna().sort_index()
    data["raw_bp"] = (data["sofr"] - data["iorb"]) * 100.0
    data["flags"] = [calendar_flags(day, treasury_settlements) for day in data.index]
    data["calendar_noise"] = data["flags"].map(bool)
    non_calendar = data.loc[~data["calendar_noise"], "raw_bp"]
    data["filtered_5d_bp"] = non_calendar.rolling(5, min_periods=3).median().reindex(data.index).ffill()
    data["daily_change_bp"] = data["raw_bp"].diff()
    return data


def sofr_iorb_status(data: pd.DataFrame) -> dict[str, Any]:
    if data.empty:
        return {"status": "unavailable", "structural": False, "message": "No matched SOFR/IORB observations"}
    latest = data.iloc[-1]
    if bool(latest["calendar_noise"]):
        return {"status": "calendar-noise", "structural": False,
                "message": "Likely calendar-related: " + ", ".join(latest["flags"])}
    non_calendar = data.loc[~data["calendar_noise"]]
    last3 = non_calendar.tail(3)["raw_bp"]
    watch = len(last3) == 3 and bool((last3 >= 2).all())
    elevated = bool(float(latest["raw_bp"]) >= 5)
    if elevated:
        return {"status": "elevated", "structural": True, "message": "Non-calendar spread at or above +5bp"}
    if watch:
        return {"status": "structural-watch", "structural": True,
                "message": "Three consecutive non-calendar observations at or above +2bp"}
    return {"status": "normal", "structural": False, "message": "No persistent non-calendar pressure"}


def reserves_gdp(reserves_millions: pd.Series, gdp_billions: pd.Series, now: datetime | None = None) -> dict[str, Any]:
    """Forward-fill quarterly GDP onto reserve dates and use a causal 3y percentile."""
    idx = reserves_millions.index
    gdp = gdp_billions.reindex(idx.union(gdp_billions.index)).sort_index().ffill().reindex(idx)
    ratio = (reserves_millions / (gdp * 1000.0) * 100.0).dropna()
    if ratio.empty:
        raise ValueError("No overlapping reserves/GDP observations")
    lookback = 156
    pctl = ratio.rolling(lookback, min_periods=52).apply(lambda x: float((x <= x.iloc[-1]).mean() * 100), raw=False)
    current = float(ratio.iloc[-1])
    cur_pctl = float(pctl.iloc[-1]) if pd.notna(pctl.iloc[-1]) else np.nan
    # Persistence: two successive weekly observations in the same low bucket.
    last2 = pctl.dropna().tail(2)
    elevated = len(last2) == 2 and bool((last2 < 10).all())
    warning = len(last2) == 2 and bool((last2 < 20).all())
    status = "elevated" if elevated else ("warning" if warning else "normal")
    obs = ratio.index[-1]
    f = freshness(obs, "weekly (GDP quarterly, carried forward)", 10, now)
    return {
        "available": not f.stale, "ratio": current,
        "raw_reserves_trillions": float(reserves_millions.reindex(ratio.index).iloc[-1]) / 1e6,
        "percentile": cur_pctl, "status": status, "persistence_weeks": len(last2),
        "observation_date": obs.strftime("%Y-%m-%d"), "freshness": f.__dict__,
        "dates": [d.strftime("%Y-%m-%d") for d in ratio.tail(170).index],
        "history": [round(float(v), 4) for v in ratio.tail(170).values],
        "percentile_history": [round(float(v), 1) if pd.notna(v) else None for v in pctl.tail(170).values],
    }


def parse_sofr_api(payload: dict[str, Any]) -> pd.DataFrame:
    rows = payload.get("refRates", [])
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["median", "p99"])
    df["date"] = pd.to_datetime(df["effectiveDate"], errors="coerce")
    df["median"] = pd.to_numeric(df["percentRate"], errors="coerce")
    df["p99"] = pd.to_numeric(df["percentPercentile99"], errors="coerce")
    return df.dropna(subset=["date", "median", "p99"]).drop_duplicates("date").set_index("date")[["median", "p99"]].sort_index()


def parse_repo_operations(payload: dict[str, Any]) -> pd.Series:
    rows = payload.get("repo", {}).get("operations", [])
    values: dict[pd.Timestamp, float] = {}
    for row in rows:
        d = pd.to_datetime(row.get("operationDate"), errors="coerce")
        try:
            accepted = float(row.get("totalAmtAccepted") or 0)
        except (TypeError, ValueError):
            continue
        if pd.isna(d):
            continue
        values[d.normalize()] = values.get(d.normalize(), 0.0) + accepted / 1e9
    if not values:
        return pd.Series(dtype=float)
    return pd.Series(values).sort_index()


def repo_usage_status(series: pd.Series, now: datetime | None = None) -> dict[str, Any]:
    if series.empty:
        return {"available": False, "status": "unavailable"}
    calendar_index = pd.date_range(series.index.min(), series.index.max(), freq="D")
    daily = series.reindex(calendar_index, fill_value=0.0)
    seven_day = daily.rolling(7, min_periods=1).sum()
    recent_nonzero = (daily.tail(7) > 0).sum()
    # No magic dollar thresholds: persistence and own trailing distribution.
    trailing = seven_day.dropna().tail(252)
    pctl = float((trailing <= seven_day.iloc[-1]).mean() * 100) if len(trailing) >= 20 else np.nan
    status = "watch" if (recent_nonzero >= 3 or (pctl == pctl and pctl >= 90 and seven_day.iloc[-1] > 0)) else "normal"
    obs = daily.index[-1]
    f = freshness(obs, "daily", 4, now)
    return {
        "available": True, "daily": float(daily.iloc[-1]), "seven_day": float(seven_day.iloc[-1]),
        "percentile": pctl, "recent_nonzero_days": int(recent_nonzero), "status": status,
        "observation_date": obs.strftime("%Y-%m-%d"), "freshness": f.__dict__,
        "dates": [d.strftime("%Y-%m-%d") for d in daily.tail(180).index],
        "history": [round(float(v), 3) for v in daily.tail(180).values],
    }


def parse_spy_holdings_excel(content: bytes) -> tuple[str, pd.DataFrame]:
    raw = pd.read_excel(BytesIO(content), header=None)
    as_of = None
    for cell in raw.iloc[:8].to_numpy().ravel():
        cell_text = str(cell)
        if "As of" in cell_text:
            as_of = cell_text.split("As of", 1)[1].strip()
            break
    header_row = next((i for i in raw.index if str(raw.iloc[i, 0]).strip() == "Name"), None)
    if header_row is None:
        raise ValueError("SSGA holdings workbook header not found")
    df = pd.read_excel(BytesIO(content), header=header_row)
    if "Weight" not in df.columns:
        raise ValueError("SSGA holdings workbook Weight column not found")
    df["Weight"] = pd.to_numeric(df["Weight"], errors="coerce")
    df = df.dropna(subset=["Weight"])
    parsed = pd.to_datetime(as_of, errors="coerce")
    if pd.isna(parsed):
        raise ValueError("SSGA holdings as-of date not found")
    return parsed.strftime("%Y-%m-%d"), df


def concentration_snapshot(as_of: str, holdings: pd.DataFrame) -> dict[str, Any]:
    weights = holdings["Weight"].sort_values(ascending=False).reset_index(drop=True)
    if len(weights) < 10:
        raise ValueError("Fewer than 10 SPY holdings parsed")
    return {"date": as_of, "top5": round(float(weights.head(5).sum()), 4),
            "top10": round(float(weights.head(10).sum()), 4)}


def archive_snapshot(path: str | Path, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Append/revise an observed daily SPY concentration record; no fabricated history."""
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, Any]] = []
    if file.exists():
        try:
            existing = json.loads(file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = []
    by_date = {str(row.get("date")): row for row in existing if row.get("date")}
    by_date[str(snapshot["date"])] = snapshot
    result = [by_date[k] for k in sorted(by_date)]
    file.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def concentration_status(snapshot: dict[str, Any], history: list[dict[str, Any]], now: datetime | None = None) -> dict[str, Any]:
    obs = snapshot.get("date")
    f = freshness(obs, "daily business day", 4, now)
    return {**snapshot, "available": not f.stale, "history_days": len(history), "status": "insufficient-history" if len(history) < 60 else "observational",
            "freshness": f.__dict__, "history": history[-180:]}


def ai_breadth_and_rs(price_df: pd.DataFrame, now: datetime | None = None) -> dict[str, Any]:
    """Calculate fixed-basket MA breadth and ETF relative-strength trends."""
    close = price_df.copy().dropna(how="all")
    available = [t for t in AI_BASKET if t in close.columns and close[t].notna().sum() >= 200]
    if not available:
        return {"available": False}
    latest = close.index.max()
    above50 = sum(close[t].iloc[-1] > close[t].rolling(50).mean().iloc[-1] for t in available)
    above200 = sum(close[t].iloc[-1] > close[t].rolling(200).mean().iloc[-1] for t in available)
    rs: dict[str, Any] = {}
    for etf in ("SMH", "SOXX"):
        if etf not in close or "SPY" not in close:
            continue
        ratio = (close[etf] / close["SPY"]).dropna()
        if len(ratio) < 200:
            continue
        rs[etf] = {"current": float(ratio.iloc[-1]), "ma50": float(ratio.rolling(50).mean().iloc[-1]),
                   "ma200": float(ratio.rolling(200).mean().iloc[-1]),
                   "change60": float(ratio.iloc[-1] / ratio.iloc[-min(61, len(ratio))] - 1),
                   "dates": [d.strftime("%Y-%m-%d") for d in ratio.tail(252).index],
                   "history": [round(float(x), 6) for x in ratio.tail(252).values]}
    f = freshness(latest, "daily business day", 4, now)
    return {"available": True, "constituents_available": available, "constituent_count": len(available),
            "above50": above50 / len(available) * 100, "above200": above200 / len(available) * 100,
            "rs": rs, "observation_date": latest.strftime("%Y-%m-%d"), "freshness": f.__dict__}


def oas_metrics(series: dict[str, pd.Series], now: datetime | None = None) -> dict[str, Any]:
    labels = {"HY": "BAMLH0A0HYM2", "IG": "BAMLC0A0CM", "BBB": "BAMLC0A4CBBB"}
    result: dict[str, Any] = {}
    for label, fred_id in labels.items():
        s = series.get(label, pd.Series(dtype=float)).dropna()
        if s.empty:
            result[label] = {"available": False, "source_series": fred_id}
            continue
        cur = float(s.iloc[-1])
        change = float(cur - s.iloc[-min(61, len(s))]) if len(s) >= 2 else np.nan
        f = freshness(s.index[-1], "daily business day", 4, now)
        result[label] = {"available": not f.stale, "value": cur, "change60": change,
                         "observation_date": s.index[-1].strftime("%Y-%m-%d"),
                         "freshness": f.__dict__, "source_series": fred_id,
                         "dates": [d.strftime("%Y-%m-%d") for d in s.tail(252).index],
                         "history": [round(float(v), 3) for v in s.tail(252).values]}
    return result


def fetch_treasury_settlements(*, session=None) -> set[date]:
    params = {
        "fields": "issue_date,offering_amt",
        "filter": "issue_date:gte:2020-01-01",
        "sort": "-issue_date", "page[size]": 1000, "page[number]": 1,
    }
    response = _get(TREASURY_AUCTIONS_URL, params=params, timeout=50, session=session)
    return parse_treasury_large_settlements(response.json().get("data", []))


def fetch_fast_metrics(snapshot_path: str | Path, now: datetime | None = None) -> dict[str, Any]:
    """Fetch all Phase A/B fast variables; every module degrades independently."""
    current = now or utc_now()
    out: dict[str, Any] = {"fetched_at": current.strftime("%Y-%m-%d %H:%M UTC"), "errors": {}}
    session = requests.Session()

    # Official Cboe curve.
    try:
        out["vix"] = vix_term_structure(fetch_cboe_history(CBOE_VIX_URL, session=session),
                                         fetch_cboe_history(CBOE_VIX3M_URL, session=session),
                                         fetch_cboe_history(CBOE_VIX9D_URL, session=session), current)
    except Exception as exc:
        out["vix"] = {"available": False, "reason": str(exc), "freshness": freshness(None, "daily business day", 4, current).__dict__}
        out["errors"]["vix"] = str(exc)

    # Reserves / GDP.
    try:
        out["reserves"] = reserves_gdp(fetch_fred_series("WRESBAL", "2018-01-01", session=session),
                                        fetch_fred_series("GDP", "2018-01-01", session=session), current)
    except Exception as exc:
        out["reserves"] = {"available": False, "freshness": freshness(None, "weekly", 10, current).__dict__}
        out["errors"]["reserves"] = str(exc)

    # SOFR / IORB, p99, and NY Fed repo operations.
    try:
        treasury_dates: set[date] = set()
        try:
            treasury_dates = fetch_treasury_settlements(session=session)
        except Exception as treasury_exc:
            out["errors"]["treasury_calendar"] = str(treasury_exc)
        sofr_series = fetch_fred_series("SOFR", "2021-07-01", session=session)
        iorb_series = fetch_fred_series("IORB", "2021-07-01", session=session)
        spread = filtered_sofr_iorb(sofr_series, iorb_series, treasury_dates)
        spread_fresh = freshness(spread.index[-1], "daily business day", 4, current)
        latest = spread.iloc[-1]
        out["sofr_iorb"] = {
            "available": not spread_fresh.stale, "observation_date": spread.index[-1].strftime("%Y-%m-%d"),
            "freshness": spread_fresh.__dict__, "current_bp": float(latest["raw_bp"]),
            "one_day_change_bp": float(latest["daily_change_bp"]) if pd.notna(latest["daily_change_bp"]) else None,
            "filtered_5d_bp": float(latest["filtered_5d_bp"]) if pd.notna(latest["filtered_5d_bp"]) else None,
            "latest_flags": latest["flags"], "status": sofr_iorb_status(spread),
            "dates": [d.strftime("%Y-%m-%d") for d in spread.index],
            "raw_bp": [round(float(v), 3) for v in spread["raw_bp"]],
            "filtered_bp": [round(float(v), 3) if pd.notna(v) else None for v in spread["filtered_5d_bp"]],
            "daily_change_bp": [round(float(v), 3) if pd.notna(v) else None for v in spread["daily_change_bp"]],
            "calendar_flags": [", ".join(f) for f in spread["flags"]],
            "calendar_noise": [bool(v) for v in spread["calendar_noise"]],
        }
        sofr_payload = _get(NYFED_SOFR_URL, session=session).json()
        p99 = parse_sofr_api(sofr_payload)
        p99_fresh = freshness(p99.index[-1], "daily business day", 4, current)
        out["sofr_tail"] = {"available": not p99_fresh.stale, "observation_date": p99.index[-1].strftime("%Y-%m-%d"),
                            "freshness": p99_fresh.__dict__, "current_bp": float((p99["p99"] - p99["median"]).iloc[-1] * 100),
                            "dates": [d.strftime("%Y-%m-%d") for d in p99.index],
                            "history_bp": [round(float(v), 3) for v in ((p99["p99"] - p99["median"]) * 100).values]}
    except Exception as exc:
        out["sofr_iorb"] = {"available": False, "freshness": freshness(None, "daily business day", 4, current).__dict__}
        out["sofr_tail"] = {"available": False, "freshness": freshness(None, "daily business day", 4, current).__dict__}
        out["errors"]["sofr"] = str(exc)

    try:
        repo = parse_repo_operations(_get(NYFED_REPO_URL, session=session).json())
        out["repo"] = repo_usage_status(repo, current)
    except Exception as exc:
        out["repo"] = {"available": False, "freshness": freshness(None, "daily", 4, current).__dict__}
        out["errors"]["repo"] = str(exc)

    try:
        oas = {label: fetch_fred_series(series, "2024-01-01", session=session) for label, series in
               {"HY": "BAMLH0A0HYM2", "IG": "BAMLC0A0CM", "BBB": "BAMLC0A4CBBB"}.items()}
        out["oas"] = oas_metrics(oas, current)
    except Exception as exc:
        out["oas"] = {label: {"available": False} for label in ("HY", "IG", "BBB")}
        out["errors"]["oas"] = str(exc)

    try:
        content = _get(SSGA_SPY_HOLDINGS_URL, timeout=60, session=session).content
        as_of, holdings = parse_spy_holdings_excel(content)
        snapshot = concentration_snapshot(as_of, holdings)
        history = archive_snapshot(snapshot_path, snapshot)
        out["concentration"] = concentration_status(snapshot, history, current)
    except Exception as exc:
        out["concentration"] = {"available": False, "freshness": freshness(None, "daily business day", 4, current).__dict__}
        out["errors"]["concentration"] = str(exc)

    # Market-price data is used only for the documented fixed basket and ETF benchmarks.
    try:
        import yfinance as yf  # local import keeps transform tests dependency-light
        symbols = AI_BASKET + ["SMH", "SOXX", "SPY"]
        px = yf.download(symbols, period="18mo", auto_adjust=True, progress=False, threads=True)["Close"]
        out["ai"] = ai_breadth_and_rs(px, current)
    except Exception as exc:
        out["ai"] = {"available": False, "freshness": freshness(None, "daily business day", 4, current).__dict__}
        out["errors"]["ai"] = str(exc)
    return out
