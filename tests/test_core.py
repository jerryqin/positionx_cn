import tempfile
import unittest
import sqlite3
from pathlib import Path

from core.db import Database
from core.models import AccountSnapshot, PositionSnapshot, OrderIntent, Side
from core.risk import RiskGate
from core.data import MarketDataService
from strategies.ma_trend import MATrendTargetStrategy
import pandas as pd


class CoreTests(unittest.TestCase):
    def test_strategy_targets(self):
        prices = list(range(1, 81))
        idx = pd.date_range("2026-06-01", periods=80, freq="D")
        df = pd.DataFrame({"close": prices, "open":prices,"high":prices,"low":prices,"volume":[1]*80}, index=idx)
        s = MATrendTargetStrategy({"fast_ma":20,"slow_ma":60,"target_weight":0.2})
        t = s.targets({"AAA.SH":df})[0]
        self.assertAlmostEqual(t.target_weight, 0.2)
        self.assertEqual(t.audit["signal_date"], str(idx[-1])[:10])
        self.assertIn("close", t.audit)
        self.assertIn("ma20", t.audit)
        self.assertIn("ma60", t.audit)


    def test_trading_plan_audit_persisted(self):
        from core.models import TradingPlanItem
        with tempfile.TemporaryDirectory() as d:
            db = Database(str(Path(d)/"x.db"))
            audit = {"signal_date":"2026-09-22","close":407.01,"ma20":390.1,"ma60":370.2}
            db.upsert_plan(TradingPlanItem("2026-09-22","NEXT_SESSION","603986.SH",0.0,"趋势条件不满足","S",audit))
            row = db.pending_plan()[0]
            self.assertEqual(row["audit"]["signal_date"], "2026-09-22")
            self.assertAlmostEqual(row["audit"]["close"], 407.01)

    def test_existing_database_migrates_audit_column(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/"legacy.db"
            conn = sqlite3.connect(path)
            conn.execute("""CREATE TABLE trading_plan (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_date TEXT NOT NULL, trade_date TEXT NOT NULL, symbol TEXT NOT NULL,
                target_weight REAL NOT NULL, reason TEXT NOT NULL, strategy_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PLANNED', created_at TEXT NOT NULL,
                UNIQUE(trade_date, symbol, strategy_name))""")
            conn.commit(); conn.close()
            db = Database(str(path))
            with db.connection() as c:
                cols = {r[1] for r in c.execute("PRAGMA table_info(trading_plan)").fetchall()}
            self.assertIn("audit_json", cols)

    def test_target_cap(self):
        with tempfile.TemporaryDirectory() as d:
            db=Database(str(Path(d)/"x.db"))
            r=RiskGate(db,{"max_symbol_weight":0.25,"max_total_weight":0.8})
            x=r.validate_target_set({"A":0.5,"B":0.5,"C":0.5,"D":0.5})
            self.assertLessEqual(sum(x.values()),0.8+1e-9)
            self.assertTrue(all(v<=0.25 for v in x.values()))

    def test_buy_lot_cash(self):
        with tempfile.TemporaryDirectory() as d:
            db=Database(str(Path(d)/"x.db"))
            r=RiskGate(db,{"buy_lot":100,"min_cash_reserve":0.1,"max_order_pct_of_asset":0.1})
            acc=AccountSnapshot("",100000,50000,50000,0)
            intent=OrderIntent("k","2026-01-01","A.SH",Side.BUY,1234,10,"",0.2,0)
            dec=r.check_intent(intent,acc,None)
            self.assertTrue(dec.allowed)
            self.assertEqual(dec.volume%100,0)
            self.assertLessEqual(dec.volume*10,10000)

    def test_sell_available(self):
        with tempfile.TemporaryDirectory() as d:
            db=Database(str(Path(d)/"x.db"))
            r=RiskGate(db,{"max_order_pct_of_asset":1.0})
            acc=AccountSnapshot("",100000,10000,90000,0)
            pos=PositionSnapshot("","A.SH",1000,300,10,10000)
            intent=OrderIntent("k","2026-01-01","A.SH",Side.SELL,900,10,"",0,0.1)
            dec=r.check_intent(intent,acc,pos)
            self.assertEqual(dec.volume,300)

    def test_kill_switch(self):
        with tempfile.TemporaryDirectory() as d:
            db=Database(str(Path(d)/"x.db")); r=RiskGate(db,{})
            r.set_kill_switch(True)
            self.assertTrue(r.kill_switch())


class ExecutionMathTests(unittest.TestCase):
    def test_external_holdings_reduce_strategy_capacity(self):
        # 该行为由 ExecutionEngine.build_intents 集成测试覆盖较重；这里至少锁死风险目标集上限。
        with tempfile.TemporaryDirectory() as d:
            db=Database(str(Path(d)/"x.db"))
            r=RiskGate(db,{"max_symbol_weight":0.25,"max_total_weight":0.50})
            x=r.validate_target_set({"A":0.4,"B":0.4})
            self.assertLessEqual(sum(x.values()),0.50+1e-9)

class SessionInferenceTests(unittest.TestCase):
    class FakeXtdata:
        @staticmethod
        def get_full_tick(symbols):
            import time
            return {symbols[0]: {"time": int(time.time() * 1000), "timetag": time.strftime("%Y%m%d %H:%M:%S"), "lastPrice": 10.0, "stockStatus": 3, "openInt": 13}}

    def test_live_session_without_calendar_api(self):
        from datetime import datetime
        with tempfile.TemporaryDirectory() as d:
            db = Database(str(Path(d)/"x.db"))
            m = MarketDataService(db)
            m._xtdata = self.FakeXtdata()
            self.assertTrue(m.is_live_session("000001.SH", 30, datetime(2026, 9, 22, 10, 0, 0)))
            self.assertFalse(m.is_live_session("000001.SH", 30, datetime(2026, 9, 22, 12, 0, 0)))


    def test_real_snapshot_shape_openint_13_is_live(self):
        from datetime import datetime
        tick = {
            "time": int(datetime.now().timestamp() * 1000),
            "timetag": datetime.now().strftime("%Y%m%d %H:%M:%S"),
            "lastPrice": 407.01,
            "stockStatus": 3,
            "openInt": 13,
            "askPrice": [407.01],
            "bidPrice": [407.00],
        }
        class RealShapeXtdata:
            @staticmethod
            def get_full_tick(symbols):
                return {symbols[0]: tick}
        with tempfile.TemporaryDirectory() as d:
            db = Database(str(Path(d)/"x.db"))
            m = MarketDataService(db)
            m._xtdata = RealShapeXtdata()
            self.assertEqual(m.security_state(tick), 13)
            self.assertTrue(m.is_live_session("603986.SH", 30, datetime.now().replace(hour=10, minute=56, second=14, microsecond=0)))

    def test_non_continuous_openint_is_closed(self):
        from datetime import datetime
        import time
        class ClosedXtdata:
            @staticmethod
            def get_full_tick(symbols):
                return {symbols[0]: {"time": int(time.time() * 1000), "lastPrice": 10.0, "stockStatus": 3, "openInt": 14}}
        with tempfile.TemporaryDirectory() as d:
            db = Database(str(Path(d)/"x.db"))
            m = MarketDataService(db)
            m._xtdata = ClosedXtdata()
            self.assertFalse(m.is_live_session("000001.SH", 30, datetime(2026, 9, 22, 10, 0, 0)))

    def test_pending_plan_claims_first_later_session(self):
        from core.models import TradingPlanItem
        with tempfile.TemporaryDirectory() as d:
            db = Database(str(Path(d)/"x.db"))
            db.upsert_plan(TradingPlanItem("2026-09-22", "NEXT_SESSION", "A.SH", 0.2, "x", "S"))
            self.assertEqual(db.claim_pending_plan("2026-09-22"), [])  # same-day execution forbidden
            rows = db.claim_pending_plan("2026-09-23")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["trade_date"], "2026-09-23")
            self.assertEqual(db.pending_plan(), [])


