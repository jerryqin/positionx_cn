import unittest
import pandas as pd
from research.watchlist import WatchlistConfig, WatchlistEngine


def bars(start=10.0, step=0.1, n=80, volume=10_000_000):
    close = [start + step * i for i in range(n)]
    return pd.DataFrame({
        "open": close,
        "high": [x * 1.01 for x in close],
        "low": [x * 0.99 for x in close],
        "close": close,
        "volume": [volume] * n,
        "amount": [close[i] * volume for i in range(n)],
    })


class WatchlistEngineTests(unittest.TestCase):
    def test_strong_trend_is_selected(self):
        e = WatchlistEngine(WatchlistConfig(top_n=10, min_avg_amount20=1))
        out = e.build({"AAA.SH": bars()})
        self.assertEqual(out[0]["status"], "SELECTED")
        self.assertTrue(out[0]["eligible"])

    def test_holding_is_never_dropped_when_screen_fails(self):
        e = WatchlistEngine(WatchlistConfig(top_n=10, min_avg_amount20=1))
        out = e.build({"BBB.SH": bars(start=30.0, step=-0.1)}, holdings={"BBB.SH"})
        self.assertEqual(out[0]["status"], "HOLDING")
        self.assertTrue(out[0]["selected"])
        self.assertFalse(out[0]["eligible"])
        self.assertIn("TREND_FAIL", out[0]["failed_reasons"])

    def test_holding_with_no_history_is_still_visible(self):
        e = WatchlistEngine(WatchlistConfig(top_n=0))
        out = e.build({}, holdings={"CCC.SH"})
        self.assertEqual(out[0]["status"], "HOLDING")
        self.assertIn("NO_HISTORY", out[0]["failed_reasons"])

    def test_top_n_limits_auto_candidates_but_not_holdings(self):
        hist = {
            "A.SH": bars(start=10.0, step=0.10),
            "B.SH": bars(start=10.0, step=0.20),
            "C.SH": bars(start=10.0, step=0.30),
        }
        e = WatchlistEngine(WatchlistConfig(top_n=1, min_avg_amount20=1))
        out = e.build(hist, holdings={"A.SH"})
        self.assertIn("A.SH", {r["symbol"] for r in out})
        self.assertEqual(len([r for r in out if r["status"] == "SELECTED"]), 1)

    def test_amount_fallback_is_explicit(self):
        e = WatchlistEngine(WatchlistConfig(top_n=10, min_avg_amount20=1))
        out = e.build({"AAA.SH": bars().drop(columns=["amount"])})
        self.assertEqual(out[0]["metrics"]["amount_source"], "close_x_volume_proxy")


if __name__ == "__main__":
    unittest.main()
