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


class WatchlistV11Tests(unittest.TestCase):
    def engine(self, top_n=20):
        return WatchlistEngine(
            WatchlistConfig(
                top_n=top_n,
                min_avg_amount20=1,
                min_ret20=0.0,
                near_high20_ratio=0.90,
            )
        )

    def test_holding_does_not_consume_auto_top_n_slot(self):
        hist = {
            "A.SH": bars(step=0.30),
            "B.SH": bars(step=0.20),
            "C.SH": bars(step=0.10),
        }
        out = self.engine(top_n=1).build(
            hist,
            holdings={"A.SH"},
            ranking_universe={"A.SH", "B.SH", "C.SH"},
        )
        self.assertEqual(len([r for r in out if r["status"] == "HOLDING"]), 1)
        self.assertEqual(len([r for r in out if r["status"] == "SELECTED"]), 1)

    def test_outside_holding_does_not_change_rs_universe(self):
        hist = {
            "A.SH": bars(step=0.10),
            "B.SH": bars(step=0.20),
            "X.SH": bars(step=1.00),
        }
        out = self.engine(top_n=2).build(
            hist,
            holdings={"X.SH"},
            ranking_universe={"A.SH", "B.SH"},
        )
        rows = {r["symbol"]: r for r in out}
        a_rs = [x for x in rows["A.SH"]["reasons"] if x.startswith("RS20_PERCENTILE")][0]
        b_rs = [x for x in rows["B.SH"]["reasons"] if x.startswith("RS20_PERCENTILE")][0]
        x_rs = [x for x in rows["X.SH"]["reasons"] if x.startswith("RS20_PERCENTILE")][0]
        self.assertEqual(a_rs, "RS20_PERCENTILE: 50.0%")
        self.assertEqual(b_rs, "RS20_PERCENTILE: 100.0%")
        self.assertIn("OUTSIDE_RANKING_UNIVERSE", x_rs)

    def test_insufficient_data_is_not_scored(self):
        out = self.engine().build(
            {"A.SH": bars(n=10)},
            holdings={"A.SH"},
            ranking_universe={"A.SH"},
        )
        self.assertIsNone(out[0]["score"])
        self.assertIn("INSUFFICIENT_DATA", out[0]["failed_reasons"])

    def test_trend_failure_is_explained(self):
        out = self.engine().build(
            {"A.SH": bars(start=30.0, step=-0.10)},
            holdings={"A.SH"},
            ranking_universe={"A.SH"},
        )
        self.assertIn("TREND_FAIL", out[0]["failed_reasons"])
        trend_detail = [x for x in out[0]["failed_reasons"] if x.startswith("TREND_DETAIL")][0]
        self.assertIn("close>MA20=", trend_detail)
        self.assertIn("MA20>MA60=", trend_detail)
        self.assertIn("close=", trend_detail)
        self.assertIn("MA20=", trend_detail)
        self.assertIn("MA60=", trend_detail)

    def test_close_high_reason_is_named_accurately(self):
        out = self.engine().build(
            {"A.SH": bars(step=0.10)},
            ranking_universe={"A.SH"},
        )
        reasons = out[0]["reasons"]
        self.assertTrue(any(x.startswith("NEAR_20D_CLOSE_HIGH") for x in reasons))
        self.assertFalse(any(x.startswith("NEAR_20D_HIGH:") for x in reasons))


if __name__ == "__main__":
    unittest.main()
