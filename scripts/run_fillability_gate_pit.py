from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research.backtest_gate import BacktestGateConfig, summarize_strategy
from research.fillability_gate import (
    FillabilityConfig,
    apply_fillability_filter,
    load_event_status_rows,
    outcome_stats,
    validate_status_asset,
)


def _fmt(v, digits=4):
    if v is None:
        return "N/A"
    try:
        return f"{float(v):.{digits}f}"
    except Exception:
        return str(v)


def _print_summary(label, summary):
    h = summary["horizons"]["20"]
    st = summary["stability"]
    rob = summary["robustness"]
    print(
        f"{label:24s} status={summary['gate']['status']:20s} "
        f"events={h['valid_events']:4d} win20={_fmt(h['win_rate'])} "
        f"avg20={_fmt(h['avg_return'])} median20={_fmt(h['median_return'])} "
        f"net_avg20={_fmt(h['net_avg_return'])} pf={_fmt(h['profit_factor'],3)} "
        f"positive_folds={st['positive_folds']}/{st['folds_requested']} "
        f"robustness={rob['status']}"
    )
    for fold in st["folds"]:
        print(
            f"  F{fold['fold']} {fold['start_date']}..{fold['end_date']} "
            f"events={fold['valid_events']} avg={_fmt(fold['avg_return'])} "
            f"net_avg={_fmt(fold['net_avg_return'])} pf={_fmt(fold['profit_factor'],3)}"
        )