class ReconcileAuditTests(unittest.TestCase):
    def test_reconcile_audit_sorts_and_classifies_sources(self):
        from core.service import _recent_order_audit, _recent_trade_audit, _trade_fill_summary
        orders = [
            {"order_id":1,"order_time":93001,"symbol":"A.SH","direction":"BUY","price":10,"volume":100,"traded_volume":100,"status":"56","strategy_name":"","remark":"manual"},
            {"order_id":2,"order_time":93102,"symbol":"B.SH","direction":"SELL","price":20,"volume":200,"traded_volume":0,"status":"50","strategy_name":"PositionX","remark":"px"},
        ]
        trades = [
            {"trade_id":"t1","order_id":1,"traded_time":93005,"symbol":"A.SH","direction":"BUY","price":10,"volume":100,"amount":1000,"strategy_name":"","remark":"manual"},
            {"trade_id":"t2","order_id":2,"traded_time":93105,"symbol":"B.SH","direction":"SELL","price":20,"volume":100,"amount":2000,"strategy_name":"PositionX","remark":"px"},
        ]
        ro = _recent_order_audit(orders, 5)
        rt = _recent_trade_audit(trades, 5)
        self.assertEqual(ro[0]["order_id"], 2)
        self.assertEqual(ro[0]["time"], "09:31:02")
        self.assertEqual(ro[0]["source"], "POSITIONX")
        self.assertEqual(ro[1]["source"], "EXTERNAL")
        self.assertEqual(rt[0]["trade_id"], "t2")
        self.assertEqual(rt[0]["time"], "09:31:05")
        self.assertEqual(rt[1]["remark"], "manual")


    def test_plan_status_audit_shows_target_actual_and_done(self):
        from core.service import _plan_status_audit
        from core.models import AccountSnapshot, PositionSnapshot
        account = AccountSnapshot("", 100000.0, 50000.0, 50000.0, 0.0)
        positions = [PositionSnapshot("", "B.SH", 1000, 1000, 10.0, 20000.0)]
        rows = [{
            "symbol": "A.SH", "strategy_name": "MA_TREND", "status": "DONE",
            "target_weight": 0.0, "reason": "趋势条件不满足",
            "audit": {"signal_date": "2026-09-22"},
        }]
        out = _plan_status_audit(rows, account, positions)
        self.assertEqual(out[0]["status"], "DONE")
        self.assertEqual(out[0]["signal_date"], "2026-09-22")
        self.assertAlmostEqual(out[0]["target_weight"], 0.0)
        self.assertAlmostEqual(out[0]["current_weight"], 0.0)
        self.assertAlmostEqual(out[0]["difference"], 0.0)

    def test_trade_audit_complete_mode_does_not_truncate_split_fills(self):
        from core.service import _recent_trade_audit, _trade_fill_summary
        trades = []
        for i, vol in enumerate([100, 200, 50, 100, 150], start=1):
            trades.append({
                "trade_id": f"t{i}", "order_id": 0, "traded_time": 102858,
                "symbol": "603259.SH", "direction": "BUY", "price": 169.7,
                "volume": vol, "amount": 169.7 * vol, "strategy_name": "", "remark": "",
            })
        for i, vol in enumerate([210, 33, 157], start=6):
            trades.append({
                "trade_id": f"t{i}", "order_id": 0, "traded_time": 103154,
                "symbol": "688235.SH", "direction": "BUY", "price": 272.4,
                "volume": vol, "amount": 272.4 * vol, "strategy_name": "", "remark": "",
            })
        full = _recent_trade_audit(trades, None)
        self.assertEqual(len(full), 8)
        limited = _recent_trade_audit(trades, 5)
        self.assertEqual(len(limited), 5)
        summary = _trade_fill_summary(trades)
        by_symbol = {x["symbol"]: x for x in summary}
        self.assertEqual(by_symbol["603259.SH"]["fill_count"], 5)
        self.assertEqual(by_symbol["603259.SH"]["volume"], 600)
        self.assertEqual(by_symbol["688235.SH"]["fill_count"], 3)
        self.assertEqual(by_symbol["688235.SH"]["volume"], 400)


