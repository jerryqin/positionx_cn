from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from xtquant import xtdata

from research.deep_oversold_economic_gate import (
    FOLDS,
    PROFILES,
    compact_result,
    concurrency_demand,
    dedup_while_held,
    economic_gate,
    load_fillable_events,
    normalize_history,
    period_return,
    simulate_shared_equity,
)
from research.watchlist_adapter import AdapterConfig, XtDataWatchlistAdapter, save_watchlist_snapshot


def main() -> int:
    p = argparse.ArgumentParser(
        description="PositionX Deep Oversold Economic Gate v1"
    )
    p.add_argument(
        "--events",
        default="data/research/deep_oversold_replication_events_latest.csv",
    )
    p.add_argument("--benchmark", default="000300.SH")
    p.add_argument(
        "--output",
        default="data/research/deep_oversold_economic_gate_latest.json",
    )
    args = p.parse_args()

    events_path = PROJECT_ROOT / args.events
    if not events_path.exists():
        raise SystemExit(f"missing replication events: {events_path}")

    raw = load_fillable_events(str(events_path))
    events = dedup_while_held(raw)
    if events.empty:
        raise SystemExit("no deduplicated fillable events")

    symbols = sorted(events["symbol"].astype(str).unique().tolist())
    adapter = XtDataWatchlistAdapter(
        xtdata,
        AdapterConfig(sector="", bars=2000, dividend_type="front"),
    )
    loaded = adapter.history(symbols + [args.benchmark])
    loaded = {
        s: normalize_history(df)
        for s, df in loaded.items()
        if df is not None and not df.empty
    }

    missing = [s for s in symbols + [args.benchmark] if s not in loaded]
    if missing:
        raise SystemExit(
            f"BLOCKED_INCOMPLETE_HISTORY missing={len(missing)} sample={missing[:20]}"
        )

    benchmark = loaded[args.benchmark]
    histories = {s: loaded[s] for s in symbols}
    start = min(raw["signal_date"].min(), events["signal_date"].min())
    end = max(events["exit_date"].max(), events["signal_date"].max())
    market_dates = [d for d in benchmark.index if start <= d <= end]
    if len(market_dates) < 500:
        raise SystemExit(f"insufficient market dates: {len(market_dates)}")

    demand = concurrency_demand(events, market_dates)

    safety = {
        "uses_broker": False,
        "production_db_opened": False,
        "creates_trading_plan": False,
        "can_submit_order": False,
        "writes_reference_db": False,
        "changes_strategy_parameters": False,
    }

    results = {}
    curve_rows = []
    for profile in PROFILES:
        results[profile.name] = {}
        for label, cost in (("20bps", 0.002), ("50bps", 0.005)):
            sim = simulate_shared_equity(
                events=events,
                histories=histories,
                market_dates=market_dates,
                profile=profile,
                round_trip_cost=cost,
            )
            compact = compact_result(sim)
            compact["fold_metrics"] = [
                {"fold": name, **period_return(sim["curve"], s, e)}
                for name, s, e in FOLDS
            ]
            results[profile.name][label] = compact

            c = sim["curve"].copy()
            c["profile"] = profile.name
            c["cost"] = label
            curve_rows.append(c)

    gate = economic_gate(
        results["BASE_1_0x"]["20bps"],
        results["BASE_1_0x"]["50bps"],
    )

    # Benchmark context over the identical calendar window.
    b = benchmark.reindex(market_dates)
    close = pd.to_numeric(b["close"], errors="coerce").ffill()
    bret = float(close.iloc[-1] / close.iloc[0] - 1.0)
    bdd = close / close.cummax() - 1.0

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "DEEP_OVERSOLD_ECONOMIC_GATE_V1",
        "status": "OK",
        "safety": safety,
        "frozen_hypothesis": {
            "event": "ret20 <= -25%",
            "entry": "T+1 exact market-session open",
            "exit": "signal+20 exact market-session close",
            "event_handling": "first signal while flat",
            "no_stop_loss": True,
            "no_take_profit": True,
            "no_regime_filter": True,
            "no_PB_filter": True,
            "no_low_attention_filter": True,
            "no_parameter_sweep": True,
        },
        "portfolio_model": {
            "initial_equity": 1_000_000,
            "fractional_shares": True,
            "existing_positions_not_rebalanced": True,
            "same_day_entry_allocation": "pro-rata if gross cap binds; no symbol ranking/tie-break",
            "risk_profiles": [
                {
                    "name": p.name,
                    "target_weight": p.target_weight,
                    "max_total_weight": p.max_total_weight,
                    "nominal_slots": p.max_total_weight / p.target_weight,
                }
                for p in PROFILES
            ],
            "costs": ["20bps", "50bps"],
            "base_profile_for_gate": "BASE_1_0x = 1% per position, 30% max gross",
        },
        "counts": {
            "fillable_stock_day_events": int(len(raw)),
            "first_signal_while_flat_events": int(len(events)),
            "unique_symbols": int(events["symbol"].nunique()),
            "unique_entry_dates": int(events["entry_date"].nunique()),
            "market_sessions": int(len(market_dates)),
        },
        "unconstrained_demand": demand,
        "benchmark_context": {
            "symbol": args.benchmark,
            "total_return_same_window": bret,
            "max_drawdown_same_window": float(bdd.min()),
        },
        "profiles": results,
        "economic_gate": gate,
        "gate_rule": {
            "base_profile": "1% target / 30% gross",
            "cagr_20bps_min": 0.03,
            "maxdd_20bps_floor": -0.15,
            "cagr_50bps_must_be_positive": True,
            "positive_folds_min": 2,
            "cagr_per_avg_gross_min": 0.10,
            "meaning": (
                "Pass means economically interesting enough for portfolio-combination "
                "and forward design; it does not authorize live capital."
            ),
        },
    }

    out_path = PROJECT_ROOT / args.output
    out_path.parent.mkdir(parents=True, exist_ok=True)
    save_watchlist_snapshot(out_path, out)

    curve_path = out_path.with_name("deep_oversold_economic_curves_latest.csv")
    pd.concat(curve_rows, ignore_index=True).to_csv(
        curve_path, index=False, encoding="utf-8-sig"
    )

    print(json.dumps(safety, ensure_ascii=False, indent=2))
    print("\nDEEP OVERSOLD ECONOMIC GATE v1")
    print(
        f"events raw={len(raw)} first-signal-while-flat={len(events)} "
        f"symbols={events['symbol'].nunique()} sessions={len(market_dates)}"
    )
    print("\nUNCONSTRAINED CAPITAL DEMAND")
    print(json.dumps(demand, ensure_ascii=False, indent=2))

    print("\nSHARED-EQUITY RESULTS")
    print(
        " profile       cost   trades  total-ret   CAGR     MaxDD    Sharpe  "
        "avg-gross  max-gross  avg-active  scaled-days  cap-eff"
    )
    print("-" * 126)
    for p in PROFILES:
        for label in ("20bps", "50bps"):
            r = results[p.name][label]
            sh = "-" if r["sharpe"] is None else f"{r['sharpe']:.3f}"
            ce = "-" if r["cagr_per_avg_gross"] is None else f"{r['cagr_per_avg_gross']:.2%}"
            print(
                f" {p.name:12s} {label:5s} "
                f"{r['trades']:7d} {r['total_return']:+9.2%} "
                f"{r['cagr']:+8.2%} {r['max_drawdown']:+8.2%} "
                f"{sh:>7s} {r['avg_gross_weight']:9.2%} "
                f"{r['max_gross_weight']:9.2%} {r['avg_active_positions']:11.2f} "
                f"{r['scaled_entry_days']:11d} {ce:>8s}"
            )

    print("\nBASE PROFILE FOLDS — 20bps")
    for f in results["BASE_1_0x"]["20bps"]["fold_metrics"]:
        print(
            f"  {f['fold']} return={f['return']:+.2%} "
            f"MDD={f['max_drawdown']:+.2%} days={f['days']}"
        )

    print("\nECONOMIC GATE")
    print(json.dumps(gate, ensure_ascii=False, indent=2))
    print(
        "\nNOTE: profiles are predeclared risk scales with the same 30 nominal slots. "
        "Do not choose the highest historical return as an optimized setting."
    )
    print(f"\nJSON: {out_path}")
    print(f"CURVES: {curve_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
