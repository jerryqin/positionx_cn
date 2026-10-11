import importlib.util
from pathlib import Path
import unittest
import pandas as pd
import sys, types

# Pure-function tests do not need a real DuckDB. Stub import for CI environments
# where duckdb is not installed; the PositionX runtime already provides duckdb.
if "duckdb" not in sys.modules:
    duckdb_stub = types.ModuleType("duckdb")
    duckdb_stub.DuckDBPyConnection = object
    def _no_connect(*a, **k):
        raise RuntimeError("duckdb stub: connect not available in pure-function tests")
    duckdb_stub.connect = _no_connect
    sys.modules["duckdb"] = duckdb_stub

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_optimus_m_evidence_v01.py"
spec = importlib.util.spec_from_file_location("m", SCRIPT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class OptimusMEvidenceTests(unittest.TestCase):
    def test_comparable_index(self):
        df = pd.DataFrame({"trade_date": pd.date_range("2026-01-01", periods=3), "close": [10, 11, 12], "pre_close": [10, 10, 11]})
        x = m.comparable_index(df).dropna()
        self.assertAlmostEqual(float(x.iloc[-1]), 1.2, places=10)

    def test_trailing_return(self):
        s = pd.Series([1.0, 1.1, 1.21])
        self.assertAlmostEqual(m.trailing_return(s, 2), 0.21, places=10)

    def test_m_state_strong(self):
        r = {"data_status":"OK", "rs20_vs_broad":0.1, "rs60_vs_broad":0.2,
             "above_ma20":True, "above_ma60":True, "above_ma120":True,
             "amount20_vs_60":1.2, "within_3pct_60d_high":False}
        self.assertEqual(m.m_state(r, 1.10), "M_STRONG")

    def test_m_state_improving(self):
        r = {"data_status":"OK", "rs20_vs_broad":0.05, "rs60_vs_broad":-0.01,
             "above_ma20":True, "above_ma60":True, "above_ma120":False,
             "amount20_vs_60":0.9, "within_3pct_60d_high":False}
        self.assertEqual(m.m_state(r, 1.10), "M_IMPROVING")

    def test_insufficient_fail_closed(self):
        self.assertEqual(m.m_state({"data_status":"INSUFFICIENT_HISTORY"}, 1.10), "INSUFFICIENT_DATA")


if __name__ == "__main__":
    unittest.main()
