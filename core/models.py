from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
import numbers
import re
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


def broker_order_key(row: Dict[str, Any]) -> str:
    """Storage identity, never an executable/cancelable local order ID."""
    oid = row.get("order_id")
    if isinstance(oid, bool) or not isinstance(oid, numbers.Integral) or oid < 0:
        raise ValueError("BROKER_ORDER_ID_INVALID")
    if oid > 0:
        key = f"LOCAL:{int(oid)}"
    else:
        sysid, symbol, stamp = row.get("order_sysid"), row.get("symbol"), row.get("order_time")
        if type(sysid) is not str or not re.fullmatch(r"[A-Za-z0-9]{1,128}", sysid):
            raise ValueError("BROKER_COUNTER_ID_MISSING_OR_INVALID")
        if type(symbol) is not str or not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
            raise ValueError("BROKER_COUNTER_MARKET_INVALID")
        # Only the native epoch-seconds format observed and approved on 2026-10-21.
        if isinstance(stamp, bool) or not isinstance(stamp, numbers.Integral) or len(str(stamp)) != 10:
            raise ValueError("BROKER_COUNTER_DATE_UNPROVEN")
        day = datetime.fromtimestamp(int(stamp), timezone(timedelta(hours=8))).strftime("%Y%m%d")
        key = f"COUNTER:{day}:{symbol[-2:]}:{sysid}"
    if row.get("order_key", key) != key:
        raise ValueError("BROKER_ORDER_KEY_MISMATCH")
    return key
