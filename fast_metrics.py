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
# Federal Reserve Data Download Program, PRATES release (IOER / IORR / IORB).
# Used as the primary IORB source because fredgraph.csv is frequently
# unreachable from CI runners, which used to freeze the SOFR-IORB spread.
FRB_POLICY_RATES_URL = (
    "https://www.federalreserve.gov/datadownload/Output.aspx?rel=PRATES"
    "&series=c27939ee810cb2e929a920a6bd77d9f6&lastobs=&from=&to="
    "&filetype=csv&label=include&layout=seriescolumn"
)
# Federal Reserve H.4.1 statistical release ("Factors Affecting Reserve Balances").
# Table 1 carries both the weekly average of daily figures and the Wednesday level
# for reserve balances and the U.S. Treasury General Account, so one fetch supplies
# the live tail for WRESBAL and WTREGEN when fredgraph.csv times out from CI.
H41_CURRENT_URL = "https://www.federalreserve.gov/releases/h41/current/h41.htm"
# Daily Treasury Statement: operating cash balance, closing TGA balance.
DTS_OPERATING_CASH_URL = (
    "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/dts/operating_cash_balance"
)
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


def parse_frb_policy_rates_csv(text: str) -> pd.Series:
    """Extract the daily IORB column from the Federal Reserve PRATES CSV.

    The file carries several metadata rows plus IOER/IORR columns that are blank
    after 2021, so the IORB column is located by its published description and
    empty cells are dropped instead of being forward-filled.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("FRB policy rates response was empty")
    header = next(pd.read_csv(StringIO(lines[0]), header=None).itertuples(index=False))
    target = None
    for pos, label in enumerate(header):
        if "IORB" in str(label).upper():
            target = pos
            break
    if target is None:
        raise ValueError("FRB policy rates CSV has no IORB column")
    rows: dict[pd.Timestamp, float] = {}
    for line in lines[1:]:
        fields = [f.strip().strip('"') for f in line.split(",")]
        if target >= len(fields):
            continue
        day = pd.to_datetime(fields[0], errors="coerce", format="%Y-%m-%d")
        value = pd.to_numeric(fields[target], errors="coerce")
        if pd.isna(day) or pd.isna(value):
            continue
        rows[day.normalize()] = float(value)
    if not rows:
        raise ValueError("FRB policy rates CSV has no valid IORB observations")
    out = pd.Series(rows).sort_index()
    out.attrs["source"] = "Federal Reserve PRATES"
    return out


def fetch_frb_iorb(*, session=None) -> pd.Series:
    """Official IORB series from the Federal Reserve, with a committed cache."""
    cache = FRED_CACHE_DIR / "IORB_FRB.csv"
    try:
        response = _get(FRB_POLICY_RATES_URL, timeout=25, retries=2, session=session)
        series = parse_frb_policy_rates_csv(response.text)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(response.text, encoding="utf-8")
        return series
    except Exception as live_error:
        if not cache.exists():
            raise live_error
        series = parse_frb_policy_rates_csv(cache.read_text(encoding="utf-8"))
        series.attrs["source"] = "Federal Reserve PRATES cached CSV"
        return series


def splice_official(base: pd.Series | None, preferred: pd.Series | None) -> pd.Series:
    """Overlay a preferred official series on a longer historical one.

    Values from ``preferred`` win on overlapping dates; ``base`` only supplies
    history the preferred source does not publish. Nothing is interpolated, so a
    gap in both sources stays a gap rather than becoming an invented value.
    """
    frames = [s.dropna() for s in (base, preferred) if s is not None and not s.dropna().empty]
    if not frames:
        return pd.Series(dtype=float)
    if len(frames) == 1:
        return frames[0].sort_index()
    base_clean, pref_clean = frames
    return pref_clean.combine_first(base_clean).sort_index()


def sofr_kpi_view(metric: dict[str, Any]) -> dict[str, str]:
    """KPI strings for the SOFR-IORB panel.

    A stale series keeps showing its real last official observation with an
    explicit stale label; only genuinely missing data renders as an em dash. The
    panel must never show a blank headline while a real observation exists, and
    it must never present an old observation as if it were today's.
    """
    fresh = metric.get("freshness", {}) or {}
    obs = fresh.get("observation_date") or metric.get("observation_date")
    status = (metric.get("status") or {})
    has_value = bool(metric.get("dates")) and metric.get("current_bp") is not None
    if not has_value:
        return {"state": "unavailable", "current": "—", "change": "—", "median": "—",
                "status_label": "unavailable", "message": "No matched official SOFR/IORB observations",
                "note": "", "as_of": obs or "unavailable"}

    def bp(value: Any) -> str:
        return "—" if value is None else "{:+.1f} bp".format(float(value))

    stale = bool(fresh.get("stale"))
    age = fresh.get("age_days")
    view = {"state": "stale" if stale else "live",
            "current": bp(metric.get("current_bp")),
            "change": bp(metric.get("one_day_change_bp")),
            "median": bp(metric.get("filtered_5d_bp")),
            "status_label": status.get("status", "unavailable").replace("-", " "),
            "message": status.get("message", "Data unavailable"),
            "note": "", "as_of": obs or "unavailable"}
    if stale:
        view["status_label"] = "stale data"
        view["message"] = ("Last official observation " + (obs or "unknown")
                           + (" ({}d old)".format(age) if age is not None else "")
                           + "; values are that observation, not today's.")
        view["note"] = "as of " + (obs or "unknown")
    return view


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


# ---------------------------------------------------------------------------
# Dollar funding pressure trio: SOFR-IORB, bank reserves, Treasury General
# Account.  All three legs come from official sources, are evaluated with the
# explicit user strategy overlay documented below, and fail independently: an
# unavailable or stale leg is reported as such and can never count as
# triggered.
#
# Strategy overlay (user-defined, NOT a universal empirical law):
#   SOFR leg     : filtered non-calendar SOFR-IORB >= +3.0 bp with persistence
#                  over the last 3 eligible (non-calendar) observations.
#   Reserve leg  : reserves <= $2.90T AND 4-week change <= -$50B
#                  (elevated when reserves <= $2.85T or 4-week change <= -$100B)
#   TGA leg      : TGA >= $0.90T AND 4-week change >= +$50B
#                  (elevated when TGA >= $1.00T or 4-week change >= +$100B)
# ---------------------------------------------------------------------------
SOFR_STRATEGY_BP = 3.0
SOFR_REFERENCE_WATCH_BP = 2.0      # retained stricter analytical reference band
SOFR_REFERENCE_ELEVATED_BP = 5.0   # retained stricter analytical reference band
SOFR_PERSISTENCE_OBS = 3

RESERVE_TRIGGER_LEVEL_T = 2.90
RESERVE_TARGET_LEVEL_T = 2.80   # chart reference: the level the decline is heading toward
RESERVE_ELEVATED_LEVEL_T = 2.85
RESERVE_TRIGGER_4W_B = -50.0
RESERVE_ELEVATED_4W_B = -100.0

TGA_TRIGGER_LEVEL_T = 0.90
TGA_ELEVATED_LEVEL_T = 1.00
TGA_TRIGGER_4W_B = 50.0
TGA_ELEVATED_4W_B = 100.0

H41_LABELS = {
    "reserves": "reserve balances with federal reserve banks",
    "tga": "u.s. treasury, general account",
}
_H41_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def _strip_tags(fragment: str) -> str:
    """Plain text of an HTML fragment without an XML parser dependency."""
    import html as _html
    import re as _re
    text = _re.sub(r"<[^>]*>", " ", fragment)
    text = _html.unescape(text).replace("\xa0", " ")
    return " ".join(text.split())


def _h41_number(cell: str) -> float | None:
    """Parse an unsigned level cell; signed change cells return ``None``."""
    token = cell.strip()
    if not token or token[0] in "+-":
        return None
    token = token.replace(",", "")
    try:
        return float(token)
    except ValueError:
        return None


def parse_h41_release(html: str) -> dict[str, Any]:
    """Read reserve balances and the TGA out of the H.4.1 release page.

    Returns the weekly average of daily figures (the definition FRED publishes as
    WRESBAL / WTREGEN) dated on the week-ended Wednesday, plus the Wednesday
    level for reference. Missing rows raise instead of returning a guess.
    """
    import re as _re
    week_dates: list[date] = []
    for month, day, year in _re.findall(
            r"Week\s+ended\s*(?:</?[^>]*>\s*)*([A-Za-z]{3,9})\.?\s+(\d{1,2}),\s*(\d{4})",
            _strip_tags(html) if "<" not in html else html):
        key = str(month)[:3].lower()
        if key in _H41_MONTHS:
            week_dates.append(date(int(year), _H41_MONTHS[key], int(day)))
    if not week_dates:
        # The header can be split across cells; fall back to the plain text form.
        text = _strip_tags(html)
        for month, day, year in _re.findall(
                r"Week\s+ended\s+([A-Za-z]{3,9})\.?\s+(\d{1,2}),\s*(\d{4})", text):
            key = str(month)[:3].lower()
            if key in _H41_MONTHS:
                week_dates.append(date(int(year), _H41_MONTHS[key], int(day)))
    if not week_dates:
        raise ValueError("H.4.1 release has no 'Week ended' header date")
    week_ended = max(week_dates)

    out: dict[str, Any] = {"week_ended": week_ended.isoformat()}
    rows = _re.split(r"<tr\b", html, flags=_re.I)
    for key, label in H41_LABELS.items():
        for row in rows:
            cells = [_strip_tags(c) for c in _re.findall(
                r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row, flags=_re.I | _re.S)]
            if not cells:
                continue
            first = cells[0].lower().rstrip(" .0123456789")
            if not first.startswith(label.rstrip(" .")):
                continue
            numbers = [v for v in (_h41_number(c) for c in cells[1:]) if v is not None]
            if not numbers:
                continue
            out[key + "_millions"] = numbers[0]
            out[key + "_wednesday_millions"] = numbers[-1]
            break
        if key + "_millions" not in out:
            raise ValueError("H.4.1 release has no parseable '" + label + "' row")
    return out


def load_h41_cache() -> pd.DataFrame:
    """Committed history of parsed H.4.1 weekly observations (may be empty)."""
    cache = FRED_CACHE_DIR / "H41_WEEKLY.csv"
    if not cache.exists():
        return pd.DataFrame(columns=["reserves_millions", "tga_millions"])
    df = pd.read_csv(cache)
    if "week_ended" not in df.columns:
        return pd.DataFrame(columns=["reserves_millions", "tga_millions"])
    df["week_ended"] = pd.to_datetime(df["week_ended"], errors="coerce")
    df = df.dropna(subset=["week_ended"]).drop_duplicates("week_ended", keep="last")
    return df.set_index("week_ended").sort_index()


def update_h41_cache(record: dict[str, Any]) -> pd.DataFrame:
    """Append one parsed release to the committed cache without interpolation."""
    cache = FRED_CACHE_DIR / "H41_WEEKLY.csv"
    df = load_h41_cache()
    day = pd.Timestamp(record["week_ended"])
    for column in ("reserves_millions", "tga_millions"):
        if record.get(column) is not None:
            df.loc[day, column] = float(record[column])
    df = df.sort_index()
    cache.parent.mkdir(parents=True, exist_ok=True)
    df.rename_axis("week_ended").to_csv(cache, date_format="%Y-%m-%d")
    return df


def fetch_h41_weekly(*, session=None) -> tuple[pd.Series, pd.Series, str]:
    """Official H.4.1 reserve-balance and TGA weekly series (in $ millions).

    The live release supplies the newest week; the committed cache supplies the
    weeks already seen. Nothing is interpolated, so a week the Fed did not
    publish stays absent rather than becoming a synthetic value.
    """
    source = "Federal Reserve H.4.1 cached observations"
    try:
        record = parse_h41_release(_get(H41_CURRENT_URL, timeout=25, retries=2, session=session).text)
        frame = update_h41_cache(record)
        source = "Federal Reserve H.4.1 current release"
    except Exception as live_error:
        frame = load_h41_cache()
        if frame.empty:
            raise live_error
    reserves = pd.to_numeric(frame.get("reserves_millions"), errors="coerce").dropna() \
        if "reserves_millions" in frame else pd.Series(dtype=float)
    tga = pd.to_numeric(frame.get("tga_millions"), errors="coerce").dropna() \
        if "tga_millions" in frame else pd.Series(dtype=float)
    reserves.attrs["source"] = source
    tga.attrs["source"] = source
    return reserves, tga, source


def parse_dts_operating_cash(rows: Iterable[dict[str, Any]]) -> pd.Series:
    """Closing TGA balance from the Daily Treasury Statement, in $ millions."""
    values: dict[pd.Timestamp, float] = {}
    for row in rows:
        account = str(row.get("account_type", ""))
        if "Treasury General Account (TGA) Closing Balance" not in account:
            continue
        day = pd.to_datetime(row.get("record_date"), errors="coerce")
        # The published field carrying the day's balance differs across DTS
        # vintages, so both documented columns are accepted and "null" coerces
        # to a skipped row instead of a zero.
        amount = pd.to_numeric(row.get("close_today_bal"), errors="coerce")
        if pd.isna(amount):
            amount = pd.to_numeric(row.get("open_today_bal"), errors="coerce")
        if pd.isna(day) or pd.isna(amount):
            continue
        values[day.normalize()] = float(amount)
    if not values:
        return pd.Series(dtype=float)
    out = pd.Series(values).sort_index()
    out.attrs["source"] = "Treasury Daily Treasury Statement (closing balance)"
    return out


def fetch_dts_tga(*, days: int = 120, session=None) -> pd.Series:
    """Daily TGA closing balance; used only as a last-resort official fallback."""
    start = (utc_now().date() - timedelta(days=days)).isoformat()
    params = {
        "fields": "record_date,account_type,close_today_bal,open_today_bal",
        "filter": "record_date:gte:" + start,
        "sort": "-record_date", "page[size]": 2000, "page[number]": 1,
    }
    payload = _get(DTS_OPERATING_CASH_URL, params=params, timeout=40, session=session).json()
    series = parse_dts_operating_cash(payload.get("data", []))
    if series.empty:
        raise ValueError("Daily Treasury Statement returned no TGA closing balance")
    return series


def _asof_value(series: pd.Series, target: pd.Timestamp,
                tolerance_days: int) -> tuple[float | None, pd.Timestamp | None]:
    """Last observation on or before ``target``, or ``None`` if too far back."""
    window = series.loc[:target]
    if window.empty:
        return None, None
    day = window.index[-1]
    if abs((target - day).days) > tolerance_days:
        return None, None
    return float(window.iloc[-1]), day


def balance_metric(series_millions: pd.Series, *, cadence: str, max_age_days: int = 10,
                   source: str | None = None, history: int = 170,
                   now: datetime | None = None) -> dict[str, Any]:
    """Level in $T plus 1-week / 4-week changes in $B from official observations.

    Deltas use an as-of lookup against real observation dates, so a missing week
    yields ``None`` instead of an interpolated or forward-filled change.
    """
    series = series_millions.dropna().sort_index() if series_millions is not None else pd.Series(dtype=float)
    if series.empty:
        f = freshness(None, cadence, max_age_days, now)
        return {"available": False, "reason": "no observations", "freshness": f.__dict__,
                "dates": [], "history": [], "source": source}
    last_day = series.index[-1]
    current = float(series.iloc[-1])
    week_ago, week_day = _asof_value(series, last_day - pd.Timedelta(days=7), 5)
    month_ago, month_day = _asof_value(series, last_day - pd.Timedelta(days=28), 7)
    f = freshness(last_day, cadence, max_age_days, now)
    one_week_b = None if week_ago is None else (current - week_ago) / 1e3
    four_week_b = None if month_ago is None else (current - month_ago) / 1e3
    tail = series.tail(history)
    return {
        "available": not f.stale,
        "observation_date": last_day.strftime("%Y-%m-%d"),
        "freshness": f.__dict__,
        "current_millions": current,
        "current_trillions": current / 1e6,
        "one_week_change_b": one_week_b,
        "four_week_change_b": four_week_b,
        "one_week_ref_date": None if week_day is None else week_day.strftime("%Y-%m-%d"),
        "four_week_ref_date": None if month_day is None else month_day.strftime("%Y-%m-%d"),
        "speed_b_per_week": None if four_week_b is None else four_week_b / 4.0,
        "source": source or str(series.attrs.get("source", "official")),
        "dates": [d.strftime("%Y-%m-%d") for d in tail.index],
        "history": [round(float(v) / 1e6, 5) for v in tail.values],
    }


def _leg_shell(name: str, label: str, metric: dict[str, Any]) -> dict[str, Any] | None:
    if metric and metric.get("available"):
        return None
    fresh = (metric or {}).get("freshness", {}) or {}
    obs = fresh.get("observation_date")
    reason = "stale" if obs else "unavailable"
    return {"name": name, "label": label, "available": False, "triggered": False,
            "state": reason, "state_label": "data " + reason, "severity": "none",
            "detail": ("Last official observation " + obs + "; a stale leg never counts as triggered."
                       if obs else "No official observation available; leg excluded from resonance."),
            "observation_date": obs, "freshness": fresh}


def reserve_leg(metric: dict[str, Any]) -> dict[str, Any]:
    """Operationalize 'reserves falling rapidly from $2.9T toward $2.8T'.

    Active requires both level and speed: level <= $2.90T AND 4-week change
    <= -$50B. Only one of the two conditions is reported as ``approaching`` so a
    fast decline from a comfortable level cannot silently trigger the leg.
    """
    shell = _leg_shell("reserves", "Liquidity buffer", metric)
    if shell is not None:
        return shell
    level = float(metric["current_trillions"])
    delta = metric.get("four_week_change_b")
    if delta is None:
        return {"name": "reserves", "label": "Liquidity buffer", "available": False, "triggered": False,
                "state": "incomplete", "state_label": "4-week change unavailable", "severity": "none",
                "detail": "No observation four weeks back; the speed test cannot be evaluated.",
                "observation_date": metric.get("observation_date"), "freshness": metric.get("freshness", {})}
    level_ok = level <= RESERVE_TRIGGER_LEVEL_T
    speed_ok = float(delta) <= RESERVE_TRIGGER_4W_B
    triggered = level_ok and speed_ok
    elevated = triggered and (level <= RESERVE_ELEVATED_LEVEL_T or float(delta) <= RESERVE_ELEVATED_4W_B)
    if triggered:
        state = "elevated" if elevated else "active"
        state_label = "draining fast" if not elevated else "draining fast · elevated"
    elif level_ok or speed_ok:
        state, state_label = "approaching", "approaching"
    else:
        state, state_label = "normal", "ample"
    detail = ("${:.3f}T vs <= ${:.2f}T ({}) · 4w {:+.0f}B vs <= {:.0f}B ({})".format(
        level, RESERVE_TRIGGER_LEVEL_T, "met" if level_ok else "not met",
        float(delta), RESERVE_TRIGGER_4W_B, "met" if speed_ok else "not met"))
    return {"name": "reserves", "label": "Liquidity buffer", "available": True, "triggered": triggered,
            "state": state, "state_label": state_label,
            "severity": "elevated" if elevated else ("warning" if triggered else "none"),
            "level_ok": level_ok, "speed_ok": speed_ok, "detail": detail,
            "observation_date": metric.get("observation_date"), "freshness": metric.get("freshness", {})}


def tga_leg(metric: dict[str, Any]) -> dict[str, Any]:
    """Operationalize 'TGA rebuilding toward $1T'.

    Active requires both level and speed: TGA >= $0.90T AND 4-week change
    >= +$50B; elevated at >= $1.00T or 4-week change >= +$100B.
    """
    shell = _leg_shell("tga", "Fiscal drain", metric)
    if shell is not None:
        return shell
    level = float(metric["current_trillions"])
    delta = metric.get("four_week_change_b")
    if delta is None:
        return {"name": "tga", "label": "Fiscal drain", "available": False, "triggered": False,
                "state": "incomplete", "state_label": "4-week change unavailable", "severity": "none",
                "detail": "No observation four weeks back; the rebuild-speed test cannot be evaluated.",
                "observation_date": metric.get("observation_date"), "freshness": metric.get("freshness", {})}
    level_ok = level >= TGA_TRIGGER_LEVEL_T
    speed_ok = float(delta) >= TGA_TRIGGER_4W_B
    triggered = level_ok and speed_ok
    elevated = triggered and (level >= TGA_ELEVATED_LEVEL_T or float(delta) >= TGA_ELEVATED_4W_B)
    if triggered:
        state = "elevated" if elevated else "active"
        state_label = "rebuilding" if not elevated else "rebuilding · elevated"
    elif level_ok or speed_ok:
        state, state_label = "approaching", "approaching"
    else:
        state, state_label = "normal", "neutral"
    detail = ("${:.3f}T vs >= ${:.2f}T ({}) · 4w {:+.0f}B vs >= {:+.0f}B ({})".format(
        level, TGA_TRIGGER_LEVEL_T, "met" if level_ok else "not met",
        float(delta), TGA_TRIGGER_4W_B, "met" if speed_ok else "not met"))
    return {"name": "tga", "label": "Fiscal drain", "available": True, "triggered": triggered,
            "state": state, "state_label": state_label,
            "severity": "elevated" if elevated else ("warning" if triggered else "none"),
            "level_ok": level_ok, "speed_ok": speed_ok, "detail": detail,
            "observation_date": metric.get("observation_date"), "freshness": metric.get("freshness", {})}


def sofr_leg(metric: dict[str, Any]) -> dict[str, Any]:
    """Strategy-overlay SOFR leg: filtered non-calendar spread >= +3bp, persistent.

    Persistence uses the last three *eligible* (non-calendar) observations, so a
    single month-end or tax-date spike cannot activate the leg on its own.
    """
    shell = _leg_shell("sofr", "Funding price", metric)
    if shell is not None:
        return shell
    raw = metric.get("raw_bp") or []
    noise = metric.get("calendar_noise") or []
    eligible = [float(v) for v, flag in zip(raw, noise) if not flag and v is not None]
    recent = eligible[-SOFR_PERSISTENCE_OBS:]
    persistent = len(recent) == SOFR_PERSISTENCE_OBS and all(v >= SOFR_STRATEGY_BP for v in recent)
    reference_watch = len(recent) == SOFR_PERSISTENCE_OBS and all(v >= SOFR_REFERENCE_WATCH_BP for v in recent)
    filtered = metric.get("filtered_5d_bp")
    filtered_ok = filtered is not None and float(filtered) >= SOFR_STRATEGY_BP
    triggered = bool(persistent and filtered_ok)
    if triggered:
        state = "elevated" if (filtered is not None and float(filtered) >= SOFR_REFERENCE_ELEVATED_BP) else "active"
        state_label = "above +3bp, persistent" if state == "active" else "above +5bp reference band"
    elif filtered_ok or persistent or reference_watch:
        state, state_label = "approaching", "approaching"
    else:
        state, state_label = "normal", "at or below IORB"
    detail = ("filtered median {} vs >= +{:.0f}bp ({}) · last {} non-calendar readings {} +{:.0f}bp ({})".format(
        "n/a" if filtered is None else "{:+.1f}bp".format(float(filtered)),
        SOFR_STRATEGY_BP, "met" if filtered_ok else "not met", SOFR_PERSISTENCE_OBS,
        "all >=" if persistent else "not all >=", SOFR_STRATEGY_BP, "met" if persistent else "not met"))
    return {"name": "sofr", "label": "Funding price", "available": True, "triggered": triggered,
            "state": state, "state_label": state_label,
            "severity": "elevated" if state == "elevated" else ("warning" if triggered else "none"),
            "persistent": persistent, "filtered_ok": filtered_ok,
            "reference_watch": reference_watch,
            "eligible_recent_bp": [round(v, 2) for v in recent], "detail": detail,
            "calendar_latest": bool(noise[-1]) if noise else False,
            "observation_date": metric.get("observation_date"), "freshness": metric.get("freshness", {})}


RESONANCE_STATES = {
    0: ("normal", "No resonance", "无共振",
        "None of the three strategy conditions is active; dollar funding looks orderly."),
    1: ("watch", "Watch", "观察",
        "One leg is active. Single-leg pressure is common and is not a fragility signal on its own."),
    2: ("warning", "Yellow warning", "黄色预警",
        "Two legs are active: funding pressure is building. Treat as a fragility warning, not a forecast."),
    3: ("structural-top-risk", "Structural-top risk signal", "结构性顶部风险",
        "All three legs are active. This is fragility confirmation under the user's strategy overlay, "
        "not a deterministic market-top prediction."),
}


def funding_resonance(legs: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Combine the three legs into a 0/3 - 3/3 resonance state.

    A leg that is stale, unavailable, or missing its 4-week comparison is never
    counted as triggered, and its absence is reported as an incomplete-data
    state so a partial fetch cannot look like confirmation.
    """
    ordered = list(legs)
    active = [leg for leg in ordered if leg.get("available") and leg.get("triggered")]
    missing = [leg for leg in ordered if not leg.get("available")]
    count = len(active)
    key, headline, headline_cn, interpretation = RESONANCE_STATES[min(count, 3)]
    if count == 1:
        interpretation = ("Only " + active[0]["label"].lower() + " (" + active[0]["name"].upper()
                          + ") is active. " + RESONANCE_STATES[1][3])
    detail = interpretation
    if missing:
        names = ", ".join(leg["name"].upper() for leg in missing)
        detail = ("Incomplete data: " + names + " unavailable or stale, so at most "
                  + str(len(ordered) - len(missing)) + " of 3 legs can be evaluated. " + interpretation)
    return {
        "state": key, "headline": headline, "headline_cn": headline_cn,
        "active_count": count, "total": len(ordered),
        "summary": "{}/{} conditions active".format(count, len(ordered)),
        "interpretation": detail, "incomplete": bool(missing),
        "active_legs": [leg["name"] for leg in active],
        "unavailable_legs": [leg["name"] for leg in missing],
        "legs": {leg["name"]: leg for leg in ordered},
    }


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

    # Official weekly H.4.1 balances (reserve balances + Treasury General Account).
    # The Fed's own release is fetched first because fredgraph.csv times out from
    # CI runners; the committed H41_WEEKLY.csv keeps the weeks already observed.
    h41_reserves = h41_tga = None
    h41_source = None
    try:
        h41_reserves, h41_tga, h41_source = fetch_h41_weekly(session=session)
    except Exception as exc:
        out["errors"]["h41"] = str(exc)
    fred_reserves = None
    try:
        fred_reserves = fetch_fred_series("WRESBAL", "2018-01-01", session=session)
    except Exception as exc:
        out["errors"]["reserves_fred"] = str(exc)
    fred_tga = None
    try:
        fred_tga = fetch_fred_series("WTREGEN", "2018-01-01", session=session)
    except Exception as exc:
        out["errors"]["tga_fred"] = str(exc)
    reserves_series = splice_official(fred_reserves, h41_reserves)
    tga_series = splice_official(fred_tga, h41_tga)

    def _src(fred_series, label):
        parts = []
        if fred_series is not None and not fred_series.empty:
            parts.append("FRED " + label + " (" + str(fred_series.attrs.get("source", "FRED")) + ")")
        if h41_source:
            parts.append(h41_source)
        return " + ".join(parts) if parts else "unavailable"

    # Reserves / GDP — normalized structural context, kept unchanged.
    try:
        if reserves_series.empty:
            raise ValueError("no official reserve-balance observations available")
        out["reserves"] = reserves_gdp(reserves_series,
                                        fetch_fred_series("GDP", "2018-01-01", session=session), current)
    except Exception as exc:
        out["reserves"] = {"available": False, "freshness": freshness(None, "weekly", 10, current).__dict__}
        out["errors"]["reserves"] = str(exc)

    # Reserve balances in levels (funding-pressure leg B).
    try:
        out["reserves_level"] = balance_metric(
            reserves_series, cadence="weekly (H.4.1, week ended Wednesday)",
            source=_src(fred_reserves, "WRESBAL"), now=current)
    except Exception as exc:
        out["reserves_level"] = {"available": False, "dates": [], "history": [],
                                 "freshness": freshness(None, "weekly", 10, current).__dict__}
        out["errors"]["reserves_level"] = str(exc)

    # Treasury General Account in levels (funding-pressure leg C).  If the weekly
    # path is stale, the Daily Treasury Statement supplies an official daily tail
    # rather than a forward-filled weekly value.
    try:
        tga_metric = balance_metric(tga_series, cadence="weekly (H.4.1, week ended Wednesday)",
                                    source=_src(fred_tga, "WTREGEN"), now=current)
        if not tga_metric.get("available"):
            try:
                dts = fetch_dts_tga(session=session)
                fallback = balance_metric(
                    splice_official(tga_series, dts),
                    cadence="weekly H.4.1 spliced with daily DTS closing balance",
                    source=_src(fred_tga, "WTREGEN") + " + Treasury DTS closing balance", now=current)
                if fallback.get("available"):
                    fallback["mixed_cadence"] = True
                    tga_metric = fallback
            except Exception as dts_exc:
                out["errors"]["tga_dts"] = str(dts_exc)
        out["tga_level"] = tga_metric
    except Exception as exc:
        out["tga_level"] = {"available": False, "dates": [], "history": [],
                            "freshness": freshness(None, "weekly", 10, current).__dict__}
        out["errors"]["tga_level"] = str(exc)

    # SOFR / IORB, p99, and NY Fed repo operations.
    nyfed_sofr = pd.DataFrame(columns=["median", "p99"])
    try:
        treasury_dates: set[date] = set()
        try:
            treasury_dates = fetch_treasury_settlements(session=session)
        except Exception as treasury_exc:
            out["errors"]["treasury_calendar"] = str(treasury_exc)
        # SOFR and IORB each come from two official sources. FRED supplies the
        # long history; the NY Fed rates API and the Federal Reserve PRATES file
        # supply the current tail, because fredgraph.csv times out from CI and a
        # FRED-only path silently froze this spread on an old cached observation.
        sources: dict[str, str] = {}
        try:
            nyfed_sofr = parse_sofr_api(_get(NYFED_SOFR_URL, session=session).json())
        except Exception as nyfed_exc:
            out["errors"]["sofr_nyfed"] = str(nyfed_exc)
        fred_sofr = None
        try:
            fred_sofr = fetch_fred_series("SOFR", "2021-07-01", session=session)
            sources["sofr_history"] = str(fred_sofr.attrs.get("source", "FRED"))
        except Exception as sofr_exc:
            out["errors"]["sofr_fred"] = str(sofr_exc)
        nyfed_median = nyfed_sofr["median"] if not nyfed_sofr.empty else None
        if nyfed_median is not None:
            sources["sofr_current"] = "NY Fed reference rates API"
        sofr_series = splice_official(fred_sofr, nyfed_median)

        frb_iorb = None
        try:
            frb_iorb = fetch_frb_iorb(session=session)
            sources["iorb"] = str(frb_iorb.attrs.get("source", "Federal Reserve PRATES"))
        except Exception as iorb_exc:
            out["errors"]["iorb_frb"] = str(iorb_exc)
        fred_iorb = None
        try:
            fred_iorb = fetch_fred_series("IORB", "2021-07-01", session=session)
            sources.setdefault("iorb", str(fred_iorb.attrs.get("source", "FRED")))
        except Exception as iorb_fred_exc:
            out["errors"]["iorb_fred"] = str(iorb_fred_exc)
        iorb_series = splice_official(fred_iorb, frb_iorb)
        if sofr_series.empty or iorb_series.empty:
            raise ValueError("no official SOFR/IORB observations available")
        spread = filtered_sofr_iorb(sofr_series, iorb_series, treasury_dates)
        if spread.empty:
            raise ValueError("SOFR and IORB have no overlapping observation dates")
        # Alignment guard: the spread must reach the latest SOFR observation.
        # A shortfall means IORB coverage lags and would silently blank the panel.
        sofr_last = sofr_series.index[-1]
        if spread.index[-1] < sofr_last:
            out["errors"]["sofr_iorb_alignment"] = (
                "IORB coverage ends " + spread.index[-1].strftime("%Y-%m-%d")
                + " but SOFR reaches " + sofr_last.strftime("%Y-%m-%d"))
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
            "sources": sources,
        }
    except Exception as exc:
        out["sofr_iorb"] = {"available": False, "freshness": freshness(None, "daily business day", 4, current).__dict__}
        out["errors"]["sofr"] = str(exc)

    try:
        p99 = nyfed_sofr if not nyfed_sofr.empty else parse_sofr_api(_get(NYFED_SOFR_URL, session=session).json())
        if p99.empty:
            raise ValueError("NY Fed SOFR API returned no observations")
        p99_fresh = freshness(p99.index[-1], "daily business day", 4, current)
        out["sofr_tail"] = {"available": not p99_fresh.stale, "observation_date": p99.index[-1].strftime("%Y-%m-%d"),
                            "freshness": p99_fresh.__dict__, "current_bp": float((p99["p99"] - p99["median"]).iloc[-1] * 100),
                            "dates": [d.strftime("%Y-%m-%d") for d in p99.index],
                            "history_bp": [round(float(v), 3) for v in ((p99["p99"] - p99["median"]) * 100).values]}
    except Exception as tail_exc:
        out["sofr_tail"] = {"available": False, "freshness": freshness(None, "daily business day", 4, current).__dict__}
        out["errors"]["sofr_tail"] = str(tail_exc)

    # Three-leg dollar funding resonance.  Built after the SOFR block so every
    # leg is evaluated from its own already-isolated metric.
    try:
        out["funding"] = funding_resonance([
            sofr_leg(out.get("sofr_iorb", {})),
            reserve_leg(out.get("reserves_level", {})),
            tga_leg(out.get("tga_level", {})),
        ])
    except Exception as exc:
        out["funding"] = {"state": "unavailable", "headline": "Data unavailable", "headline_cn": "数据不可用",
                          "active_count": 0, "total": 3, "summary": "0/3 conditions active",
                          "interpretation": "Funding resonance could not be evaluated.",
                          "incomplete": True, "active_legs": [], "unavailable_legs": ["sofr", "reserves", "tga"],
                          "legs": {}}
        out["errors"]["funding"] = str(exc)

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