if __name__ == "__main__": unittest.main()


class InstrumentTradableCompatibilityTests(unittest.TestCase):
    class FakeInstrumentXtdata:
        def __init__(self, detail):
            self.detail = detail
        def get_instrument_detail(self, symbol, *args):
            return dict(self.detail)

    def _market(self, detail):
        with tempfile.TemporaryDirectory() as d:
            db = Database(str(Path(d)/"x.db"))
            m = MarketDataService(db)
            m._xtdata = self.FakeInstrumentXtdata(detail)
            # Keep temp DB alive only for this immediate check.
            m.ensure_tradable("603986.SH")

    def test_a_share_is_trading_false_but_instrument_status_zero_is_allowed(self):
        # Official xtdata stock examples can expose IsTrading=False while
        # InstrumentStatus=0.  For A-share cash equities this must not block.
        self._market({"InstrumentStatus": 0, "IsTrading": False})

    def test_resumed_instrument_status_minus_one_is_allowed(self):
        self._market({"InstrumentStatus": -1, "IsTrading": False})

    def test_positive_instrument_status_is_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(str(Path(d)/"x.db"))
            m = MarketDataService(db)
            m._xtdata = self.FakeInstrumentXtdata({"InstrumentStatus": 2, "IsTrading": True})
            with self.assertRaises(Exception):
                m.ensure_tradable("603986.SH")

