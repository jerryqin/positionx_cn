import unittest
import pandas as pd

from research.watchlist import WatchlistConfig, WatchlistEngine


def bars(start=10.0, step=0.1, n=80, volume=10_000_000):
    close = [start + step * i for i in range(n)]
    return pd.DataFrame({
        "close": close,
        "volume": [volume] * n,
        "amount": [close[i] * volume for i in range(n)],
    })


class WatchlistV12Tests(unittest.TestCase):
    def engine(self, top_n=20, min_amount=1):
        return WatchlistEngine(
            WatchlistConfig(
                top_n=top_n,
                min_avg_amount20=min_amount,
                min_ret20=0.0,
                near_high20_ratio=0.90,
                require_trend=True,
            )
        )

    def test_strict_watchlist_behavior_is_preserved(self):
        falling = bars(start=30.0, step=-0.1)
        out = self.engine().build(
            {"A.SH": falling},
            holdings={"A.SH"},
            ranking_universe={"A.SH"},
        )
        self.assertFalse(out[0]["eligible"])
        self.assertEqual(out[0]["status"], "HOLDING")

    def test_broad_candidate_gate_keeps_liquid_trend_fail(self):
        falling = bars(start=30.0, step=-0.1)
        out = self.engine().build(
            {"A.SH": falling},
            ranking_universe={"A.SH"},
            mode="candidate_pool",
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["status"], "CANDIDATE")
        self.assertTrue(out[0]["candidate_eligible"])
        self.assertFalse(out[0]["eligible"])
        self.assertIn("TREND_FAIL", out[0]["failed_reasons"])

    def test_low_liquidity_nonholding_is_not_candidate(self):
        low = bars(start=10.0, step=0.1, volume=10)
        out = self.engine(min_amount=50_000_000).build(
            {"A.SH": low},
            ranking_universe={"A.SH"},
            mode="candidate_pool",
        )
        self.assertEqual(out, [])

    def test_forced_holding_survives_candidate_pool_even_without_history(self):
        out = self.engine().build(
            {},
            holdings={"A.SH"},
            ranking_universe=set(),
            mode="candidate_pool",
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["status"], "HOLDING")
        self.assertFalse(out[0]["candidate_eligible"])
        self.assertIn("NO_HISTORY", out[0]["failed_reasons"])

    def test_invalid_mode_fails_fast(self):
        with self.assertRaises(ValueError):
            self.engine().build({}, mode="bad")


if __name__ == "__main__":
    unittest.main()
