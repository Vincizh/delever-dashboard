"""Offline tests for Phase A/B fast-variable transformations and parsers."""
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

import fast_metrics as fm


class FastMetricsTests(unittest.TestCase):
    def test_freshness_marks_old_observation_stale(self):
        f = fm.freshness("2026-08-01", "daily", 3, datetime(2026, 8, 10, tzinfo=timezone.utc))
        self.assertTrue(f.stale)
        self.assertEqual(f.age_days, 9)
        self.assertEqual(f.observation_date, "2026-08-01")

    def test_cboe_vix_requires_exact_matching_observation_dates(self):
        i = pd.to_datetime(["2026-08-06", "2026-08-07"])
        spot = pd.Series([17, 18], index=i)
        v3 = pd.Series([20], index=i[:1])
        v9 = pd.Series([15, 16], index=i)
        out = fm.vix_term_structure(spot, v3, v9, datetime(2026, 8, 8, tzinfo=timezone.utc))
        self.assertFalse(out["available"])
        self.assertEqual(out["reason"], "date mismatch")

    def test_calendar_flags_tax_month_and_quarter_end(self):
        self.assertIn("corporate tax date", fm.calendar_flags(date(2026, 6, 15)))
        flags = fm.calendar_flags(date(2026, 9, 30))
        self.assertIn("month-end", flags)
        self.assertIn("quarter-end", flags)

    def test_large_treasury_settlement_parser(self):
        rows = [{"issue_date": "2026-08-15", "offering_amt": "31000000000"},
                {"issue_date": "2026-08-16", "offering_amt": "29000000000"}]
        self.assertEqual(fm.parse_treasury_large_settlements(rows), {date(2026, 8, 15)})

    def test_sofr_filter_excludes_calendar_noise_and_requires_persistence(self):
        idx = pd.bdate_range("2026-06-22", periods=8)
        sofr = pd.Series([3.65, 3.65, 3.67, 3.67, 3.67, 3.65, 3.65, 3.65], index=idx)
        iorb = pd.Series([3.65] * 8, index=idx)
        frame = fm.filtered_sofr_iorb(sofr, iorb)
        self.assertEqual(fm.sofr_iorb_status(frame)["status"], "normal")
        idx2 = pd.bdate_range("2026-07-20", periods=7)
        stable = fm.filtered_sofr_iorb(pd.Series([3.65, 3.65, 3.67, 3.67, 3.67, 3.67, 3.67], index=idx2),
                                         pd.Series([3.65] * 7, index=idx2))
        self.assertEqual(fm.sofr_iorb_status(stable)["status"], "structural-watch")

    def test_reserve_status_requires_two_week_persistence(self):
        idx = pd.date_range("2023-01-04", periods=160, freq="W-WED")
        reserves = pd.Series([1_000_000.0 + 1000 * i for i in range(160)], index=idx)
        reserves.iloc[-2:] = [1_000.0, 500.0]
        gdp = pd.Series([20_000.0] * 20, index=pd.date_range("2023-01-01", periods=20, freq="QS"))
        out = fm.reserves_gdp(reserves, gdp, datetime(2026, 2, 1, tzinfo=timezone.utc))
        self.assertEqual(out["status"], "elevated")

    def test_repo_parser_and_status(self):
        p = {"repo": {"operations": [
            {"operationDate": "2026-08-01", "totalAmtAccepted": 1000000000},
            {"operationDate": "2026-08-01", "totalAmtAccepted": 2000000000},
            {"operationDate": "2026-08-02", "totalAmtAccepted": 1000000000},
        ]}}
        s = fm.parse_repo_operations(p)
        self.assertEqual(float(s.iloc[0]), 3.0)
        out = fm.repo_usage_status(s, datetime(2026, 8, 3, tzinfo=timezone.utc))
        self.assertEqual(out["seven_day"], 4.0)

    def test_snapshot_archive_deduplicates_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            h1 = fm.archive_snapshot(path, {"date": "2026-08-06", "top5": 27.0, "top10": 38.0})
            h2 = fm.archive_snapshot(path, {"date": "2026-08-06", "top5": 27.1, "top10": 38.1})
            self.assertEqual(len(h1), 1)
            self.assertEqual(len(h2), 1)
            self.assertEqual(h2[0]["top10"], 38.1)

    def test_spy_snapshot_sums_weights(self):
        holdings = pd.DataFrame({"Weight": [10, 9, 8, 7, 6, 5, 4, 3, 2, 1, .5]})
        out = fm.concentration_snapshot("2026-08-06", holdings)
        self.assertEqual(out["top5"], 40.0)
        self.assertEqual(out["top10"], 55.0)

    def test_sofr_api_parser(self):
        frame = fm.parse_sofr_api({"refRates": [{"effectiveDate": "2026-08-06", "percentRate": 3.65,
                                                    "percentPercentile99": 3.73}]})
        self.assertAlmostEqual(float((frame["p99"] - frame["median"]).iloc[0] * 100), 8.0)


