from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_P3 = Path(os.environ.get(
    "POSITIONX_RET120_DEEP_P3",
    r"D:\positionx_data\research\ret120_vs_deep_incremental_v0_1\incremental_value_summary_v01.json",
))
DEFAULT_P1_FAMILY = Path(os.environ.get(
    "POSITIONX_RET120_FAMILY_P1",
    r"D:\positionx_data\research\ret120_vs_52wh_family_v0_2_frozen_signal\family_comparison_summary.json",
))
DEFAULT_OUT = Path(os.environ.get(
    "POSITIONX_STRATEGY2_OUT",
    r"D:\positionx_data\research\strategy2_ret120_qualification_v0_1",
))

EXPECTED_P3 = "PASS_RET120_VS_DEEP_INCREMENTAL_VALUE_AUDIT_V0_1"
EXPECTED_FAMILY = "PASS_RET120_VS_52WH_FAMILY_COMPARISON_V0_2_FROZEN_SIGNAL"
EXPECTED_REP = "RET120_H120"
EXPECTED_P1 = "PASS_RET120_FROZEN_SIGNAL_COMPARABLE_PORTFOLIO_AUDIT_V0_2"
EXPECTED_P0 = "PASS_RET120_FROZEN_SIGNAL_LINEAGE_V0_1"
EXPECTED_DEEP = "PASS_DEEP_OVERSOLD_ECONOMIC_EQUIVALENCE_V0_1_3"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(f"FAIL_CLOSED missing artifact: {path}")
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise RuntimeError(f"FAIL_CLOSED invalid json: {path}: {exc}") from exc
    if not isinstance(obj, dict):
        raise RuntimeError(f"FAIL_CLOSED expected object json: {path}")
    return obj