class PilotSchedulerSafetyTests(unittest.TestCase):
    def test_pilot_mode_never_auto_executes(self):
        from unittest.mock import patch
        from datetime import datetime as RealDateTime
        from core.scheduler import DailyScheduler

        class FakeSettings:
            def section(self, name):
                if name == "schedule":
                    return {
                        "readiness_time": "99:99", "execute_time": "09:35",
                        "plan_time": "99:99", "reconcile_time": "99:99",
                    }
                return {}
            def get(self, key, default=None):
                values = {
                    "execution.auto_execute": True,
                    "execution.pilot_mode": True,
                    "market.symbols": ["A.SH"],
                    "market.stale_tick_seconds": 30,
                }
                return values.get(key, default)

        class FakeMarket:
            def is_live_session(self, symbol, stale): return True

        class FakeDB:
            def get_state(self, key, default=None): return default
            def set_state(self, key, value): pass

        class FakeService:
            def __init__(self):
                self.settings = FakeSettings(); self.market = FakeMarket(); self.db = FakeDB()
                self.execute_calls = 0
            def execute_plan(self, *args, **kwargs):
                self.execute_calls += 1
                return ["SHOULD_NOT_RUN"]

        class FixedDateTime:
            @classmethod
            def now(cls):
                return RealDateTime(2026, 9, 23, 10, 0, 0)

        svc = FakeService()
        with patch("core.scheduler.datetime", FixedDateTime):
            out = DailyScheduler(svc).tick()
        self.assertEqual(svc.execute_calls, 0)
        self.assertNotIn("execute", out)