FRB_CSV = (
    '"Series Description","Interest rate on excess reserves (IOER rate)",'
    '"Interest rate on required reserves (IORR rate)","Interest rate on reserve balances (IORB rate)"\r\n'
    '"Unit:","Percent","Percent","Percent"\r\n'
    '"Multiplier:","1","1","1"\r\n'
    '"Currency:","NA","NA","NA"\r\n'
    '"Unique Identifier: ","PRATES/A","PRATES/B","PRATES/C"\r\n'
    '"Time Period","RESBME_N.D","RESBMS_N.D","RESBM_N.D"\r\n'
    '2021-07-28,0.15,0.15,\r\n'
    '2026-08-11,,,3.65\r\n'
    '2026-08-12,,,3.65\r\n'
    '2026-08-13,,,\r\n'
)


class SofrIorbDisplayRegressionTests(unittest.TestCase):
    """Regressions for the production failure where the SOFR-IORB panel showed
    no values: FRED was unreachable from CI, the committed cache froze SOFR at an
    old date, the spread inherited that date, and every KPI collapsed to an em
    dash while the chart kept plotting a week-old tail."""

    def test_frb_policy_rates_csv_parses_iorb_and_skips_blank_rows(self):
        s = fm.parse_frb_policy_rates_csv(FRB_CSV)
        self.assertEqual(s.index[-1], pd.Timestamp("2026-08-12"))
        self.assertAlmostEqual(float(s.iloc[-1]), 3.65)
        self.assertNotIn(pd.Timestamp("2021-07-28"), s.index)  # IORB column empty
        self.assertNotIn(pd.Timestamp("2026-08-13"), s.index)  # no value published

    def test_frb_policy_rates_csv_without_iorb_column_raises(self):
        bad = '"Series Description","Interest rate on excess reserves (IOER rate)"\r\n2026-08-12,0.15\r\n'
        with self.assertRaises(ValueError):
            fm.parse_frb_policy_rates_csv(bad)

    def test_splice_official_prefers_live_tail_over_stale_cache(self):
        stale = pd.Series([3.60, 3.61, 3.62],
                          index=pd.to_datetime(["2026-08-04", "2026-08-05", "2026-08-06"]))
        live = pd.Series([3.65, 3.66, 3.67],
                         index=pd.to_datetime(["2026-08-06", "2026-08-11", "2026-08-12"]))
        out = fm.splice_official(stale, live)
        self.assertEqual(out.index[-1], pd.Timestamp("2026-08-12"))
        self.assertAlmostEqual(float(out.loc["2026-08-06"]), 3.65)  # live wins on overlap
        self.assertAlmostEqual(float(out.loc["2026-08-04"]), 3.60)  # history preserved

    def test_splice_official_handles_missing_sources_without_inventing_data(self):
        live = pd.Series([3.65], index=pd.to_datetime(["2026-08-12"]))
        self.assertTrue(fm.splice_official(None, None).empty)
        self.assertEqual(list(fm.splice_official(None, live).index), [pd.Timestamp("2026-08-12")])
        self.assertEqual(list(fm.splice_official(live, None).index), [pd.Timestamp("2026-08-12")])
        self.assertTrue(fm.splice_official(pd.Series(dtype=float), pd.Series(dtype=float)).empty)

    def test_spread_reaches_latest_sofr_when_iorb_is_calendar_daily(self):
        sofr_idx = pd.bdate_range("2026-07-27", "2026-08-12")
        sofr = pd.Series([3.65] * len(sofr_idx), index=sofr_idx)
        iorb_idx = pd.date_range("2026-07-27", "2026-08-14")  # weekends included
        iorb = pd.Series([3.65] * len(iorb_idx), index=iorb_idx)
        frame = fm.filtered_sofr_iorb(sofr, iorb)
        self.assertEqual(frame.index[-1], sofr_idx[-1])
        self.assertEqual(len(frame), len(sofr_idx))

    def test_lagging_iorb_truncates_spread_and_is_reported_stale(self):
        sofr_idx = pd.bdate_range("2026-07-27", "2026-08-12")
        sofr = pd.Series([3.66] * len(sofr_idx), index=sofr_idx)
        iorb_idx = pd.date_range("2026-07-27", "2026-08-06")
        iorb = pd.Series([3.65] * len(iorb_idx), index=iorb_idx)
        frame = fm.filtered_sofr_iorb(sofr, iorb)
        self.assertEqual(frame.index[-1], pd.Timestamp("2026-08-06"))
        f = fm.freshness(frame.index[-1], "daily business day", 4,
                         datetime(2026, 8, 14, tzinfo=timezone.utc))
        self.assertTrue(f.stale)

    def test_filtered_median_stays_empty_without_enough_non_calendar_readings(self):
        idx = pd.to_datetime(["2026-06-30", "2026-07-31", "2026-08-31"])  # all month-end
        frame = fm.filtered_sofr_iorb(pd.Series([3.70, 3.72, 3.71], index=idx),
                                      pd.Series([3.65, 3.65, 3.65], index=idx))
        self.assertTrue(frame["calendar_noise"].all())
        self.assertTrue(frame["filtered_5d_bp"].isna().all())

    def _payload(self, obs, stale, current=1.5, change=-0.5, median=-1.0):
        return {"dates": ["2026-08-05", obs], "current_bp": current,
                "one_day_change_bp": change, "filtered_5d_bp": median,
                "observation_date": obs,
                "status": {"status": "calendar-noise", "message": "Likely calendar-related: month-end"},
                "freshness": {"observation_date": obs, "stale": stale, "age_days": 8 if stale else 1}}

    def test_kpi_view_renders_real_values_when_series_is_fresh(self):
        v = fm.sofr_kpi_view(self._payload("2026-08-12", False))
        self.assertEqual(v["state"], "live")
        self.assertEqual(v["current"], "+1.5 bp")
        self.assertEqual(v["change"], "-0.5 bp")
        self.assertEqual(v["median"], "-1.0 bp")
        self.assertEqual(v["status_label"], "calendar noise")
        self.assertEqual(v["note"], "")

    def test_kpi_view_never_blanks_a_stale_observation(self):
        v = fm.sofr_kpi_view(self._payload("2026-08-06", True))
        self.assertEqual(v["state"], "stale")
        for key in ("current", "change", "median"):
            self.assertNotEqual(v[key], "\u2014", key + " collapsed to an em dash")
        self.assertEqual(v["status_label"], "stale data")
        self.assertIn("2026-08-06", v["message"])
        self.assertIn("2026-08-06", v["note"])

    def test_kpi_view_reports_unavailable_without_fabricating_numbers(self):
        v = fm.sofr_kpi_view({"available": False, "freshness": {"observation_date": None, "stale": True}})
        self.assertEqual(v["state"], "unavailable")
        self.assertEqual([v["current"], v["change"], v["median"]], ["\u2014", "\u2014", "\u2014"])
        self.assertEqual(v["status_label"], "unavailable")

    def test_kpi_view_keeps_missing_single_fields_as_dashes(self):
        payload = self._payload("2026-08-12", False)
        payload["one_day_change_bp"] = None
        payload["filtered_5d_bp"] = None
        v = fm.sofr_kpi_view(payload)
        self.assertEqual(v["current"], "+1.5 bp")
        self.assertEqual(v["change"], "\u2014")
        self.assertEqual(v["median"], "\u2014")


