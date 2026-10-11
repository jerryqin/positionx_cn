from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from xtquant import xtdata

from research.fillability_gate import (
    FillabilityConfig,
    apply_fillability_filter,
    load_event_status_rows,
    validate_status_asset,
)
from research.pit_backtest_support import CSI300ReferenceUniverse
from research.pit_portfolio_support import (
    add_capacity_rates,
    audit_next_market_session,
    load_market_calendar_from_parquet,
)
from research.portfolio_simulation import (
    BreakoutRetestPortfolioSimulator,
    PortfolioSimulationConfig,
)
from research.watchlist_adapter import AdapterConfig, XtDataWatchlistAdapter, save_watchlist_snapshot


def _simulate(history, events, args):
    cfg = PortfolioSimulationConfig(
        initial_capital=args.initial_capital,
        max_positions=args.max_positions,
        holding_days=args.holding_days,
        round_trip_cost=args.round_trip_cost,
    )
    result = BreakoutRetestPortfolioSimulator(cfg).simulate(history, events)
    add_capacity_rates(result["summary"])
    return result


def _brief(label, result):
    s = result["summary"]
    print(f"{label}:")
    print(
        f"  period={s['start_date']}..{s['end_date']} sessions={s['sessions']} "
        f"trades={s['trades_executed']}/{s['signals_executable']}"
    )
    print(
        f"  total_return={s['total_return']:.4f} "
        f"cagr_calendar={s['cagr_calendar_basis']:.4f} "
        f"max_drawdown={s['max_drawdown']:.4f} "
        f"sharpe={s['sharpe_actual_sessions_including_first_day']:.4f}"
    )
    print(
        f"  avg_exposure={s['avg_exposure']:.4f} "
        f"avg_active={s['avg_active_positions']:.2f} "
        f"max_concurrent={s['max_concurrent_positions']}"
    )
    print(
        f"  skipped_no_slot={s['skipped_no_slot']} "
        f"skipped_duplicate={s['skipped_duplicate_symbol']} "
        f"capacity_skip_rate={s['capacity_skip_rate']:.4f} "
        f"total_skip_rate={s['total_skip_rate']:.4f}"
    )