class PostCloseDailyBarTests(unittest.TestCase):
    class LaggingHistoryXtdata:
        def download_history_data(self, *args, **kwargs):
            return None

        def get_market_data_ex(self, *args, **kwargs):
            symbol = kwargs.get("stock_list", ["603986.SH"])[0] if kwargs else "603986.SH"
            return {
                symbol: pd.DataFrame(
                    [{"open": 410.0, "high": 412.0, "low": 400.0, "close": 402.1, "volume": 1000.0}],
                    index=["20260922"],
                )
            }

        def get_full_kline(self, *args, **kwargs):
            symbol = kwargs.get("stock_list", ["603986.SH"])[0] if kwargs else "603986.SH"
            col = ["20260923"]
            return {
                "time": pd.DataFrame([["20260923"]], index=[symbol], columns=col),
                "open": pd.DataFrame([[401.0]], index=[symbol], columns=col),
                "high": pd.DataFrame([[405.0]], index=[symbol], columns=col),
                "low": pd.DataFrame([[397.0]], index=[symbol], columns=col),
                "close": pd.DataFrame([[398.72]], index=[symbol], columns=col),
                "volume": pd.DataFrame([[123456.0]], index=[symbol], columns=col),
            }

    def test_post_close_falls_back_to_full_kline_when_history_cache_lags(self):
        from datetime import datetime
        with tempfile.TemporaryDirectory() as d:
            db = Database(str(Path(d) / "x.db"))
            m = MarketDataService(db)
            m._xtdata = self.LaggingHistoryXtdata()
            state = m.refresh_completed_daily_bar(
                "603986.SH", "2026-09-23", datetime(2026, 9, 23, 15, 10, 0)
            )
            self.assertTrue(state["ok"])
            self.assertEqual(state["source"], "FULL_KLINE")
            rows = db.load_daily_bars("603986.SH", 3)
            self.assertEqual(rows[-1]["trade_date"], "2026-09-23")
            self.assertAlmostEqual(rows[-1]["close"], 398.72)

    def test_intraday_never_promotes_current_full_kline_to_completed_daily_bar(self):
        from datetime import datetime
        with tempfile.TemporaryDirectory() as d:
            db = Database(str(Path(d) / "x.db"))
            m = MarketDataService(db)
            m._xtdata = self.LaggingHistoryXtdata()
            state = m.refresh_completed_daily_bar(
                "603986.SH", "2026-09-23", datetime(2026, 9, 23, 14, 59, 0)
            )
            self.assertFalse(state["ok"])
            self.assertEqual(state["reason"], "TODAY_NOT_CLOSED")
            rows = db.load_daily_bars("603986.SH", 3)
            self.assertEqual(rows[-1]["trade_date"], "2026-09-22")

    def test_full_kline_parser_accepts_symbol_on_columns(self):
        class ColumnShapeXtdata(self.LaggingHistoryXtdata):
            def get_full_kline(self, *args, **kwargs):
                symbol = "603986.SH"
                idx = ["20260923"]
                return {
                    "time": pd.DataFrame([["20260923"]], index=idx, columns=[symbol]),
                    "open": pd.DataFrame([[401.0]], index=idx, columns=[symbol]),
                    "high": pd.DataFrame([[405.0]], index=idx, columns=[symbol]),
                    "low": pd.DataFrame([[397.0]], index=idx, columns=[symbol]),
                    "close": pd.DataFrame([[398.72]], index=idx, columns=[symbol]),
                    "volume": pd.DataFrame([[123456.0]], index=idx, columns=[symbol]),
                }
        with tempfile.TemporaryDirectory() as d:
            db = Database(str(Path(d) / "x.db"))
            m = MarketDataService(db)
            m._xtdata = ColumnShapeXtdata()
            row = m.latest_full_kline_bar("603986.SH")
            self.assertEqual(row["trade_date"], "2026-09-23")
            self.assertAlmostEqual(row["close"], 398.72)

