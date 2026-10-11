import unittest
from pathlib import Path
import tempfile

import pandas as pd

from research.watchlist_adapter import AdapterConfig, XtDataWatchlistAdapter, connected_xtdata, save_watchlist_snapshot


class FakeXtData:
    def __init__(self):
        self.sector_downloads = 0
        self.history_downloads = []

    def download_sector_data(self):
        self.sector_downloads += 1

    def get_stock_list_in_sector(self, name):
        return ["600001.SH", "000001.SZ", "BAD"]

    def download_history_data2(self, symbols, period, start, end, callback, incrementally):
        self.history_downloads.append((list(symbols), period))

    def get_market_data(self, **kwargs):
        symbols = kwargs["stock_list"]
        times = [20260101, 20260102, 20260103]
        out = {}
        for field, base in [
            ("open", 10.0), ("high", 11.0), ("low", 9.0),
            ("close", 10.5), ("volume", 1000.0), ("amount", 10000.0),
        ]:
            out[field] = pd.DataFrame(
                [[base, base + 1, base + 2] for _ in symbols],
                index=symbols,
                columns=times,
            )
        return out


class WatchlistAdapterTests(unittest.TestCase):
    def test_sector_refresh_and_filter(self):
        xt = FakeXtData()
        a = XtDataWatchlistAdapter(xt, AdapterConfig(sector="沪深300"))
        rows = a.universe(refresh_sector=True)
        self.assertEqual(rows, ["000001.SZ", "600001.SH"])
        self.assertEqual(xt.sector_downloads, 1)

    def test_holdings_are_merged_even_outside_sector(self):
        a = XtDataWatchlistAdapter(FakeXtData())
        rows = a.merge_holdings(["600001.SH"], ["601689.SH"])
        self.assertEqual(rows, ["600001.SH", "601689.SH"])

    def test_batch_history_parser(self):
        xt = FakeXtData()
        a = XtDataWatchlistAdapter(xt, AdapterConfig(bars=3))
        h = a.history(["600001.SH", "000001.SZ"])
        self.assertEqual(set(h), {"600001.SH", "000001.SZ"})
        self.assertEqual(list(h["600001.SH"].columns), ["open","high","low","close","volume","amount"])
        self.assertEqual(len(h["600001.SH"]), 3)

    def test_refresh_history_uses_batch_api(self):
        xt = FakeXtData()
        a = XtDataWatchlistAdapter(xt)
        a.refresh_history(["600001.SH", "000001.SZ"])
        self.assertEqual(len(xt.history_downloads), 1)
        self.assertEqual(xt.history_downloads[0][1], "1d")

    def test_connected_xtdata_initializes_lazy_market(self):
        xt = object()

        class LazyMarket:
            def __init__(self):
                self._xtdata = None
                self.calls = 0

            def connect(self):
                self.calls += 1
                self._xtdata = xt

        market = LazyMarket()
        self.assertIs(connected_xtdata(market), xt)
        self.assertEqual(market.calls, 1)

    def test_snapshot_write_is_atomic_shape(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.json"
            save_watchlist_snapshot(p, {"watchlist": [{"symbol": "A.SH"}]})
            self.assertTrue(p.exists())
            self.assertIn("A.SH", p.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
