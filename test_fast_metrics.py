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


if __name__ == "__main__":
    unittest.main(verbosity=2)
