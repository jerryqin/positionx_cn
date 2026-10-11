from __future__ import annotations
import argparse, json, sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from xtquant import xtdata

from research.capacity_selection_audit import extract_candidates
from research.pit_portfolio_support import load_market_calendar_from_parquet
from research.risk_budget_sweep import DEFAULT_RISK_SCALES, risk_budget_verdict
from research.shared_equity_portfolio import SharedEquityConfig, simulate_shared_equity
from research.watchlist_adapter import AdapterConfig, XtDataWatchlistAdapter, save_watchlist_snapshot


def main() -> int:
    p = argparse.ArgumentParser(description="PositionX BreakoutRetest Risk Budget Sweep v1.1")
    p.add_argument("--source", default="data/research/portfolio_simulation_pit_fillable_latest.json")
    p.add_argument("--robustness", default="data/research/shared_equity_robustness_latest.json")
    p.add_argument(
        "--status-parquet",
        default="D:/positionx_data/incoming/eva/20260925_foundation_assets/"
                "data/research/px_assets/a_share_daily_trading_status.parquet",
    )
    p.add_argument("--max-positions", type=int, default=40)
    p.add_argument("--drawdown-limit", type=float, default=0.20)
    p.add_argument("--bars", type=int, default=1700)
    p.add_argument("--output", default="data/research/risk_budget_sweep_latest.json")
    args = p.parse_args()

    src = PROJECT_ROOT / args.source
    robust_path = PROJECT_ROOT / args.robustness
    status_path = Path(args.status_parquet)

    for path in (src, robust_path, status_path):
        if not path.exists():
            raise SystemExit(f"missing input: {path}")

    source = json.loads(src.read_text(encoding="utf-8"))
    robust = json.loads(robust_path.read_text(encoding="utf-8"))
    if source.get("status") != "OK" or robust.get("status") != "OK":
        raise SystemExit("upstream input not OK")
    if not robust.get("verdict", {}).get("portfolio_robust_candidate"):
        raise SystemExit("portfolio robustness gate did not pass")

    candidates = extract_candidates(source["standard"])
    symbols = sorted({c["symbol"] for c in candidates})

    adapter = XtDataWatchlistAdapter(
        xtdata,
        AdapterConfig(sector="", bars=args.bars, dividend_type="front"),
    )
    history = adapter.history(symbols)
    missing = sorted(
        s for s in symbols
        if s not in history or history[s] is None or history[s].empty
    )
    if missing:
        raise SystemExit(
            f"BLOCKED_INCOMPLETE_HISTORY: {len(missing)}; sample={missing[:20]}"
        )

    start = min(c["entry_date"] for c in candidates)
    end = max(c["exit_date"] for c in candidates)
    market_dates = load_market_calendar_from_parquet(status_path, start, end)

    rows = []
    for spec in DEFAULT_RISK_SCALES:
        common = dict(
            initial_capital=1_000_000.0,
            max_positions=args.max_positions,
            target_weight=spec["target_weight"],
            max_total_weight=spec["max_total_weight"],
        )

        base = simulate_shared_equity(
            history,
            candidates,
            market_dates,
            SharedEquityConfig(**common, round_trip_cost=0.002),
        )["summary"]

        stressed = simulate_shared_equity(
            history,
            candidates,
            market_dates,
            SharedEquityConfig(**common, round_trip_cost=0.005),
        )["summary"]

        rows.append({
            "name": spec["name"],
            "target_weight": spec["target_weight"],
            "max_total_weight": spec["max_total_weight"],
            "base_cost_20bps": base,
            "stress_cost_50bps": stressed,
            "verdict": risk_budget_verdict(base, stressed, args.drawdown_limit),
        })

    eligible = [
        x for x in rows
        if x["verdict"]["risk_budget_eligible"]
        and "STRESS_ONLY" not in x["name"]
    ]
    highest = eligible[-1]["name"] if eligible else None

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "RISK_BUDGET_SWEEP",
        "version": "v1.1",
        "status": "OK",
        "safety": {
            "uses_broker": False,
            "production_db_opened": False,
            "creates_trading_plan": False,
            "can_submit_order": False,
            "writes_reference_db": False,
            "changes_strategy_parameters": False,
            "optimizes_for_return": False,
        },
        "methodology": {
            "strategy": "BREAKOUT_RETEST_ONLY",
            "selection_capacity": args.max_positions,
            "candidate_source": "full PIT + standard-fillability candidate stream",
            "risk_scales": DEFAULT_RISK_SCALES,
            "base_round_trip_cost": 0.002,
            "stress_round_trip_cost": 0.005,
            "drawdown_limit": args.drawdown_limit,
            "important_change_from_v1": (
                "Each risk scale is replayed through the real shared-equity simulator. "
                "Risk-budget blocks and same-day pro-rata scaling are expected behavior, "
                "not fatal errors."
            ),
        },
        "input": {
            "candidates": len(candidates),
            "symbols": len(symbols),
            "history_loaded": len(history),
        },
        "rows": rows,
        "highest_nonstress_risk_budget_eligible": highest,
    }

    out_path = PROJECT_ROOT / args.output
    out_path.parent.mkdir(parents=True, exist_ok=True)
    save_watchlist_snapshot(out_path, out)

    print(json.dumps(out["safety"], ensure_ascii=False, indent=2))
    print("\nRISK BUDGET SWEEP v1.1 — fixed BreakoutRetest, max_positions=40")
    print(
        " scale | target | gross cap | trades | risk-block | scaled | "
        "base ret | base MDD | 50bp ret | 50bp MDD | eligible"
    )
    print("-" * 122)

    for x in rows:
        b = x["base_cost_20bps"]
        s = x["stress_cost_50bps"]
        print(
            f"{x['name']:>16} | "
            f"{x['target_weight']:6.2%} | "
            f"{x['max_total_weight']:8.2%} | "
            f"{b['trades_executed']:6d} | "
            f"{b['risk_budget_blocked']:10d} | "
            f"{b['scaled_entry_days']:6d} | "
            f"{b['total_return']:8.2%} | "
            f"{b['max_drawdown']:8.2%} | "
            f"{s['total_return']:8.2%} | "
            f"{s['max_drawdown']:8.2%} | "
            f"{str(x['verdict']['risk_budget_eligible']):>8}"
        )

    print("\nHIGHEST NON-STRESS ELIGIBLE RISK SCALE:", highest)
    print(
        "NOTE: this is a risk-limit diagnostic; do not choose a scale by highest return."
    )
    print(f"\nOUTPUT: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
