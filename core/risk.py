from __future__ import annotations

from typing import Dict, Iterable, Optional

from .db import Database
from .models import AccountSnapshot, OrderIntent, PositionSnapshot, RiskDecision, Side


class RiskGate:
    def __init__(self, db: Database, cfg: Dict):
        self.db = db
        self.cfg = cfg

    def kill_switch(self) -> bool:
        return self.db.get_state("kill_switch", "0") == "1"

    def set_kill_switch(self, on: bool):
        self.db.set_state("kill_switch", "1" if on else "0")

    def validate_target_set(self, targets: Dict[str, float]) -> Dict[str, float]:
        max_symbol = float(self.cfg.get("max_symbol_weight", 0.25))
        max_total = float(self.cfg.get("max_total_weight", 0.80))
        clean = {s: max(0.0, min(float(w), max_symbol)) for s, w in targets.items()}
        total = sum(clean.values())
        if total > max_total and total > 0:
            scale = max_total / total
            clean = {s: w * scale for s, w in clean.items()}
        return clean

    def check_intent(self, intent: OrderIntent, account: AccountSnapshot, position: Optional[PositionSnapshot]) -> RiskDecision:
        if self.kill_switch():
            return RiskDecision(False, 0, "Kill Switch 已开启")
        if intent.side == Side.BUY and not bool(self.cfg.get("allow_buy", True)):
            return RiskDecision(False, 0, "配置禁止买入")
        if intent.side == Side.SELL and not bool(self.cfg.get("allow_sell", True)):
            return RiskDecision(False, 0, "配置禁止卖出")

        vol = int(intent.volume)
        if intent.side == Side.SELL:
            available = int(position.can_use_volume if position else 0)
            vol = min(vol, available)
            if vol <= 0:
                return RiskDecision(False, 0, "无可卖数量")
        else:
            lot = int(self.cfg.get("buy_lot", 100))
            vol = (vol // lot) * lot
            reserve = float(self.cfg.get("min_cash_reserve", 0.10))
            spendable = max(0.0, account.cash - account.total_asset * reserve)
            affordable = int(spendable // intent.limit_price)
            affordable = (affordable // lot) * lot
            vol = min(vol, affordable)
            if vol <= 0:
                return RiskDecision(False, 0, "现金/保留资金约束后无可买数量")

        max_order_pct = float(self.cfg.get("max_order_pct_of_asset", 0.10))
        max_notional = account.total_asset * max_order_pct
        if intent.limit_price > 0:
            max_vol = int(max_notional // intent.limit_price)
            if intent.side == Side.BUY:
                lot = int(self.cfg.get("buy_lot", 100))
                max_vol = (max_vol // lot) * lot
            vol = min(vol, max_vol)

        if vol <= 0:
            return RiskDecision(False, 0, "单笔金额上限后无可交易数量")
        return RiskDecision(True, vol, "PASS")
