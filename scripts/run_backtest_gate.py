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
from research.watchlist_adapter import AdapterConfig, XtDataWatchlistAdapter, save_watchlist_snapshot


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
    p = argparse.ArgumentParser(description="PositionX Backtest Gate v1.2 — expectancy + robustness gate")
    p.add_argument("--watchlist", default="data/research/watchlist_latest.json")
    p.add_argument("--bars", type=int, default=1500)
    p.add_argument("--refresh-history", action="store_true")
    p.add_argument("--history-calendar-days", type=int, default=2600)
    p.add_argument("--sample", type=int, default=0, help="0=全部Candidate Pool")
    p.add_argument("--output", default="data/research/backtest_gate_latest.json")
    args = p.parse_args()

    source = PROJECT_ROOT / args.watchlist
    if not source.exists():
        raise SystemExit(f"找不到 {source}; 请先运行 Watchlist")

    payload = json.loads(source.read_text(encoding="utf-8"))
    pool = payload.get("candidate_pool")
    if not isinstance(pool, list):
        raise SystemExit("candidate_pool 缺失；请先运行 Watchlist v1.2+")

    symbols = sorted({str(r["symbol"]) for r in pool if r.get("symbol")})
    if args.sample > 0:
        symbols = symbols[: args.sample]

    adapter = XtDataWatchlistAdapter(
        xtdata,
        AdapterConfig(sector="", bars=args.bars, dividend_type="front"),
    )

    if args.refresh_history:
        adapter.refresh_history(symbols, calendar_days=args.history_calendar_days)

    history = adapter.history(symbols)

    all_events = []
    per_strategy_raw = {
        "BREAKOUT_RETEST": 0,
        "MA60_SUPPORT": 0,
    }

    cfg = BacktestGateConfig(min_positive_folds=2)

    for symbol in symbols:
        df = history.get(symbol)
        if df is None or df.empty:
            continue

        br = breakout_retest_events(symbol, df)
        ms = ma60_support_events(
            symbol,
            df,
            event_gap=cfg.ma60_event_gap,
        )
        per_strategy_raw["BREAKOUT_RETEST"] += len(br)
        per_strategy_raw["MA60_SUPPORT"] += len(ms)

        all_events.extend(attach_forward_outcomes(df, br, cfg.horizons))
        all_events.extend(attach_forward_outcomes(df, ms, cfg.horizons))

    summaries = {
        name: summarize_strategy(name, all_events, cfg)
        for name in ("BREAKOUT_RETEST", "MA60_SUPPORT")
    }

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "RESEARCH_BACKTEST_GATE_EVENT_STUDY",
        "safety": {
            "uses_broker": False,
            "production_db_opened": False,
            "creates_trading_plan": False,
            "can_submit_order": False,
        },
        "methodology": {
            "version": "1.2",
            "universe": "CURRENT_CANDIDATE_POOL",
            "survivorship_bias_warning": True,
            "entry": "NEXT_SESSION_OPEN",
            "horizons": list(cfg.horizons),
            "mfe_mae_horizon": max(cfg.horizons),
            "ma60_event_gap": cfg.ma60_event_gap,
            "gate_basis": "EXPECTANCY_PLUS_STABILITY_PLUS_DISTRIBUTION_ROBUSTNESS",
            "assumed_round_trip_cost": cfg.assumed_round_trip_cost,
            "naive_iid_ci_warning": (
                "95% CI is descriptive only; events are cross-sectionally and "
                "temporally correlated, so it is not used as a hard gate."
            ),
            "robustness_note": (
                "Distribution robustness is diagnostic in v1.2 and does not alter "
                "the PASS_CANDIDATE hard gate yet."
            ),
            "note": (
                "v1.2 is still a sanity gate on current membership, not final "
                "point-in-time universe validation."
            ),
        },
        "input": {
            "symbols": len(symbols),
            "history_loaded": len(history),
            "bars_requested": args.bars,
            "history_refreshed": bool(args.refresh_history),
            "history_calendar_days": (
                int(args.history_calendar_days)
                if args.refresh_history else None
            ),
        },
        "raw_signal_counts": per_strategy_raw,
        "strategy_summary": summaries,
        "events": all_events,
    }

    path = save_watchlist_snapshot(PROJECT_ROOT / args.output, out)

    print(json.dumps(out["safety"], ensure_ascii=False, indent=2))
    print(json.dumps(out["methodology"], ensure_ascii=False, indent=2))
    print(json.dumps(out["input"], ensure_ascii=False, indent=2))
    print(json.dumps(out["raw_signal_counts"], ensure_ascii=False, indent=2))

    print("\nBACKTEST GATE v1.2:")
    for name, s in summaries.items():
        h = s["horizons"][str(cfg.gate_horizon)]
        st = s["stability"]
        conc = s["concentration"]
        rob = s["robustness"]
        print(
            f"{name:16s} status={s['gate']['status']:20s} "
            f"events={h['valid_events']:4d} "
            f"win20={_fmt(h['win_rate'])} "
            f"avg20={_fmt(h['avg_return'])} "
            f"median20={_fmt(h['median_return'])} "
            f"net_avg20={_fmt(h['net_avg_return'])} "
            f"payoff={_fmt(h['payoff_ratio'], 3)} "
            f"profit_factor={_fmt(h['profit_factor'], 3)} "
            f"positive_folds={st['positive_folds']}/{st['folds_requested']}"
        )
        print(
            f"  naive_ci95=[{_fmt(h['naive_iid_ci95_low'])}, "
            f"{_fmt(h['naive_iid_ci95_high'])}] "
            f"max_symbol_share={_fmt(conc['max_symbol_share'], 3)} "
            f"max_date_share={_fmt(conc['max_signal_date_share'], 3)}"
        )
        stress = {float(x['round_trip_cost']): x for x in rob['cost_sensitivity']}
        print(
            f"  robustness={rob['status']} "
            f"trim5={_fmt(rob['trimmed_mean_5pct'])} "
            f"winsor5={_fmt(rob['winsorized_mean_5pct'])} "
            f"top1_profit_share={_fmt(rob['top1pct_share_of_gross_profit'], 3)} "
            f"top5_profit_share={_fmt(rob['top5pct_share_of_gross_profit'], 3)} "
            f"net@0.5%={_fmt(stress.get(0.005, {}).get('net_avg_return'))} "
            f"net@1.0%={_fmt(stress.get(0.010, {}).get('net_avg_return'))}"
        )
        for fold in st["folds"]:
            print(
                f"  F{fold['fold']} {fold['start_date']}..{fold['end_date']} "
                f"events={fold['valid_events']} "
                f"win={_fmt(fold['win_rate'])} "
                f"avg={_fmt(fold['avg_return'])} "
                f"net_avg={_fmt(fold['net_avg_return'])} "
                f"pf={_fmt(fold['profit_factor'], 3)}"
            )
        if s["gate"]["reasons"]:
            print("  reasons:", "; ".join(s["gate"]["reasons"]))

    print(f"\nOUTPUT: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
