import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audit_deep_oversold_origin_snapshot_v01 as audit


class DeepOversoldOriginSnapshotV01Tests(unittest.TestCase):

    def test_function_profiles_finds_trade_function(self):
        src = """
def foo(events, max_gross):
    trades = len(events)
    return trades, max_gross
"""
        p = audit.function_profiles(src)
        self.assertEqual(p[0]["name"], "foo")
        self.assertGreater(p[0]["score"], 0)

    def test_find_metric_paths(self):
        obj = {
            "main": {
                "trades": 1365,
                "cagr": 0.11,
                "max_drawdown": -0.09,
            }
        }
        hits = audit.find_metric_paths(obj)
        paths = {x["path"] for x in hits}
        self.assertIn("$.main.trades", paths)
        self.assertIn("$.main.cagr", paths)


if __name__ == "__main__":
    unittest.main()
