from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict, List, Optional
import logging

from .broker import MiniQmtBroker
from .config import Settings
from .data import MarketDataService
from .db import Database
from .execution import ExecutionEngine
from .providers.factory import build_legacy_xtquant_bundle
from .models import TradingPlanItem
from .risk import RiskGate
from .strategy import load_strategy


def _broker_source(row: Dict) -> str:
    """Classify broker rows without guessing from symbols or account state."""
    strategy_name = str(row.get("strategy_name", "") or "")
    return "POSITIONX" if strategy_name.startswith("PositionX") else "EXTERNAL"


def _broker_time_value(value) -> float:
    """Best-effort sortable numeric broker time; unknown formats sort last."""
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _format_broker_time(value) -> str:
    """Human-friendly time while preserving odd broker formats verbatim."""
    if value in (None, "", 0, "0"):
        return "-"
    try:
        v = float(value)
        # milliseconds / seconds Unix timestamps
        if v > 1e12:
            v /= 1000.0
        if v > 1e9:
            return datetime.fromtimestamp(v).strftime("%Y-%m-%d %H:%M:%S")
        # Some gateways expose HHMMSS as an integer.
        iv = int(v)
        if 0 <= iv <= 235959:
            hh, mm, ss = iv // 10000, (iv // 100) % 100, iv % 100
            if hh < 24 and mm < 60 and ss < 60:
                return f"{hh:02d}:{mm:02d}:{ss:02d}"
    except Exception:
        pass
    return str(value)


def _recent_order_audit(orders: List[Dict], limit: int = 5) -> List[Dict]:
    rows = sorted(orders, key=lambda x: _broker_time_value(x.get("order_time")), reverse=True)[:limit]
    return [{
        "time": _format_broker_time(o.get("order_time")),
        "symbol": o.get("symbol", ""),
        "side": o.get("direction", ""),
        "price": float(o.get("price", 0) or 0),
        "volume": int(o.get("volume", 0) or 0),
        "traded_volume": int(o.get("traded_volume", 0) or 0),
        "status": str(o.get("status", "")),
        "status_msg": str(o.get("status_msg", "") or ""),
        "source": _broker_source(o),
        "strategy_name": str(o.get("strategy_name", "") or ""),
        "remark": str(o.get("remark", "") or ""),
        "order_id": o.get("order_id"),
    } for o in rows]


def _recent_trade_audit(trades: List[Dict], limit: Optional[int] = None) -> List[Dict]:
    """Return broker trade fills newest-first.

    ``limit=None`` means complete output.  Reconcile uses the complete mode so
    split fills can never make a fully-filled order look partially filled.
    """
    rows = sorted(trades, key=lambda x: _broker_time_value(x.get("traded_time")), reverse=True)
    if limit is not None:
        rows = rows[:max(0, int(limit))]
    return [{
        "time": _format_broker_time(t.get("traded_time")),
        "symbol": t.get("symbol", ""),
        "side": t.get("direction", ""),
        "price": float(t.get("price", 0) or 0),
        "volume": int(t.get("volume", 0) or 0),
        "amount": float(t.get("amount", 0) or 0),
        "source": _broker_source(t),
        "strategy_name": str(t.get("strategy_name", "") or ""),
        "remark": str(t.get("remark", "") or ""),
        "order_id": t.get("order_id"),
        "trade_id": str(t.get("trade_id", "") or ""),
    } for t in rows]


def _trade_fill_summary(trades: List[Dict]) -> List[Dict]:
    """Exact fill totals grouped by symbol/side/source (not inferred per order)."""
    groups = {}
    for t in trades:
        key = (t.get("symbol", ""), t.get("direction", ""), _broker_source(t))
        g = groups.setdefault(key, {
            "symbol": key[0],
            "side": key[1],
            "source": key[2],
            "fill_count": 0,
            "volume": 0,
            "amount": 0.0,
        })
        g["fill_count"] += 1
        g["volume"] += int(t.get("volume", 0) or 0)
        amount = float(t.get("amount", 0) or 0)
        if not amount:
            amount = float(t.get("price", 0) or 0) * int(t.get("volume", 0) or 0)
        g["amount"] += amount
    out = []
    for g in groups.values():
        g["vwap"] = (g["amount"] / g["volume"]) if g["volume"] else 0.0
        g["amount"] = round(g["amount"], 4)
        g["vwap"] = round(g["vwap"], 6)
        out.append(g)
    return sorted(out, key=lambda x: (x["symbol"], x["side"], x["source"]))


