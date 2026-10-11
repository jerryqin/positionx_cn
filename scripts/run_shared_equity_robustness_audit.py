from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from xtquant import xtdata

from research.capacity_selection_audit import extract_candidates, replay_selection
from research.pit_portfolio_support import load_market_calendar_from_parquet
from research.shared_equity_portfolio import SharedEquityConfig
from research.shared_equity_robustness import (
    distribution,
    period_metrics,
    prepare_prices,
    simulate_prepared,
)
from research.watchlist_adapter import AdapterConfig, XtDataWatchlistAdapter, save_watchlist_snapshot


FOLDS = [
    ("F1", "20200806", "20221017"),
    ("F2", "20221019", "20240919"),
    ("F3", "20240920", "20260827"),
]


def main() -> int:
    p = argparse.ArgumentParser(description="Shared-Equity Portfolio Robustness Audit")
    p.add_argument("--source", default="data/research/portfolio_simulation_pit_fillable_latest.json")
    p.add_argument("--shared-result", default="data/research/portfolio_simulation_shared_equity_latest.json")
    p.add_argument(
        "--status-parquet",
        default="D:/positionx_data/incoming/eva/20260925_foundation_assets/"
                "data/research/px_assets/a_share_daily_trading_status.parquet",
    )
    p.add_argument("--seeds", type=int, default=300)
    p.add_argument("--bars", type=int, default=1700)
    p.add_argument("--output", default="data/research/shared_equity_robustness_latest.json")
    args = p.parse_args()

    source_path = PROJECT_ROOT / args.source
    shared_path = PROJECT_ROOT / args.shared_result
    status_path = Path(args.status_parquet)
    for path in (source_path, shared_path, status_path):
        if not path.exists():
            raise SystemExit(f"missing input: {path}")

    source = json.loads(source_path.read_text(encoding="utf-8"))
    shared = json.loads(shared_path.read_text(encoding="utf-8"))
    if source.get("status") != "OK" or shared.get("status") != "OK":
        raise SystemExit("upstream result not OK")

    candidates = extract_candidates(source["standard"])
    symbols = sorted({c["symbol"] for c in candidates})

    adapter = XtDataWatchlistAdapter(
        xtdata, AdapterConfig(sector="", bars=args.bars, dividend_type="front")
    )
    history = adapter.history(symbols)
    missing = sorted(s for s in symbols if s not in history or history[s] is None or history[s].empty)
    if missing:
        raise SystemExit(f"BLOCKED_INCOMPLETE_HISTORY: {len(missing)}; sample={missing[:20]}")

    prices = prepare_prices(history)
    start = min(c["entry_date"] for c in candidates)
    end = max(c["exit_date"] for c in candidates)
    market_dates = load_market_calendar_from_parquet(status_path, start, end)

    meth = shared["methodology"]
    cfg = SharedEquityConfig(
        initial_capital=1_000_000.0,
        max_positions=int(meth["max_positions"]),
        target_weight=float(meth["target_weight_per_entry"]),
        max_total_weight=float(meth["max_total_weight"]),
        round_trip_cost=float(meth["round_trip_cost"]),
    )

    # Exact baseline reconciliation using SYMBOL_ASC preselection.
    asc_sel = replay_selection(candidates, cfg.max_positions, mode="SYMBOL_ASC")["selected"]
    baseline = simulate_prepared(prices, asc_sel, market_dates, cfg)
    stored = shared["result"]["summary"]
    checks = {
        "trades": [baseline["summary"]["trades"], int(stored["trades_executed"])],
        "final_equity_abs_error": abs(
            baseline["summary"]["final_equity"] - float(stored["final_equity"])
        ),
        "total_return_abs_error": abs(
            baseline["summary"]["total_return"] - float(stored["total_return"])
        ),
    }
    if (
        checks["trades"][0] != checks["trades"][1]
        or checks["final_equity_abs_error"] > 0.02
        or checks["total_return_abs_error"] > 1e-8
    ):
        raise SystemExit(f"BLOCKED_BASELINE_RECONCILIATION: {checks}")

    # Temporal audit from reconciled baseline.
    yearly = []
    years = sorted({str(x["date"])[:4] for x in baseline["daily"]})
    for y in years:
        yearly.append({"year": y, **period_metrics(baseline["daily"], y+"0101", y+"1231")})
    folds = [{"fold": name, **period_metrics(baseline["daily"], d0, d1)} for name,d0,d1 in FOLDS]

    # Random same-day capacity selection, then identical shared-equity money model.
    rand_rows = []
    for seed in range(args.seeds):
        selected = replay_selection(candidates, cfg.max_positions, mode="RANDOM", seed=seed)["selected"]
        r = simulate_prepared(prices, selected, market_dates, cfg)["summary"]
        rand_rows.append({
            "seed": seed,
            "total_return": r["total_return"],
            "max_drawdown": r["max_drawdown"],
            "sharpe": r["sharpe"],
            "final_equity": r["final_equity"],
        })

    random_dist = {
        "total_return": distribution([x["total_return"] for x in rand_rows]),
        "max_drawdown": distribution([x["max_drawdown"] for x in rand_rows]),
        "sharpe": distribution([x["sharpe"] for x in rand_rows]),
    }

    # Cost stress uses same deterministic SYMBOL_ASC selected set.
    cost_stress = []
    for cost in (0.002, 0.005, 0.010):
        r = simulate_prepared(prices, asc_sel, market_dates, replace(cfg, round_trip_cost=cost))["summary"]
        cost_stress.append({
            "round_trip_cost": cost,
            "total_return": r["total_return"],
            "cagr": r["cagr"],
            "max_drawdown": r["max_drawdown"],
            "sharpe": r["sharpe"],
        })

    positive_folds = sum(
        1 for x in folds
        if x.get("total_return") is not None and x["total_return"] > 0
    )
    verdict = {
        "positive_folds": positive_folds,
        "folds_total": len(folds),
        "random_p05_total_return_positive": (
            random_dist["total_return"].get("p05") is not None
            and random_dist["total_return"]["p05"] > 0
        ),
        "cost_50bps_total_return_positive": (
            next(x for x in cost_stress if x["round_trip_cost"] == 0.005)["total_return"] > 0
        ),
    }
    verdict["portfolio_robust_candidate"] = (
        positive_folds >= 2
        and verdict["random_p05_total_return_positive"]
        and verdict["cost_50bps_total_return_positive"]
    )

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "SHARED_EQUITY_ROBUSTNESS_AUDIT",
        "version": "v1",
        "status": "OK",
        "safety": {
            "uses_broker": False,
            "production_db_opened": False,
            "creates_trading_plan": False,
            "can_submit_order": False,
            "writes_reference_db": False,
            "changes_strategy_parameters": False,
        },
        "config": {
            "max_positions": cfg.max_positions,
            "target_weight": cfg.target_weight,
            "max_total_weight": cfg.max_total_weight,
        },
        "baseline_reconciliation": checks,
        "baseline_summary": baseline["summary"],
        "yearly": yearly,
        "folds": folds,
        "random_tie_break": {
            "seeds": args.seeds,
            "distribution": random_dist,
        },
        "cost_stress": cost_stress,
        "verdict": verdict,
    }

    out_path = PROJECT_ROOT / args.output
    out_path.parent.mkdir(parents=True, exist_ok=True)
    save_watchlist_snapshot(out_path, out)

    print(json.dumps(out["safety"], ensure_ascii=False, indent=2))
    print("\nBASELINE RECONCILIATION:")
    print(json.dumps(checks, ensure_ascii=False, indent=2))

    print("\nTEMPORAL FOLDS:")
    for x in folds:
        print(
            f"{x['fold']} {x['start']}..{x['end']} sessions={x['sessions']} "
            f"return={x.get('total_return')} mdd={x.get('max_drawdown')}"
        )

    print("\nYEARLY:")
    for x in yearly:
        print(
            f"{x['year']} sessions={x['sessions']} "
            f"return={x.get('total_return')} mdd={x.get('max_drawdown')}"
        )

    d = random_dist
    print("\nRANDOM SAME-DAY TIE-BREAK — SHARED EQUITY:")
    print(
        "total_return p05/median/p95 = "
        f"{d['total_return'].get('p05'):.4f} / "
        f"{d['total_return'].get('median'):.4f} / "
        f"{d['total_return'].get('p95'):.4f}"
    )
    print(
        "sharpe      p05/median/p95 = "
        f"{d['sharpe'].get('p05'):.4f} / "
        f"{d['sharpe'].get('median'):.4f} / "
        f"{d['sharpe'].get('p95'):.4f}"
    )

    print("\nCOST STRESS:")
    for x in cost_stress:
        print(
            f"cost={x['round_trip_cost']:.3%} "
            f"return={x['total_return']:.4f} cagr={x['cagr']:.4f} "
            f"mdd={x['max_drawdown']:.4f} sharpe={x['sharpe']:.4f}"
        )

    print("\nVERDICT:")
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    print(f"\nOUTPUT: {out_path}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
