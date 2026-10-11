from __future__ import annotations

from dataclasses import dataclass, asdict
import math
from typing import Any
import pandas as pd


@dataclass(frozen=True)
class CapitalFloorV02Policy:
    policy_id: str = "PILOT_CAPITAL_FLOOR_V0_2_FORENSICS"
    deep_sleeve_weight: float = 0.50
    ret120_sleeve_weight: float = 0.50
    strategy_target_weight: float = 0.01
    fidelity_min: float = 0.90
    max_relative_target_error: float = 0.25
    point_capitals: tuple[int, ...] = (
        2_000_000, 2_500_000, 3_000_000, 4_000_000, 5_000_000,
        6_000_000, 8_000_000, 10_000_000, 12_000_000,
    )
    official_sizing_policy: str = "ROUND_DOWN_NEVER_EXCEED_TARGET"
    sensitivity_sizing_policy: str = "NEAREST_EXECUTABLE_WITHIN_PLUS_MINUS_25PCT_DIAGNOSTIC_ONLY"

    @property
    def portfolio_target_fraction(self) -> float:
        return self.deep_sleeve_weight * self.strategy_target_weight


def board_of(symbol: str) -> str:
    code, _, market = str(symbol).upper().partition(".")
    if market == "SH" and code.startswith(("688", "689")):
        return "STAR"
    if market == "SH":
        return "SSE_MAIN"
    if market == "SZ" and code.startswith(("300", "301")):
        return "CHINEXT"
    if market == "SZ":
        return "SZSE_MAIN"
    return "OTHER"


def official_round_down_shares(target_notional: float, price: float, board: str) -> int:
    if not (target_notional > 0 and price > 0 and math.isfinite(target_notional) and math.isfinite(price)):
        return 0
    raw = int(math.floor(target_notional / price + 1e-12))
    if board == "STAR":
        return raw if raw >= 200 else 0
    return (raw // 100) * 100


def nearest_executable_shares(target_notional: float, price: float, board: str) -> int:
    """Diagnostic only: choose nearest legal buy size; may exceed target."""
    if not (target_notional > 0 and price > 0 and math.isfinite(target_notional) and math.isfinite(price)):
        return 0
    raw = target_notional / price
    if board == "STAR":
        # STAR: minimum 200, then 1-share increments.
        if raw < 200:
            return 200
        return max(200, int(round(raw)))
    # Main/ChiNext: 100-share increments, minimum 100.
    lots = max(1, int(round(raw / 100.0)))
    return lots * 100


def evaluate_official(price: float, total_capital: float, sleeve_weight: float, target_weight: float,
                      board: str, max_rel_error: float) -> dict[str, Any]:
    target = float(total_capital) * float(sleeve_weight) * float(target_weight)
    shares = official_round_down_shares(target, float(price), board)
    executed = shares * float(price)
    under = 1.0 if target <= 0 else max(0.0, (target - executed) / target)
    return {
        "target_notional": target,
        "shares": shares,
        "executed_notional": executed,
        "relative_underweight": under,
        "fidelity_pass": bool(shares > 0 and under <= max_rel_error),
        "blocked_one_lot": bool(shares == 0),
    }


def evaluate_nearest_sensitivity(price: float, total_capital: float, sleeve_weight: float, target_weight: float,
                                 board: str, max_abs_rel_error: float) -> dict[str, Any]:
    target = float(total_capital) * float(sleeve_weight) * float(target_weight)
    shares = nearest_executable_shares(target, float(price), board)
    executed = shares * float(price)
    rel = None if target <= 0 else (executed - target) / target
    ok = bool(shares > 0 and rel is not None and abs(rel) <= max_abs_rel_error)
    return {
        "target_notional": target,
        "shares": shares,
        "executed_notional": executed,
        "relative_error_signed": rel,
        "absolute_relative_error": abs(rel) if rel is not None else None,
        "fidelity_pass": ok,
        "over_target": bool(rel is not None and rel > 0),
    }


def guaranteed_required_total_capital(price: float, board: str, portfolio_target_fraction: float,
                                      max_underweight: float) -> float:
    """Monotone sufficient floor for official round-down sizing.

    Main/ChiNext 100-share step: once target >= lot_value/max_underweight,
    rounding loss (<1 lot) is always <= max_underweight.
    STAR 1-share step after 200-share minimum: minimum dominates for 25% tolerance.
    """
    if not (price > 0 and portfolio_target_fraction > 0 and 0 < max_underweight < 1):
        return math.inf
    if board == "STAR":
        required_target = max(200.0 * price, price / max_underweight)
    else:
        lot_value = 100.0 * price
        required_target = lot_value / max_underweight
    return required_target / portfolio_target_fraction


def empirical_floor_from_required(required: pd.Series, fidelity_min: float) -> float | None:
    x = pd.to_numeric(required, errors="coerce").dropna().astype(float)
    x = x[x.map(math.isfinite)]
    if x.empty:
        return None
    vals = sorted(x.tolist())
    k = max(1, math.ceil(fidelity_min * len(vals)))
    return float(vals[k - 1])


def summarize_point(df: pd.DataFrame, *, error_col: str, blocked_col: str | None = None) -> dict[str, Any]:
    if df.empty:
        return {"n": 0, "fidelity": None}
    err = pd.to_numeric(df[error_col], errors="coerce")
    out = {
        "n": int(len(df)),
        "fidelity": float(df["fidelity_pass"].mean()),
        "median_error": float(err.median()),
        "p90_error": float(err.quantile(.90)),
        "max_error": float(err.max()),
        "median_entry_open": float(pd.to_numeric(df["entry_open"], errors="coerce").median()),
        "p90_entry_open": float(pd.to_numeric(df["entry_open"], errors="coerce").quantile(.90)),
    }
    if blocked_col and blocked_col in df.columns:
        out["blocked_fraction"] = float(df[blocked_col].mean())
    return out


def diagnostics_by_group(df: pd.DataFrame) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if df.empty:
        return out
    price = pd.to_numeric(df["entry_open"], errors="coerce")
    bins = [-float("inf"), 10, 20, 40, 80, 120, 200, float("inf")]
    labels = ["<=10", "10-20", "20-40", "40-80", "80-120", "120-200", ">200"]
    y = df.copy()
    y["price_band"] = pd.cut(price, bins=bins, labels=labels, right=True)
    out["board_counts"] = {str(k): int(v) for k, v in y["board"].value_counts(dropna=False).to_dict().items()}
    out["price_band_counts"] = {str(k): int(v) for k, v in y["price_band"].value_counts(sort=False, dropna=False).to_dict().items()}
    return out


def policy_dict(p: CapitalFloorV02Policy) -> dict[str, Any]:
    d = asdict(p)
    d["portfolio_target_fraction"] = p.portfolio_target_fraction
    return d