def main() -> int:
    p = argparse.ArgumentParser(
        description="PositionX BreakoutRetest PIT + Fillability Gate v1"
    )
    p.add_argument(
        "--pit-result",
        default="data/research/backtest_gate_pit_db_latest.json",
    )
    p.add_argument(
        "--status-parquet",
        default=(
            "D:/positionx_data/incoming/eva/20260925_foundation_assets/"
            "data/research/px_assets/a_share_daily_trading_status.parquet"
        ),
    )
    p.add_argument("--output", default="data/research/fillability_gate_pit_latest.json")
    args = p.parse_args()

    pit_path = PROJECT_ROOT / args.pit_result
    if not pit_path.exists():
        raise SystemExit(f"PIT result missing: {pit_path}")
    pit = json.loads(pit_path.read_text(encoding="utf-8"))
    if pit.get("status") != "OK":
        raise SystemExit(f"PIT result not OK: {pit.get('status')}")
    if not pit.get("methodology", {}).get("universe_is_point_in_time"):
        raise SystemExit("PIT result is not point-in-time; refusing fillability validation")

    all_events = list(pit.get("events") or [])
    br_events = [e for e in all_events if e.get("strategy") == "BREAKOUT_RETEST"]
    if not br_events:
        raise SystemExit("No BREAKOUT_RETEST events in PIT result")

    status_path = Path(args.status_parquet)
    meta_path = status_path.with_name("a_share_daily_trading_status.metadata.json")
    verify_path = status_path.with_name("a_share_daily_trading_status.verification.json")
    asset = validate_status_asset(
        status_path,
        meta_path if meta_path.exists() else None,
        verify_path if verify_path.exists() else None,
    )

    status_rows = load_event_status_rows(br_events, status_path)

    standard_events, standard_audit = apply_fillability_filter(
        br_events,
        status_rows,
        FillabilityConfig(conservative_unknown_st=False),
    )
    conservative_events, conservative_audit = apply_fillability_filter(
        br_events,
        status_rows,
        FillabilityConfig(conservative_unknown_st=True),
    )

    cfg = BacktestGateConfig(min_positive_folds=2)
    baseline = summarize_strategy("BREAKOUT_RETEST", br_events, cfg)
    standard_summary = summarize_strategy("BREAKOUT_RETEST", standard_events, cfg)
    conservative_summary = summarize_strategy("BREAKOUT_RETEST", conservative_events, cfg)

    blocked_standard = standard_audit.pop("blocked_events_detail")
    blocked_conservative = conservative_audit.pop("blocked_events_detail")

    safety = {
        "uses_broker": False,
        "production_db_opened": False,
        "creates_trading_plan": False,
        "can_submit_order": False,
        "writes_reference_db": False,
    }

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "RESEARCH_BREAKOUT_RETEST_PIT_FILLABILITY_GATE",
        "version": "FILLABILITY-v1",
        "status": "OK",
        "safety": safety,
        "methodology": {
            "strategy": "BREAKOUT_RETEST_ONLY",
            "signal_source": "backtest_gate_pit_db_latest.json",
            "universe": "CSI300_POINT_IN_TIME_CANONICAL_REFERENCE_DB",
            "entry": "NEXT_SESSION_OPEN",
            "standard_rule": (
                "BLOCK missing status, suspension, new-listing-special, supplied limit-surface "
                "inconsistency, or can_buy_at_open!=True"
            ),
            "conservative_unknown_st_rule": (
                "STANDARD + for MAIN board with st_known=false, block if open >= hypothetical 5% ST up-limit"
            ),
            "interpretation": (
                "STANDARD is the primary estimate from the supplied asset; CONSERVATIVE_UNKNOWN_ST is a lower-bound stress test."
            ),
            "remaining_limitations": [
                "unknown Shanghai/BSE ST history is not fully resolved",
                "open-price fill assumes execution at official open when row is classified fillable",
                "events remain cross-sectionally and temporally correlated",
            ],
        },
        "input": {
            "pit_result": str(pit_path),
            "pit_reference_fingerprint_sha256": pit.get("input", {}).get("reference_fingerprint_sha256"),
            "fillability_asset": asset,
            "breakout_events_with_forward_outcomes": len(br_events),
        },
        "baseline": {
            "summary": baseline,
            "outcome20": outcome_stats(br_events),
        },
        "standard": {
            "audit": standard_audit,
            "blocked_outcome20": outcome_stats(blocked_standard),
            "summary": standard_summary,
        },
        "conservative_unknown_st": {
            "audit": conservative_audit,
            "blocked_outcome20": outcome_stats(blocked_conservative),
            "summary": conservative_summary,
        },
        "blocked_standard_events": blocked_standard,
        "blocked_conservative_extra_events": [
            e for e in blocked_conservative
            if e.get("fillability", {}).get("reason") == "UNKNOWN_MAIN_ST_AT_OR_ABOVE_5PCT"
        ],
    }

    output_path = PROJECT_ROOT / args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(output_path)

    print(json.dumps(safety, ensure_ascii=False, indent=2))
    print(json.dumps({
        "fillability_asset": str(status_path),
        "asset_sha256": asset["sha256"],
        "asset_rows": asset["rows"],
        "asset_window": [asset["start_date"], asset["end_date"]],
        "breakout_events_with_forward_outcomes": len(br_events),
    }, ensure_ascii=False, indent=2))

    print("\nFILLABILITY AUDIT:")
    print("STANDARD:", json.dumps(standard_audit, ensure_ascii=False, indent=2))
    print("STANDARD_BLOCKED_COUNTERFACTUAL_20D:", json.dumps(outcome_stats(blocked_standard), ensure_ascii=False))
    print("CONSERVATIVE_UNKNOWN_ST:", json.dumps(conservative_audit, ensure_ascii=False, indent=2))
    print("CONSERVATIVE_EXTRA_BLOCKED_20D:", json.dumps(
        outcome_stats(out["blocked_conservative_extra_events"]), ensure_ascii=False
    ))

    print("\nBACKTEST GATE — PIT + FILLABILITY v1:")
    _print_summary("PIT_BASELINE", baseline)
    _print_summary("STANDARD_FILLABLE", standard_summary)
    _print_summary("CONSERVATIVE_ST", conservative_summary)

    print(f"\nOUTPUT: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
