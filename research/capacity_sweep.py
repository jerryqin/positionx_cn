from __future__ import annotations
from typing import Any, Dict, Iterable, List, Mapping, Sequence

DEFAULT_CAPACITIES = (5, 10, 15, 20, 25, 30, 40, 50)

def normalize_capacities(values: Iterable[int]) -> List[int]:
    out = sorted({int(v) for v in values if int(v) > 0})
    if not out:
        raise ValueError("at least one positive capacity is required")
    return out

def extract_capacity_point(capacity: int, summary: Mapping[str, Any]) -> Dict[str, Any]:
    executable = int(summary.get("signals_executable") or 0)
    trades = int(summary.get("trades_executed") or 0)
    no_slot = int(summary.get("skipped_no_slot") or 0)
    duplicate = int(summary.get("skipped_duplicate_symbol") or 0)
    return {
        "max_positions": int(capacity),
        "trades_executed": trades,
        "signals_executable": executable,
        "execution_coverage": trades / executable if executable else 0.0,
        "skipped_no_slot": no_slot,
        "skipped_duplicate_symbol": duplicate,
        "capacity_skip_rate": no_slot / executable if executable else 0.0,
        "total_skip_rate": (no_slot + duplicate) / executable if executable else 0.0,
        "total_return": summary.get("total_return"),
        "cagr_calendar": summary.get("cagr_calendar_basis"),
        "max_drawdown": summary.get("max_drawdown"),
        "sharpe": summary.get("sharpe_actual_sessions_including_first_day"),
        "avg_exposure": summary.get("avg_exposure"),
        "avg_active_positions": summary.get("avg_active_positions"),
        "max_concurrent_positions": summary.get("max_concurrent_positions"),
    }

def classify_capacity_shape(points: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    def first_below(threshold: float):
        for p in points:
            if float(p["capacity_skip_rate"]) <= threshold:
                return int(p["max_positions"])
        return None

    first_positive = None
    sign_changes = []
    prev = None
    for p in points:
        r = p.get("total_return")
        if r is None:
            continue
        sign = 1 if float(r) > 0 else (-1 if float(r) < 0 else 0)
        if first_positive is None and sign > 0:
            first_positive = int(p["max_positions"])
        if prev is not None and sign != prev["sign"]:
            sign_changes.append({
                "from_capacity": prev["capacity"],
                "to_capacity": int(p["max_positions"]),
                "from_sign": prev["sign"],
                "to_sign": sign,
            })
        prev = {"capacity": int(p["max_positions"]), "sign": sign}

    return {
        "first_capacity_below_20pct_no_slot": first_below(0.20),
        "first_capacity_below_10pct_no_slot": first_below(0.10),
        "first_capacity_below_5pct_no_slot": first_below(0.05),
        "first_positive_total_return_capacity": first_positive,
        "return_sign_changes": sign_changes,
        "note": "Descriptive only; do not select the highest-return K as an optimum.",
    }