def write_json_atomic(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def require(cond: bool, msg: str) -> None:
    if not cond:
        raise RuntimeError("FAIL_CLOSED " + msg)


def classify(p3: dict[str, Any], family: dict[str, Any]) -> dict[str, Any]:
    require(p3.get("status") == EXPECTED_P3, "unexpected P3 status")
    require(family.get("status") == EXPECTED_FAMILY, "unexpected family comparison status")

    decision = p3.get("trend_family_decision") or {}
    require(decision.get("representative") == EXPECTED_REP, "RET120 is not frozen trend-family representative")

    lineage = p3.get("lineage") or {}
    require(lineage.get("ret120_p1_status") == EXPECTED_P1, "P1 lineage status mismatch")
    require(lineage.get("ret120_p0_status") == EXPECTED_P0, "P0 lineage status mismatch")
    require(lineage.get("deep_status") == EXPECTED_DEEP, "Deep lineage status mismatch")

    deltas = ((p3.get("delta_fixed_50_50") or {}).get("vs_deep") or {})
    dcagr = float(deltas.get("cagr"))
    dmdd = float(deltas.get("max_drawdown"))
    dsharpe = float(deltas.get("sharpe"))

    # This is the already-used PositionX STRONG_PASS logic from the earlier
    # incremental-value work: higher CAGR, higher Sharpe, and non-worse MDD.
    # No new outcome-tuned numeric threshold is introduced here.
    strong_pass = dcagr > 0.0 and dsharpe > 0.0 and dmdd >= 0.0
    require(strong_pass, "fixed 50/50 incremental STRONG_PASS failed")

    rnd = p3.get("random_selection_sensitivity") or {}
    corr = p3.get("correlation_and_drawdown") or {}
    exec_ov = p3.get("executed_trade_overlap") or {}
    hold_ov = p3.get("holding_overlap") or {}

    return {
        "status": "QUALIFIED_STRATEGY_2_HISTORICAL_EVIDENCE_V0_1",
        "strategy_number": 2,
        "strategy_id": "RET120_H120",
        "family": "trend_underreaction",
        "family_representative": EXPECTED_REP,
        "family_alternate": "52WH_H120_FROZEN_BENCHMARK",
        "qualification_basis": {
            "fixed_50_50_rule_was_frozen_before_result": True,
            "strong_pass_rule": "dCAGR>0 and dSharpe>0 and dMDD>=0",
            "delta_vs_deep": {
                "cagr": dcagr,
                "max_drawdown": dmdd,
                "sharpe": dsharpe,
            },
        },
        "supporting_independence_evidence_not_new_numeric_gate": {
            "daily_return_correlation": corr.get("daily_return_correlation"),
            "drawdown_correlation": corr.get("drawdown_correlation"),
            "executed_trade_intersection": exec_ov.get("intersection"),
            "executed_trade_jaccard": exec_ov.get("jaccard"),
            "holding_mean_symbol_jaccard": hold_ov.get("mean_symbol_jaccard"),
            "random_fraction_combo_cagr_gt_deep": rnd.get("fraction_seeds_combo_cagr_gt_deep"),
            "random_fraction_combo_mdd_better_than_deep": rnd.get("fraction_seeds_combo_mdd_better_than_deep"),
            "random_fraction_combo_sharpe_gt_deep": rnd.get("fraction_seeds_combo_sharpe_gt_deep"),
        },
        "important_interpretation": [
            "Qualification is historical/portfolio evidence, not Forward approval.",
            "RET120 and 52WH remain one trend family; only RET120 consumes the Strategy #2 slot.",
            "The 50/50 result is a fixed diversification diagnostic, not an optimized production allocation.",
        ],
    }


def forward_design() -> dict[str, Any]:
    return {
        "status": "FORWARD_PROTOCOL_DESIGN_FROZEN_PENDING_EXECUTION_SEMANTICS_AUDIT_V0_1",
        "strategy_id": "RET120_CSI500_FORWARD_V1_0",
        "purpose": "research Forward collection only; no broker and no production execution",
        "signal": {
            "universe": "CSI500 observed PIT/current exact-500 membership",
            "feature": "ret120 = back_ratio close / back_ratio close.shift(120) - 1",
            "signal_time": "T CLOSE after completed session",
            "source_representation": "QMT back_ratio observed as-of T; persist snapshot so later history drift cannot rewrite signal",
            "feature_min_coverage": "at least 80% of the 500-member cross-section (>=400 valid ret120 values)",
            "tie_semantics": "sort symbol ASC, then pandas rank(method='first')",
            "bucket_semantics": "pd.qcut(rank, 5, labels=Q1..Q5)",
            "selected_bucket": "Q5",
            "future_labels_used": False,
            "immutable_daily_snapshot": True,
        },
        "entry_exit_portfolio": {
            "entry": "S1 / T+1 exact OPEN",
            "exit_historical_reference": "S120 exact CLOSE",
            "target_weight_per_position": 0.01,
            "max_target_gross_at_entry": 0.30,
            "max_positions": 30,
            "one_position_per_symbol": True,
            "existing_positions_rebalanced": False,
            "same_day_close_exit_reusable_at_open": False,
            "same_day_candidate_tie_break": "SYMBOL_ASC",
            "fractional_shares": "research only",
            "main_round_trip_cost": 0.002,
            "stress_round_trip_cost": 0.005,
        },
        "true_forward_integrity": {
            "retro_snapshot": "FORBIDDEN",
            "missed_session": "FAIL_CLOSED; do not reconstruct later with today's universe/status",
            "start_state": "flat research cohort; do not invent carry positions from incomplete historical outcomes",
            "session_persistence": "atomic session commit required",
            "persist_signal_values_and_bucket_membership": True,
            "production_order_submit": False,
        },
        "must_resolve_before_forward_collection": [
            {
                "id": "ENTRY_FILLABILITY_EQUIVALENCE",
                "why": "P1 excluded signal/entry suspension, known ST/new-listing special and non-buyable open; Forward must map observed QMT fields to the same economic meaning.",
                "status": "PENDING_AUDIT",
            },
            {
                "id": "S120_EXIT_SUSPENSION_POLICY",
                "why": "Historical complete-outcome cohort required exact S120 close; a true Forward position cannot be silently dropped if S120 is suspended/missing.",
                "status": "PENDING_PREDECLARATION_AND_ROBUSTNESS_CHECK",
            },
            {
                "id": "FORWARD_REVIEW_GATE",
                "why": "120-session holding makes the Deep 60-trades/3-month gate structurally inappropriate; a RET120-specific gate must be frozen before collection starts.",
                "status": "PENDING_PREDECLARATION",
            },
        ],
        "scheduler": "NOT_CREATED",
        "broker": "DISABLED",
        "auto_execute": False,
    }


def run(p3_path: Path, family_path: Path, out_dir: Path) -> int:
    p3 = read_json(p3_path)
    fam = read_json(family_path)
    q = classify(p3, fam)
    design = forward_design()

    q["source_artifacts"] = {
        "p3": str(p3_path),
        "p3_sha256": sha256_file(p3_path),
        "p1_family": str(family_path),
        "p1_family_sha256": sha256_file(family_path),
    }
    q["forward_status"] = "NOT_STARTED_PENDING_EXECUTION_SEMANTICS_AUDIT"

    write_json_atomic(out_dir / "strategy2_qualification_v01.json", q)
    write_json_atomic(out_dir / "ret120_forward_protocol_design_v01.json", design)

    result = {
        "status": "PASS_RET120_STRATEGY2_QUALIFICATION_AND_FORWARD_DESIGN_V0_1",
        "qualification": q,
        "forward_design": design,
        "output_dir": str(out_dir),
        "no_network": True,
        "no_qmt": True,
        "no_broker": True,
        "no_production_db": True,
        "no_database_mutation": True,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--p3", default=str(DEFAULT_P3))
    p.add_argument("--family", default=str(DEFAULT_P1_FAMILY))
    p.add_argument("--out", default=str(DEFAULT_OUT))
    args = p.parse_args()
    try:
        return run(Path(args.p3), Path(args.family), Path(args.out))
    except Exception as exc:
        print(json.dumps({
            "status": "FAIL_CLOSED_RET120_STRATEGY2_QUALIFICATION_V0_1",
            "error": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False, indent=2))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