class CloseSnapshotDailyBarTests(unittest.TestCase):
    class SnapshotFallbackXtdata:
        def download_history_data(self, *args, **kwargs):
            return None

        def get_market_data_ex(self, *args, **kwargs):
            symbol = kwargs.get("stock_list", ["603986.SH"])[0] if kwargs else "603986.SH"
            return {
                symbol: pd.DataFrame(
                    [{"open": 410.0, "high": 412.0, "low": 400.0, "close": 402.1, "volume": 1000.0}],
                    index=["20260922"],
                )
            }

        def get_full_kline(self, *args, **kwargs):
            raise RuntimeError('function not realize')

        def get_full_tick(self, symbols):
            symbol = symbols[0]
            return {
                symbol: {
                    "time": 1790146800000,
                    "timetag": "20260923 15:00:00",
                    "open": 401.00,
                    "high": 404.88,
                    "low": 396.51,
                    "lastPrice": 398.72,
                    "volume": 123456.0,
                    "openInt": 15,
                    "stockStatus": 5,
                }
            }

    def test_post_close_falls_back_to_strict_close_snapshot(self):
        from datetime import datetime
        with tempfile.TemporaryDirectory() as d:
            db = Database(str(Path(d) / "x.db"))
            m = MarketDataService(db)
            m._xtdata = self.SnapshotFallbackXtdata()
            state = m.refresh_completed_daily_bar(
                "603986.SH", "2026-09-23", datetime(2026, 9, 23, 15, 10, 0)
            )
            self.assertTrue(state["ok"])
            self.assertEqual(state["source"], "CLOSE_SNAPSHOT")
            self.assertEqual(state["snapshot_state"], 15)
            rows = db.load_daily_bars("603986.SH", 3)
            self.assertEqual(rows[-1]["trade_date"], "2026-09-23")
            self.assertAlmostEqual(rows[-1]["close"], 398.72)
            self.assertAlmostEqual(rows[-1]["open"], 401.00)
            self.assertAlmostEqual(rows[-1]["high"], 404.88)
            self.assertAlmostEqual(rows[-1]["low"], 396.51)

    def test_snapshot_fallback_refuses_intraday_continuous_state(self):
        class IntradayXtdata(self.SnapshotFallbackXtdata):
            def get_full_tick(self, symbols):
                symbol = symbols[0]
                return {symbol: {
                    "timetag": "20260923 14:59:59",
                    "open": 401.0, "high": 405.0, "low": 397.0,
                    "lastPrice": 399.0, "volume": 123000.0,
                    "openInt": 13, "stockStatus": 3,
                }}
        with tempfile.TemporaryDirectory() as d:
            db = Database(str(Path(d) / "x.db"))
            m = MarketDataService(db); m._xtdata = IntradayXtdata()
            self.assertIsNone(m.latest_close_snapshot_bar("603986.SH", "2026-09-23"))

    def test_snapshot_fallback_refuses_previous_day_snapshot(self):
        class OldXtdata(self.SnapshotFallbackXtdata):
            def get_full_tick(self, symbols):
                symbol = symbols[0]
                return {symbol: {
                    "timetag": "20260922 15:00:00",
                    "open": 410.0, "high": 412.0, "low": 400.0,
                    "lastPrice": 402.1, "volume": 1000.0,
                    "openInt": 15, "stockStatus": 5,
                }}
        with tempfile.TemporaryDirectory() as d:
            db = Database(str(Path(d) / "x.db"))
            m = MarketDataService(db); m._xtdata = OldXtdata()
            self.assertIsNone(m.latest_close_snapshot_bar("603986.SH", "2026-09-23"))

    def test_snapshot_fallback_refuses_invalid_ohlc(self):
        class BadXtdata(self.SnapshotFallbackXtdata):
            def get_full_tick(self, symbols):
                symbol = symbols[0]
                return {symbol: {
                    "timetag": "20260923 15:00:00",
                    "open": 401.0, "high": 398.0, "low": 397.0,
                    "lastPrice": 399.0, "volume": 123000.0,
                    "openInt": 15, "stockStatus": 5,
                }}
        with tempfile.TemporaryDirectory() as d:
            db = Database(str(Path(d) / "x.db"))
            m = MarketDataService(db); m._xtdata = BadXtdata()
            self.assertIsNone(m.latest_close_snapshot_bar("603986.SH", "2026-09-23"))


class ExecuteBusinessBlockTests(unittest.TestCase):
    def _service_shell(self):
        from core.service import TradingService
        svc = object.__new__(TradingService)
        svc.broker = type("B", (), {"connected": True})()
        svc.settings = type("S", (), {"get": lambda self, k, d=None: {
            "market.symbols": ["603986.SH"],
            "market.stale_tick_seconds": 30,
        }.get(k, d)})()
        svc.market = type("M", (), {"is_live_session": lambda self, symbol, sec: False})()
        return svc

    def test_execute_session_closed_returns_blocked_not_exception(self):
        svc = self._service_shell()
        svc.readiness_check = lambda: {
            "ready": True, "broker": True, "account": True, "market": True,
            "kill_switch": False, "session_open": False,
        }
        out = svc.execute_plan(dry_run=False)
        self.assertEqual(out[0]["status"], "BLOCKED")
        self.assertEqual(out[0]["block_type"], "SESSION_CLOSED_OR_STALE")

    def test_execute_kill_switch_returns_blocked_not_exception(self):
        svc = self._service_shell()
        svc.readiness_check = lambda: {
            "ready": False, "broker": True, "account": True, "market": True,
            "kill_switch": True, "session_open": True,
        }
        out = svc.execute_plan(dry_run=False)
        self.assertEqual(out[0]["status"], "BLOCKED")
        self.assertEqual(out[0]["block_type"], "KILL_SWITCH")

    def test_execute_wrong_date_returns_blocked_not_exception(self):
        svc = self._service_shell()
        svc.market = type("M", (), {"is_live_session": lambda self, symbol, sec: True})()
        svc.readiness_check = lambda: {
            "ready": True, "broker": True, "account": True, "market": True,
            "kill_switch": False, "session_open": True,
        }
        out = svc.execute_plan(trade_date="1999-01-01", dry_run=False)
        self.assertEqual(out[0]["status"], "BLOCKED")
        self.assertEqual(out[0]["block_type"], "INVALID_TRADE_DATE")
