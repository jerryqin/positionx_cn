from __future__ import annotations

from datetime import datetime
from typing import Callable, Dict


class DailyScheduler:
    """极简日内调度器。

    不依赖交易日历。交易动作只在 miniQMT 的新鲜实时行情确认市场处于
    连续交易时段后触发；收盘策略则用“今日完整日K是否存在”确认今天确实
    是交易日。这样周末/节假日自然无动作，也兼容不实现交易日历接口的客户端。
    """
    def __init__(self, service):
        self.service = service
        self.cfg = service.settings.section("schedule")
        self._last_plan_probe_minute = ""

    def _once(self, task: str, fn: Callable):
        today = datetime.now().date().isoformat()
        key = f"task:v102:{task}:{today}"
        if self.service.db.get_state(key) == "DONE":
            return None
        result = fn()
        self.service.db.set_state(key, "DONE")
        return result

    def tick(self) -> Dict:
        now = datetime.now()
        today = now.date().isoformat()
        hhmm = now.strftime("%H:%M")
        out = {}

        if hhmm >= self.cfg.get("readiness_time", "08:45"):
            r = self._once("readiness", self.service.readiness_check)
            if r is not None:
                out["readiness"] = r

        symbols = self.service.settings.get("market.symbols", []) or []
        ref = symbols[0] if symbols else ""
        session_open = bool(ref and self.service.market_provider.is_live_session(
            ref, int(self.service.settings.get("market.stale_tick_seconds", 30))
        ))

        # Pilot mode is manual-only by design.  Even if auto_execute is
        # accidentally set true, the scheduler will not submit pilot orders.
        pilot_mode = bool(self.service.settings.get("execution.pilot_mode", False))
        if (hhmm >= self.cfg.get("execute_time", "09:35")
                and bool(self.service.settings.get("execution.auto_execute", False))
                and not pilot_mode
                and session_open):
            r = self._once("execute", lambda: self.service.execute_plan(today, dry_run=False))
            if r is not None:
                out["execute"] = r

        # Probe at most once per minute after the close.  Every configured
        # symbol must have today's completed bar before the plan is frozen.
        # Historical cache publication can lag briefly after 15:00;
        # MarketDataService may use latest-trading-day full-kline data as a
        # safe fallback.  Weekends/holidays naturally remain not-ready.
        if hhmm >= self.cfg.get("plan_time", "15:05") and ref:
            minute_key = now.strftime("%Y-%m-%d %H:%M")
            if minute_key != self._last_plan_probe_minute:
                self._last_plan_probe_minute = minute_key
                bars_ready = all(
                    self.service.market_provider.has_completed_daily_bar(s, today, refresh=True)
                    for s in symbols
                )
                if bars_ready:
                    r = self._once("plan", lambda: self.service.prepare_plan(today))
                    if r is not None:
                        out["plan"] = r

        if hhmm >= self.cfg.get("reconcile_time", "15:15"):
            r = self._once("reconcile", self.service.reconcile)
            if r is not None:
                out["reconcile"] = r
        return out
