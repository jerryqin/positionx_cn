from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import math
import re
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

ST_RE = re.compile(r"^(?:\*?ST|S\*?ST|SST|NST)", re.I)
Q_LABELS = ["Q1", "Q2", "Q3", "Q4", "Q5"]


@dataclass(frozen=True)
class Ret120ForwardPolicy:
    strategy_id: str = "RET120_CSI500_FORWARD_V1_0"
    edge_id: str = "ret120_trend_underreaction"
    protocol_version: str = "1.0"
    historical_cutoff: str = "20260930"
    forward_start_rule: str = "FIRST_COMPLETED_MARKET_SESSION_AFTER_CUTOFF"
    universe: str = "CSI500"
    sector_name: str = "中证500"
    benchmark: str = "000905.SH"
    dividend_type: str = "back_ratio"
    feature_lookback_sessions: int = 120
    feature_min_valid: int = 400
    selected_bucket: str = "Q5"
    target_weight: float = 0.01
    max_target_gross: float = 0.30
    max_positions: int = 30
    exit_offset_sessions_from_signal: int = 120
    main_round_trip_cost: float = 0.002
    shadow_round_trip_cost: float = 0.005
    completed_trades_target: int = 60
    review_calendar_months: int = 12
    same_day_tie_break: str = "SYMBOL_ASC"
    research_only_fractional_shares: bool = True

    def fingerprint(self, artifact_hashes: Mapping[str, str] | None = None) -> str:
        payload = {
            "policy": asdict(self),
            "frozen_artifact_hashes": dict(sorted((artifact_hashes or {}).items())),
        }
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


def norm_date(v: Any) -> str:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        x = float(v)
        if x > 1e12:
            x /= 1000.0
        if x > 1e9:
            return datetime.fromtimestamp(x).strftime("%Y%m%d")
    s = "".join(ch for ch in str(v) if ch.isdigit())
    if len(s) < 8:
        raise ValueError(f"invalid date {v!r}")
    return s[:8]


def clean_name(v: Any) -> str:
    return str(v or "").replace(" ", "").replace("　", "").replace("＊", "*").strip()


def is_st_name(v: Any) -> bool:
    return bool(ST_RE.match(clean_name(v).upper()))


def board_of(symbol: str) -> str:
    code, _, market = str(symbol).upper().partition(".")
    if market == "SH":
        if code.startswith(("688", "689")):
            return "STAR"
        if code.startswith(("600", "601", "603", "605")):
            return "MAIN"
    if market == "SZ":
        if code.startswith(("300", "301")):
            return "CHINEXT"
        if code.startswith(("000", "001", "002", "003")):
            return "MAIN"
    if market == "BJ":
        return "BSE"
    return "OTHER"