def _plan_status_audit(plan_rows: List[Dict], account, positions) -> List[Dict]:
    """Read-only reconciliation summary for today's trading plans."""
    pmap = {p.symbol: p for p in positions}
    out = []
    for row in plan_rows:
        p = pmap.get(row.get("symbol"))
        current_mv = float(p.market_value if p else 0.0)
        current_weight = current_mv / account.total_asset if account.total_asset > 0 else 0.0
        target_weight = float(row.get("target_weight", 0.0) or 0.0)
        audit = dict(row.get("audit") or {})
        out.append({
            "symbol": row.get("symbol", ""),
            "strategy_name": row.get("strategy_name", ""),
            "status": row.get("status", ""),
            "signal_date": audit.get("signal_date"),
            "target_weight": target_weight,
            "current_weight": current_weight,
            "difference": target_weight - current_weight,
            "current_market_value": current_mv,
            "reason": row.get("reason", ""),
        })
    return out


class TradingService:
    def __init__(self, settings_path: str = "config/settings.yaml"):
        self.settings = Settings(settings_path)
        self.db = Database(self.settings.get("app.db_path", "data/positionx.db"))
        self.market = MarketDataService(self.db)
        q = self.settings.section("qmt")
        self.broker: Optional[MiniQmtBroker] = None
        if q.get("account_id"):
            self.broker = MiniQmtBroker(q.get("mini_path", ""), str(q.get("account_id")), q.get("account_type", "STOCK"), int(q.get("session_id",0) or 0), self._broker_event)
        self.providers = build_legacy_xtquant_bundle(market_service=self.market, broker=self.broker)
        self.market_provider = self.providers.market_data
        self.broker_provider = self.providers.broker
        self.execution_provider = self.providers.execution
        self.risk = RiskGate(self.db, self.settings.section("risk"))
        self.strategy = load_strategy(self.settings.get("strategy.class"), self.settings.section("strategy").get("params", {}))
        self.log = logging.getLogger("positionx")

    def _broker_event(self, kind: str, payload: Dict):
        level = "ERROR" if kind in {"DISCONNECTED","ORDER_ERROR","CANCEL_ERROR"} else "INFO"
        self.db.event(level, f"BROKER_{kind}", kind, payload)
        if kind == "DISCONNECTED":
            self.db.set_state("ready", "0")

    def connect(self):
        if self.broker is None:
            raise RuntimeError("config/settings.yaml 中 qmt.account_id 为空")
        self.broker.connect()
        self.db.event("INFO", "BROKER_CONNECTED", "miniQMT连接成功")
        return True

    def disconnect(self):
        if self.broker:
            self.broker.disconnect()

    def sync_broker(self):
        if not self.broker or not self.broker.connected:
            raise RuntimeError("Broker 未连接")
        account = self.broker.query_account()
        positions = self.broker.query_positions()
        orders = self.broker.query_orders()
        trades = self.broker.query_trades()
        self.db.save_account(account)
        self.db.save_positions(positions)
        self.db.save_broker_orders(orders)
        self.db.save_broker_trades(trades)
        return account, positions, orders, trades

    def readiness_check(self) -> Dict:
        checks = {}
        try:
            if not self.broker or not self.broker.connected:
                self.connect()
            account, positions, orders, trades = self.sync_broker()
            checks["broker"] = True
            checks["account"] = account.total_asset > 0
        except Exception as e:
            checks["broker"] = False
            checks["account"] = False
            checks["broker_error"] = str(e)

        try:
            symbols = self.settings.get("market.symbols", []) or []
            for s in symbols:
                self.market.full_tick(s)
            checks["market"] = True
        except Exception as e:
            checks["market"] = False
            checks["market_error"] = str(e)

        # Trading calendar is intentionally NOT a hard dependency.  Some
        # miniQMT clients expose get_trading_calendar() in Python but the
        # backend returns "function not realize".  Execution is guarded by a
        # fresh live-session tick instead.
        symbols = self.settings.get("market.symbols", []) or []
        ref = symbols[0] if symbols else ""
        checks["calendar_mode"] = "LIVE_SESSION_INFERENCE"
        checks["session_open"] = bool(ref and self.market.is_live_session(
            ref, int(self.settings.get("market.stale_tick_seconds", 30))
        ))

        checks["kill_switch"] = self.risk.kill_switch()
        live_enabled = bool(self.settings.get("execution.live_enabled", False))
        pilot_mode = bool(self.settings.get("execution.pilot_mode", False))
        checks["execution_mode"] = (
            "SAFE_PREVIEW" if not live_enabled else
            "PILOT_LIVE" if pilot_mode else
            "LIVE"
        )
        checks["pilot"] = {
            "enabled": pilot_mode,
            "max_order_value": float(self.settings.get("execution.pilot_max_order_value", 0) or 0),
            "max_orders_per_day": int(self.settings.get("execution.pilot_max_orders_per_day", 0) or 0),
            "auto_execute_allowed": not pilot_mode,
        }
        ready = bool(checks.get("broker") and checks.get("account") and checks.get("market") and not checks["kill_switch"])
        self.db.set_state("ready", "1" if ready else "0")
        self.db.event("INFO" if ready else "ERROR", "READINESS", "READY" if ready else "NOT_READY", checks)
        checks["ready"] = ready
        return checks

    def prepare_plan(self, plan_date: Optional[str] = None):
        plan_date = plan_date or date.today().isoformat()
        if plan_date == date.today().isoformat():
            hhmm = datetime.now().strftime("%H:%M")
            plan_time = self.settings.get("schedule.plan_time", "15:05")
            if hhmm < plan_time:
                result = {
                    "status": "BLOCKED",
                    "block_type": "BEFORE_PLAN_TIME",
                    "plan_date": plan_date,
                    "current_time": hhmm,
                    "plan_time": plan_time,
                    "reason": f"日线策略只能在收盘后生成计划；当前 {hhmm} < plan_time {plan_time}",
                }
                self.db.event("INFO", "PLAN_BLOCKED", result["reason"], result)
                return result
        symbols = self.settings.get("market.symbols", []) or []
        if not symbols:
            raise RuntimeError("market.symbols 为空，无法生成计划")
        bars = int(self.settings.get("market.history_bars", 260))
        history = {}
        bar_status = {}
        missing = []
        for symbol in symbols:
            state = self.market.refresh_completed_daily_bar(symbol, plan_date)
            bar_status[symbol] = state
            if not state.get("ok"):
                missing.append(symbol)
                continue
            history[symbol] = self.market.history(symbol, bars=bars, refresh=False)
            if str(history[symbol].index[-1])[:10] != plan_date:
                # At this point refresh reported success, so a mismatch is an
                # internal invariant failure rather than an expected data lag.
                raise RuntimeError(
                    f"{symbol} 日K刷新状态与本地数据不一致："
                    f"latest={history[symbol].index[-1]} plan_date={plan_date}"
                )
        if missing:
            result = {
                "status": "WAITING_DAILY_BAR",
                "plan_date": plan_date,
                "missing_symbols": missing,
                "bar_status": bar_status,
                "reason": "收盘后日K尚未全部就绪；未生成交易计划，将等待后续刷新",
            }
            self.db.event("INFO", "PLAN_WAITING_DAILY_BAR", result["reason"], result)
            return result
        targets = self.strategy.targets(history)
        raw = {t.symbol: t.target_weight for t in targets}
        clean = self.risk.validate_target_set(raw)
        # Do not predict exchange holidays.  Bind this plan to the first later
        # live A-share session confirmed by a fresh miniQMT tick.
        trade_date = "NEXT_SESSION"
        result = []
        for t in targets:
            audit = dict(t.audit or {})
            item = TradingPlanItem(plan_date, trade_date, t.symbol, clean[t.symbol], t.reason, self.strategy.name, audit)
            self.db.upsert_plan(item)
            row = {
                "symbol": t.symbol,
                **audit,
                "target_weight": clean[t.symbol],
                "reason": t.reason,
                "trade_date": trade_date,
            }
            result.append(row)
        self.db.event("INFO", "PLAN_CREATED", "生成下个真实交易时段计划", {"items": result})
        return result

    def execute_plan(self, trade_date: Optional[str] = None, dry_run: bool = False):
        if not self.broker or not self.broker.connected:
            self.connect()
        checks = self.readiness_check()
        if not checks["ready"]:
            # Expected safety states are business outcomes, not program errors.
            # Keep fail-closed while returning structured audit data to CLI/GUI.
            if checks.get("kill_switch"):
                return [{
                    "status": "BLOCKED",
                    "block_type": "KILL_SWITCH",
                    "reason": "Kill Switch 已开启；拒绝执行计划",
                    "checks": checks,
                }]
            return [{
                "status": "BLOCKED",
                "block_type": "NOT_READY",
                "reason": "系统未READY；拒绝执行计划",
                "checks": checks,
            }]
        trade_date = trade_date or date.today().isoformat()
        today = date.today().isoformat()
        if trade_date != today:
            return [{
                "status": "BLOCKED",
                "block_type": "INVALID_TRADE_DATE",
                "trade_date": trade_date,
                "today": today,
                "reason": f"实盘执行只允许今日计划：today={today} requested={trade_date}",
            }]
        symbols = self.settings.get("market.symbols", []) or []
        if not symbols:
            raise RuntimeError("market.symbols 为空，无法确认交易时段")
        if not self.market.is_live_session(symbols[0], int(self.settings.get("market.stale_tick_seconds", 30))):
            return [{
                "status": "BLOCKED",
                "block_type": "SESSION_CLOSED_OR_STALE",
                "trade_date": trade_date,
                "reason": "当前不是已确认的A股连续交易时段，或行情不新鲜；拒绝执行计划",
            }]
        # Bind NEXT_SESSION only after a real live session is observed.
        self.db.claim_pending_plan(trade_date)
        engine = ExecutionEngine(self.db, self.broker, self.market, self.risk,
                                 self.settings.section("risk"), self.settings.section("execution"), self.settings.section("market"))
        result = engine.execute_plan(trade_date, dry_run=dry_run)
        self.sync_broker()
        if not dry_run and bool(self.settings.get("execution.live_enabled", False)):
            by_symbol = {r.get("symbol"): r for r in result if r.get("symbol")}
            for row in self.db.plan_for_date(trade_date):
                r = by_symbol.get(row["symbol"])
                if r:
                    status = "EXECUTING" if r["status"] == "SUBMITTED" else "BLOCKED" if r["status"] in {"BLOCKED","ERROR"} else row["status"]
                    self.db.set_plan_status(trade_date,row["symbol"],row["strategy_name"],status)
        self.db.event("INFO", "PLAN_EXECUTED", f"执行 {trade_date} 计划", {"results": result})
        return result



    def execute_quantity(
        self,
        symbol: str,
        *,
        target_shares: int | None = None,
        side: str | None = None,
        volume: int | None = None,
        trade_date: Optional[str] = None,
        dry_run: bool = True,
    ):
        """Manual/Pilot share-based execution path.

        This path does not create or mutate a Strategy trading_plan row.  It uses
        the same broker, market, RiskGate, Pilot guard, intent persistence and
        submit path as normal execution, while keeping exact-share semantics
        separate from target_weight Strategy logic.
        """
        if not self.broker or not self.broker.connected:
            self.connect()

        checks = self.readiness_check()
        if not checks["ready"]:
            if checks.get("kill_switch"):
                return [{
                    "status": "BLOCKED",
                    "block_type": "KILL_SWITCH",
                    "reason": "Kill Switch 已开启；拒绝精确数量订单",
                    "checks": checks,
                }]
            return [{
                "status": "BLOCKED",
                "block_type": "NOT_READY",
                "reason": "系统未READY；拒绝精确数量订单",
                "checks": checks,
            }]

        trade_date = trade_date or date.today().isoformat()
        today = date.today().isoformat()
        if trade_date != today:
            return [{
                "status": "BLOCKED",
                "block_type": "INVALID_TRADE_DATE",
                "trade_date": trade_date,
                "today": today,
                "reason": f"实盘精确数量执行只允许今日：today={today} requested={trade_date}",
            }]

        stale_seconds = int(self.settings.get("market.stale_tick_seconds", 30))
        if not self.market.is_live_session(symbol, stale_seconds):
            return [{
                "status": "BLOCKED",
                "block_type": "SESSION_CLOSED_OR_STALE",
                "trade_date": trade_date,
                "symbol": symbol,
                "reason": "当前不是已确认的A股连续交易时段，或行情不新鲜；拒绝精确数量订单",
            }]

        engine = ExecutionEngine(
            self.db,
            self.broker,
            self.market,
            self.risk,
            self.settings.section("risk"),
            self.settings.section("execution"),
            self.settings.section("market"),
        )
        result = engine.execute_quantity_request(
            trade_date,
            symbol,
            target_shares=target_shares,
            side=side,
            volume=volume,
            dry_run=dry_run,
        )
        self.sync_broker()
        self.db.event(
            "INFO",
            "EXACT_QUANTITY_EXECUTION",
            f"{'预览' if dry_run else '执行'} {trade_date} {symbol} 精确数量请求",
            {"results": result},
        )
        return result


    def monitor_orders(self) -> Dict:
        if not self.broker or not self.broker.connected:
            return {"cancelled": []}
        timeout = int(self.settings.get("execution.cancel_unfilled_after_seconds", 90))
        now_ts = datetime.now().timestamp()
        cancelled = []
        active_status = {48,49,50,51,52,55}
        for o in self.broker.query_orders():
            if not str(o.get("strategy_name", "")).startswith("PositionX"):
                continue
            try: status = int(o.get("status", -1))
            except Exception: continue
            if status not in active_status:
                continue
            raw_time = o.get("order_time")
            try:
                t = float(raw_time)
                if t > 1e12: t /= 1000.0
            except Exception:
                continue
            if t > 0 and now_ts - t >= timeout:
                if self.broker.cancel(int(o["order_id"])):
                    cancelled.append(int(o["order_id"]))
                    self.db.event("WARNING", "STALE_ORDER_CANCEL", f"超时撤单 {o['order_id']}", o)
        return {"cancelled": cancelled}

    def _mark_completed_plans(self, trade_date: str, account, positions):
        pmap = {p.symbol: p for p in positions}
        threshold = float(self.settings.get("risk.rebalance_threshold_pct", 0.01))
        for row in self.db.plan_for_date(trade_date):
            p = pmap.get(row["symbol"])
            cw = (p.market_value / account.total_asset) if p and account.total_asset > 0 else 0.0
            if abs(float(row["target_weight"]) - cw) <= threshold:
                self.db.set_plan_status(trade_date,row["symbol"],row["strategy_name"],"DONE")

    def reconcile(self) -> Dict:
        if not self.broker or not self.broker.connected:
            self.connect()
        account, positions, orders, trades = self.sync_broker()
        self.monitor_orders()
        today = date.today().isoformat()
        self._mark_completed_plans(today, account, positions)
        # Re-read after completion marking so the audit reflects the final DB status.
        today_plans = self.db.plan_for_date(today)
        plan_audit = _plan_status_audit(today_plans, account, positions)
        external = self.broker.active_external_orders() if self.broker else []
        ok = len(external) == 0
        result = {
            "ok": ok,
            "external_active_orders": external,
            "positions": len(positions),
            "orders": len(orders),
            "trades": len(trades),
            "order_sources": {
                "positionx": sum(1 for o in orders if _broker_source(o) == "POSITIONX"),
                "external": sum(1 for o in orders if _broker_source(o) == "EXTERNAL"),
            },
            "trade_sources": {
                "positionx": sum(1 for t in trades if _broker_source(t) == "POSITIONX"),
                "external": sum(1 for t in trades if _broker_source(t) == "EXTERNAL"),
            },
            "recent_orders": _recent_order_audit(orders, 5),
            # Full fill list: do not truncate split executions in an audit command.
            "recent_trades": _recent_trade_audit(trades, None),
            "recent_trades_complete": True,
            "trade_fill_summary": _trade_fill_summary(trades),
            "plans": plan_audit,
            "plan_status_counts": {
                status: sum(1 for p in plan_audit if p.get("status") == status)
                for status in sorted({p.get("status", "") for p in plan_audit if p.get("status")})
            },
        }
        self.db.event("INFO" if ok else "WARNING", "RECONCILE", "OK" if ok else "EXTERNAL_ACTIVE_ORDER", result)
        return result
