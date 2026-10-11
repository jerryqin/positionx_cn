import sys
import unittest
import importlib.util
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "d", ROOT/"scripts"/"diagnose_e1_entry_mapping_v01.py"
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_canonical_standard(self):
        self.assertEqual(m.canonical_symbol("000001.SZ"), "000001.SZ")

    def test_canonical_prefix(self):
        self.assertEqual(m.canonical_symbol("SZ000001"), "000001.SZ")
        self.assertEqual(m.canonical_symbol("SH.600000"), "600000.SH")

    def test_canonical_bare(self):
        self.assertEqual(m.canonical_symbol("600000"), "600000.SH")
        self.assertEqual(m.canonical_symbol("300001"), "300001.SZ")

    def test_setup(self):
        df = pd.DataFrame({
            "event_time": ["2025-01-01", "2025-01-02"],
            "strategy_eligible_semantics": [True, True],
            "event_kind": ["ORIGINAL_FORECAST", "FOLLOWUP_DOCUMENT"],
            "change_type": ["TURNAROUND", "TURNAROUND"],
            "symbol": ["000001.SZ", "000002.SZ"],
        })
        z = m.event_setup(df)
        self.assertEqual(len(z), 1)
        self.assertEqual(z.iloc[0]["symbol_canonical"], "000001.SZ")

    def test_reason_normalized_only(self):
        setup = pd.DataFrame({
            "announcementId": ["1"],
            "symbol": ["000001"],
            "symbol_exact": ["000001"],
            "symbol_canonical": ["000001.SZ"],
            "event_time": [pd.Timestamp("2025-01-01")],
            "event_date": [pd.Timestamp("2025-01-01")],
            "event_year": [2025],
            "change_type": ["TURNAROUND"],
        })
        market = pd.DataFrame({
            "symbol": ["000001.SZ"],
            "symbol_exact": ["000001.SZ"],
            "symbol_canonical": ["000001.SZ"],
            "min_trade_date": [pd.Timestamp("2020-01-01")],
            "max_trade_date": [pd.Timestamp("2026-01-01")],
            "rows": [1000],
        })
        z = m.classify_mappings(setup, market)
        self.assertEqual(z.loc[0, "mapping_reason"], "NORMALIZED_SYMBOL_ONLY")

if __name__ == "__main__":
    unittest.main()
