import tempfile
import unittest
from pathlib import Path

from core.db import Database
from core.execution import ExecutionEngine
from core.models import AccountSnapshot, PositionSnapshot, TradingPlanItem
from core.risk import RiskGate


class FakeMarket:
    def ensure_tradable(self, symbol): pass
    def full_tick(self, symbol): return {"lastPrice": 10.0, "askPrice": [10.01], "bidPrice": [9.99], "stockStatus": 3, "openInt": 13}
    def is_tick_fresh(self, tick, sec): return True
    def tick_price(self, tick, side): return 10.01 if side == "BUY" else 9.99
    def price_limits(self, symbol): return (8.0, 12.0)
    def price_tick(self, symbol, default=0.01): return 0.01
    def min_limit_order_volume(self, symbol, default=100): return 100


class FakeBroker:
    def __init__(self):
        self.connected = True
        self.submitted = []
        self.account = AccountSnapshot("", 100000, 100000, 0, 0)
        self.positions = []
    def active_external_orders(self): return []
    def query_account(self): return self.account
    def query_positions(self): return list(self.positions)
    def submit_limit(self, symbol, side, volume, price, remark):
        self.submitted.append((symbol, side, volume, price, remark))
        return 1000 + len(self.submitted)


class ExecutionTests(unittest.TestCase):
    def make(self, live=True, pilot=False, pilot_value=50000, pilot_orders=1):
        td = tempfile.TemporaryDirectory()
        db = Database(str(Path(td.name) / "x.db"))
        db.upsert_plan(TradingPlanItem(
            "2026-01-01", "2026-01-02", "A.SH", 0.20, "test", "S",
            {"signal_date":"2026-01-01","close":10.0,"ma20":9.8,"ma60":9.5}
        ))
        risk_cfg = {
            "max_total_weight": 0.80, "max_symbol_weight": 0.25, "min_cash_reserve": 0.10,
            "max_order_pct_of_asset": 0.25, "min_order_value": 3000, "rebalance_threshold_pct": 0.01,
            "buy_lot": 100, "allow_buy": True, "allow_sell": True, "stop_on_external_active_order": True,
        }
        broker = FakeBroker(); market = FakeMarket(); risk = RiskGate(db, risk_cfg)
        eng = ExecutionEngine(
            db, broker, market, risk, risk_cfg,
            {
                "live_enabled": live, "limit_offset_bps": 0,
                "pilot_mode": pilot, "pilot_max_order_value": pilot_value,
                "pilot_max_orders_per_day": pilot_orders,
            },
            {"stale_tick_seconds": 30, "default_price_tick": 0.01}
        )
        return td, db, broker, eng

    def test_dry_run_does_not_poison_idempotency(self):
        td, db, broker, eng = self.make(live=True)
        try:
            first = eng.execute_plan("2026-01-02", dry_run=True)
            self.assertEqual(first[0]["status"], "ORDER_PREVIEW")
            self.assertEqual(first[0]["signal_date"], "2026-01-01")
            self.assertEqual(first[0]["side"], "BUY")
            self.assertGreater(first[0]["volume"], 0)
            self.assertGreater(first[0]["notional"], 0)
            self.assertAlmostEqual(first[0]["ma20"], 9.8)
            self.assertAlmostEqual(first[0]["ma60"], 9.5)
            self.assertEqual(len(broker.submitted), 0)
            second = eng.execute_plan("2026-01-02", dry_run=False)
            self.assertEqual(second[0]["status"], "SUBMITTED")
            self.assertEqual(len(broker.submitted), 1)
        finally:
            td.cleanup()


    def test_dry_run_returns_no_action_for_reached_target(self):
        td, db, broker, eng = self.make(live=True)
        try:
            with db.connection() as conn:
                conn.execute("UPDATE trading_plan SET target_weight=0.0 WHERE trade_date='2026-01-02'")
            out = eng.execute_plan("2026-01-02", dry_run=True)
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0]["status"], "NO_ACTION")
            self.assertEqual(out[0]["symbol"], "A.SH")
            self.assertAlmostEqual(out[0]["target_weight"], 0.0)
            self.assertAlmostEqual(out[0]["current_weight"], 0.0)
            self.assertIn("目标仓位已达到", out[0]["reason"])
            self.assertEqual(len(broker.submitted), 0)
        finally:
            td.cleanup()

    def test_dry_run_returns_no_plan_explicitly(self):
        td, db, broker, eng = self.make(live=True)
        try:
            out = eng.execute_plan("2026-01-03", dry_run=True)
            self.assertEqual(out[0]["status"], "NO_PLAN")
            self.assertIn("没有可执行交易计划", out[0]["reason"])
        finally:
            td.cleanup()

    def test_dry_run_returns_blocked_when_risk_rejects(self):
        td, db, broker, eng = self.make(live=True)
        try:
            eng.risk.cfg["allow_buy"] = False
            out = eng.execute_plan("2026-01-02", dry_run=True)
            self.assertEqual(out[0]["status"], "BLOCKED")
            self.assertEqual(out[0]["symbol"], "A.SH")
            self.assertIn("禁止买入", out[0]["reason"])
            self.assertEqual(len(broker.submitted), 0)
        finally:
            td.cleanup()


    def test_external_order_block_distinguishes_planned_and_effective_target(self):
        td, db, broker, eng = self.make(live=False)
        try:
            broker.active_external_orders = lambda: [{"symbol":"X.SH"}]
            out = eng.execute_plan("2026-01-02", dry_run=True)
            self.assertEqual(out[0]["status"], "BLOCKED")
            self.assertAlmostEqual(out[0]["planned_target_weight"], 0.20)
            self.assertIsNone(out[0]["effective_target_weight"])
            self.assertIsNone(out[0]["target_weight"])
            self.assertEqual(out[0]["block_type"], "EXTERNAL_ACTIVE_ORDER")
            self.assertEqual(len(broker.submitted), 0)
        finally:
            td.cleanup()

    def test_sell_zero_target_requests_exact_position_volume(self):
        td, db, broker, eng = self.make(live=False)
        try:
            with db.connection() as conn:
                conn.execute("UPDATE trading_plan SET target_weight=0.0 WHERE trade_date='2026-01-02'")
            # 市值故意与委托限价不完全一致；旧实现会反推出 >600 股。
            broker.positions = [PositionSnapshot("", "A.SH", 600, 600, 10, 6100)]
            rows = db.plan_for_date("2026-01-02")
            intents = eng.build_intents("2026-01-02", rows, broker.account, broker.positions)
            self.assertEqual(len(intents), 1)
            self.assertEqual(intents[0].side.value, "SELL")
            self.assertEqual(intents[0].volume, 600)
        finally:
            td.cleanup()

    def test_no_sellable_audit_shows_requested_and_allowed_volume(self):
        td, db, broker, eng = self.make(live=False)
        try:
            with db.connection() as conn:
                conn.execute("UPDATE trading_plan SET target_weight=0.0 WHERE trade_date='2026-01-02'")
            broker.positions = [PositionSnapshot("", "A.SH", 600, 0, 10, 6100)]
            out = eng.execute_plan("2026-01-02", dry_run=True)
            self.assertEqual(out[0]["status"], "BLOCKED")
            self.assertEqual(out[0]["requested_volume"], 600)
            self.assertEqual(out[0]["allowed_volume"], 0)
            self.assertEqual(out[0]["volume"], 0)
            self.assertIn("无可卖数量", out[0]["reason"])
        finally:
            td.cleanup()

    def test_execute_path_live_disabled_returns_preview_and_never_submits(self):
        td, db, broker, eng = self.make(live=False)
        try:
            out = eng.execute_plan("2026-01-02", dry_run=False)
            self.assertEqual(out[0]["status"], "ORDER_PREVIEW")
            self.assertGreater(out[0]["requested_volume"], 0)
            self.assertGreater(out[0]["allowed_volume"], 0)
            self.assertEqual(out[0]["volume"], out[0]["allowed_volume"])
            self.assertGreater(out[0]["notional"], 0)
            self.assertIn("live_enabled=false", out[0]["reason"])
            self.assertEqual(len(broker.submitted), 0)
            self.assertFalse(db.has_intent(out[0]["intent"]))
        finally:
            td.cleanup()


    def test_pilot_caps_buy_notional_and_lot(self):
        td, db, broker, eng = self.make(live=False, pilot=True, pilot_value=10000, pilot_orders=1)
        try:
            out = eng.execute_plan("2026-01-02", dry_run=True)
            self.assertEqual(out[0]["status"], "ORDER_PREVIEW")
            self.assertTrue(out[0]["pilot_mode"])
            self.assertTrue(out[0]["pilot_limited"])
            self.assertEqual(out[0]["risk_allowed_volume"], 1900)
            self.assertEqual(out[0]["allowed_volume"], 900)
            self.assertLessEqual(out[0]["notional"], 10000)
            self.assertEqual(out[0]["allowed_volume"] % 100, 0)
            self.assertEqual(len(broker.submitted), 0)
        finally:
            td.cleanup()

    def test_pilot_daily_quota_blocks_when_used(self):
        td, db, broker, eng = self.make(live=False, pilot=True, pilot_value=50000, pilot_orders=1)
        try:
            from core.models import OrderIntent, Side
            db.save_intent(OrderIntent("used", "2026-01-02", "X.SH", Side.BUY, 100, 10, "used", 0.1, 0.0))
            out = eng.execute_plan("2026-01-02", dry_run=True)
            self.assertEqual(out[0]["status"], "BLOCKED")
            self.assertIn("Pilot每日委托上限", out[0]["reason"])
            self.assertEqual(out[0]["pilot_orders_used"], 1)
            self.assertEqual(out[0]["allowed_volume"], 0)
            self.assertEqual(len(broker.submitted), 0)
        finally:
            td.cleanup()

    def test_pilot_live_submission_is_capped_and_counted(self):
        td, db, broker, eng = self.make(live=True, pilot=True, pilot_value=10000, pilot_orders=1)
        try:
            out = eng.execute_plan("2026-01-02", dry_run=False)
            self.assertEqual(out[0]["status"], "SUBMITTED")
            self.assertEqual(out[0]["volume"], 900)
            self.assertLessEqual(out[0]["notional"], 10000)
            self.assertEqual(len(broker.submitted), 1)
            self.assertEqual(db.count_order_intents_for_date("2026-01-02"), 1)
        finally:
            td.cleanup()

    def test_real_execution_is_idempotent(self):
        td, db, broker, eng = self.make(live=True)
        try:
            one = eng.execute_plan("2026-01-02", dry_run=False)
            two = eng.execute_plan("2026-01-02", dry_run=False)
            self.assertEqual(one[0]["status"], "SUBMITTED")
            self.assertEqual(two[0]["status"], "SKIP_DUPLICATE")
            self.assertEqual(len(broker.submitted), 1)
        finally:
            td.cleanup()


    def test_rebalance_threshold_boundary_is_no_action(self):
        td, db, broker, eng = self.make(live=True)
        try:
            # Target 20%, current 19% => exactly 1% difference. Reconcile uses
            # <= threshold, so preview/execution must use the same boundary.
            broker.positions = [PositionSnapshot("", "A.SH", 1900, 1900, 10, 19000)]
            out = eng.execute_plan("2026-01-02", dry_run=True)
            self.assertEqual(out[0]["status"], "NO_ACTION")
            self.assertAlmostEqual(abs(out[0]["difference"]), 0.01, places=9)
        finally:
            td.cleanup()

    def test_external_holdings_consume_total_weight_cap(self):
        td, db, broker, eng = self.make(live=False)
        try:
            # 外部持仓已经占50%，max_total=80%，策略最多还能占30%。计划目标20%，无需缩放。
            broker.positions = [PositionSnapshot("", "EXT.SH", 5000, 5000, 10, 50000)]
            rows = db.plan_for_date("2026-01-02")
            intents = eng.build_intents("2026-01-02", rows, broker.account, broker.positions)
            self.assertTrue(intents)
            self.assertLessEqual(intents[0].target_weight, 0.30 + 1e-9)
        finally:
            td.cleanup()


    def test_target_shares_sell_is_exact_and_price_independent(self):
        td, db, broker, eng = self.make(live=False, pilot=False)
        try:
            broker.positions = [PositionSnapshot("", "A.SH", 3000, 3000, 10, 31000)]
            out = eng.execute_quantity_request(
                "2026-01-02", "A.SH", target_shares=2100, dry_run=True
            )
            self.assertEqual(out[0]["status"], "ORDER_PREVIEW")
            self.assertEqual(out[0]["quantity_mode"], "TARGET_SHARES")
            self.assertEqual(out[0]["current_shares"], 3000)
            self.assertEqual(out[0]["target_shares"], 2100)
            self.assertEqual(out[0]["side"], "SELL")
            self.assertEqual(out[0]["requested_volume"], 900)
            self.assertEqual(out[0]["risk_allowed_volume"], 900)
            self.assertEqual(out[0]["allowed_volume"], 900)
            self.assertEqual(out[0]["volume"], 900)
            self.assertEqual(len(broker.submitted), 0)
        finally:
            td.cleanup()

    def test_target_shares_no_action_when_already_at_target(self):
        td, db, broker, eng = self.make(live=False, pilot=False)
        try:
            broker.positions = [PositionSnapshot("", "A.SH", 2100, 2100, 10, 21000)]
            out = eng.execute_quantity_request(
                "2026-01-02", "A.SH", target_shares=2100, dry_run=True
            )
            self.assertEqual(out[0]["status"], "NO_ACTION")
            self.assertEqual(out[0]["requested_volume"], 0)
        finally:
            td.cleanup()

    def test_exact_volume_sell_900_is_exact(self):
        td, db, broker, eng = self.make(live=False, pilot=False)
        try:
            broker.positions = [PositionSnapshot("", "A.SH", 3000, 3000, 10, 30000)]
            out = eng.execute_quantity_request(
                "2026-01-02", "A.SH", side="SELL", volume=900, dry_run=True
            )
            self.assertEqual(out[0]["status"], "ORDER_PREVIEW")
            self.assertEqual(out[0]["quantity_mode"], "EXACT_VOLUME")
            self.assertEqual(out[0]["requested_volume"], 900)
            self.assertEqual(out[0]["volume"], 900)
            self.assertEqual(out[0]["target_shares"], 2100)
        finally:
            td.cleanup()

    def test_exact_volume_blocks_instead_of_risk_resizing(self):
        td, db, broker, eng = self.make(live=False, pilot=False)
        try:
            broker.positions = [PositionSnapshot("", "A.SH", 3000, 800, 10, 30000)]
            out = eng.execute_quantity_request(
                "2026-01-02", "A.SH", side="SELL", volume=900, dry_run=True
            )
            self.assertEqual(out[0]["status"], "BLOCKED_EXACT_VOLUME")
            self.assertEqual(out[0]["block_type"], "RISK_WOULD_RESIZE")
            self.assertEqual(out[0]["requested_volume"], 900)
            self.assertEqual(out[0]["risk_allowed_volume"], 800)
            self.assertEqual(out[0]["volume"], 0)
            self.assertEqual(len(broker.submitted), 0)
        finally:
            td.cleanup()

    def test_target_shares_blocks_instead_of_pilot_resizing(self):
        td, db, broker, eng = self.make(
            live=False, pilot=True, pilot_value=5000, pilot_orders=1
        )
        try:
            broker.positions = [PositionSnapshot("", "A.SH", 3000, 3000, 10, 30000)]
            out = eng.execute_quantity_request(
                "2026-01-02", "A.SH", target_shares=2100, dry_run=True
            )
            self.assertEqual(out[0]["status"], "BLOCKED_EXACT_VOLUME")
            self.assertEqual(out[0]["block_type"], "PILOT_WOULD_RESIZE")
            self.assertEqual(out[0]["requested_volume"], 900)
            self.assertLess(out[0]["pilot_allowed_volume"], 900)
            self.assertEqual(out[0]["volume"], 0)
            self.assertEqual(len(broker.submitted), 0)
        finally:
            td.cleanup()

    def test_target_shares_live_submission_is_exact(self):
        td, db, broker, eng = self.make(
            live=True, pilot=True, pilot_value=50000, pilot_orders=1
        )
        try:
            broker.positions = [PositionSnapshot("", "A.SH", 3000, 3000, 10, 30000)]
            out = eng.execute_quantity_request(
                "2026-01-02", "A.SH", target_shares=2100, dry_run=False
            )
            self.assertEqual(out[0]["status"], "SUBMITTED")
            self.assertEqual(out[0]["volume"], 900)
            self.assertEqual(broker.submitted[0][2], 900)
            self.assertEqual(db.count_order_intents_for_date("2026-01-02"), 1)
        finally:
            td.cleanup()


if __name__ == "__main__": unittest.main()
