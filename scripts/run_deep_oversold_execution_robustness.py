from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import duckdb
import pandas as pd
from xtquant import xtdata

from research.deep_oversold_economic_gate import (
    EconomicProfile,
    compact_result,
    load_fillable_events,
    normalize_history,
    period_return,
    simulate_shared_equity,
)
from research.deep_oversold_execution_robustness import (
    BASELINE,
    CONSERVATIVE,
    STANDARD,
    apply_exit_policy,
    robustness_verdict,
)
from research.watchlist_adapter import AdapterConfig, XtDataWatchlistAdapter, save_watchlist_snapshot


DEFAULT_STATUS = Path(
    "D:/positionx_data/incoming/eva/20260925_foundation_assets/"
    "data/research/px_assets/a_share_daily_trading_status.parquet"
)
BASE_PROFILE = EconomicProfile("BASE_1_0x", 0.01, 0.30)
FOLDS = (
    ("F1", "20200803", "20221017"),
    ("F2", "20221018", "20240919"),
    ("F3", "20240920", "20260924"),
)


def load_exit_status(path: Path, symbols, start_date: str, end_date: str) -> pd.DataFrame:
    if not path.exists():
        raise SystemExit(f"missing status parquet: {path}")
    key_df = pd.DataFrame({"symbol": sorted(set(symbols))})
    con = duckdb.connect()
    try:
        con.register("symbols_key", key_df)
        p = str(path).replace("\\", "/").replace("'", "''")
        q = f"""
        SELECT
          strftime(CAST(s.trade_date AS DATE), '%Y%m%d') AS trade_date,
          s.symbol,
          s.is_suspended,
          s.is_one_price_down_limit,
          s.is_close_at_down_limit
        FROM read_parquet('{p}') s
        INNER JOIN symbols_key k ON s.symbol = k.symbol
        WHERE CAST(s.trade_date AS DATE)
          BETWEEN strptime('{start_date}', '%Y%m%d')::DATE
              AND strptime('{end_date}', '%Y%m%d')::DATE
        """
        return con.execute(q).fetchdf()
    finally:
        con.close()


def compact_with_folds(sim):
    out = compact_result(sim)
    out["fold_metrics"] = [
        {"fold": name, **period_return(sim["curve"], start, end)}
        for name, start, end in FOLDS
    ]
    return out


