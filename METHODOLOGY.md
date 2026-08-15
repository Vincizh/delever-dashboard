# Methodology & Data Dictionary

## Design principle

The dashboard measures **fragility**: the possibility that funding pressure, crowding, or market structure could amplify a move. It does not predict direction and does not combine slow/low-frequency inputs into the fast-variable summary score.

All Phase A/B metrics use a visible observation date, fetch time, cadence, and stale state. Sources below are linked directly to their public endpoints.

## L1 · Funding & Liquidity

| Metric | Definition | Status logic | Official source | Cadence / limitations |
|---|---|---|---|---|
| Reserves / GDP | `WRESBAL / (GDP × 1,000) × 100` | Rolling 156-week percentile. Warning below 20th percentile for two weekly observations; elevated below 10th for two. | [WRESBAL](https://fred.stlouisfed.org/graph/fredgraph.csv?id=WRESBAL), [GDP](https://fred.stlouisfed.org/graph/fredgraph.csv?id=GDP) | WRESBAL weekly; GDP quarterly and carried forward. Raw reserves are displayed only as context. |
| NY Fed repo accepted | Sum of `totalAmtAccepted` per operation day; show daily and 7-day sum | Watch uses non-zero persistence or a high percentile of the operation's own trailing distribution, not a magic dollar level. | [NY Fed repo results](https://markets.newyorkfed.org/api/rp/repo/all/results/last/40.json) | Daily. Operational usage is confirmation, not a top signal. |
| SOFR p99 − median | `percentPercentile99 − percentRate` | Display/confirmation only; no hard-coded alert score. | [NY Fed SOFR API](https://markets.newyorkfed.org/api/rates/secured/sofr/last/15.json) | Daily. A wider tail can indicate funding stress before the median moves. |
| SOFR − IORB (funding-price leg) | `(SOFR − IORB) × 100` basis points | Calendar days cannot alert. **Strategy leg active** = filtered non-calendar 5-observation median ≥ +3bp *and* last 3 non-calendar observations all ≥ +3bp; elevated ≥ +5bp. Reference bands retained: watch = 3 consecutive non-calendar ≥ +2bp, elevated ≥ +5bp. | [NY Fed SOFR](https://markets.newyorkfed.org/api/rates/secured/sofr/last/15.json), [FRB PRATES IORB](https://www.federalreserve.gov/datadownload/Choose.aspx?rel=PRATES), [Treasury auction data](https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query) | Daily; featured line is a median of the latest 5 non-calendar observations. Month/quarter ends, corporate estimated-tax dates, and Treasury auction issue dates with at least `$30B` offered are labeled and excluded from the rule. |
| Bank reserve balances (liquidity-buffer leg) | Level in `$T`; `Δ1w = current − as-of(t−7d)`, `Δ4w = current − as-of(t−28d)` in `$B`; speed = `Δ4w ÷ 4` | **Active** = level ≤ `$2.90T` *and* `Δ4w` ≤ `−$50B`; **elevated** = level ≤ `$2.85T` or `Δ4w` ≤ `−$100B`; one condition only = *approaching* (does not count); missing 4-week observation = *incomplete* (never counts). | [H.4.1 Table 1](https://www.federalreserve.gov/releases/h41/current/h41.htm), [FRED WRESBAL](https://fred.stlouisfed.org/series/WRESBAL) | Weekly average of daily figures, week ended Wednesday, released Thursday; stale after 10 days. As-of deltas use real observation dates — no interpolation or forward-fill. Separate from the reserves/GDP percentile metric. |
| Treasury General Account (fiscal-drain leg) | Level in `$T`; same `Δ1w` / `Δ4w` / speed formulas | **Active** = level ≥ `$0.90T` *and* `Δ4w` ≥ `+$50B`; **elevated** = level ≥ `$1.00T` or `Δ4w` ≥ `+$100B`; one condition only = *approaching*; missing 4-week observation = *incomplete*. | [H.4.1 Table 1](https://www.federalreserve.gov/releases/h41/current/h41.htm), [FRED WTREGEN](https://fred.stlouisfed.org/series/WTREGEN), [Treasury DTS operating cash](https://fiscaldata.treasury.gov/datasets/daily-treasury-statement/operating-cash-balance) | Weekly (DTS daily closing balance only as an official fallback tail, flagged as mixed cadence). Bill-settlement timing makes the balance lumpy. |
| Dollar funding resonance | Count of active legs out of three | `0/3` no resonance · `1/3` watch (leg named) · `2/3` yellow warning, pressure building · `3/3` structural-top risk signal = fragility **confirmation**, explicitly not a deterministic market-top prediction. Stale, unavailable, or incomplete legs never count and raise an incomplete-data state. | Same sources as the three legs | The absolute thresholds are a documented **user-defined strategy overlay**, not an estimated statistical law, and are regime-dependent. |
| LPI | Equal-weight causal percentile composite of short rate, duration supply, inverted net liquidity, and VIX | Separate LPI regime logic; not a directional forecast. | [FRED](https://fred.stlouisfed.org/), [FiscalData](https://fiscaldata.treasury.gov/), [Cboe](https://www.cboe.com/) | Weekly composite; inputs have mixed cadences. |

## L2 · Crowding

| Metric | Definition | Status logic | Source | Cadence / limitations |
|---|---|---|---|---|
| COT positioning | Leveraged-fund or managed-money net contracts, own-history percentile and z-score | Observational crowding context and basis-trade proxy. | [CFTC public reporting](https://publicreporting.cftc.gov/) | Weekly, published with lag. |
| Basis-trade proxy | Mean across 2y/5y/10y of asset-manager long and leveraged-fund short percentiles | Percentile proxy, not a measured cash-futures basis position. | [CFTC public reporting](https://publicreporting.cftc.gov/) | Weekly; positioning proxy only. |
| SPY Top-10 / Top-5 | Sum of top 10 / top 5 official SPY holding weights | No percentile status until at least 60 locally collected daily snapshots exist. | [SSGA SPY holdings](https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx) | Daily business day. SPY is a proxy for S&P 500; issuer history is not supplied. |

## L3 · Transmission & Amplifiers

| Metric | Definition | How to read | Source | Cadence / limitations |
|---|---|---|---|---|
| VIX term structure | Cboe VIX9D, VIX and VIX3M daily close | The term structure is available only when all latest observations have the exact same date; contango is VIX3M above VIX. | [VIX](https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv), [VIX9D](https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX9D_History.csv), [VIX3M](https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX3M_History.csv) | Daily business day. Curve shape is not a directional forecast. |
| HY / IG / BBB OAS | ICE BofA option-adjusted spread and 60-day change | Credit-spread widening can confirm transmission beyond equities. | [HY](https://fred.stlouisfed.org/graph/fredgraph.csv?id=BAMLH0A0HYM2), [IG](https://fred.stlouisfed.org/graph/fredgraph.csv?id=BAMLC0A0CM), [BBB](https://fred.stlouisfed.org/graph/fredgraph.csv?id=BAMLC0A4CBBB) | Daily. Index spreads do not isolate AI issuers. |
| AI breadth | Fixed basket percentage above 50d / 200d moving average | A breadth/leadership measure, not a scoring input. | [Yahoo Finance](https://finance.yahoo.com/) adjusted-close data | Daily. Fixed basket has selection/survivorship bias. |
| SMH/SPY and SOXX/SPY | ETF adjusted-close ratio, with 50d/200d averages and 60-day change | Semiconductor leadership versus broad market. | [Yahoo Finance](https://finance.yahoo.com/) | Daily. ETF rebalances can cause structural breaks. |
| Homebrew GEX | Black–Scholes gamma aggregation by strike from SPY/QQQ option chains | Positive estimate can dampen, negative can amplify; it is model-based. | [Yahoo Finance options](https://finance.yahoo.com/) | Intraday/daily chain availability and model assumptions limit precision. |

## Intentionally excluded or manual-only

* **DIX:** no durable public endpoint is used. It is excluded from automated scoring. FINRA ATS/Reg SHO is not treated as equivalent.
* **AI CapEx cycle:** deliberately out of scope for this fast-variable implementation; slow quarterly data is not added as a fourth layer or folded into fast confirmation.
* **Forward P/E, issuer CDS, detailed DRAM/NAND contract prices and management guidance:** require paid or manual processes and are not represented as automatic current data.

## Freshness

A daily business-day series is gray after four calendar days; the weekly reserves series is gray after ten calendar days. Every new card reports both the underlying observation date and the pipeline fetch time. FRED live reads retry, then use a committed cache of official FRED CSV responses only when necessary; freshness remains based on the observation date.
