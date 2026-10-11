import unittest
import pandas as pd

from research.deep_oversold_regime_map import (
    classify_breadth,
    classify_panic_breadth,
    classify_trend,
    classify_vol,
    event_stats,
)


class DeepOversoldRegimeMapTests(unittest.TestCase):
    def test_panic_bins_are_fixed(self):
        self.assertEqual(classify_panic_breadth(0.009), "ISOLATED_LT_1PCT")
        self.assertEqual(classify_panic_breadth(0.01), "BROAD_1_TO_5PCT")
        self.assertEqual(classify_panic_breadth(0.049), "BROAD_1_TO_5PCT")
        self.assertEqual(classify_panic_breadth(0.05), "SYSTEMIC_GE_5PCT")

    def test_state_classifiers(self):
        self.assertEqual(classify_trend(101,100), "ABOVE_MA60")
        self.assertEqual(classify_trend(99,100), "BELOW_MA60")
        self.assertEqual(classify_breadth(0.50), "BREADTH_GE_50PCT")
        self.assertEqual(classify_vol(0.30,0.20), "HIGH_VOL")
        self.assertEqual(classify_vol(0.10,0.20), "LOW_VOL")

    def test_date_equal_prevents_large_day_domination(self):
        x = pd.DataFrame([
            {"signal_date":"20260101","v":1.0},
            {"signal_date":"20260101","v":1.0},
            {"signal_date":"20260101","v":1.0},
            {"signal_date":"20260102","v":-1.0},
        ])
        st = event_stats(x, "v")
        self.assertAlmostEqual(st["avg"], 0.5)
        self.assertAlmostEqual(st["date_equal_avg"], 0.0)


if __name__ == "__main__":
    unittest.main()