def normalize_history(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    x = df.copy()
    x.columns = [str(c).lower() for c in x.columns]
    idx: list[str] = []
    for v in x.index:
        try:
            idx.append(pd.Timestamp(v).strftime("%Y%m%d"))
        except Exception:
            idx.append(norm_date(v))
    x.index = idx
    x = x[~x.index.duplicated(keep="last")].sort_index()
    for c in ("open", "high", "low", "close", "volume", "amount", "preclose"):
        if c in x.columns:
            x[c] = pd.to_numeric(x[c], errors="coerce")
    return x


def ret120_with_reason(df: pd.DataFrame | None, trade_date: str) -> tuple[float | None, str]:
    if df is None or df.empty:
        return None, "NO_HISTORY"
    if trade_date not in df.index:
        return None, "NO_EXACT_T_BAR"
    if "close" not in df.columns:
        return None, "NO_CLOSE_FIELD"
    loc = df.index.get_loc(trade_date)
    if not isinstance(loc, int):
        return None, "NON_UNIQUE_T_BAR"
    if loc < 120:
        return None, "SHORT_HISTORY_LT_121_OBS"
    c0 = df.iloc[loc - 120]["close"]
    c1 = df.iloc[loc]["close"]
    try:
        c0f, c1f = float(c0), float(c1)
    except Exception:
        return None, "NON_NUMERIC_CLOSE"
    if not (math.isfinite(c0f) and math.isfinite(c1f) and c0f > 0 and c1f > 0):
        return None, "INVALID_CLOSE"
    r = c1f / c0f - 1.0
    if not math.isfinite(r):
        return None, "INVALID_RET120"
    return float(r), "OK"


def build_daily_snapshot(
    histories: Mapping[str, pd.DataFrame],
    members: Iterable[str],
    trade_date: str,
    min_valid: int = 400,
) -> pd.DataFrame:
    symbols = sorted(set(map(str, members)))
    if len(symbols) != 500:
        raise ValueError(f"CSI500 snapshot must contain exact 500 members, got {len(symbols)}")
    rows = []
    for s in symbols:
        value, reason = ret120_with_reason(histories.get(s), trade_date)
        rows.append({
            "symbol": s,
            "signal_date": trade_date,
            "ret120": value,
            "feature_valid": value is not None,
            "feature_reason": reason,
        })
    out = pd.DataFrame(rows)
    valid = out[out["feature_valid"]].copy()
    if len(valid) < int(min_valid):
        raise ValueError(f"RET120_VALID_COVERAGE_BELOW_MIN valid={len(valid)} min={min_valid}")
    # Frozen v0.2.2 semantics: symbol-ASC row order, rank(method='first'), qcut(5).
    vals = valid.set_index("symbol")["ret120"].astype(float)
    ranks = vals.rank(method="first")
    buckets = pd.qcut(ranks, 5, labels=Q_LABELS)
    rank_map = ranks.to_dict()
    bucket_map = {k: str(v) for k, v in buckets.items()}
    out["rank_first"] = out["symbol"].map(rank_map)
    out["quintile"] = out["symbol"].map(bucket_map)
    out["selected_q5"] = out["quintile"].eq("Q5")
    return out.sort_values("symbol").reset_index(drop=True)


def snapshot_sha256(snapshot: pd.DataFrame) -> str:
    cols = [
        "signal_date", "symbol", "ret120", "rank_first", "quintile",
        "selected_q5", "feature_valid", "feature_reason",
    ]
    x = snapshot[cols].sort_values(["signal_date", "symbol"]).copy()
    records = []
    for r in x.to_dict("records"):
        records.append({
            "signal_date": str(r["signal_date"]),
            "symbol": str(r["symbol"]),
            "ret120": None if pd.isna(r["ret120"]) else format(float(r["ret120"]), ".17g"),
            "rank_first": None if pd.isna(r["rank_first"]) else format(float(r["rank_first"]), ".17g"),
            "quintile": None if pd.isna(r["quintile"]) else str(r["quintile"]),
            "selected_q5": bool(r["selected_q5"]),
            "feature_valid": bool(r["feature_valid"]),
            "feature_reason": str(r["feature_reason"]),
        })
    raw = json.dumps(records, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def choose_current_universe(
    sector_members: Iterable[str] | None,
    weight_members: Iterable[str] | None,
    expected_size: int = 500,
) -> dict[str, Any]:
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
        return {"ok": True, "members": sorted(s), "source": "QMT_SECTOR_AND_INDEX_WEIGHT_EXACT"}
    if s_ok:
        return {"ok": True, "members": sorted(s), "source": "QMT_SECTOR_ONLY_EXACT500"}
    if w_ok:
        return {"ok": True, "members": sorted(w), "source": "QMT_INDEX_WEIGHT_ONLY_EXACT500"}
    return {
        "ok": False,
        "reason": "NO_EXACT500_CURRENT_UNIVERSE_SOURCE",
        "sector_count": len(s),
        "weight_count": len(w),
    }


def standard_open_v1(
    open_price: float | None,
    up_stop_price: float | None,
    instrument_status: int | None,
    price_tick: float | None = None,
    high_price: float | None = None,
    low_price: float | None = None,
    close_price: float | None = None,
    prev_close: float | None = None,
) -> dict[str, Any]:
    if open_price is None or not math.isfinite(float(open_price)) or float(open_price) <= 0:
        return {"fillable": False, "reason": "NO_VALID_OPEN"}
    if instrument_status is not None and int(instrument_status) >= 1:
        return {"fillable": False, "reason": f"SUSPENDED_STATUS_{int(instrument_status)}"}
    up_known = up_stop_price is not None and math.isfinite(float(up_stop_price)) and float(up_stop_price) > 0
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


def new_listing_special(
    symbol: str,
    open_date: str | None,
    trade_date: str,
    market_dates: Sequence[str],
) -> bool:
    if not open_date:
        return False
    try:
        od = norm_date(open_date)
        td = norm_date(trade_date)
    except Exception:
        return False
    if td < od:
        return True
    b = board_of(symbol)
    sessions = [d for d in market_dates if od <= d <= td]
    if not sessions:
        return False
    if b == "MAIN":
        return len(sessions) <= 1
    if b in {"CHINEXT", "STAR"}:
        return len(sessions) <= 5
    if b == "BSE":
        return len(sessions) <= 1
    return False


def signal_day_eligibility(
    symbol: str,
    detail: Mapping[str, Any] | None,
    trade_date: str,
    market_dates: Sequence[str],
) -> dict[str, Any]:
    d = dict(detail or {})
    name = d.get("InstrumentName")
    if is_st_name(name):
        return {"eligible": False, "reason": "SIGNAL_KNOWN_ST", "st_unknown": False}
    raw_status = d.get("InstrumentStatus")
    status_i = None
    try:
        status_i = int(raw_status) if raw_status is not None else None
    except Exception:
        status_i = None
    if status_i is not None and status_i >= 1:
        return {"eligible": False, "reason": f"SIGNAL_SUSPENDED_STATUS_{status_i}", "st_unknown": name in (None, "")}
    if new_listing_special(symbol, d.get("OpenDate"), trade_date, market_dates):
        return {"eligible": False, "reason": "SIGNAL_NEW_LISTING_SPECIAL", "st_unknown": name in (None, "")}
    return {
        "eligible": True,
        "reason": "SIGNAL_ELIGIBLE",
        "st_unknown": name in (None, ""),
        "instrument_status": status_i,
        "instrument_name": name,
        "open_date": d.get("OpenDate"),
    }


def exit_action(session_distance_from_signal: int | None, close_price: float | None, exit_offset: int = 120) -> str:
    if session_distance_from_signal is None:
        return "FAIL_SESSION_DISTANCE"
    if session_distance_from_signal < exit_offset:
        return "HOLD"
    if close_price is None:
        return "DEFER_NO_VALID_CLOSE"
    try:
        cp = float(close_price)
    except Exception:
        return "DEFER_NO_VALID_CLOSE"
    if not math.isfinite(cp) or cp <= 0:
        return "DEFER_NO_VALID_CLOSE"
    return "EXIT_AT_CLOSE"


def review_not_before(started_at: str, months: int = 12) -> str:
    return (pd.Timestamp(started_at) + pd.DateOffset(months=months)).strftime("%Y%m%d")


def initial_state(policy: Ret120ForwardPolicy, artifact_hashes: Mapping[str, str]) -> dict[str, Any]:
    return {
        "status": "ARMED_WAIT_NEXT_SESSION",
        "historical_cutoff": policy.historical_cutoff,
        "started_at": None,
        "last_processed_date": policy.historical_cutoff,
        "review_not_before": None,
        "completed_trades_target": policy.completed_trades_target,
        "review_calendar_months": policy.review_calendar_months,
        "completed_trades": 0,
        "active_positions": 0,
        "pending_signals": 0,
        "blocked_entries": 0,
        "base_equity": 1_000_000.0,
        "shadow_equity": 1_000_000.0,
        "base_cash": 1_000_000.0,
        "shadow_cash": 1_000_000.0,
        "high_water_base": 1_000_000.0,
        "max_drawdown_base": 0.0,
        "due_unresolved_positions": 0,
        "artifact_hashes": dict(artifact_hashes),
    }


def public_health(state: Mapping[str, Any], policy: Ret120ForwardPolicy, today: str | None = None) -> dict[str, Any]:
    x = dict(state)
    today = today or datetime.now().strftime("%Y%m%d")
    x["trades_ready"] = int(x.get("completed_trades", 0)) >= policy.completed_trades_target
    review_date = x.get("review_not_before")
    x["calendar_months_ready"] = bool(review_date and today >= review_date)
    x["review_ready"] = bool(x["trades_ready"] and x["calendar_months_ready"])
    return x
