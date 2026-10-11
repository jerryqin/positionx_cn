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

from research.backtest_gate import (
    BacktestGateConfig,
    attach_forward_outcomes,
    breakout_retest_events,
    ma60_support_events,
    summarize_strategy,
)
from research.pit_backtest_support import (
    CSI300ReferenceUniverse,
    PITCandidateConfig,
    filter_events_point_in_time,
)
from research.watchlist_adapter import (
    AdapterConfig,
    XtDataWatchlistAdapter,
    save_watchlist_snapshot,
)


def _fmt(value, digits=4):
    if value is None:
        return "N/A"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return str(value)
    if value == float("inf"):
        return "inf"
    if value == float("-inf"):
        return "-inf"
    return f"{value:.{digits}f}"


def main() -> int:
    p = argparse.ArgumentParser(
        description="PositionX Backtest Gate PIT DB v1 — CSI300 canonical reference database"
    )
    p.add_argument(
        "--reference-db",
        default="data/research/positionx_reference.db",
    )
    p.add_argument("--start-date", default="20200801")
    p.add_argument("--end-date", default="20260924")
    p.add_argument("--bars", type=int, default=1700)
    p.add_argument("--refresh-history", action="store_true")
    p.add_argument("--history-calendar-days", type=int, default=3000)
    p.add_argument("--sample", type=int, default=0, help="0=全部PIT历史成员")
    p.add_argument("--min-price", type=float, default=2.0)
    p.add_argument("--min-avg-amount20", type=float, default=50_000_000.0)
    p.add_argument(
        "--allow-incomplete-history",
        action="store_true",
        help="仅用于诊断；正式PIT验证不建议启用",
    )
    p.add_argument(
        "--output",
        default="data/research/backtest_gate_pit_db_latest.json",
    )
    args = p.parse_args()

    db_path = PROJECT_ROOT / args.reference_db
    universe = CSI300ReferenceUniverse(db_path)
    universe.validate_requested_range(args.start_date, args.end_date)

    symbols = universe.union_members(args.start_date, args.end_date)
    if args.sample > 0:
        symbols = symbols[: args.sample]

    adapter = XtDataWatchlistAdapter(
        xtdata,
        AdapterConfig(sector="", bars=args.bars, dividend_type="front"),
    )
    if args.refresh_history:
        adapter.refresh_history(symbols, calendar_days=args.history_calendar_days)

    history = adapter.history(symbols)
    missing = [
        s for s in symbols
        if s not in history or history[s] is None or history[s].empty
    ]

    safety = {
        "uses_broker": False,
        "production_db_opened": False,
        "creates_trading_plan": False,
        "can_submit_order": False,
    }

    if missing and not args.allow_incomplete_history:
        blocked = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "mode": "RESEARCH_BACKTEST_GATE_PIT_DB",
            "version": "PIT-DB-v1",
            "status": "BLOCKED_INCOMPLETE_HISTORY",
            "safety": safety,
            "input": {
                "reference_db": str(db_path),
                "dataset_id": universe.dataset_id,
                "reference_fingerprint_sha256": universe.source_sha256,
                "membership_intervals": universe.interval_count,
                "pit_union_symbols": len(symbols),
                "history_loaded": len(history),
                "missing_history_count": len(missing),
                "missing_history_symbols": missing,
            },
        }
        path = save_watchlist_snapshot(PROJECT_ROOT / args.output, blocked)
        print(json.dumps(blocked, ensure_ascii=False, indent=2))
        print(f"\nOUTPUT: {path}")
        return 2

    raw_br = []
    raw_ms = []
    for symbol in symbols:
        df = history.get(symbol)
        if df is None or df.empty:
            continue
        raw_br.extend(breakout_retest_events(symbol, df))
        raw_ms.extend(ma60_support_events(symbol, df))

    pit_cfg = PITCandidateConfig(
        min_price=args.min_price,
        min_avg_amount20=args.min_avg_amount20,
    )

    br, br_audit = filter_events_point_in_time(
        history,
        raw_br,
        universe,
        pit_cfg,
        start_date=args.start_date,
        end_date=args.end_date,
    )
    ms, ms_audit = filter_events_point_in_time(
        history,
        raw_ms,
        universe,
        pit_cfg,
        start_date=args.start_date,
        end_date=args.end_date,
    )

    cfg = BacktestGateConfig(min_positive_folds=2)
    all_events = []
    for event in br:
        df = history.get(event["symbol"])
        if df is not None:
            all_events.extend(attach_forward_outcomes(df, [event], cfg.horizons))
    for event in ms:
        df = history.get(event["symbol"])
        if df is not None:
            all_events.extend(attach_forward_outcomes(df, [event], cfg.horizons))

    summaries = {
        name: summarize_strategy(name, all_events, cfg)
        for name in ("BREAKOUT_RETEST", "MA60_SUPPORT")
    }

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "RESEARCH_BACKTEST_GATE_PIT_DB",
        "version": "PIT-DB-v1",
        "status": "OK",
        "safety": safety,
        "methodology": {
            "universe": "CSI300_POINT_IN_TIME_CANONICAL_REFERENCE_DB",
            "universe_is_point_in_time": True,
            "current_membership_lookahead_removed": True,
            "forced_current_holdings_in_historical_universe": False,
            "membership_interval_semantics": (
                "valid_from<=d AND (valid_to_exclusive IS NULL OR valid_to_exclusive>d)"
            ),
            "candidate_gate": (
                f"CSI300_MEMBER_AND_CLOSE>={args.min_price}"
                f"_AND_AVG_AMOUNT20>={args.min_avg_amount20}"
            ),
            "candidate_gate_semantics": (
                "Historical price + historical liquidity only; no trend/momentum/near-high blocker."
            ),
            "entry": "NEXT_SESSION_OPEN",
            "horizons": list(cfg.horizons),
            "mfe_mae_horizon": max(cfg.horizons),
            "ma60_event_gap": cfg.ma60_event_gap,
            "gate_basis": "EXPECTANCY_PLUS_STABILITY_PLUS_DISTRIBUTION_ROBUSTNESS",
            "assumed_round_trip_cost": cfg.assumed_round_trip_cost,
            "remaining_limitations": [
                "exact_fillability_not_yet_filtered",
                "events_are_cross_sectionally_and_temporally_correlated",
            ],
        },
        "input": {
            "reference_db": str(db_path),
            "dataset_id": universe.dataset_id,
            "reference_fingerprint_sha256": universe.source_sha256,
            "membership_intervals": universe.interval_count,
            "membership_current_members": universe.current_member_count,
            "pit_distinct_symbols": universe.distinct_symbol_count,
            "pit_trade_dates": universe.trade_date_count,
            "start_date": args.start_date,
            "end_date": args.end_date,
            "pit_union_symbols": len(symbols),
            "history_loaded": len(history),
            "missing_history_count": len(missing),
            "missing_history_symbols": missing,
            "bars_requested": args.bars,
            "history_refreshed": bool(args.refresh_history),
            "history_calendar_days": int(args.history_calendar_days) if args.refresh_history else None,
        },
        "raw_signal_counts": {
            "BREAKOUT_RETEST": len(raw_br),
            "MA60_SUPPORT": len(raw_ms),
        },
        "pit_signal_counts": {
            "BREAKOUT_RETEST": len(br),
            "MA60_SUPPORT": len(ms),
        },
        "pit_filter_audit": {
            "BREAKOUT_RETEST": br_audit,
            "MA60_SUPPORT": ms_audit,
        },
        "strategy_summary": summaries,
        "events": all_events,
    }

    path = save_watchlist_snapshot(PROJECT_ROOT / args.output, out)

    print(json.dumps(out["safety"], ensure_ascii=False, indent=2))
    print(json.dumps(out["methodology"], ensure_ascii=False, indent=2))
    print(json.dumps(out["input"], ensure_ascii=False, indent=2))
    print(json.dumps(out["raw_signal_counts"], ensure_ascii=False, indent=2))
    print(json.dumps(out["pit_signal_counts"], ensure_ascii=False, indent=2))
    print("PIT REJECTIONS:")
    print(json.dumps({
        "BREAKOUT_RETEST": br_audit["rejection_counts"],
        "MA60_SUPPORT": ms_audit["rejection_counts"],
    }, ensure_ascii=False, indent=2))

    print("\nBACKTEST GATE PIT DB v1:")
    for name, s in summaries.items():
        h = s["horizons"][str(cfg.gate_horizon)]
        st = s["stability"]
        rob = s["robustness"]
        print(
            f"{name:16s} status={s['gate']['status']:20s} "
            f"events={h['valid_events']:4d} "
            f"win20={_fmt(h['win_rate'])} "
            f"avg20={_fmt(h['avg_return'])} "
            f"median20={_fmt(h['median_return'])} "
            f"net_avg20={_fmt(h['net_avg_return'])} "
            f"pf={_fmt(h['profit_factor'], 3)} "
            f"positive_folds={st['positive_folds']}/{st['folds_requested']} "
            f"robustness={rob['status']}"
        )
        for fold in st["folds"]:
            print(
                f"  F{fold['fold']} {fold['start_date']}..{fold['end_date']} "
                f"events={fold['valid_events']} "
                f"avg={_fmt(fold['avg_return'])} "
                f"net_avg={_fmt(fold['net_avg_return'])} "
                f"pf={_fmt(fold['profit_factor'], 3)}"
            )

    print(f"\nOUTPUT: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