def main() -> int:
    p = argparse.ArgumentParser(
        description="PositionX BreakoutRetest PIT + Fillability Portfolio Capacity Baseline"
    )
    p.add_argument("--pit-result", default="data/research/backtest_gate_pit_db_latest.json")
    p.add_argument("--fillability-result", default="data/research/fillability_gate_pit_latest.json")
    p.add_argument("--reference-db", default="data/research/positionx_reference.db")
    p.add_argument(
        "--status-parquet",
        default=(
            "D:/positionx_data/incoming/eva/20260925_foundation_assets/"
            "data/research/px_assets/a_share_daily_trading_status.parquet"
        ),
    )
    p.add_argument("--bars", type=int, default=1700)
    p.add_argument("--initial-capital", type=float, default=1_000_000.0)
    p.add_argument("--max-positions", type=int, default=10)
    p.add_argument("--holding-days", type=int, default=20)
    p.add_argument("--round-trip-cost", type=float, default=0.002)
    p.add_argument("--output", default="data/research/portfolio_simulation_pit_fillable_latest.json")
    args = p.parse_args()

    pit_path = PROJECT_ROOT / args.pit_result
    fill_path = PROJECT_ROOT / args.fillability_result
    ref_db = PROJECT_ROOT / args.reference_db
    status_path = Path(args.status_parquet)

    if not pit_path.exists():
        raise SystemExit(f"PIT result missing: {pit_path}")
    if not fill_path.exists():
        raise SystemExit(f"Fillability result missing: {fill_path}")

    pit = json.loads(pit_path.read_text(encoding="utf-8"))
    fill = json.loads(fill_path.read_text(encoding="utf-8"))
    if pit.get("status") != "OK":
        raise SystemExit(f"PIT result not OK: {pit.get('status')}")
    if fill.get("status") != "OK":
        raise SystemExit(f"Fillability result not OK: {fill.get('status')}")

    std_gate = (
        fill.get("standard", {})
        .get("summary", {})
    )
    if std_gate.get("gate", {}).get("status") != "PASS_CANDIDATE":
        raise SystemExit("STANDARD fillability result is not PASS_CANDIDATE")
    if std_gate.get("robustness", {}).get("status") != "ROBUST_CANDIDATE":
        raise SystemExit("STANDARD fillability result is not ROBUST_CANDIDATE")

    all_events = list(pit.get("events") or [])
    br_events = [e for e in all_events if e.get("strategy") == "BREAKOUT_RETEST"]
    if not br_events:
        raise SystemExit("No BREAKOUT_RETEST events in PIT result")

    asset = validate_status_asset(
        status_path,
        status_path.with_name("a_share_daily_trading_status.metadata.json"),
        status_path.with_name("a_share_daily_trading_status.verification.json"),
    )
    expected_sha = fill.get("input", {}).get("fillability_asset", {}).get("sha256")
    if expected_sha and expected_sha != asset["sha256"]:
        raise SystemExit(
            f"Fillability asset SHA changed since Gate run: gate={expected_sha} current={asset['sha256']}"
        )

    market_dates = load_market_calendar_from_parquet(
        status_path,
        pit.get("input", {}).get("start_date", "20200801"),
        pit.get("input", {}).get("end_date", "20260924"),
    )
    calendar_audit = audit_next_market_session(br_events, market_dates)
    if not calendar_audit["pass"]:
        out = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "status": "BLOCKED_ENTRY_CALENDAR_MISMATCH",
            "calendar_audit": calendar_audit,
        }
        out_path = PROJECT_ROOT / args.output
        out_path.parent.mkdir(parents=True, exist_ok=True)
        save_watchlist_snapshot(out_path, out)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 2

    status_rows = load_event_status_rows(br_events, status_path)
    standard_events, standard_audit = apply_fillability_filter(
        br_events, status_rows, FillabilityConfig(conservative_unknown_st=False)
    )
    conservative_events, conservative_audit = apply_fillability_filter(
        br_events, status_rows, FillabilityConfig(conservative_unknown_st=True)
    )
    standard_audit.pop("blocked_events_detail", None)
    conservative_audit.pop("blocked_events_detail", None)

    expected_kept = fill.get("standard", {}).get("audit", {}).get("kept_events")
    if expected_kept is not None and int(expected_kept) != len(standard_events):
        raise SystemExit(
            f"STANDARD event-count drift: fillability_gate={expected_kept} now={len(standard_events)}"
        )

    universe = CSI300ReferenceUniverse(ref_db)
    start_date = pit.get("input", {}).get("start_date", "20200801")
    end_date = pit.get("input", {}).get("end_date", "20260924")
    symbols = universe.union_members(start_date, end_date)

    adapter = XtDataWatchlistAdapter(
        xtdata,
        AdapterConfig(sector="", bars=args.bars, dividend_type="front"),
    )
    history = adapter.history(symbols)
    missing = sorted(s for s in symbols if s not in history or history[s] is None or history[s].empty)
    if missing:
        raise SystemExit(f"BLOCKED_INCOMPLETE_HISTORY: {len(missing)} symbols missing; sample={missing[:20]}")

    standard_result = _simulate(history, standard_events, args)
    conservative_result = _simulate(history, conservative_events, args)

    safety = {
        "uses_broker": False,
        "production_db_opened": False,
        "creates_trading_plan": False,
        "can_submit_order": False,
        "writes_reference_db": False,
    }
    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "RESEARCH_PORTFOLIO_CAPACITY_BASELINE",
        "version": "PIT-FILLABLE-v1",
        "status": "OK",
        "safety": safety,
        "methodology": {
            "strategy": "BREAKOUT_RETEST_ONLY",
            "universe": "CSI300_POINT_IN_TIME_CANONICAL_REFERENCE_DB",
            "fillability": "STANDARD primary + CONSERVATIVE_UNKNOWN_ST sensitivity",
            "entry": "NEXT_MARKET_SESSION_OPEN",
            "exit": f"SIGNAL_PLUS_{args.holding_days}_BAR_CLOSE",
            "max_positions": args.max_positions,
            "round_trip_cost": args.round_trip_cost,
            "position_model": "FIXED_CAPITAL_SLOTS_COMPOUND_INDEPENDENTLY",
            "position_model_reason": (
                "Kept unchanged from the audited v1.1 baseline so this run isolates "
                "the effect of PIT universe + fillability. This is a capacity baseline, "
                "not the final PositionX sizing model."
            ),
            "same_day_tie_break": "SYMBOL_ASC",
            "tie_break_warning": (
                "Engineering-only deterministic ordering; allocator design remains unresolved."
            ),
            "no_stop_loss": True,
            "no_take_profit": True,
            "no_regime_filter": True,
            "no_dynamic_position_sizing": True,
        },
        "input": {
            "pit_result": str(pit_path),
            "fillability_result": str(fill_path),
            "reference_db": str(ref_db),
            "reference_fingerprint_sha256": universe.source_sha256,
            "fillability_asset": asset,
            "symbols": len(symbols),
            "history_loaded": len(history),
            "breakout_events": len(br_events),
            "standard_fillable_events": len(standard_events),
            "conservative_fillable_events": len(conservative_events),
        },
        "calendar_audit": calendar_audit,
        "standard_fillability_audit": standard_audit,
        "conservative_fillability_audit": conservative_audit,
        "standard": standard_result,
        "conservative_unknown_st": conservative_result,
    }

    out_path = PROJECT_ROOT / args.output
    out_path.parent.mkdir(parents=True, exist_ok=True)
    save_watchlist_snapshot(out_path, out)

    print(json.dumps(safety, ensure_ascii=False, indent=2))
    print("\nENTRY CALENDAR AUDIT:")
    print(json.dumps(calendar_audit, ensure_ascii=False, indent=2))
    print("\nINPUT:")
    print(json.dumps({
        "symbols": len(symbols),
        "history_loaded": len(history),
        "breakout_events": len(br_events),
        "standard_fillable_events": len(standard_events),
        "conservative_fillable_events": len(conservative_events),
        "fillability_asset_sha256": asset["sha256"],
    }, ensure_ascii=False, indent=2))

    print("\nPORTFOLIO CAPACITY BASELINE — PIT + FILLABILITY:")
    _brief("STANDARD", standard_result)
    _brief("CONSERVATIVE_ST", conservative_result)
    print(f"\nOUTPUT: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
