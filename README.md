# Market Fragility Dashboard

A public-data dashboard for **market fragility, not market direction**. It separates three distinct questions:

1. **L1 · Funding & liquidity** — is the market's funding cushion becoming less resilient?
2. **L2 · Crowding** — where is positioning or index concentration vulnerable to an unwind?
3. **L3 · Transmission & amplifiers** — are volatility, credit, dealer positioning, or AI leadership likely to amplify a move?

**Live dashboard:** <https://vincizh.github.io/market-fragility-dashboard/>

The dashboard is deterministic and refreshes hourly through GitHub Actions. A missing upstream source degrades only that module; stale or date-misaligned data is shown in gray and is never rendered as a current value.

## Current modules

| Layer | Module | Formula / display | Source and cadence |
|---|---|---|---|
| L1 | Liquidity Pressure Index | Causal weekly percentiles of short rate, coupon supply, net liquidity and VIX | FRED, FiscalData, Cboe/Yahoo market history; weekly / daily inputs |
| L1 | Reserves / GDP | `WRESBAL / (GDP × 1,000)` with a rolling 156-week percentile; warning/elevated requires two weekly observations | [FRED WRESBAL](https://fred.stlouisfed.org/graph/fredgraph.csv?id=WRESBAL), [FRED GDP](https://fred.stlouisfed.org/graph/fredgraph.csv?id=GDP) |
| L1 | NY Fed repo accepted | Daily accepted amount and trailing 7-calendar-day sum; persistence / own-history percentile, not a dollar threshold | [NY Fed Markets API](https://markets.newyorkfed.org/api/rp/repo/all/results/last/40.json) |
| L1 | SOFR − IORB | Raw daily spread and a five-observation median of non-calendar readings; calendar-filtered structural watch/elevated logic | [FRED SOFR](https://fred.stlouisfed.org/graph/fredgraph.csv?id=SOFR), [FRED IORB](https://fred.stlouisfed.org/graph/fredgraph.csv?id=IORB), [Treasury auction data](https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query) |
| L1 | SOFR p99 − median | NY Fed published 99th percentile less the published median SOFR | [NY Fed SOFR API](https://markets.newyorkfed.org/api/rates/secured/sofr/last/15.json) |
| L2 | CFTC positioning and basis proxy | Leveraged-fund / managed-money net positions, percentiles, and a 2y/5y/10y basis-trade proxy | [CFTC public reporting API](https://publicreporting.cftc.gov/) · weekly |
| L2 | S&P 500 concentration | Sum of the largest 10 and 5 weights in SPY's official holdings file; local daily snapshots begin the history | [SSGA SPY holdings](https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx) |
| L3 | VIX term structure | Official Cboe VIX9D, VIX and VIX3M closes; curve output requires the same observation date for all three | [Cboe VIX3M history](https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX3M_History.csv) |
| L3 | Credit transmission | HY, IG and BBB ICE BofA option-adjusted spreads, plus 60-day changes | [FRED HY OAS](https://fred.stlouisfed.org/graph/fredgraph.csv?id=BAMLH0A0HYM2), [IG](https://fred.stlouisfed.org/graph/fredgraph.csv?id=BAMLC0A0CM), [BBB](https://fred.stlouisfed.org/graph/fredgraph.csv?id=BAMLC0A4CBBB) |
| L3 | AI breadth & relative strength | Fixed 19-security basket above 50d/200d MAs; SMH/SPY and SOXX/SPY ratio trends | Yahoo Finance adjusted daily closes |
| L3 | Dealer gamma | Homebrew Black–Scholes gamma aggregation from SPY/QQQ option chains; optional FlashAlpha single-stock monitor | Yahoo option chains / FlashAlpha |
| L3 | Funding, CTA and correlation | Crypto funding, NDX COT short-covering proxy, cross-sector correlation | CoinGecko/OKX, CFTC, Yahoo Finance |

## Critical data-quality rules

### VIX term structure

The dashboard **does not use Yahoo `^VIX3M`**. It uses Cboe's official daily-history CSVs for VIX, VIX9D and VIX3M. The curve is unavailable if the latest dates do not exactly match or if the observation is stale. This avoids silently ratioing a current VIX against an old VIX3M print.

### Reserves are relative, not fixed dollar thresholds

The dashboard does not use `$2.8T` or `$2.5T` reserve triggers. Its signal is reserves/GDP and the causal 3-year (156-week) percentile: below the 20th percentile for two weekly observations is a warning; below the 10th for two is elevated. The raw reserve dollar value remains context only.

### SOFR − IORB is calendar-filtered

Month-end, quarter-end, major US corporate estimated-tax dates (April/June/September/December 15, adjusted to business days), and large Treasury settlement dates derived from [Treasury auction issue dates](https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query) are labeled as calendar noise. A “large” settlement is an official auction issue date with at least `$30B` offered. They are displayed but cannot trigger a structural alert.

* **Structural watch:** three consecutive non-calendar observations at or above `+2bp`.
* **Elevated:** a non-calendar observation at or above `+5bp`.
* The emphasized series is a five-observation median of non-calendar readings.

Positive SOFR − IORB means secured overnight funding trades above the Fed's administered reserve rate. It is a stress confirmation signal — especially with repo use and SOFR-tail widening — not a stand-alone market-top predictor.

### Freshness and fallback

Every Phase A/B card shows both the **observation date** and the **pipeline fetch time**, plus its expected cadence. Daily business-day data becomes stale after four calendar days and weekly reserve data after ten; stale data is gray. FRED requests use retry plus a versioned archive of official FRED CSV responses as a graceful fallback. The displayed observation date, not the page refresh time, governs freshness.

### S&P 500 concentration history

SSGA publishes the current SPY holdings file, not an index history. The pipeline appends the observed Top-5/Top-10 result to `data/spy-concentration-history.json` once per day. Until 60 snapshots exist, it explicitly reports **insufficient history** and does not fabricate percentile alerts.

### AI basket and survivorship caveat

The fixed basket is: `NVDA, MU, MSFT, GOOGL, AMZN, META, AVGO, ORCL, VRT, ETN, PWR, CEG, TSM, ANET, DELL, SMCI, KLAC, AMAT, LRCX`. It is a documented, subjective supply-chain basket, not an index or recommendation; it has survivorship and selection bias. `SMH` is used only as an ETF benchmark, alongside `SOXX`, for relative-strength ratios.

### DIX boundary

DIX is **not an automated signal or score** in this repository. FINRA ATS or Reg SHO data must never be silently presented as equivalent to DIX because neither provides its directional dark-pool-buying construct. Any future DIX display must be explicitly manual, proprietary/unverified, and excluded from automated scoring.

## Running locally

```bash
pip install yfinance requests pandas numpy openpyxl
python -m unittest -v test_fast_metrics.py test_gex.py
python test_gex.py
python generate_dashboard.py
```

The generator writes the static `index.html`. GitHub Actions runs hourly and commits `index.html`, daily SPY concentration snapshots, and refreshed official FRED fallback responses when data changes.

## Methodology

See [METHODOLOGY.md](METHODOLOGY.md) for definitions, status logic, sources, cadence, limitations, and what is intentionally excluded.

## Disclaimer

This dashboard is research infrastructure, not investment advice. It measures fragility and confirmation conditions; it does not predict market direction or provide a trading recommendation.
