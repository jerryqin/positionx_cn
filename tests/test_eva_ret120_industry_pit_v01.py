import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import validate_eva_ret120_industry_pit_v01 as v


class EvaIndustryValidationTests(unittest.TestCase):

    def test_normalize_date(self):
        self.assertEqual(v.normalize_date("2026-09-24"), "20260924")
        self.assertEqual(v.normalize_date("20260924"), "20260924")

    def test_invalid_date(self):
        self.assertEqual(v.normalize_date("20261399"), "INVALID")

    def test_open_end_allowed(self):
        self.assertIsNone(v.normalize_date("", allow_blank=True))

    def test_overlap_detected(self):
        df = pd.DataFrame([
            {
                "symbol": "000001.SZ",
                "effective_from": "20200101",
                "effective_to_exclusive": "20210101",
            },
            {
                "symbol": "000001.SZ",
                "effective_from": "20201201",
                "effective_to_exclusive": None,
            },
        ])
        issues = v.validate_intervals(df)
        self.assertTrue(any(x[1] == "OVERLAPPING_INTERVAL" for x in issues))


if __name__ == "__main__":
    unittest.main()
