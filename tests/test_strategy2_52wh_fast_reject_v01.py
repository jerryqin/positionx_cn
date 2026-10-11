import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audit_strategy2_52wh_fast_reject_v01 as audit


class Strategy252WHFastRejectV01Tests(unittest.TestCase):

    def test_frozen_primary_horizon(self):
        self.assertEqual(audit.LOOKBACK, 250)
        self.assertEqual(audit.PRIMARY_HORIZON, 60)
        self.assertEqual(audit.DIAGNOSTIC_HORIZONS, (20, 120))

    def test_summarize_trim(self):
        s = pd.Series(range(100), dtype=float)
        r = audit.summarize(s)
        self.assertEqual(r["n"], 100)
        self.assertAlmostEqual(r["median"], 49.5)
        self.assertAlmostEqual(r["trim5"], 49.5)

    def test_fast_reject_pass(self):
        primary = {
            "excess": {"n": 500, "mean": 0.02, "trim5": 0.01},
            "signal_date_equal_weight_excess": {"mean": 0.01},
            "net20": {"mean": 0.03},
            "net50": {"mean": 0.027},
        }
        folds = [
            {"excess_mean": 0.01, "date_equal_excess_mean": 0.01},
            {"excess_mean": -0.01, "date_equal_excess_mean": -0.01},
            {"excess_mean": 0.02, "date_equal_excess_mean": 0.02},
        ]
        gate = audit.fast_reject_gate(primary, folds)
        self.assertEqual(gate["decision"], "PASS_TO_SHARED_EQUITY")

    def test_fast_reject_rejects_negative_trim(self):
        primary = {
            "excess": {"n": 500, "mean": 0.02, "trim5": -0.001},
            "signal_date_equal_weight_excess": {"mean": 0.01},
            "net20": {"mean": 0.03},
            "net50": {"mean": 0.027},
        }
        folds = [
            {"excess_mean": 0.01, "date_equal_excess_mean": 0.01},
            {"excess_mean": 0.01, "date_equal_excess_mean": 0.01},
            {"excess_mean": 0.02, "date_equal_excess_mean": 0.02},
        ]
        gate = audit.fast_reject_gate(primary, folds)
        self.assertEqual(gate["decision"], "REJECT_FAST")


if __name__ == "__main__":
    unittest.main()
