import sys
import unittest
import importlib.util
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "d",
    ROOT / "scripts" / "diagnose_eva_forecast_payload_v011.py",
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class T(unittest.TestCase):
    def test_payload_numeric(self):
        df = pd.DataFrame({
            "current_profit_min_yuan": [1.0, None],
            "profit_state": [None, None],
        })
        self.assertEqual(m.strict_semantic_payload_mask(df).tolist(), [True, False])

    def test_payload_unknown_not_valid(self):
        df = pd.DataFrame({
            "profit_state": ["UNKNOWN", "PROFIT"],
            "change_type": [None, None],
        })
        self.assertEqual(m.strict_semantic_payload_mask(df).tolist(), [False, True])

    def test_payload_excluded_not_valid(self):
        df = pd.DataFrame({
            "profit_state": ["EXCLUDED"],
            "change_type": ["NOT_APPLICABLE"],
        })
        self.assertEqual(m.strict_semantic_payload_mask(df).tolist(), [False])

    def test_norm_bool(self):
        s = pd.Series([True, False, "True", "eligible", "0"])
        self.assertEqual(m.norm_bool(s).tolist(), [True, False, True, True, False])

    def test_counts_dict(self):
        s = pd.Series(["A", "A", "B"])
        self.assertEqual(m.counts_dict(s), {"A": 2, "B": 1})


if __name__ == "__main__":
    unittest.main()
