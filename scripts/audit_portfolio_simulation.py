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
from research.portfolio_simulation_audit import reconcile_accounting, selection_audit
from research.watchlist_adapter import AdapterConfig, XtDataWatchlistAdapter, save_watchlist_snapshot


def main() -> int:
    p = argparse.ArgumentParser(description="PositionX Portfolio Simulation Audit v1")
    p.add_argument("--simulation", default="data/research/portfolio_simulation_latest.json")
    p.add_argument("--watchlist", default="data/research/watchlist_latest.json")
    p.add_argument("--bars", type=int, default=1500)
    p.add_argument("--output", default="data/research/portfolio_simulation_audit_latest.json")
    args = p.parse_args()

    sim_path = PROJECT_ROOT / args.simulation
    watch_path = PROJECT_ROOT / args.watchlist
    if not sim_path.exists() or not watch_path.exists():
        raise SystemExit("simulation/watchlist input missing")

    simulation = json.loads(sim_path.read_text(encoding="utf-8"))
    watch = json.loads(watch_path.read_text(encoding="utf-8"))
    pool = watch.get("candidate_pool")
    if not isinstance(pool, list):
        raise SystemExit("candidate_pool missing")

    symbols = sorted({str(r["symbol"]) for r in pool if r.get("symbol")})
    adapter = XtDataWatchlistAdapter(xtdata, AdapterConfig(sector="", bars=args.bars, dividend_type="front"))
    history = adapter.history(symbols)

    events = []
    for symbol in symbols:
        df = history.get(symbol)
        if df is not None and not df.empty:
            events.extend(breakout_retest_events(symbol, df))

    accounting = reconcile_accounting(simulation)
    selection = selection_audit(history, events, simulation)

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "RESEARCH_PORTFOLIO_SIMULATION_AUDIT_ONLY",
        "safety": {
            "uses_broker": False,
            "production_db_opened": False,
            "creates_trading_plan": False,
            "can_submit_order": False,
        },
        "accounting": accounting,
        "selection": selection,
    }
    path = save_watchlist_snapshot(PROJECT_ROOT / args.output, out)

    print(json.dumps(out["safety"], ensure_ascii=False, indent=2))
    a = accounting
    print("\nACCOUNTING RECONCILIATION:")
    print(
        f"reconciles={a['accounting_reconciles']} actual_final={a['actual_final_equity']:.2f} "
        f"slot_product_final={a['expected_final_equity_from_slot_products']:.2f} "
        f"error={a['reconciliation_error']:.8f}"
    )
    print(
        f"avg_trade_arith={a['arithmetic_avg_trade_return']} "
        f"avg_trade_geo={a['geometric_avg_trade_return_all_trades']}"
    )
    print(
        f"stored_sharpe={a['stored_sharpe']} corrected_sharpe={a['corrected_sharpe_including_first_day']} "
        f"turnover_total={a['aggregate_turnover']} turnover_annualized={a['annualized_turnover']}"
    )

    print("\nSELECTION / CAPACITY AUDIT:")
    for key in ("all_candidate_counterfactual", "executed_counterfactual", "skipped_counterfactual"):
        print(f"{key}: {selection[key]}")
    print(f"executed_minus_skipped_avg={selection['executed_minus_skipped_avg']}")

    print("\nSLOT COMPOUNDING:")
    for s in a["slots"]:
        print(
            f"slot={s['slot_id']} trades={s['trades']} final={s['final_value_from_trade_product']:.2f} "
            f"arith={s['arithmetic_avg_trade_return']} geo={s['geometric_avg_trade_return']}"
        )

    print(f"\nOUTPUT: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
