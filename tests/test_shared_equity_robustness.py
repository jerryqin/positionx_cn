import unittest
import pandas as pd
from research.shared_equity_robustness import curve_metrics, period_metrics, distribution

class SharedEquityRobustnessTests(unittest.TestCase):
    def test_distribution(self):
        d = distribution([1,2,3,4,5])
        self.assertEqual(d["median"], 3)
        self.assertEqual(d["n"], 5)

    def test_curve_metrics_positive(self):
        daily = [
            {"date":"20260102","equity":100.0},
            {"date":"20260105","equity":101.0},
            {"date":"20260106","equity":102.0},
        ]
        m = curve_metrics(daily, 100.0)
        self.assertAlmostEqual(m["total_return"], 0.02)

    def test_period_metrics(self):
        daily = [
            {"date":"20260102","equity":100.0},
            {"date":"20260105","equity":110.0},
            {"date":"20260106","equity":105.0},
        ]
        m = period_metrics(daily, "20260101", "20261231")
        self.assertEqual(m["sessions"], 3)
        self.assertLess(m["max_drawdown"], 0)

if __name__ == "__main__":
    unittest.main()
