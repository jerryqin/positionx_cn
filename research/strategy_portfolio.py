from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
from typing import Any, Mapping

import pandas as pd


DEEP_ID = "DEEP_OVERSOLD_CSI500_FORWARD_V1_1"
RET120_ID = "RET120_CSI500_FORWARD_V1_0"
PORTFOLIO_ID = "STRATEGY_PORTFOLIO_V0_1"


@dataclass(frozen=True)
class StrategyPortfolioPolicy:
    portfolio_id: str = PORTFOLIO_ID
    version: str = "0.1"
    deep_strategy_id: str = DEEP_ID
    ret120_strategy_id: str = RET120_ID
    deep_initial_weight: float = 0.50
    ret120_initial_weight: float = 0.50
    rebalance: bool = False
    inception_rule: str = "FIRST_COMMON_FORWARD_SESSION_AFTER_RET120_START"
    attribution: str = "INITIAL_CAPITAL_SLEEVES"
    max_portfolio_target_gross: float = 0.30
    same_symbol_policy: str = "ALLOW_BOTH_SLEEVES_BUT_AGGREGATE_AND_REPORT_EXPOSURE"
    research_only: bool = True
    auto_execute: bool = False

    def fingerprint(self) -> str:
        raw = json.dumps(asdict(self), sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class PilotReadinessPolicy:
    policy_id: str = "PILOT_LIVE_READINESS_V0_1"
    common_forward_burn_in_sessions: int = 20
    portfolio_shadow_sessions: int = 20
    min_shadow_order_decisions: int = 20
    board_lot_fidelity_min: float = 0.90
    target_weight_relative_error_max: float = 0.25
    require_zero_retro_snapshot: bool = True
    require_zero_unexplained_state_mismatch: bool = True
    require_zero_duplicate_order_intent: bool = True
    auto_execute: bool = False
    live_mode: str = "MANUAL_APPROVAL_REQUIRED"

    def fingerprint(self) -> str:
        raw = json.dumps(asdict(self), sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


def normalize_sleeve_curve(df: pd.DataFrame, inception: str) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    x = df.copy()
    x["trade_date"] = x["trade_date"].astype(str).str.replace("-", "", regex=False)
    x = x[x["trade_date"] >= inception].sort_values("trade_date").reset_index(drop=True)
    if x.empty:
        return x
    e0 = float(x.iloc[0]["equity"])
    if not (e0 > 0):
        raise ValueError("non-positive sleeve inception equity")
    for c in ("equity", "cash", "gross_market_value"):
        x[c] = pd.to_numeric(x[c], errors="raise")
        x[f"norm_{c}"] = x[c] / e0
    return x


def combine_initial_capital_sleeves(
    deep: pd.DataFrame,
    ret120: pd.DataFrame,
    deep_weight: float = 0.5,
    ret120_weight: float = 0.5,
    initial_equity: float = 1_000_000.0,
) -> pd.DataFrame:
    if abs(deep_weight + ret120_weight - 1.0) > 1e-12:
        raise ValueError("sleeve weights must sum to 1")
    if deep.empty or ret120.empty:
        return pd.DataFrame()
    common = sorted(set(deep["trade_date"].astype(str)) & set(ret120["trade_date"].astype(str)))
    if not common:
        return pd.DataFrame()
    inception = common[0]
    d = normalize_sleeve_curve(deep, inception)
    r = normalize_sleeve_curve(ret120, inception)
    d = d[d["trade_date"].isin(common)].set_index("trade_date")
    r = r[r["trade_date"].isin(common)].set_index("trade_date")
    rows = []
    for dt in common:
        deq = deep_weight * initial_equity * float(d.loc[dt, "norm_equity"])
        req = ret120_weight * initial_equity * float(r.loc[dt, "norm_equity"])
        dcash = deep_weight * initial_equity * float(d.loc[dt, "norm_cash"])
        rcash = ret120_weight * initial_equity * float(r.loc[dt, "norm_cash"])
        dgmv = deep_weight * initial_equity * float(d.loc[dt, "norm_gross_market_value"])
        rgmv = ret120_weight * initial_equity * float(r.loc[dt, "norm_gross_market_value"])
        equity = deq + req
        gmv = dgmv + rgmv
        rows.append({
            "trade_date": dt,
            "portfolio_equity": equity,
            "portfolio_cash": dcash + rcash,
            "portfolio_gross_market_value": gmv,
            "portfolio_gross_weight": gmv / equity if equity > 0 else 0.0,
            "deep_sleeve_equity": deq,
            "ret120_sleeve_equity": req,
            "deep_contribution": deq - deep_weight * initial_equity,
            "ret120_contribution": req - ret120_weight * initial_equity,
        })
    return pd.DataFrame(rows)


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    x = pd.to_numeric(equity, errors="raise").astype(float)
    h = x.cummax()
    dd = x / h - 1.0
    return float(dd.min())


def overlap_stats(deep_symbols: set[str], ret_symbols: set[str]) -> dict[str, Any]:
    inter = deep_symbols & ret_symbols
    union = deep_symbols | ret_symbols
    return {
        "deep_active_symbols": len(deep_symbols),
        "ret120_active_symbols": len(ret_symbols),
        "same_symbol_count": len(inter),
        "same_symbols": sorted(inter),
        "symbol_jaccard": len(inter) / len(union) if union else 0.0,
    }


def initial_portfolio_state(policy: StrategyPortfolioPolicy, readiness: PilotReadinessPolicy) -> dict[str, Any]:
    return {
        "status": "WAITING_FOR_COMMON_FORWARD",
        "portfolio_id": policy.portfolio_id,
        "policy_hash": policy.fingerprint(),
        "readiness_policy_hash": readiness.fingerprint(),
        "started_at": None,
        "last_aggregated_date": None,
        "common_sessions": 0,
        "max_drawdown": 0.0,
        "latest_equity": None,
        "latest_gross_weight": None,
        "same_symbol_count": 0,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def readiness_summary(
    common_sessions: int,
    portfolio_sessions: int,
    shadow_order_decisions: int,
    board_lot_fidelity: float | None,
    execution_equivalence_pass: bool,
    risk_recovery_pass: bool,
    pilot_policy_frozen: bool,
    policy: PilotReadinessPolicy,
) -> dict[str, Any]:
    gates = {
        "G1_STRATEGY_EVIDENCE": {"status": "PASS"},
        "G2_TRUE_FORWARD_BURN_IN": {
            "status": "PASS" if common_sessions >= policy.common_forward_burn_in_sessions else "COLLECTING",
            "actual": common_sessions,
            "required": policy.common_forward_burn_in_sessions,
        },
        "G3_PORTFOLIO_SHADOW": {
            "status": "PASS" if portfolio_sessions >= policy.portfolio_shadow_sessions else "COLLECTING",
            "actual": portfolio_sessions,
            "required": policy.portfolio_shadow_sessions,
        },
        "G4_EXECUTION_EQUIVALENCE": {
            "status": "PASS" if execution_equivalence_pass and shadow_order_decisions >= policy.min_shadow_order_decisions else "PENDING",
            "shadow_order_decisions": shadow_order_decisions,
            "required": policy.min_shadow_order_decisions,
        },
        "G5_BOARD_LOT_CAPITAL_FLOOR": {
            "status": "PASS" if board_lot_fidelity is not None and board_lot_fidelity >= policy.board_lot_fidelity_min else "PENDING",
            "fidelity": board_lot_fidelity,
            "required": policy.board_lot_fidelity_min,
        },
        "G6_RISK_AND_RECOVERY": {"status": "PASS" if risk_recovery_pass else "PENDING"},
        "G7_PILOT_POLICY_FREEZE": {"status": "PASS" if pilot_policy_frozen else "PENDING"},
    }
    ready = all(v["status"] == "PASS" for v in gates.values())
    return {
        "policy_id": policy.policy_id,
        "status": "PILOT_LIVE_READY" if ready else "PILOT_LIVE_READINESS_IN_PROGRESS",
        "ready": ready,
        "gates": gates,
        "auto_execute": False,
        "live_mode": policy.live_mode,
    }
