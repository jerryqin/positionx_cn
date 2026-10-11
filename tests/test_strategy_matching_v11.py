import unittest
import pandas as pd
from research.strategy_matching import BreakoutRetestMatcher


def base_bars(n=50, close=10.0, volume=100.0):
    idx = [f"2026-01-{i+1:02d}" for i in range(n)]
    return pd.DataFrame({
        "high": [close] * n,
        "low": [close * 0.99] * n,
        "close": [close] * n,
        "volume": [volume] * n,
    }, index=idx)


class StrategyMatchingAuditTests(unittest.TestCase):
    def test_breakout_evidence_contains_dates(self):
        df = base_bars()
        df.loc[df.index[-3], ["high", "low", "close", "volume"]] = [10.3, 10.0, 10.05, 120.0]
        df.loc[df.index[-2], ["high", "low", "close", "volume"]] = [10.12, 10.04, 10.08, 100.0]
        df.loc[df.index[-1], ["high", "low", "close", "volume"]] = [10.06, 9.99, 10.03, 100.0]
        r = BreakoutRetestMatcher().match("A.SH", df)
        self.assertIn("breakout_date", r["evidence"])
        self.assertIn("latest_date", r["evidence"])
        self.assertEqual(r["evidence"]["breakout_date"], df.index[-3])
        self.assertEqual(r["evidence"]["latest_date"], df.index[-1])

    def test_failed_breakout_still_carries_dates(self):
        df = base_bars()
        df.loc[df.index[-3], ["high", "low", "close", "volume"]] = [10.3, 10.0, 10.05, 120.0]
        df.loc[df.index[-2], ["high", "low", "close", "volume"]] = [10.0, 9.7, 9.80, 100.0]
        df.loc[df.index[-1], ["high", "low", "close", "volume"]] = [9.9, 9.8, 9.85, 100.0]
        r = BreakoutRetestMatcher().match("A.SH", df)
        self.assertEqual(r["state"], "BREAKOUT_FAILED")
        self.assertEqual(r["evidence"]["breakout_date"], df.index[-3])
        self.assertEqual(r["evidence"]["latest_date"], df.index[-1])


if __name__ == "__main__":
    unittest.main()
