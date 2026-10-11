import unittest
import pandas as pd

from research.strategy_matching import MA60SupportMatcher
from research.watchlist_adapter import AdapterConfig, XtDataWatchlistAdapter


def confirmed_ma60_bars():
    close = [80.0] * 170 + [100.0] * 55 + [99.0, 98.0, 97.0, 96.0, 100.2]
    n = len(close)
    volume = [100.0] * n
    volume[-1] = 200.0
    df = pd.DataFrame({
        "open": close,
        "high": [c + 1.0 for c in close],
        "low": [c - 1.0 for c in close],
        "close": close,
        "volume": volume,
    })
    ma60 = df["close"].rolling(60).mean().iloc[-1]
    df.loc[df.index[-1], "low"] = ma60 - 0.5
    return df


class MA60SupportTests(unittest.TestCase):
    def test_confirmed_setup_triggers(self):
        r = MA60SupportMatcher().match("A.SH", confirmed_ma60_bars())
        self.assertTrue(r["matched"])
        self.assertTrue(r["triggered"])
        self.assertEqual(r["state"], "SUPPORT_CONFIRMED")
        self.assertTrue(r["evidence"]["trend_ok"])
        self.assertTrue(r["evidence"]["support_zone"])
        self.assertTrue(r["evidence"]["stop_confirmed"])
        self.assertGreaterEqual(r["evidence"]["filter_count"], 2)

    def test_support_zone_can_be_watched_without_trigger(self):
        df = confirmed_ma60_bars()
        ma60 = df["close"].rolling(60).mean().iloc[-1]
        df.loc[df.index[-1], "low"] = ma60 + 0.2
        df.loc[df.index[-1], "volume"] = 100.0
        r = MA60SupportMatcher().match("A.SH", df)
        self.assertTrue(r["matched"])
        self.assertFalse(r["triggered"])
        self.assertEqual(r["state"], "WATCH_SUPPORT")

    def test_big_trend_failure_is_not_matched(self):
        close = [100.0] * 220 + [70.0] * 10
        df = pd.DataFrame({
            "low": [c - 1 for c in close],
            "close": close,
            "volume": [100.0] * len(close),
        })
        r = MA60SupportMatcher().match("A.SH", df)
        self.assertFalse(r["matched"])
        self.assertEqual(r["reason"], "BIG_TREND_FAIL")

    def test_requires_ma200_history(self):
        close = [100.0] * 100
        df = pd.DataFrame({
            "low": [99.0] * 100,
            "close": close,
            "volume": [100.0] * 100,
        })
        r = MA60SupportMatcher().match("A.SH", df)
        self.assertFalse(r["matched"])
        self.assertIn("INSUFFICIENT_DATA", r["reason"])

    def test_adapter_can_request_wider_refresh_window(self):
        calls = []
        class X:
            def download_history_data2(self, symbols, period, start, end, callback, incrementally):
                calls.append((list(symbols), period, start, end))
        a = XtDataWatchlistAdapter(X(), AdapterConfig())
        a.refresh_history(["A.SH"], calendar_days=550)
        self.assertEqual(calls[0][1], "1d")
        self.assertTrue(calls[0][2].isdigit())
        self.assertEqual(len(calls[0][2]), 8)


if __name__ == "__main__":
    unittest.main()
