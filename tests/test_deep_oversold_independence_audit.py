import unittest
import pandas as pd

from research.deep_oversold_independence_audit import (
    dedup_while_held,
    equal_weight_by_signal_date,
    stats,
)


class DeepOversoldIndependenceAuditTests(unittest.TestCase):
    def test_dedup_removes_signal_while_position_active(self):
        x = pd.DataFrame([
            {"symbol":"A","signal_date":"20260101","exit_date":"20260121"},
            {"symbol":"A","signal_date":"20260102","exit_date":"20260122"},
            {"symbol":"A","signal_date":"20260121","exit_date":"20260210"},
            {"symbol":"B","signal_date":"20260102","exit_date":"20260122"},
        ])
        y = dedup_while_held(x)
        self.assertEqual(len(y), 3)
        self.assertEqual((y["symbol"]=="A").sum(), 2)

    def test_date_equal_weight_prevents_large_date_from_dominating(self):
        x = pd.DataFrame([
            {"signal_date":"20260101","symbol":"A","r":1.0},
            {"signal_date":"20260101","symbol":"B","r":1.0},
            {"signal_date":"20260101","symbol":"C","r":1.0},
            {"signal_date":"20260102","symbol":"D","r":-1.0},
        ])
        g = equal_weight_by_signal_date(x, "r")
        self.assertAlmostEqual(float(g["value"].mean()), 0.0)

    def test_stats_reports_trimmed_mean(self):
        x = pd.Series([-10,1,1,1,1,1,1,1,1,1,20], dtype=float)
        s = stats(x)
        self.assertIsNotNone(s["trimmed_mean_5pct"])


if __name__ == "__main__":
    unittest.main()
