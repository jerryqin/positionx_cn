import sys
import unittest
from pathlib import Path
import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Import pure module via runpy-style selective execution is unnecessary here:
# pandas is available and script imports are standard.
import importlib.util

spec = importlib.util.spec_from_file_location(
    "validator",
    ROOT / "scripts" / "validate_sector_pit_candidate_v03.py",
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_parse_yyyymmdd(self):
        s = pd.Series(["20200102", "2026-09-30"])
        out = m.parse_date_series(s)
        self.assertEqual(str(out.iloc[0].date()), "2020-01-02")
        self.assertEqual(str(out.iloc[1].date()), "2026-09-30")


    def test_parse_missing_end_dates(self):
        s = pd.Series([pd.NA, None, "", "2026-09-30"])
        out = m.parse_date_series(s)
        self.assertTrue(pd.isna(out.iloc[0]))
        self.assertTrue(pd.isna(out.iloc[1]))
        self.assertTrue(pd.isna(out.iloc[2]))
        self.assertEqual(str(out.iloc[3].date()), "2026-09-30")


    def test_parse_float_like_yyyymmdd_end_dates(self):
        s = pd.Series([
            "20210730.0",
            20220317.0,
            "2026-09-30",
            pd.NA,
            "",
        ])
        out = m.parse_date_series(s)
        self.assertEqual(str(out.iloc[0].date()), "2021-07-30")
        self.assertEqual(str(out.iloc[1].date()), "2022-03-17")
        self.assertEqual(str(out.iloc[2].date()), "2026-09-30")
        self.assertTrue(pd.isna(out.iloc[3]))
        self.assertTrue(pd.isna(out.iloc[4]))

    def test_lineage_completeness(self):
        df = pd.DataFrame({
            "source_id": ["A", "B"],
            "source_url": ["u1", "u2"],
            "provenance_note": ["p1", "p2"],
        })
        r = m.lineage_completeness(df)
        self.assertEqual(r["source_id"]["nonempty_rate"], 1.0)

    def test_interval_invalid_detection(self):
        df = pd.DataFrame({
            "symbol": ["A", "B"],
            "effective_from": ["2025-01-01", "2025-02-01"],
            "effective_to_exclusive": ["2025-01-10", "2025-01-20"],
        })
        r = m.interval_integrity(df, "effective_from", "effective_to_exclusive")
        self.assertEqual(r["invalid_interval_rows"], 1)

    def test_symbol_normalization(self):
        s = pd.Series([" 000001.SZ ", "600000.sh"])
        out = m.normalize_symbol(s)
        self.assertEqual(out.tolist(), ["000001.SZ", "600000.SH"])

    def test_acceptance_rejects_low_coverage(self):
        cand = {
            "status": "SCHEMA_OK",
            "interval_integrity": {
                "invalid_start_rows": 0,
                "invalid_end_rows": 0,
                "invalid_interval_rows": 0,
                "overlap_count": 0,
                "date_start_min": "2020-01-01",
            },
            "lineage": {
                "source_id": {"exists": True, "nonempty_rate": 1.0},
                "source_url": {"exists": True, "nonempty_rate": 1.0},
                "provenance_note": {"exists": True, "nonempty_rate": 1.0},
            },
        }
        cov = {
            "coverage_rate": 0.50,
            "ambiguity_rate": 0.0,
            "by_year": {"2020": {"coverage_rate": 1.0}},
        }
        cfg = {"gate": {
            "overall_coverage_min": 0.95,
            "yearly_coverage_min": 0.90,
            "ambiguity_rate_max": 0.001,
            "lineage_nonempty_min": 0.95,
            "required_start_date": "2020-08-03",
        }}
        r = m.acceptance(cand, cov, cfg)
        self.assertFalse(r["accepted"])

if __name__ == "__main__":
    unittest.main()