H41_HTML = """
<table><tr><th>Reserve Bank credit</th><th>Averages of daily figures<br>Week ended Aug 12, 2026</th>
<th>Change from<br>week ended</th><th>Wednesday<br>Aug 12, 2026</th></tr>
<tr><td>Total factors supplying reserve funds</td><td>6,749,000</td><td>-12,000</td><td>6,750,100</td></tr>
<tr><td>U.S. Treasury, General Account</td><td>963,950</td><td>+56,626</td><td>975,300</td></tr>
<tr><td>Reserve balances with Federal Reserve Banks</td><td>2,944,059</td><td>-49,290</td><td>2,930,100</td></tr>
</table>
"""

DTS_ROWS = [
    {"record_date": "2026-08-12", "account_type": "Treasury General Account (TGA) Closing Balance",
     "close_today_bal": "null", "open_today_bal": "961000"},
    {"record_date": "2026-08-13", "account_type": "Treasury General Account (TGA) Closing Balance",
     "close_today_bal": "966587", "open_today_bal": "961000"},
    {"record_date": "2026-08-13", "account_type": "Federal Reserve Account",
     "close_today_bal": "12345", "open_today_bal": "12000"},
    {"record_date": "2026-08-14", "account_type": "Treasury General Account (TGA) Closing Balance",
     "close_today_bal": "null", "open_today_bal": "null"},
]

