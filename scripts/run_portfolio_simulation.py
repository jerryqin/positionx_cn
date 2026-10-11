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

from research.backtest_gate import breakout_retest_events
from research.portfolio_simulation import (
    BreakoutRetestPortfolioSimulator,
    PortfolioSimulationConfig,
)
from research.watchlist_adapter import (
    AdapterConfig,
    XtDataWatchlistAdapter,
    save_watchlist_snapshot,
)


def main() -> int:
    p = argparse.ArgumentParser(
        description="PositionX BreakoutRetest Portfolio Simulation v1 — research only"
    )
    p.add_argument("--watchlist", default="data/research/watchlist_latest.json")
    p.add_argument("--bars", type=int, default=1500)
    p.add_argument("--initial-capital", type=float, default=1_000_000.0)
    p.add_argument("--max-positions", type=int, default=10)
    p.add_argument("--holding-days", type=int, default=20)
    p.add_argument("--round-trip-cost", type=float, default=0.002)
    p.add_argument("--output", default="data/research/portfolio_simulation_latest.json")
    args = p.parse_args()

    source = PROJECT_ROOT / args.watchlist
    if not source.exists():
        raise SystemExit(f"找不到 {source}; 请先运行 Watchlist v1.2+")

    payload = json.loads(source.read_text(encoding="utf-8"))
    pool = payload.get("candidate_pool")
    if not isinstance(pool, list):
        raise SystemExit("candidate_pool 缺失；请先运行 Watchlist v1.2+")

    symbols = sorted({str(r["symbol"]) for r in pool if r.get("symbol")})

    adapter = XtDataWatchlistAdapter(
        xtdata,
        AdapterConfig(sector="", bars=args.bars, dividend_type="front"),
    )
    history = adapter.history(symbols)

    events = []
    for symbol in symbols:
        df = history.get(symbol)
        if df is None or df.empty:
            continue
        events.extend(breakout_retest_events(symbol, df))

    cfg = PortfolioSimulationConfig(
        initial_capital=args.initial_capital,
        max_positions=args.max_positions,
        holding_days=args.holding_days,
        round_trip_cost=args.round_trip_cost,
    )
    result = BreakoutRetestPortfolioSimulator(cfg).simulate(history, events)

    summary = result["summary"]
    executable = int(summary.get("signals_executable") or 0)
    skipped_no_slot = int(summary.get("skipped_no_slot") or 0)
    skipped_duplicate = int(summary.get("skipped_duplicate_symbol") or 0)
    skip_total = skipped_no_slot + skipped_duplicate
    summary["capacity_skip_rate"] = (
        skipped_no_slot / executable if executable > 0 else 0.0
    )
    summary["total_skip_rate"] = (
        skip_total / executable if executable > 0 else 0.0
    )

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "RESEARCH_PORTFOLIO_SIMULATION_ONLY",
        "version": "1.1",
        "safety": {
            "uses_broker": False,
            "production_db_opened": False,
            "creates_trading_plan": False,
            "can_submit_order": False,
        },
        "methodology": {
            "strategy": "BREAKOUT_RETEST_ONLY",
            "universe": "CURRENT_CANDIDATE_POOL",
            "survivorship_bias_warning": True,
            "universe_is_point_in_time": False,
            "performance_interpretation": "DIAGNOSTIC_ONLY_NOT_A_VALID_STRATEGY_PERFORMANCE_ESTIMATE",
            "entry": "NEXT_SESSION_OPEN",
            "exit": f"SIGNAL_PLUS_{args.holding_days}_BAR_CLOSE",
            "holding_days": args.holding_days,
            "round_trip_cost": args.round_trip_cost,
            "max_positions": args.max_positions,
            "position_model": "FIXED_CAPITAL_SLOTS_COMPOUND_INDEPENDENTLY",
            "position_model_warning": (
                "This is not constant 10%-of-current-equity weighting after slots diverge. "
                "Treat v1.1 as an auditable baseline, not the final equal-weight portfolio model."
            ),
            "slot_weight_at_start": 1.0 / args.max_positions,
            "same_day_tie_break": "SYMBOL_ASC",
            "tie_break_warning": (
                "SYMBOL_ASC is deterministic engineering behavior, not an alpha ranking. "
                "If capacity_skip_rate is material, allocator design is required before promotion."
            ),
            "no_duplicate_symbol_while_held": True,
            "slot_exiting_at_close_not_reusable_at_same_day_open": True,
            "fractional_shares": True,
            "no_stop_loss": True,
            "no_take_profit": True,
            "no_regime_filter": True,
            "no_dynamic_position_sizing": True,
            "fillability_filter_applied": False,
            "fillability_proxy_note": (
                "Only diagnostic one-price/open-gap flags are recorded. Historical exact "
                "limit-up fillability still requires stop-price or equivalent point-in-time data."
            ),
        },
        "input": {
            "candidate_pool_symbols": len(symbols),
            "history_loaded": len(history),
            "bars_requested": args.bars,
            "breakout_retest_events": len(events),
        },
        "config": result["config"],
        "summary": summary,
        "trades": result["trades"],
        "skipped": result.get("skipped", []),
        "equity_curve": result["equity_curve"],
        "daily_audit": result.get("daily_audit", []),
    }

    path = save_watchlist_snapshot(PROJECT_ROOT / args.output, out)

    print(json.dumps(out["safety"], ensure_ascii=False, indent=2))
    print(json.dumps(out["methodology"], ensure_ascii=False, indent=2))
    print(json.dumps(out["input"], ensure_ascii=False, indent=2))

    s = out["summary"]
    print("\nPORTFOLIO SIMULATION v1.1 AUDITABLE BASELINE:")
    print(
        f"period={s['start_date']}..{s['end_date']} sessions={s['sessions']} "
        f"trades={s['trades_executed']}/{s['signals_executable']}"
    )
    print(
        f"total_return={s['total_return']:.4f} cagr_calendar={s['cagr_calendar_basis']} "
        f"cagr_252={s['cagr_252_session_basis']} max_drawdown={s['max_drawdown']:.4f} "
        f"sharpe_actual={s['sharpe_actual_sessions_including_first_day']}"
    )
    print(
        f"trade_win_rate={s['trade_win_rate']} avg_trade_arith={s['avg_trade_return']} "
        f"avg_trade_cap_weighted={s['avg_trade_return_capital_weighted']} "
        f"avg_trade_geo={s['geometric_avg_trade_return']} median_trade={s['median_trade_return']}"
    )
    print(
        f"turnover_cum_double={s['turnover_cumulative_double_sided']:.2f} "
        f"turnover_ann_double={s['turnover_annualized_double_sided']} "
        f"turnover_ann_single={s['turnover_annualized_single_sided']} calmar={s['calmar']}"
    )
    print(
        f"avg_exposure={s['avg_exposure']:.4f} "
        f"avg_active_positions={s['avg_active_positions']:.2f} "
        f"max_concurrent={s['max_concurrent_positions']}"
    )
    print(
        f"skipped_no_slot={s['skipped_no_slot']} "
        f"skipped_duplicate_symbol={s['skipped_duplicate_symbol']} "
        f"capacity_skip_rate={s['capacity_skip_rate']:.4f} "
        f"total_skip_rate={s['total_skip_rate']:.4f}"
    )
    print(
        f"fillability_suspect_executed={s.get('executed_fillability_suspect', 0)} "
        f"fillability_suspect_skipped={s.get('skipped_fillability_suspect', 0)}"
    )
    print(f"\nOUTPUT: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
