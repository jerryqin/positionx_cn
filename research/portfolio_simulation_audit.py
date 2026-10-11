from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple
import math
import pandas as pd

from research.portfolio_simulation import prepare_fixed_horizon_candidates


@dataclass(frozen=True)
class SimulationAuditConfig:
    annual_sessions: int = 252
    tolerance: float = 1e-8


def candidate_net_return(candidate: Mapping[str, Any], round_trip_cost: float) -> float:
    entry = float(candidate["entry_price"])
    exit_price = float(candidate["exit_price"])
    half = float(round_trip_cost) / 2.0
    return exit_price / entry * (1.0 - half) / (1.0 + half) - 1.0


def _stats(values: Sequence[float]) -> Dict[str, Any]:
    vals = [float(v) for v in values if math.isfinite(float(v))]
    if not vals:
        return {"n": 0, "avg": None, "median": None, "win_rate": None}
    s = pd.Series(vals, dtype="float64")
    return {
        "n": len(vals),
        "avg": float(s.mean()),
        "median": float(s.median()),
        "win_rate": float((s > 0).mean()),
    }


def reconcile_accounting(simulation: Mapping[str, Any], config: SimulationAuditConfig | None = None) -> Dict[str, Any]:
    cfg = config or SimulationAuditConfig()
    sim_cfg = simulation["config"]
    summary = simulation["summary"]
    trades = simulation.get("trades", [])
    curve = simulation.get("equity_curve", [])

    initial_capital = float(sim_cfg["initial_capital"])
    max_positions = int(sim_cfg["max_positions"])
    initial_slot = initial_capital / max_positions

    by_slot: Dict[int, List[Mapping[str, Any]]] = {i: [] for i in range(max_positions)}
    for trade in trades:
        by_slot[int(trade["slot_id"])].append(trade)

    slots = []
    expected_final = 0.0
    for slot_id in range(max_positions):
        factor = 1.0
        rets = []
        for trade in by_slot[slot_id]:
            r = float(trade["net_return"])
            factor *= 1.0 + r
            rets.append(r)
        final_value = initial_slot * factor
        expected_final += final_value
        slots.append({
            "slot_id": slot_id,
            "trades": len(rets),
            "compound_factor": factor,
            "final_value_from_trade_product": final_value,
            "arithmetic_avg_trade_return": (sum(rets) / len(rets)) if rets else None,
            "geometric_avg_trade_return": (factor ** (1.0 / len(rets)) - 1.0) if rets and factor > 0 else None,
        })

    actual_final = float(curve[-1]["equity"]) if curve else initial_capital
    recon_error = actual_final - expected_final

    trade_returns = [float(t["net_return"]) for t in trades]
    all_trade_factor = 1.0
    for r in trade_returns:
        all_trade_factor *= 1.0 + r
    geometric_trade = (
        all_trade_factor ** (1.0 / len(trade_returns)) - 1.0
        if trade_returns and all_trade_factor > 0 else None
    )

    # Correct Sharpe diagnostic: include first-day return from initial capital.
    corrected_sharpe = None
    if curve:
        equities = pd.Series([initial_capital] + [float(r["equity"]) for r in curve], dtype="float64")
        daily = equities.pct_change().dropna()
        if len(daily) > 1:
            sd = float(daily.std(ddof=1))
            if sd > 0:
                corrected_sharpe = float(daily.mean() / sd * math.sqrt(cfg.annual_sessions))

    sessions = int(summary.get("sessions") or 0)
    total_turnover = float(summary.get("turnover") or 0.0)
    annualized_turnover = total_turnover * cfg.annual_sessions / sessions if sessions > 0 else None

    return {
        "accounting_reconciles": abs(recon_error) <= cfg.tolerance * max(1.0, initial_capital),
        "initial_capital": initial_capital,
        "expected_final_equity_from_slot_products": expected_final,
        "actual_final_equity": actual_final,
        "reconciliation_error": recon_error,
        "stored_total_return": summary.get("total_return"),
        "recomputed_total_return": actual_final / initial_capital - 1.0,
        "arithmetic_avg_trade_return": (sum(trade_returns) / len(trade_returns)) if trade_returns else None,
        "geometric_avg_trade_return_all_trades": geometric_trade,
        "stored_sharpe": summary.get("sharpe"),
        "corrected_sharpe_including_first_day": corrected_sharpe,
        "aggregate_turnover": total_turnover,
        "annualized_turnover": annualized_turnover,
        "slots": slots,
    }


def selection_audit(
    history: Mapping[str, pd.DataFrame],
    events: Iterable[Dict[str, Any]],
    simulation: Mapping[str, Any],
) -> Dict[str, Any]:
    sim_cfg = simulation["config"]
    holding_days = int(sim_cfg["holding_days"])
    cost = float(sim_cfg["round_trip_cost"])
    candidates = prepare_fixed_horizon_candidates(history, events, holding_days)

    executed_keys = {
        (
            str(t["symbol"]), str(t["signal_date"]), str(t["entry_date"]), str(t["exit_date"])
        )
        for t in simulation.get("trades", [])
    }

    executed_cf: List[float] = []
    skipped_cf: List[float] = []
    all_cf: List[float] = []
    skipped_rows = []

    for c in candidates:
        key = (str(c["symbol"]), str(c["signal_date"]), str(c["entry_date"]), str(c["exit_date"]))
        r = candidate_net_return(c, cost)
        all_cf.append(r)
        if key in executed_keys:
            executed_cf.append(r)
        else:
            skipped_cf.append(r)
            skipped_rows.append({
                "symbol": c["symbol"],
                "signal_date": c["signal_date"],
                "entry_date": c["entry_date"],
                "exit_date": c["exit_date"],
                "counterfactual_net_return": r,
            })

    return {
        "candidates_reconstructed": len(candidates),
        "executed_keys": len(executed_keys),
        "all_candidate_counterfactual": _stats(all_cf),
        "executed_counterfactual": _stats(executed_cf),
        "skipped_counterfactual": _stats(skipped_cf),
        "executed_minus_skipped_avg": (
            _stats(executed_cf)["avg"] - _stats(skipped_cf)["avg"]
            if executed_cf and skipped_cf else None
        ),
        "note": "Skipped counterfactual combines no-slot and duplicate-symbol skips; it diagnoses selection bias but does not assign skip reason.",
        "skipped_sample": skipped_rows[:50],
    }