NOW = datetime(2026, 8, 15, 12, tzinfo=timezone.utc)


def _weekly(values, *, end="2026-08-12", weeks=None):
    """Weekly Wednesday series in $ millions, oldest first."""
    idx = pd.date_range(end=pd.Timestamp(end), periods=len(values), freq="7D")
    return pd.Series([float(v) for v in values], index=idx)


def _metric(level_t, four_week_b, *, stale=False, observation="2026-08-12"):
    """Minimal balance_metric-shaped dict for leg-rule tests."""
    return {
        "available": not stale,
        "observation_date": observation,
        "freshness": {"observation_date": observation, "stale": stale, "cadence": "weekly"},
        "current_trillions": level_t,
        "four_week_change_b": four_week_b,
        "one_week_change_b": None if four_week_b is None else four_week_b / 4.0,
        "speed_b_per_week": None if four_week_b is None else four_week_b / 4.0,
    }


def _sofr_metric(raw, noise, filtered, *, stale=False):
    return {"available": not stale, "observation_date": "2026-08-13",
            "freshness": {"observation_date": "2026-08-13", "stale": stale, "cadence": "daily"},
            "raw_bp": raw, "calendar_noise": noise, "filtered_5d_bp": filtered}


class FundingPressureParsingTests(unittest.TestCase):
    def test_h41_release_yields_weekly_average_and_wednesday_level(self):
        out = fm.parse_h41_release(H41_HTML)
        self.assertEqual(out["week_ended"], "2026-08-12")
        self.assertAlmostEqual(out["reserves_millions"], 2944059.0)
        self.assertAlmostEqual(out["reserves_wednesday_millions"], 2930100.0)
        self.assertAlmostEqual(out["tga_millions"], 963950.0)
        self.assertAlmostEqual(out["tga_wednesday_millions"], 975300.0)

    def test_h41_release_without_reserve_row_raises_instead_of_guessing(self):
        broken = H41_HTML.replace("Reserve balances with Federal Reserve Banks", "Some other line")
        with self.assertRaises(ValueError):
            fm.parse_h41_release(broken)

    def test_h41_release_without_week_ended_header_raises(self):
        with self.assertRaises(ValueError):
            fm.parse_h41_release(H41_HTML.replace("Week ended Aug 12, 2026", "As of date"))

    def test_dts_parser_reads_tga_only_and_skips_null_balances(self):
        s = fm.parse_dts_operating_cash(DTS_ROWS)
        self.assertEqual([d.strftime("%Y-%m-%d") for d in s.index], ["2026-08-12", "2026-08-13"])
        self.assertAlmostEqual(float(s.loc["2026-08-12"]), 961000.0)   # open_today_bal fallback
        self.assertAlmostEqual(float(s.loc["2026-08-13"]), 966587.0)   # close_today_bal preferred
        self.assertNotIn(pd.Timestamp("2026-08-14"), s.index)          # both fields "null"

    def test_dts_parser_returns_empty_series_when_account_missing(self):
        self.assertTrue(fm.parse_dts_operating_cash(
            [{"record_date": "2026-08-13", "account_type": "Federal Reserve Account",
              "close_today_bal": "1"}]).empty)


