from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
import hashlib
import json
import math
from pathlib import Path
import statistics
from typing import Any, Dict, List, Mapping, Optional, Sequence


STATE_VERSION = "BREAKOUT_FORWARD_V1_1"
HISTORICAL_CUTOFF = "20260924"


@dataclass(frozen=True)
class ForwardPolicy:
    initial_capital: float = 1_000_000.0
    max_positions: int = 40
    target_weight: float = 0.015
    max_total_weight: float = 0.60
    holding_sessions_from_signal: int = 20
    round_trip_cost: float = 0.002
    shadow_round_trip_cost: float = 0.005
    min_completed_trades_for_review: int = 60
    min_calendar_months_for_review: int = 3
    historical_cutoff: str = HISTORICAL_CUTOFF

    def validate(self) -> None:
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        if self.max_positions <= 0:
            raise ValueError("max_positions must be positive")
        if not 0 < self.target_weight <= 1:
            raise ValueError("target_weight must be in (0,1]")
        if not 0 < self.max_total_weight <= 1:
            raise ValueError("max_total_weight must be in (0,1]")
        if self.target_weight * self.max_positions > self.max_total_weight + 1e-12:
            raise ValueError("target_weight * max_positions exceeds max_total_weight")
        if self.holding_sessions_from_signal < 2:
            raise ValueError("holding_sessions_from_signal too small")
        if self.round_trip_cost < 0 or self.shadow_round_trip_cost < 0:
            raise ValueError("costs must be non-negative")
        norm_date(self.historical_cutoff)


def norm_date(v: Any) -> str:
    s = str(v or "").replace("-", "").replace("/", "")
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) < 8:
        raise ValueError(f"invalid date: {v!r}")
    return digits[:8]


def add_months_yyyymmdd(day: str, months: int) -> str:
    d = date(int(day[:4]), int(day[4:6]), int(day[6:8]))
    total = d.year * 12 + (d.month - 1) + months
    year, month0 = divmod(total, 12)
    month = month0 + 1
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    last_day = (next_month - timedelta(days=1)).day
    return f"{year:04d}{month:02d}{min(d.day, last_day):02d}"


def policy_payload(policy: ForwardPolicy, matcher_config: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "policy": asdict(policy),
        "matcher_config": dict(matcher_config),
        "signal": {
            "strategy": "BREAKOUT_RETEST",
            "state": "RETEST_HOLD",
            "triggered": True,
        },
        "entry": "NEXT_MARKET_SESSION_OPEN",
        "exit": "SIGNAL_PLUS_20_MARKET_SESSION_CLOSE_OR_FIRST_TRADABLE_CLOSE",
        "no_duplicate_symbol_while_held": True,
        "same_day_exit_not_reusable_at_open": True,
        "historical_sample_exclusion": (
            f"Signals dated <= {policy.historical_cutoff} are never admitted "
            "to the forward sample."
        ),
    }


