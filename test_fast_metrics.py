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


if __name__ == "__main__":
    unittest.main(verbosity=2)