class BalanceMetricTests(unittest.TestCase):
    def test_deltas_speed_and_reference_dates_use_real_observations(self):
        s = _weekly([3_142_721, 3_100_000, 3_050_000, 2_993_349, 2_944_059])
        m = fm.balance_metric(s, cadence="weekly", source="H.4.1", now=NOW)
        self.assertTrue(m["available"])
        self.assertEqual(m["observation_date"], "2026-08-12")
        self.assertAlmostEqual(m["current_trillions"], 2.944059, places=6)
        self.assertAlmostEqual(m["one_week_change_b"], -49.29, places=3)
        self.assertEqual(m["one_week_ref_date"], "2026-08-05")
        self.assertAlmostEqual(m["four_week_change_b"], -198.662, places=3)
        self.assertEqual(m["four_week_ref_date"], "2026-07-15")
        self.assertAlmostEqual(m["speed_b_per_week"], -49.6655, places=4)
        self.assertEqual(len(m["dates"]), len(m["history"]))

    def test_missing_week_is_left_missing_rather_than_forward_filled(self):
        # Only two observations: the 4-week comparison genuinely does not exist.
        s = _weekly([2_993_349, 2_944_059])
        m = fm.balance_metric(s, cadence="weekly", now=NOW)
        self.assertIsNone(m["four_week_change_b"])
        self.assertIsNone(m["four_week_ref_date"])
        self.assertIsNone(m["speed_b_per_week"])
        self.assertAlmostEqual(m["one_week_change_b"], -49.29, places=3)

    def test_stale_series_is_reported_unavailable(self):
        s = _weekly([3_000_000, 2_950_000, 2_944_059], end="2026-06-10")
        m = fm.balance_metric(s, cadence="weekly", max_age_days=10, now=NOW)
        self.assertFalse(m["available"])
        self.assertTrue(m["freshness"]["stale"])

    def test_empty_series_reports_unavailable_without_fabricating_values(self):
        m = fm.balance_metric(pd.Series(dtype=float), cadence="weekly", now=NOW)
        self.assertFalse(m["available"])
        self.assertEqual(m["reason"], "no observations")
        self.assertEqual(m["dates"], [])


class FundingLegRuleTests(unittest.TestCase):
    def test_reserve_leg_needs_both_level_and_speed(self):
        active = fm.reserve_leg(_metric(2.88, -60.0))
        self.assertTrue(active["triggered"])
        self.assertEqual(active["state"], "active")
        # fast decline but still a comfortable level -> approaching, not counted
        approaching = fm.reserve_leg(_metric(2.944, -198.7))
        self.assertFalse(approaching["triggered"])
        self.assertEqual(approaching["state"], "approaching")
        # low level but flat -> approaching
        flat = fm.reserve_leg(_metric(2.85, -10.0))
        self.assertFalse(flat["triggered"])
        self.assertEqual(flat["state"], "approaching")
        normal = fm.reserve_leg(_metric(3.10, +20.0))
        self.assertFalse(normal["triggered"])
        self.assertEqual(normal["state"], "normal")

    def test_reserve_leg_elevated_at_lower_level_or_faster_drain(self):
        self.assertEqual(fm.reserve_leg(_metric(2.84, -60.0))["state"], "elevated")
        self.assertEqual(fm.reserve_leg(_metric(2.89, -120.0))["state"], "elevated")

    def test_tga_leg_needs_both_level_and_rebuild_speed(self):
        self.assertTrue(fm.tga_leg(_metric(0.95, +80.0))["triggered"])
        self.assertEqual(fm.tga_leg(_metric(0.95, +80.0))["state"], "active")
        self.assertEqual(fm.tga_leg(_metric(0.96, +207.7))["state"], "elevated")
        self.assertEqual(fm.tga_leg(_metric(1.02, +60.0))["state"], "elevated")
        self.assertEqual(fm.tga_leg(_metric(0.95, +10.0))["state"], "approaching")
        self.assertEqual(fm.tga_leg(_metric(0.60, +80.0))["state"], "approaching")
        self.assertEqual(fm.tga_leg(_metric(0.60, -30.0))["state"], "normal")

    def test_missing_four_week_change_gives_incomplete_not_triggered(self):
        for leg in (fm.reserve_leg(_metric(2.80, None)), fm.tga_leg(_metric(1.10, None))):
            self.assertFalse(leg["triggered"])
            self.assertFalse(leg["available"])
            self.assertEqual(leg["state"], "incomplete")

    def test_stale_leg_reports_stale_and_never_triggers(self):
        leg = fm.reserve_leg(_metric(2.80, -200.0, stale=True))
        self.assertFalse(leg["triggered"])
        self.assertFalse(leg["available"])
        self.assertEqual(leg["state"], "stale")
        self.assertIn("2026-08-12", leg["detail"])

    def test_unavailable_leg_reports_unavailable(self):
        leg = fm.tga_leg({"available": False, "freshness": {}})
        self.assertEqual(leg["state"], "unavailable")
        self.assertFalse(leg["triggered"])

    def test_sofr_leg_requires_persistence_and_ignores_calendar_spikes(self):
        # one calendar spike above +3bp, everything else easy -> not triggered
        spike = fm.sofr_leg(_sofr_metric([-2.0, -1.0, -2.0, 9.0], [False, False, False, True], -2.0))
        self.assertFalse(spike["triggered"])
        self.assertEqual(spike["state"], "normal")
        # three eligible readings at/above +3bp and filtered median above +3bp
        on = fm.sofr_leg(_sofr_metric([1.0, 3.2, 3.5, 4.0, 12.0],
                                      [False, False, False, False, True], 3.4))
        self.assertTrue(on["triggered"])
        self.assertEqual(on["state"], "active")
        self.assertEqual(on["eligible_recent_bp"], [3.2, 3.5, 4.0])
        # only two eligible readings above the threshold -> approaching
        partial = fm.sofr_leg(_sofr_metric([1.0, 2.0, 3.5, 4.0], [False, False, False, False], 3.1))
        self.assertFalse(partial["triggered"])
        self.assertEqual(partial["state"], "approaching")
        # filtered median below the strategy line -> not triggered
        weak = fm.sofr_leg(_sofr_metric([3.1, 3.2, 3.3], [False, False, False], 2.2))
        self.assertFalse(weak["triggered"])

    def test_sofr_leg_elevated_above_reference_band(self):
        hot = fm.sofr_leg(_sofr_metric([5.5, 6.0, 6.5], [False, False, False], 6.0))
        self.assertEqual(hot["state"], "elevated")