def payload_hash(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def recursive_match_rows(obj: Any) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    seen = set()

    def visit(x: Any) -> None:
        if isinstance(x, dict):
            keys = set(x)
            if "symbol" in keys and ("strategy" in keys or "state" in keys):
                sig = json.dumps(x, sort_keys=True, ensure_ascii=False, default=str)
                if sig not in seen:
                    rows.append(dict(x))
                    seen.add(sig)
            for v in x.values():
                visit(v)
        elif isinstance(x, list):
            for v in x:
                visit(v)

    visit(obj)
    return rows


def row_latest_date(row: Mapping[str, Any]) -> Optional[str]:
    for key in ("latest_date", "asof_date", "trade_date", "signal_date"):
        if row.get(key):
            try:
                return norm_date(row[key])
            except Exception:
                pass
    return None


def infer_asof(rows: Sequence[Mapping[str, Any]], payload: Mapping[str, Any]) -> Optional[str]:
    dates = [d for d in (row_latest_date(r) for r in rows) if d]
    if dates:
        return max(dates)
    for key in ("asof_date", "trade_date", "date"):
        if payload.get(key):
            try:
                return norm_date(payload[key])
            except Exception:
                pass
    generated = payload.get("generated_at")
    if generated:
        try:
            return norm_date(generated)
        except Exception:
            pass
    return None


def triggered_breakout_signals(
    rows: Sequence[Mapping[str, Any]],
    asof: str,
    historical_cutoff: str = HISTORICAL_CUTOFF,
) -> List[Dict[str, Any]]:
    if asof <= historical_cutoff:
        return []
    out = []
    for r in rows:
        strategy = str(r.get("strategy") or "").upper()
        state = str(r.get("state") or "").upper()
        triggered = bool(r.get("triggered"))
        latest = row_latest_date(r)
        if strategy != "BREAKOUT_RETEST":
            continue
        if state != "RETEST_HOLD" or not triggered:
            continue
        if latest != asof:
            continue
        symbol = str(r.get("symbol") or "").strip()
        if not symbol:
            continue
        out.append({
            "signal_key": f"{symbol}|{asof}",
            "symbol": symbol,
            "signal_date": asof,
            "breakout_date": (
                norm_date(r["breakout_date"]) if r.get("breakout_date") else None
            ),
            "level": r.get("level"),
            "reason": r.get("reason"),
            "source_state": state,
        })
    out.sort(key=lambda x: x["symbol"])
    return out


def init_state(
    policy_data: Mapping[str, Any],
    policy_hash: str,
    historical_cutoff: str,
    initial_capital: float,
) -> Dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "policy": dict(policy_data),
        "policy_hash": policy_hash,
        "historical_cutoff": historical_cutoff,
        "started_at": None,
        "last_processed_date": historical_cutoff,
        "cash": float(initial_capital),
        "positions": {},
        "pending": [],
        "closed_trades": [],
        "blocked_entries": [],
        "daily": [],
        "ingested_signal_keys": [],
        "processed_dates": [],
    }


def assert_state_policy(state: Mapping[str, Any], expected_hash: str) -> None:
    if state.get("version") != STATE_VERSION:
        raise RuntimeError(
            f"state version mismatch: {state.get('version')}; "
            "do not reuse a v1 state for v1.1"
        )
    if state.get("policy_hash") != expected_hash:
        raise RuntimeError(
            "FORWARD_POLICY_CHANGED: frozen forward policy or BreakoutRetest matcher config changed"
        )


def market_sessions_between(market_dates: Sequence[str], after: str, through: str) -> List[str]:
    return [d for d in market_dates if after < d <= through]


def expected_next_session(market_dates: Sequence[str], after: str) -> Optional[str]:
    for d in market_dates:
        if d > after:
            return d
    return None


def session_age_from_signal(market_dates: Sequence[str], signal_date: str, asof: str) -> int:
    return len(market_sessions_between(market_dates, signal_date, asof))


def board_limit_ratio(symbol: str, instrument_name: Optional[str] = None) -> float:
    name = str(instrument_name or "").upper()
    if "ST" in name:
        return 0.05
    code = symbol.split(".")[0]
    if code.startswith(("300", "301", "688")):
        return 0.20
    return 0.10


def rounded_limit(pre_close: float, ratio: float) -> float:
    return math.floor(pre_close * (1.0 + ratio) * 100.0 + 0.5) / 100.0


