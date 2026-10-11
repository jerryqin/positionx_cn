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

from research.capacity_selection_audit import extract_candidates
from research.pit_portfolio_support import load_market_calendar_from_parquet
from research.shared_equity_portfolio import SharedEquityConfig, simulate_shared_equity
from research.watchlist_adapter import AdapterConfig, XtDataWatchlistAdapter, save_watchlist_snapshot


def main() -> int:
    p = argparse.ArgumentParser(description="PositionX Shared-Equity Portfolio Simulation v2")
    p.add_argument(
        "--input",
        default="data/research/portfolio_simulation_pit_fillable_latest.json",
    )
    p.add_argument(
        "--capacity-audit",
        default="data/research/capacity_selection_audit_pit_fillable_latest.json",
    )
    p.add_argument(
        "--status-parquet",
        default="D:/positionx_data/incoming/eva/20260925_foundation_assets/"
                "data/research/px_assets/a_share_daily_trading_status.parquet",
    )
    p.add_argument("--max-positions", type=int, default=40)
    p.add_argument("--target-weight", type=float, default=0.02)
    p.add_argument("--max-total-weight", type=float, default=0.80)
    p.add_argument("--initial-capital", type=float, default=1_000_000.0)
    p.add_argument("--round-trip-cost", type=float, default=0.002)
    p.add_argument("--bars", type=int, default=1700)
    p.add_argument(
        "--output",
        default="data/research/portfolio_simulation_shared_equity_latest.json",
    )
    args = p.parse_args()

    src = PROJECT_ROOT / args.input
    audit_path = PROJECT_ROOT / args.capacity_audit
    status_path = Path(args.status_parquet)
    for path in (src, audit_path, status_path):
        if not path.exists():
            raise SystemExit(f"required input missing: {path}")

    data = json.loads(src.read_text(encoding="utf-8"))
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    if data.get("status") != "OK" or audit.get("status") != "OK":
        raise SystemExit("upstream input is not OK")

    standard = data.get("standard")
    candidates = extract_candidates(standard)
    if not candidates:
        raise SystemExit("no candidates reconstructed")

    # Capacity 40 must have already passed the selection-only random sensitivity audit.
    r40 = next(
        (x for x in audit.get("random_tie_break", []) if int(x.get("capacity", -1)) == args.max_positions),
        None,
    )
    if r40 is None:
        raise SystemExit(
            f"capacity {args.max_positions} has not been random-sensitivity audited"
        )

    symbols = sorted({c["symbol"] for c in candidates})
    adapter = XtDataWatchlistAdapter(
        xtdata, AdapterConfig(sector="", bars=args.bars, dividend_type="front")
    )
    history = adapter.history(symbols)
    missing = sorted(s for s in symbols if s not in history or history[s] is None or history[s].empty)
    if missing:
        raise SystemExit(f"BLOCKED_INCOMPLETE_HISTORY: {len(missing)}; sample={missing[:20]}")

    start = min(c["entry_date"] for c in candidates)
    end = max(c["exit_date"] for c in candidates)
    market_dates = load_market_calendar_from_parquet(status_path, start, end)

    cfg = SharedEquityConfig(
        initial_capital=args.initial_capital,
        max_positions=args.max_positions,
        target_weight=args.target_weight,
        max_total_weight=args.max_total_weight,
        round_trip_cost=args.round_trip_cost,
    )
    result = simulate_shared_equity(history, candidates, market_dates, cfg)

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
        "mode": "RESEARCH_SHARED_EQUITY_PORTFOLIO",
        "version": "v2",
        "status": "OK",
        "safety": safety,
        "methodology": {
            "strategy": "BREAKOUT_RETEST_ONLY",
            "candidate_source": "PIT + STANDARD_FILLABILITY",
            "capital_model": "SHARED_ACCOUNT_EQUITY",
            "max_positions": cfg.max_positions,
            "target_weight_per_entry": cfg.target_weight,
            "max_total_weight": cfg.max_total_weight,
            "same_day_tie_break": "SYMBOL_ASC",
            "same_day_exit_not_reusable_at_open": True,
            "no_duplicate_symbol_while_held": True,
            "entry_sizing": (
                "Each same-day eligible entry receives the same notional. "
                "Desired notional = target_weight * pre-entry account equity; "
                "pro-rata scaled if cash/gross budget is tighter."
            ),
            "no_rebalance_existing_positions": True,
            "fractional_shares": True,
            "round_trip_cost": cfg.round_trip_cost,
            "note": (
                "max_positions=40 is an engineering baseline because it is the first tested "
                "capacity with <10% no-slot loss and already-stable random tie-break selection. "
                "It was not selected for highest historical return."
            ),
        },
        "input": {
            "source": str(src),
            "capacity_audit": str(audit_path),
            "candidate_count": len(candidates),
            "symbols": len(symbols),
            "history_loaded": len(history),
            "random_capacity_audit": r40,
        },
        "result": result,
    }

    out_path = PROJECT_ROOT / args.output
    out_path.parent.mkdir(parents=True, exist_ok=True)
    save_watchlist_snapshot(out_path, out)

    s = result["summary"]
    print(json.dumps(safety, ensure_ascii=False, indent=2))
    print("\nSHARED-EQUITY PORTFOLIO v2:")
    print(
        f"max_positions={cfg.max_positions} target_weight={cfg.target_weight:.2%} "
        f"max_total_weight={cfg.max_total_weight:.2%}"
    )
    print(
        f"trades={s['trades_executed']} "
        f"skip_no_slot={s['skipped_no_slot']} "
        f"skip_duplicate={s['skipped_duplicate_symbol']} "
        f"risk_blocked={s['risk_budget_blocked']} "
        f"scaled_entry_days={s['scaled_entry_days']}"
    )
    print(
        f"total_return={s['total_return']:.4f} "
        f"cagr={s['cagr_calendar']:.4f} "
        f"max_drawdown={s['max_drawdown']:.4f} "
        f"sharpe={s['sharpe_actual_sessions']:.4f}"
    )
    print(
        f"avg_gross_weight={s['avg_gross_weight']:.4f} "
        f"max_gross_weight={s['max_gross_weight']:.4f} "
        f"avg_active={s['avg_active_positions']:.2f} "
        f"max_active={s['max_active_positions']}"
    )
    print(f"final_equity={s['final_equity']:.2f}")
    print(f"OUTPUT: {out_path}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
