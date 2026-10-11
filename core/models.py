from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class PlanStatus(str, Enum):
    PLANNED = "PLANNED"
    EXECUTING = "EXECUTING"
    DONE = "DONE"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class AccountSnapshot:
    ts: str
    total_asset: float
    cash: float
    market_value: float
    frozen_cash: float = 0.0


@dataclass(frozen=True)
class PositionSnapshot:
    ts: str
    symbol: str
    volume: int
    can_use_volume: int
    avg_price: float
    market_value: float
    last_price: float = 0.0
    pnl: float = 0.0


@dataclass(frozen=True)
class TargetPosition:
    symbol: str
    target_weight: float
    reason: str
    audit: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TradingPlanItem:
    plan_date: str
    trade_date: str
    symbol: str
    target_weight: float
    reason: str
    strategy_name: str
    audit: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OrderIntent:
    intent_key: str
    trade_date: str
    symbol: str
    side: Side
    volume: int
    limit_price: float
    reason: str
    target_weight: float
    current_weight: float


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    volume: int
    message: str


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def as_dict(obj: Any) -> Dict[str, Any]:
    return asdict(obj)