class FundingResonanceTests(unittest.TestCase):
    def _legs(self, sofr_on, res_on, tga_on):
        return [
            fm.sofr_leg(_sofr_metric([3.2, 3.5, 4.0] if sofr_on else [-2.0, -1.0, -2.0],
                                     [False, False, False], 3.4 if sofr_on else -2.0)),
            fm.reserve_leg(_metric(2.88 if res_on else 3.10, -60.0 if res_on else +20.0)),
            fm.tga_leg(_metric(0.95 if tga_on else 0.60, +80.0 if tga_on else -30.0)),
        ]

    def test_zero_of_three_is_normal(self):
        r = fm.funding_resonance(self._legs(False, False, False))
        self.assertEqual((r["active_count"], r["state"]), (0, "normal"))
        self.assertEqual(r["summary"], "0/3 conditions active")
        self.assertFalse(r["incomplete"])

    def test_one_of_three_names_the_active_leg(self):
        r = fm.funding_resonance(self._legs(False, False, True))
        self.assertEqual((r["active_count"], r["state"]), (1, "watch"))
        self.assertEqual(r["active_legs"], ["tga"])
        self.assertIn("fiscal drain", r["interpretation"].lower())

    def test_two_of_three_is_a_yellow_warning(self):
        r = fm.funding_resonance(self._legs(False, True, True))
        self.assertEqual((r["active_count"], r["state"]), (2, "warning"))
        self.assertEqual(r["headline"], "Yellow warning")
        self.assertIn("building", r["interpretation"])

    def test_three_of_three_is_fragility_confirmation_not_a_forecast(self):
        r = fm.funding_resonance(self._legs(True, True, True))
        self.assertEqual((r["active_count"], r["state"]), (3, "structural-top-risk"))
        self.assertIn("not a deterministic market-top prediction", r["interpretation"])

    def test_stale_or_missing_legs_never_count_and_flag_incomplete(self):
        legs = [
            fm.sofr_leg(_sofr_metric([9.0, 9.0, 9.0], [False, False, False], 9.0, stale=True)),
            fm.reserve_leg(_metric(2.80, -200.0, stale=True)),
            fm.tga_leg(_metric(0.95, +80.0)),
        ]
        r = fm.funding_resonance(legs)
        self.assertEqual(r["active_count"], 1)
        self.assertEqual(r["active_legs"], ["tga"])
        self.assertTrue(r["incomplete"])
        self.assertEqual(sorted(r["unavailable_legs"]), ["reserves", "sofr"])
        self.assertIn("Incomplete data", r["interpretation"])

    def test_one_failing_source_does_not_blank_the_other_two_legs(self):
        legs = [
            fm.sofr_leg(_sofr_metric([-2.0, -1.0, -2.0], [False, False, False], -2.0)),
            fm.reserve_leg({"available": False, "freshness": {}}),   # source outage
            fm.tga_leg(_metric(0.96, +207.7)),
        ]
        r = fm.funding_resonance(legs)
        self.assertTrue(r["legs"]["sofr"]["available"])
        self.assertTrue(r["legs"]["tga"]["available"])
        self.assertEqual(r["legs"]["reserves"]["state"], "unavailable")
        self.assertEqual(r["active_count"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
