import unittest
import pandas as pd

from research.strategy_matching import (
    BreakoutRetestConfig,
    BreakoutRetestMatcher,
    LevelHoldMatcher,
)


def base_bars(n=50, close=10.0, volume=100.0):
    return pd.DataFrame({
        "high": [close] * n,
        "low": [close * 0.99] * n,
        "close": [close] * n,
        "volume": [volume] * n,
    })


class StrategyMatchingTests(unittest.TestCase):
    def test_breakout_day_is_armed_not_triggered(self):
        df = base_bars()
        df.loc[df.index[-1], ["high", "low", "close", "volume"]] = [10.2, 10.1, 10.05, 120.0]
        m = BreakoutRetestMatcher()
        r = m.match("A.SH", df)
        self.assertTrue(r["matched"])
        self.assertFalse(r["triggered"])
        self.assertEqual(r["state"], "ARMED")

    def test_valid_breakout_then_retest_hold_triggers(self):
        df = base_bars()
        # Breakout 2 bars before latest: prior 20d high = 10.
        df.loc[df.index[-3], ["high", "low", "close", "volume"]] = [10.3, 10.0, 10.05, 120.0]
        # Still above invalidation on next day.
        df.loc[df.index[-2], ["high", "low", "close", "volume"]] = [10.12, 10.04, 10.08, 100.0]
        # Latest retests 10 and closes within +0.5%.
        df.loc[df.index[-1], ["high", "low", "close", "volume"]] = [10.06, 9.99, 10.03, 100.0]
        r = BreakoutRetestMatcher().match("A.SH", df)
        self.assertTrue(r["matched"])
        self.assertTrue(r["triggered"])
        self.assertEqual(r["state"], "RETEST_HOLD")

    def test_breakout_invalidated_is_failed(self):
        df = base_bars()
        df.loc[df.index[-3], ["high", "low", "close", "volume"]] = [10.3, 10.0, 10.05, 120.0]
        df.loc[df.index[-2], ["high", "low", "close", "volume"]] = [10.0, 9.7, 9.80, 100.0]
        df.loc[df.index[-1], ["high", "low", "close", "volume"]] = [9.9, 9.8, 9.85, 100.0]
        r = BreakoutRetestMatcher().match("A.SH", df)
        self.assertFalse(r["matched"])
        self.assertEqual(r["state"], "BREAKOUT_FAILED")

    def test_level_hold_two_closes_and_low_tolerance(self):
        df = base_bars()
        df.loc[df.index[-2], ["high", "low", "close"]] = [10.3, 10.01, 10.12]
        df.loc[df.index[-1], ["high", "low", "close"]] = [10.25, 9.95, 10.08]
        r = LevelHoldMatcher().match("A.SH", df, level=10.0)
        self.assertTrue(r["matched"])
        self.assertTrue(r["triggered"])
        self.assertEqual(r["state"], "HOLD_CONFIRMED")

    def test_level_hold_does_not_invent_or_relax_level(self):
        df = base_bars()
        df.loc[df.index[-2], ["low", "close"]] = [9.70, 9.95]
        df.loc[df.index[-1], ["low", "close"]] = [9.80, 10.01]
        r = LevelHoldMatcher().match("A.SH", df, level=10.0)
        self.assertFalse(r["matched"])
        self.assertIn("CLOSE_CONFIRM_FAIL", r["reason"])


if __name__ == "__main__":
    unittest.main()
