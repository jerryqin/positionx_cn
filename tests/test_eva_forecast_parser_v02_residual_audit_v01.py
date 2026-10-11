import sys
import unittest
import importlib.util
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "a",
    ROOT / "scripts" / "audit_eva_forecast_parser_v02_residuals_v01.py",
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_payload_unknown_is_missing(self):
        df = pd.DataFrame({"profit_state": ["UNKNOWN", "PROFIT"]})
        self.assertEqual(m.strict_payload_mask(df).tolist(), [False, True])

    def test_payload_numeric(self):
        df = pd.DataFrame({"current_profit_min_yuan": [None, 1.0]})
        self.assertEqual(m.strict_payload_mask(df).tolist(), [False, True])

    def test_max_missing_2025(self):
        self.assertEqual(m.max_missing_for_rate(3547, 0.99), 35)
        self.assertEqual(42 - m.max_missing_for_rate(3547, 0.99), 7)

    def test_max_missing_2026(self):
        self.assertEqual(m.max_missing_for_rate(3817, 0.99), 38)
        self.assertEqual(58 - m.max_missing_for_rate(3817, 0.99), 20)

    def test_title_pattern(self):
        self.assertIn("RATING_OR_TRUSTEE", m.title_pattern("某证券受托管理事务报告"))
        self.assertIn("STANDARD_FORECAST", m.title_pattern("2026年度业绩预告"))

if __name__ == "__main__":
    unittest.main()
