from __future__ import annotations

import ast
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.providers import (
    ApprovalContext,
    BarsRequest,
    CancelRequest,
    OrderRequest,
    ProviderApprovalError,
    build_legacy_xtquant_bundle,
)
from core.providers.broker import BrokerProvider
from core.providers.xtquant_execution import XtQuantExecutionProvider
from core.providers.xtquant_market_data import XtQuantMarketDataProvider


class FakeMarket:
    def history(self, symbol, bars=260, refresh=False):
        return ("history", symbol, bars, refresh)
    def full_tick(self, symbol):
        return {"symbol": symbol, "lastPrice": 12.3}
    def instrument_detail(self, symbol):
        return {"symbol": symbol, "PriceTick": 0.01}


class FakeXtData:
    def get_market_data_ex(self, **kwargs):
        return {"kwargs": kwargs}
    def get_trading_dates(self, market, start, end):
        return [start, end]
    def get_stock_list_in_sector(self, universe):
        return [universe + "_A"]
    def get_index_weight(self, index):
        return {index + "_A": 1.0}


class FakeBroker:
    connected = True
    def __init__(self):
        self.submits = []
        self.cancels = []
    def query_account(self):
        return {"cash": 1}
    def query_positions(self):
        return [{"symbol": "000001.SZ"}]
    def query_orders(self):
        return [{"order_id": 1}]
    def query_trades(self):
        return [{"trade_id": 2}]
    def submit_limit(self, symbol, side, volume, price, remark):
        self.submits.append((symbol, side, volume, price, remark))
        return 101
    def cancel(self, order_id):
        self.cancels.append(order_id)
        return 0


class ProviderContractTests(unittest.TestCase):
    def test_neutral_contract_modules_do_not_directly_import_external_xtquant(self):
        for rel in [
            "core/providers/models.py",
            "core/providers/market_data.py",
            "core/providers/broker.py",
            "core/providers/execution.py",
            "core/providers/factory.py",
        ]:
            text = (ROOT/rel).read_text(encoding="utf-8")
            tree = ast.parse(text)
            direct_external = []
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    direct_external.extend(
                        a.name for a in n.names if a.name.startswith("xtquant")
                    )
                elif isinstance(n, ast.ImportFrom):
                    # Only absolute imports count as direct external xtquant imports.
                    # Relative imports such as `.xtquant_broker` are local adapter modules.
                    if n.level == 0 and (n.module or "").startswith("xtquant"):
                        direct_external.append(n.module or "")
            self.assertEqual(direct_external, [], rel)

    def test_broker_contract_is_read_only(self):
        names = set(BrokerProvider.__dict__)
        self.assertNotIn("submit_order", names)
        self.assertNotIn("cancel_order", names)

    def test_market_adapter_delegates_legacy_service(self):
        p = XtQuantMarketDataProvider(FakeMarket(), FakeXtData())
        self.assertEqual(
            p.get_bars(BarsRequest("000001.SZ", bars=20, refresh=True)),
            ("history", "000001.SZ", 20, True),
        )
        self.assertEqual(p.get_snapshot(["000001.SZ"])["000001.SZ"]["lastPrice"], 12.3)
        self.assertEqual(p.get_instrument("000001.SZ")["PriceTick"], 0.01)

    def test_execution_is_fail_closed_without_approval(self):
        b = FakeBroker()
        p = XtQuantExecutionProvider(b)
        req = OrderRequest("000001.SZ", "BUY", 100, 10.0, "test")
        with self.assertRaises(ProviderApprovalError):
            p.submit_order(req, ApprovalContext(False, ""))
        self.assertEqual(b.submits, [])

    def test_execution_delegates_only_after_explicit_approval(self):
        b = FakeBroker()
        p = XtQuantExecutionProvider(b)
        req = OrderRequest("000001.SZ", "BUY", 100, 10.0, "test")
        result = p.submit_order(req, ApprovalContext(True, "APPROVED-TEST", actor="unit"))
        self.assertEqual(result, 101)
        self.assertEqual(len(b.submits), 1)
        c = p.cancel_order(CancelRequest(101), ApprovalContext(True, "APPROVED-CANCEL"))
        self.assertEqual(c, 0)
        self.assertEqual(b.cancels, [101])

    def test_factory_wraps_without_connecting(self):
        b = FakeBroker()
        bundle = build_legacy_xtquant_bundle(
            market_service=FakeMarket(),
            broker=b,
            xtdata_backend=FakeXtData(),
        )
        self.assertEqual(bundle.broker.get_account_snapshot()["cash"], 1)
        self.assertEqual(bundle.broker.get_positions()[0]["symbol"], "000001.SZ")
        self.assertFalse(bundle.execution.preview_order(
            OrderRequest("000001.SZ", "BUY", 100, 10.0)
        )["submitted"])


if __name__ == "__main__":
    unittest.main()
