from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class CapitalFloorPolicy:
    policy_id: str = "PILOT_CAPITAL_FLOOR_V0_1"
    deep_sleeve_weight: float = 0.50
    ret120_sleeve_weight: float = 0.50
    strategy_target_weight: float = 0.01
    fidelity_min: float = 0.90
    max_relative_target_error: float = 0.25
    candidate_capitals: tuple[int, ...] = (300_000, 500_000, 800_000, 1_000_000, 1_500_000, 2_000_000)


def board_of(symbol: str) -> str:
    code, _, market = str(symbol).upper().partition(".")
    if market == "SH" and code.startswith(("688", "689")):
        return "STAR"
    if market == "SH": return "SSE_MAIN"
    if market == "SZ" and code.startswith(("300", "301")): return "CHINEXT"
    if market == "SZ": return "SZSE_MAIN"
    return "OTHER"


def executable_buy_shares(target_notional: float, price: float, board: str) -> int:
    if not (target_notional > 0 and price > 0 and math.isfinite(target_notional) and math.isfinite(price)):
        return 0
    raw = int(math.floor(target_notional / price + 1e-12))
    if board == "STAR":
        return raw if raw >= 200 else 0
    # SSE main / SZSE main / ChiNext: buy declarations in 100-share lots.
    return (raw // 100) * 100


def evaluate_trade(price: float, total_capital: float, sleeve_weight: float, target_weight: float, board: str, max_rel_error: float) -> dict[str, Any]:
    target = float(total_capital) * float(sleeve_weight) * float(target_weight)
    shares = executable_buy_shares(target, float(price), board)
    executed = shares * float(price)
    rel_error = 1.0 if target <= 0 else max(0.0, (target - executed) / target)
    return {
        "target_notional": target,
        "shares": shares,
        "executed_notional": executed,
        "relative_underweight": rel_error,
        "fidelity_pass": bool(shares > 0 and rel_error <= max_rel_error),
        "blocked_one_lot": bool(shares == 0),
    }


def summarize(df: pd.DataFrame) -> dict[str, Any]:
    if df.empty:
        return {"n": 0, "fidelity": None, "blocked_fraction": None}
    rel = pd.to_numeric(df["relative_underweight"], errors="coerce")
    return {
        "n": int(len(df)),
        "fidelity": float(df["fidelity_pass"].mean()),
        "blocked_fraction": float(df["blocked_one_lot"].mean()),
        "median_relative_underweight": float(rel.median()),
        "p90_relative_underweight": float(rel.quantile(0.90)),
        "max_relative_underweight": float(rel.max()),
        "median_entry_open": float(pd.to_numeric(df["entry_open"], errors="coerce").median()),
        "p90_entry_open": float(pd.to_numeric(df["entry_open"], errors="coerce").quantile(.90)),
    }