def fillability_at_open(
    symbol: str,
    bar: Optional[Mapping[str, Any]],
    prev_close: Optional[float],
    instrument_name: Optional[str] = None,
    instrument_status: Optional[Any] = None,
) -> Dict[str, Any]:
    if bar is None:
        return {"fillable": False, "reason": "NO_DAILY_BAR_OR_SUSPENDED"}

    if instrument_status is not None:
        try:
            if int(instrument_status) >= 1:
                return {"fillable": False, "reason": "INSTRUMENT_STATUS_BLOCKED"}
        except Exception:
            return {"fillable": False, "reason": "INSTRUMENT_STATUS_UNPARSEABLE"}

    required = ("open", "high", "low", "close")
    try:
        px = {k: float(bar[k]) for k in required}
    except Exception:
        return {"fillable": False, "reason": "INVALID_OHLC"}

    if any((not math.isfinite(v) or v <= 0) for v in px.values()):
        return {"fillable": False, "reason": "INVALID_OHLC"}
    if px["high"] + 1e-9 < max(px["open"], px["close"], px["low"]):
        return {"fillable": False, "reason": "INCONSISTENT_OHLC"}
    if px["low"] - 1e-9 > min(px["open"], px["close"], px["high"]):
        return {"fillable": False, "reason": "INCONSISTENT_OHLC"}

    if prev_close is None or not math.isfinite(float(prev_close)) or float(prev_close) <= 0:
        return {"fillable": False, "reason": "MISSING_PREVIOUS_CLOSE"}

    ratio = board_limit_ratio(symbol, instrument_name)
    up = rounded_limit(float(prev_close), ratio)

    if px["open"] >= up - 1e-8:
        return {
            "fillable": False,
            "reason": "OPEN_AT_OR_ABOVE_UP_LIMIT",
            "up_limit": up,
            "ratio": ratio,
        }

    return {
        "fillable": True,
        "reason": "FILLABLE_AT_OPEN",
        "up_limit": up,
        "ratio": ratio,
        "open": px["open"],
    }


def trade_return(entry: float, exit_: float, round_trip_cost: float) -> float:
    half = round_trip_cost / 2.0
    return (exit_ * (1.0 - half)) / (entry * (1.0 + half)) - 1.0


def stats(values: Sequence[float]) -> Dict[str, Any]:
    xs = [float(x) for x in values if x is not None and math.isfinite(float(x))]
    if not xs:
        return {"n": 0, "avg": None, "median": None, "win_rate": None, "profit_factor": None}
    gains = sum(x for x in xs if x > 0)
    losses = -sum(x for x in xs if x < 0)
    return {
        "n": len(xs),
        "avg": statistics.fmean(xs),
        "median": statistics.median(xs),
        "win_rate": sum(1 for x in xs if x > 0) / len(xs),
        "profit_factor": (gains / losses if losses > 0 else None),
    }


def max_drawdown(equities: Sequence[float], initial: float) -> float:
    curve = [float(initial)] + [float(x) for x in equities]
    peak = curve[0]
    mdd = 0.0
    for x in curve:
        peak = max(peak, x)
        if peak > 0:
            mdd = min(mdd, x / peak - 1.0)
    return mdd


def forward_health(state: Mapping[str, Any], policy: ForwardPolicy) -> Dict[str, Any]:
    closed = list(state.get("closed_trades") or [])
    daily = list(state.get("daily") or [])
    base = stats([x.get("net_return_20bps") for x in closed])
    shadow = stats([x.get("net_return_50bps") for x in closed])

    started_raw = state.get("started_at")
    started = str(started_raw) if started_raw else None
    last = str(state.get("last_processed_date") or policy.historical_cutoff)
    due_date = (
        add_months_yyyymmdd(started, policy.min_calendar_months_for_review)
        if started else None
    )
    months_ready = bool(due_date and last >= due_date)
    trades_ready = len(closed) >= policy.min_completed_trades_for_review

    equity = float(daily[-1]["equity"]) if daily else float(policy.initial_capital)
    total_return = equity / policy.initial_capital - 1.0
    mdd = max_drawdown([x["equity"] for x in daily], policy.initial_capital)

    if not started:
        status = "ARMED_WAIT_NEXT_SESSION"
    else:
        status = "REVIEW_DUE" if (months_ready and trades_ready) else "COLLECTING"

    return {
        "status": status,
        "historical_cutoff": policy.historical_cutoff,
        "started_at": started,
        "last_processed_date": last,
        "review_not_before": due_date,
        "completed_trades": len(closed),
        "completed_trades_target": policy.min_completed_trades_for_review,
        "calendar_months_ready": months_ready,
        "trades_ready": trades_ready,
        "active_positions": len(state.get("positions") or {}),
        "pending_signals": len(state.get("pending") or []),
        "blocked_entries": len(state.get("blocked_entries") or []),
        "equity": equity,
        "total_return": total_return,
        "max_drawdown": mdd,
        "base_20bps": base,
        "shadow_50bps": shadow,
    }


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