def main() -> int:
    p = argparse.ArgumentParser(
        description="PositionX Deep Oversold Exit Execution Robustness Gate v1"
    )
    p.add_argument(
        "--events",
        default="data/research/deep_oversold_replication_events_latest.csv",
    )
    p.add_argument(
        "--economic-json",
        default="data/research/deep_oversold_economic_gate_latest.json",
    )
    p.add_argument("--benchmark", default="000300.SH")
    p.add_argument("--status-parquet", default=str(DEFAULT_STATUS))
    p.add_argument(
        "--output",
        default="data/research/deep_oversold_execution_robustness_latest.json",
    )
    args = p.parse_args()

    events_path = PROJECT_ROOT / args.events
    if not events_path.exists():
        raise SystemExit(f"missing events: {events_path}")
    raw = load_fillable_events(str(events_path))
    symbols = sorted(raw["symbol"].astype(str).unique().tolist())

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

    start = min(raw["signal_date"])
    intended_end = max(raw["exit_date"])
    market_dates_all = [d for d in benchmark.index if d >= start]
    if intended_end not in market_dates_all:
        raise SystemExit(f"intended end not in market calendar: {intended_end}")

    intended_i = market_dates_all.index(intended_end)
    end_i = min(len(market_dates_all)-1, intended_i + 20)
    market_dates = market_dates_all[:end_i+1]
    query_end = market_dates[-1]

    status = load_exit_status(
        Path(args.status_parquet), symbols, start, query_end
    )
    if status.empty:
        raise SystemExit("BLOCKED_EMPTY_EXIT_STATUS")

    policies = [BASELINE, STANDARD, CONSERVATIVE]
    policy_events = {}
    audits = {}
    sims = {}

    # `market_dates` above is only a RESOLUTION calendar: it may extend past the
    # last trade to allow deferred exits to be found. Each portfolio simulation
    # must stop on that policy's own last actual exit. Otherwise extra flat days
    # change CAGR even though final equity and drawdown are identical.
    for policy in policies:
        ev, audit = apply_exit_policy(
            raw_fillable_events=raw,
            market_dates=market_dates,
            status_rows=status,
            policy=policy,
        )
        if ev.empty:
            raise SystemExit(f"no events after exit policy: {policy.name}")
        policy_events[policy.name] = ev
        audits[policy.name] = audit

        sim_end = max(ev["exit_date"].astype(str))
        sim_dates = [d for d in market_dates if start <= d <= sim_end]
        if not sim_dates or sim_dates[-1] != sim_end:
            raise SystemExit(
                f"BLOCKED_POLICY_SIM_CALENDAR_END policy={policy.name} "
                f"sim_end={sim_end} last_date={sim_dates[-1] if sim_dates else None}"
            )

        audit["simulation_start"] = sim_dates[0]
        audit["simulation_end"] = sim_dates[-1]
        audit["simulation_sessions"] = len(sim_dates)

        sims[policy.name] = {}
        for label, cost in (("20bps", 0.002), ("50bps", 0.005)):
            sim = simulate_shared_equity(
                events=ev,
                histories=histories,
                market_dates=sim_dates,
                profile=BASE_PROFILE,
                round_trip_cost=cost,
            )
            sims[policy.name][label] = compact_with_folds(sim)

    # Reconcile baseline against the already accepted Economic Gate output.
    econ_path = PROJECT_ROOT / args.economic_json
    reconciliation = {"available": econ_path.exists()}
    if econ_path.exists():
        prior = json.loads(econ_path.read_text(encoding="utf-8"))
        prior_base = prior["profiles"]["BASE_1_0x"]["20bps"]
        current_base = sims["INTENDED_CLOSE"]["20bps"]
        reconciliation.update({
            "prior_cagr": prior_base["cagr"],
            "current_cagr": current_base["cagr"],
            "cagr_abs_diff": abs(prior_base["cagr"] - current_base["cagr"]),
            "prior_total_return": prior_base["total_return"],
            "current_total_return": current_base["total_return"],
            "total_return_abs_diff": abs(
                prior_base["total_return"] - current_base["total_return"]
            ),
            "prior_maxdd": prior_base["max_drawdown"],
            "current_maxdd": current_base["max_drawdown"],
            "maxdd_abs_diff": abs(
                prior_base["max_drawdown"] - current_base["max_drawdown"]
            ),
            "baseline_simulation_end": audits["INTENDED_CLOSE"]["simulation_end"],
        })
        reconciliation["pass"] = (
            reconciliation["cagr_abs_diff"] < 1e-10
            and reconciliation["total_return_abs_diff"] < 1e-10
            and reconciliation["maxdd_abs_diff"] < 1e-10
        )
        if not reconciliation["pass"]:
            raise SystemExit(
                "BLOCKED_BASELINE_RECONCILIATION_FAILED "
                + json.dumps(reconciliation, ensure_ascii=False)
            )

    verdict = robustness_verdict(
        sims["INTENDED_CLOSE"]["20bps"],
        sims["STANDARD_DEFERRED_CLOSE"]["20bps"],
        sims["CONSERVATIVE_DEFERRED_CLOSE"]["20bps"],
        sims["CONSERVATIVE_DEFERRED_CLOSE"]["50bps"],
        audits,
    )

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
        "mode": "DEEP_OVERSOLD_EXECUTION_ROBUSTNESS_V1",
        "status": "OK",
        "safety": safety,
        "purpose": (
            "Stress the already-replicated Deep Oversold Edge against historical "
            "exit-side sellability. No signal/entry/threshold/risk parameter is optimized."
        ),
        "exit_policies": {
            "INTENDED_CLOSE": (
                "reference baseline; exit exactly on the predeclared close"
            ),
            "STANDARD_DEFERRED_CLOSE": (
                "if suspended or one-price down-limit on intended exit, defer to "
                "the first later close without those blocks"
            ),
            "CONSERVATIVE_DEFERRED_CLOSE": (
                "STANDARD plus defer when the intended close itself is at down-limit"
            ),
        },
        "base_profile": {
            "target_weight": 0.01,
            "max_total_weight": 0.30,
        },
        "audits": audits,
        "simulations": sims,
        "baseline_reconciliation": reconciliation,
        "verdict": verdict,
    }

    out_path = PROJECT_ROOT / args.output
    out_path.parent.mkdir(parents=True, exist_ok=True)
    save_watchlist_snapshot(out_path, out)

    print(json.dumps(safety, ensure_ascii=False, indent=2))
    print("\nDEEP OVERSOLD EXIT EXECUTION ROBUSTNESS v1")
    print(
        " policy                       events deferred unresolved  "
        "CAGR20   MDD20   CAGR50  avg-gross"
    )
    print("-" * 92)
    for policy in policies:
        name = policy.name
        a = audits[name]
        r20 = sims[name]["20bps"]
        r50 = sims[name]["50bps"]
        print(
            f" {name:28s} {a['first_signal_while_actual_flat_events']:6d} "
            f"{a['deferred_exit_events']:8d} {a['unresolved_events']:10d} "
            f"{r20['cagr']:+7.2%} {r20['max_drawdown']:+7.2%} "
            f"{r50['cagr']:+7.2%} {r20['avg_gross_weight']:9.2%}"
        )
        if a["deferred_exit_events"]:
            print(
                f"   defer sessions: mean={a['defer_sessions_mean']:.2f} "
                f"p90={a['defer_sessions_p90']:.1f} "
                f"max={a['defer_sessions_max']}"
            )

    print("\nBASELINE RECONCILIATION")
    print(json.dumps(reconciliation, ensure_ascii=False, indent=2))

    print("\nVERDICT")
    print(json.dumps(verdict, ensure_ascii=False, indent=2))

    print(
        "\nNOTE: this is exit-execution stress only. It does not test CSI500/CSI1000 "
        "universe robustness and it does not change the -25% event definition."
    )
    print(f"\nOUTPUT: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
