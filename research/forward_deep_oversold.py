from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
import hashlib
import json
import math
from typing import Any, Dict, Iterable, Mapping, Sequence

import pandas as pd


@dataclass(frozen=True)
class DeepOversoldForwardPolicy:
    strategy_id: str = "DEEP_OVERSOLD_CSI500_FORWARD_V1_1"
    edge_id: str = "ret20_reversal"
    protocol_version: str = "1.1"
    frozen_at: str = "20260924"
    forward_start_rule: str = "FIRST_COMPLETED_MARKET_SESSION_AFTER_CUTOFF"
    historical_cutoff: str = "20260924"
    universe: str = "CSI500"
    sector_name: str = "中证500"
    benchmark: str = "000905.SH"
    ret20_threshold: float = -0.25
    target_weight: float = 0.01
    max_total_weight: float = 0.30
    exit_offset_sessions_from_signal: int = 20
    exit_counting_semantics: str = "S1_IS_ENTRY_SESSION; EXIT_AT_S20_CLOSE"
    fillability_version: str = "STANDARD_OPEN_V1"
    main_round_trip_cost: float = 0.002
    main_cost_semantics: str = "ALL_IN_ROUND_TRIP_REFERENCE_20BP"
    shadow_round_trip_cost: float = 0.005
    shadow_cost_semantics: str = "ALL_IN_ROUND_TRIP_STRESS_50BP"
    lot_sizing_version: str = "ROUND_DOWN_TO_MIN_LIMIT_ORDER_VOLUME_V1"
    completed_trades_target: int = 60
    review_calendar_months: int = 3
    dividend_type: str = "front"

    def fingerprint(self) -> str:
        raw = json.dumps(asdict(self), sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


def norm_date(v: Any) -> str:
    s = "".join(ch for ch in str(v) if ch.isdigit())
    if len(s) < 8:
        raise ValueError(f"invalid date {v!r}")
    return s[:8]


def normalize_history(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    x = df.copy()
    x.columns = [str(c).lower() for c in x.columns]
    idx = []
    for v in x.index:
        try:
            idx.append(pd.Timestamp(v).strftime("%Y%m%d"))
        except Exception:
            idx.append(norm_date(v))
    x.index = idx
    x = x[~x.index.duplicated(keep="last")].sort_index()
    for c in ("open", "high", "low", "close", "volume", "amount"):
        if c in x.columns:
            x[c] = pd.to_numeric(x[c], errors="coerce")
    return x


def ret20_at_close(df: pd.DataFrame, trade_date: str) -> float | None:
    if df.empty or trade_date not in df.index or "close" not in df.columns:
        return None
    loc = list(df.index).index(trade_date)
    if loc < 20:
        return None
    c0 = df.iloc[loc - 20]["close"]
    c1 = df.iloc[loc]["close"]
    if pd.isna(c0) or pd.isna(c1) or float(c0) <= 0:
        return None
    return float(c1) / float(c0) - 1.0


def parse_index_weight_members(raw: Any) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, dict):
        return {str(k) for k in raw.keys()}
    if isinstance(raw, (list, tuple, set)):
        return {str(x) for x in raw}
    return set()


def choose_current_universe(
    sector_members: Iterable[str] | None,
    weight_members: Iterable[str] | None,
    expected_size: int = 500,
) -> Dict[str, Any]:
    s = set(map(str, sector_members or []))
    w = set(map(str, weight_members or []))
    s_ok = len(s) == expected_size
    w_ok = len(w) == expected_size

    if s_ok and w_ok:
        if s != w:
            return {
                "ok": False,
                "reason": "CURRENT_UNIVERSE_SOURCE_MISMATCH",
                "sector_count": len(s),
                "weight_count": len(w),
                "symdiff": sorted(s ^ w)[:50],
            }
        return {
            "ok": True,
            "members": sorted(s),
            "source": "QMT_SECTOR_AND_INDEX_WEIGHT_EXACT",
        }
    if s_ok:
        return {
            "ok": True,
            "members": sorted(s),
            "source": "QMT_SECTOR_ONLY_EXACT500",
        }
    if w_ok:
        return {
            "ok": True,
            "members": sorted(w),
            "source": "QMT_INDEX_WEIGHT_ONLY_EXACT500",
        }
    return {
        "ok": False,
        "reason": "NO_EXACT500_CURRENT_UNIVERSE_SOURCE",
        "sector_count": len(s),
        "weight_count": len(w),
    }


def entry_fillability(
    open_price: float | None,
    up_stop_price: float | None,
    instrument_status: int | None,
    price_tick: float | None = None,
    high_price: float | None = None,
    low_price: float | None = None,
    close_price: float | None = None,
    prev_close: float | None = None,
) -> Dict[str, Any]:
    """STANDARD_OPEN_V1 — exact historical/forward semantics.

    Historical STANDARD was:
      valid open, not suspended, not one-price-up locked, and
      (up_limit is NULL OR open < up_limit).

    For no-limit/special-listing sessions where UpStopPrice is unavailable,
    absence of a limit is NOT itself a block.  We only block when the completed
    daily bar proves a one-price-up lock (high == low and close > prev_close).
    """
    if open_price is None or not math.isfinite(float(open_price)) or float(open_price) <= 0:
        return {"fillable": False, "reason": "NO_VALID_OPEN"}
    if instrument_status is not None and int(instrument_status) >= 1:
        return {"fillable": False, "reason": f"SUSPENDED_STATUS_{int(instrument_status)}"}

    up_known = (
        up_stop_price is not None
        and math.isfinite(float(up_stop_price))
        and float(up_stop_price) > 0
    )
    if up_known:
        tick = float(price_tick or 0.01)
        if float(open_price) >= float(up_stop_price) - tick / 2.0:
            return {"fillable": False, "reason": "OPEN_AT_UP_LIMIT"}
        return {"fillable": True, "reason": "STANDARD_OPEN_V1_LIMITED"}

    vals = (high_price, low_price, close_price, prev_close)
    if all(v is not None and math.isfinite(float(v)) for v in vals):
        if abs(float(high_price) - float(low_price)) < 1e-12 and float(close_price) > float(prev_close):
            return {"fillable": False, "reason": "ONE_PRICE_UP_NO_LIMIT_DATA"}
    return {"fillable": True, "reason": "STANDARD_OPEN_V1_NO_LIMIT"}


def lot_rounded_shares(
    allocation_notional: float,
    open_price: float,
    min_lot: int,
) -> int:
    """Executable A-share sizing: target is a ceiling, never force one lot.

    If the allocated 1% target cannot buy one minimum lot, return 0.  This
    records a real account-size limitation instead of silently exceeding risk.
    """
    if allocation_notional <= 0 or open_price <= 0 or min_lot <= 0:
        return 0
    lots = int(allocation_notional // (open_price * min_lot))
    return lots * int(min_lot)


def session_distance(market_dates: Sequence[str], signal_date: str, current_date: str) -> int | None:
    pos = {d: i for i, d in enumerate(market_dates)}
    if signal_date not in pos or current_date not in pos:
        return None
    return pos[current_date] - pos[signal_date]


def review_not_before(started_at: str, months: int = 3) -> str:
    d = pd.Timestamp(started_at)
    return (d + pd.DateOffset(months=months)).strftime("%Y%m%d")


def initial_state(policy: DeepOversoldForwardPolicy) -> Dict[str, Any]:
    return {
        "status": "ARMED_WAIT_NEXT_SESSION",
        "historical_cutoff": policy.historical_cutoff,
        "started_at": None,
        "last_processed_date": policy.historical_cutoff,
        "review_not_before": None,
        "completed_trades_target": policy.completed_trades_target,
        "completed_trades": 0,
        "active_positions": 0,
        "pending_signals": 0,
        "blocked_entries": 0,
        "base_equity": 1_000_000.0,
        "shadow_equity": 1_000_000.0,
        "base_cash": 1_000_000.0,
        "shadow_cash": 1_000_000.0,
        "max_drawdown_base": 0.0,
        "high_water_base": 1_000_000.0,
        "last_universe_snapshot": policy.historical_cutoff,
    }


def public_health(
    state: Mapping[str, Any],
    policy: DeepOversoldForwardPolicy,
    today: str | None = None,
) -> Dict[str, Any]:
    s = dict(state)
    today = today or datetime.now().strftime("%Y%m%d")
    review_date = s.get("review_not_before")
    s["trades_ready"] = int(s.get("completed_trades", 0)) >= policy.completed_trades_target
    s["calendar_months_ready"] = bool(review_date and today >= review_date)
    s["review_ready"] = bool(s["trades_ready"] and s["calendar_months_ready"])
    return s
