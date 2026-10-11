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

from research.capacity_sweep import (
    DEFAULT_CAPACITIES,
    classify_capacity_shape,
    extract_capacity_point,
    normalize_capacities,
)
from research.fillability_gate import (
    FillabilityConfig,
    apply_fillability_filter,
    load_event_status_rows,
    validate_status_asset,
)
from research.pit_backtest_support import CSI300ReferenceUniverse
from research.pit_portfolio_support import audit_next_market_session, load_market_calendar_from_parquet
from research.portfolio_simulation import BreakoutRetestPortfolioSimulator, PortfolioSimulationConfig
from research.watchlist_adapter import AdapterConfig, XtDataWatchlistAdapter, save_watchlist_snapshot


def simulate_one(history, events, max_positions, args):
    cfg = PortfolioSimulationConfig(
        initial_capital=args.initial_capital,
        max_positions=max_positions,
        holding_days=args.holding_days,
        round_trip_cost=args.round_trip_cost,
    )
    return BreakoutRetestPortfolioSimulator(cfg).simulate(history, events)


def pct(v):
    return "NA" if v is None else f"{100.0 * float(v):.2f}%"


def main() -> int:
    p = argparse.ArgumentParser(description="PositionX BreakoutRetest capacity sweep")
    p.add_argument("--pit-result", default="data/research/backtest_gate_pit_db_latest.json")
    p.add_argument("--fillability-result", default="data/research/fillability_gate_pit_latest.json")
    p.add_argument("--reference-db", default="data/research/positionx_reference.db")
    p.add_argument(
        "--status-parquet",
        default="D:/positionx_data/incoming/eva/20260925_foundation_assets/"
                "data/research/px_assets/a_share_daily_trading_status.parquet",
    )
    p.add_argument("--capacities", default=",".join(str(x) for x in DEFAULT_CAPACITIES))
    p.add_argument("--bars", type=int, default=1700)
    p.add_argument("--initial-capital", type=float, default=1_000_000.0)
    p.add_argument("--holding-days", type=int, default=20)
    p.add_argument("--round-trip-cost", type=float, default=0.002)
    p.add_argument("--output", default="data/research/capacity_sweep_pit_fillable_latest.json")
    args = p.parse_args()

    capacities = normalize_capacities(int(x.strip()) for x in args.capacities.split(",") if x.strip())
    pit_path = PROJECT_ROOT / args.pit_result
    fill_path = PROJECT_ROOT / args.fillability_result
    ref_db = PROJECT_ROOT / args.reference_db
    status_path = Path(args.status_parquet)

    for path in (pit_path, fill_path, ref_db, status_path):
        if not path.exists():
            raise SystemExit(f"required input missing: {path}")

    pit = json.loads(pit_path.read_text(encoding="utf-8"))
    fill = json.loads(fill_path.read_text(encoding="utf-8"))
    if pit.get("status") != "OK" or fill.get("status") != "OK":
        raise SystemExit("PIT or Fillability input is not OK")

    std = fill.get("standard", {}).get("summary", {})
    if std.get("gate", {}).get("status") != "PASS_CANDIDATE":
        raise SystemExit("STANDARD fillability Gate is not PASS_CANDIDATE")
    if std.get("robustness", {}).get("status") != "ROBUST_CANDIDATE":
        raise SystemExit("STANDARD fillability robustness is not ROBUST_CANDIDATE")

    all_events = list(pit.get("events") or [])
    br_events = [e for e in all_events if e.get("strategy") == "BREAKOUT_RETEST"]
    asset = validate_status_asset(
        status_path,
        status_path.with_name("a_share_daily_trading_status.metadata.json"),
        status_path.with_name("a_share_daily_trading_status.verification.json"),
    )
    expected_sha = fill.get("input", {}).get("fillability_asset", {}).get("sha256")
    if expected_sha and expected_sha != asset["sha256"]:
        raise SystemExit("fillability asset SHA changed since Gate run")

    start_date = pit.get("input", {}).get("start_date", "20200801")
    end_date = pit.get("input", {}).get("end_date", "20260924")
    market_dates = load_market_calendar_from_parquet(status_path, start_date, end_date)
    cal_audit = audit_next_market_session(br_events, market_dates)
    if not cal_audit["pass"]:
        raise SystemExit("BLOCKED_ENTRY_CALENDAR_MISMATCH")

    status_rows = load_event_status_rows(br_events, status_path)
    standard_events, standard_audit = apply_fillability_filter(
        br_events, status_rows, FillabilityConfig(conservative_unknown_st=False)
    )
    standard_audit.pop("blocked_events_detail", None)

    expected_kept = fill.get("standard", {}).get("audit", {}).get("kept_events")
    if expected_kept is not None and int(expected_kept) != len(standard_events):
        raise SystemExit("STANDARD event-count drift")

    universe = CSI300ReferenceUniverse(ref_db)
    symbols = universe.union_members(start_date, end_date)

    adapter = XtDataWatchlistAdapter(
        xtdata, AdapterConfig(sector="", bars=args.bars, dividend_type="front")
    )
    history = adapter.history(symbols)
    missing = sorted(s for s in symbols if s not in history or history[s] is None or history[s].empty)
    if missing:
        raise SystemExit(f"BLOCKED_INCOMPLETE_HISTORY: {len(missing)} symbols; sample={missing[:20]}")

    points, raw = [], {}
    for cap in capacities:
        result = simulate_one(history, standard_events, cap, args)
        summary = result["summary"]
        points.append(extract_capacity_point(cap, summary))
        raw[str(cap)] = summary

    shape = classify_capacity_shape(points)
    safety = {
        "uses_broker": False,
        "production_db_opened": False,
        "creates_trading_plan": False,
        "can_submit_order": False,
        "writes_reference_db": False,
        "changes_strategy_parameters": False,
    }

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "RESEARCH_CAPACITY_SWEEP_PIT_FILLABLE",
        "version": "CAPACITY-v1",
        "status": "OK",
        "safety": safety,
        "methodology": {
            "strategy": "BREAKOUT_RETEST_ONLY",
            "universe": "CSI300_POINT_IN_TIME_CANONICAL_REFERENCE_DB",
            "fillability": "STANDARD",
            "entry": "NEXT_MARKET_SESSION_OPEN",
            "holding_days": args.holding_days,
            "round_trip_cost": args.round_trip_cost,
            "capacities": capacities,
            "position_model": "FIXED_CAPITAL_SLOTS_COMPOUND_INDEPENDENTLY",
            "same_day_tie_break": "SYMBOL_ASC",
            "warning": "This is a capacity diagnostic. Do not pick the highest-return K as the optimum.",
        },
        "input": {
            "symbols": len(symbols),
            "history_loaded": len(history),
            "breakout_events": len(br_events),
            "standard_fillable_events": len(standard_events),
            "fillability_asset_sha256": asset["sha256"],
        },
        "calendar_audit": cal_audit,
        "standard_fillability_audit": standard_audit,
        "capacity_points": points,
        "capacity_shape": shape,
        "raw_summaries": raw,
    }

    out_path = PROJECT_ROOT / args.output
    out_path.parent.mkdir(parents=True, exist_ok=True)
    save_watchlist_snapshot(out_path, out)

    print(json.dumps(safety, ensure_ascii=False, indent=2))
    print("\nCAPACITY SWEEP — PIT + STANDARD FILLABILITY")
    print(" cap | executed | coverage | no-slot | cap-skip | total-ret | CAGR | MaxDD | Sharpe | avg-active")
    print("-" * 105)
    for x in points:
        print(
            f"{x['max_positions']:4d} | "
            f"{x['trades_executed']:8d} | "
            f"{pct(x['execution_coverage']):>8} | "
            f"{x['skipped_no_slot']:7d} | "
            f"{pct(x['capacity_skip_rate']):>8} | "
            f"{pct(x['total_return']):>9} | "
            f"{pct(x['cagr_calendar']):>7} | "
            f"{pct(x['max_drawdown']):>7} | "
            f"{float(x['sharpe'] or 0):6.3f} | "
            f"{float(x['avg_active_positions'] or 0):10.2f}"
        )

    print("\nDESCRIPTIVE CAPACITY SHAPE:")
    print(json.dumps(shape, ensure_ascii=False, indent=2))
    print(f"\nOUTPUT: {out_path}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
