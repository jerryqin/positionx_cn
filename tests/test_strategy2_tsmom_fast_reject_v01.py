import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audit_strategy2_tsmom_fast_reject_v01 as audit


class Strategy2TSMOMFastRejectV01Tests(unittest.TestCase):

    def test_frozen_hypothesis(self):
        self.assertEqual(audit.SYMBOL, "510500.SH")
        self.assertEqual(audit.LOOKBACK, 250)
        self.assertEqual(audit.ROUND_TRIP_MAIN, 0.002)
        self.assertEqual(audit.ROUND_TRIP_SHADOW, 0.005)

    def test_month_end_dates(self):
        dates = [
            "20200130", "20200131",
            "20200203", "20200228",
            "20200302",
        ]
        self.assertEqual(
            audit.month_end_dates(dates),
            ["20200131", "20200228", "20200302"],
        )

    def test_gate_pass(self):
        strat20 = {
            "cagr": 0.09,
            "max_drawdown": -0.20,
            "sharpe": 0.80,
        }
        bh20 = {
            "cagr": 0.10,
            "max_drawdown": -0.40,
            "sharpe": 0.50,
        }
        strat50 = {
            "cagr": 0.085,
            "max_drawdown": -0.21,
            "sharpe": 0.75,
        }
        bh50 = {
            "cagr": 0.099,
            "max_drawdown": -0.40,
            "sharpe": 0.49,
        }
        rolling = {
            "windows": 10,
            "strategy_sharpe_ge_buy_hold_ratio": 0.7,
            "strategy_lower_abs_mdd_ratio": 0.9,
        }
        g = audit.fast_reject_gate(
            strat20, bh20, strat50, bh50, rolling
        )
        self.assertEqual(g["decision"], "PASS_TO_ROBUSTNESS")

    def test_gate_rejects_destroyed_cagr(self):
        strat20 = {
            "cagr": 0.04,
            "max_drawdown": -0.15,
            "sharpe": 0.80,
        }
        bh20 = {
            "cagr": 0.10,
            "max_drawdown": -0.40,
            "sharpe": 0.50,
        }
        strat50 = {
            "cagr": 0.035,
            "max_drawdown": -0.16,
            "sharpe": 0.75,
        }
        bh50 = {
            "cagr": 0.099,
            "max_drawdown": -0.40,
            "sharpe": 0.49,
        }
        rolling = {
            "windows": 10,
            "strategy_sharpe_ge_buy_hold_ratio": 0.7,
            "strategy_lower_abs_mdd_ratio": 0.9,
        }
        g = audit.fast_reject_gate(
            strat20, bh20, strat50, bh50, rolling
        )
        self.assertEqual(g["decision"], "REJECT_FAST")


if __name__ == "__main__":
    unittest.main()
