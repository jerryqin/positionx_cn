from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import duckdb
import pandas as pd
from xtquant import xtdata

from research.deep_oversold_replication import (
    OversoldReplicationConfig,
    apply_standard_fillability,
    generate_events,
    normalize_history,
    summarize_variant,
)
from research.deep_oversold_economic_gate import (
    EconomicProfile,
    compact_result,
    dedup_while_held,
    period_return,
    simulate_shared_equity,
)
from research.deep_oversold_universe import CSI500, load_accepted_daily
from research.watchlist_adapter import AdapterConfig, XtDataWatchlistAdapter, save_watchlist_snapshot


STATUS_PARQUET = Path(
    "D:/positionx_data/incoming/eva/20260925_foundation_assets/"
    "data/research/px_assets/a_share_daily_trading_status.parquet"
)
ACCEPTED_DIR = PROJECT_ROOT / "data/research/universe_assets/csi500"
CSI300_ECON = PROJECT_ROOT / "data/research/deep_oversold_economic_gate_latest.json"

FOLDS = (
    ("F1", "20200803", "20221017"),
    ("F2", "20221018", "20240919"),
    ("F3", "20240920", "20260924"),
)
BASE_PROFILE = EconomicProfile("BASE_1_0x", 0.01, 0.30)


def refresh_symbol(symbol: str, start: str, end: str) -> None:
    try:
        xtdata.download_history_data(
            symbol, period="1d", start_time=start, end_time=end
        )
    except TypeError:
        xtdata.download_history_data(symbol, "1d", start, end)


def load_status_for_events(path: Path, events: pd.DataFrame) -> pd.DataFrame:
    if not path.exists():
        raise SystemExit(f"missing status parquet: {path}")
    keys = events[["entry_date", "symbol"]].drop_duplicates().copy()
    con = duckdb.connect()
    try:
        con.register("keys", keys)
        p = str(path).replace("\\", "/").replace("'", "''")
        q = f"""
        SELECT
          strftime(CAST(s.trade_date AS DATE), '%Y%m%d') AS trade_date,
          s.symbol,
          s.is_suspended,
          s.new_listing_special,
          s.can_buy_at_open,
          s.st_known
        FROM read_parquet('{p}') s
        INNER JOIN keys k
          ON strftime(CAST(s.trade_date AS DATE), '%Y%m%d') = k.entry_date
         AND s.symbol = k.symbol
        """
        return con.execute(q).fetchdf()
    finally:
        con.close()


def event_membership(daily: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    x = daily[(daily["trade_date"] >= start) & (daily["trade_date"] <= end)].copy()
    out = x[["trade_date", "symbol"]].rename(columns={"trade_date": "signal_date"})
    if out.duplicated(["signal_date", "symbol"]).any():
        raise SystemExit("BLOCKED_DUPLICATE_CSI500_MEMBERSHIP")
    counts = out.groupby("signal_date")["symbol"].nunique()
    bad = counts[counts != 500]
    if len(bad):
        raise SystemExit(
            "BLOCKED_CSI500_MEMBERSHIP_SIZE "
            + json.dumps(bad.head(20).to_dict(), ensure_ascii=False)
        )
    return out


def load_csi300_comparison():
    if not CSI300_ECON.exists():
        return None
    x = json.loads(CSI300_ECON.read_text(encoding="utf-8"))
    r20 = x["profiles"]["BASE_1_0x"]["20bps"]
    r50 = x["profiles"]["BASE_1_0x"]["50bps"]
    return {
        "cagr20": r20["cagr"],
        "mdd20": r20["max_drawdown"],
        "sharpe20": r20["sharpe"],
        "avg_gross20": r20["avg_gross_weight"],
        "capital_efficiency20": r20["cagr_per_avg_gross"],
        "cagr50": r50["cagr"],
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Deep Oversold CSI500 Universe Robustness v1")
    p.add_argument("--start-date", default="20200803")
    p.add_argument("--end-date", default="20260924")
    p.add_argument("--refresh-history", action="store_true")
    p.add_argument(
        "--output",
        default="data/research/deep_oversold_csi500_universe_latest.json",
    )
    args = p.parse_args()

    daily = load_accepted_daily(ACCEPTED_DIR, CSI500)
    membership = event_membership(daily, args.start_date, args.end_date)
    symbols = sorted(membership["symbol"].unique().tolist())

    if args.refresh_history:
        dl_start = (
            datetime.strptime(args.start_date, "%Y%m%d") - timedelta(days=120)
        ).strftime("%Y%m%d")
        for i, symbol in enumerate(symbols + [CSI500.expected_index], 1):
            print(f"[DATA] refresh {i}/{len(symbols)+1} {symbol}")
            try:
                refresh_symbol(symbol, dl_start, args.end_date)
            except Exception as exc:
                print(f"[DATA] warning {symbol}: {type(exc).__name__}: {exc}")

    adapter = XtDataWatchlistAdapter(
        xtdata,
        AdapterConfig(sector="", bars=2000, dividend_type="front"),
    )
    loaded = adapter.history(symbols + [CSI500.expected_index])
    loaded = {
        s: normalize_history(df)
        for s, df in loaded.items()
        if df is not None and not df.empty
    }
    missing = [s for s in symbols + [CSI500.expected_index] if s not in loaded]
    if missing:
        raise SystemExit(
            f"BLOCKED_INCOMPLETE_HISTORY missing={len(missing)} "
            f"sample={missing[:30]}\n"
            "Rerun with --refresh-history if these histories are not cached."
        )

    benchmark = loaded.pop(CSI500.expected_index)
    market_dates = [
        d for d in benchmark.index
        if args.start_date <= d <= args.end_date
    ]
    if len(market_dates) < 500:
        raise SystemExit(f"BLOCKED_SHORT_CSI500_CALENDAR sessions={len(market_dates)}")

    cfg = OversoldReplicationConfig()
    events = generate_events(
        loaded, benchmark, membership, market_dates, cfg
    )
    if events.empty:
        raise SystemExit("no CSI500 deep-oversold events")

    complete = events[events["complete_outcome"] == True].copy()
    status = load_status_for_events(STATUS_PARQUET, complete)
    screened = apply_standard_fillability(complete, status)
    if int(screened["status_found"].sum()) == 0:
        raise SystemExit("BLOCKED_STATUS_JOIN_ZERO_MATCH")
    fillable = screened[screened["standard_fillable"] == True].copy()
    if fillable.empty:
        raise SystemExit("no fillable CSI500 events")

    dedup = dedup_while_held(fillable)
    open_event = summarize_variant(fillable, "open", cfg)
    open_event_dedup = summarize_variant(dedup, "open", cfg)

    sim_dates = [
        d for d in market_dates
        if min(dedup["entry_date"]) <= d <= max(dedup["exit_date"])
    ]
    sims = {}
    for label, cost in (("20bps", 0.002), ("50bps", 0.005)):
        sim = simulate_shared_equity(
            events=dedup,
            histories=loaded,
            market_dates=sim_dates,
            profile=BASE_PROFILE,
            round_trip_cost=cost,
        )
        compact = compact_result(sim)
        compact["fold_metrics"] = [
            {"fold": name, **period_return(sim["curve"], s, e)}
            for name, s, e in FOLDS
        ]
        sims[label] = compact

    event_folds = open_event_dedup["folds"]
    positive_event_folds = sum(
        1 for x in event_folds
        if x["avg_excess"] is not None and x["avg_excess"] > 0
    )
    portfolio_folds = sims["20bps"]["fold_metrics"]
    positive_portfolio_folds = sum(
        1 for x in portfolio_folds
        if x["return"] is not None and x["return"] > 0
    )

    gate = {
        "event_mean_excess_positive": open_event_dedup["excess_gross"]["avg"] > 0,
        "event_trim5_excess_positive": (
            open_event_dedup["excess_gross"]["trimmed_mean_5pct"] > 0
        ),
        "event_positive_folds": positive_event_folds,
        "portfolio_cagr20_positive": sims["20bps"]["cagr"] > 0,
        "portfolio_cagr50_positive": sims["50bps"]["cagr"] > 0,
        "portfolio_positive_folds": positive_portfolio_folds,
        "portfolio_maxdd20_within_20pct": sims["20bps"]["max_drawdown"] >= -0.20,
    }
    gate["csi500_replication_pass"] = (
        gate["event_mean_excess_positive"]
        and gate["event_trim5_excess_positive"]
        and positive_event_folds >= 2
        and gate["portfolio_cagr20_positive"]
        and gate["portfolio_cagr50_positive"]
        and positive_portfolio_folds >= 2
        and gate["portfolio_maxdd20_within_20pct"]
    )

    csi300 = load_csi300_comparison()
    comparison = None
    if csi300:
        r20 = sims["20bps"]
        r50 = sims["50bps"]
        comparison = {
            "csi300": csi300,
            "csi500": {
                "cagr20": r20["cagr"],
                "mdd20": r20["max_drawdown"],
                "sharpe20": r20["sharpe"],
                "avg_gross20": r20["avg_gross_weight"],
                "capital_efficiency20": r20["cagr_per_avg_gross"],
                "cagr50": r50["cagr"],
            },
            "delta_csi500_minus_csi300": {
                "cagr20": r20["cagr"] - csi300["cagr20"],
                "mdd20": r20["max_drawdown"] - csi300["mdd20"],
                "avg_gross20": r20["avg_gross_weight"] - csi300["avg_gross20"],
                "capital_efficiency20": (
                    r20["cagr_per_avg_gross"]
                    - csi300["capital_efficiency20"]
                ),
                "cagr50": r50["cagr"] - csi300["cagr50"],
            },
        }

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
        "mode": "DEEP_OVERSOLD_CSI500_UNIVERSE_ROBUSTNESS_V1",
        "status": "OK",
        "safety": safety,
        "frozen_hypothesis": {
            "ret20_threshold": -0.25,
            "entry": "T+1 exact market-session open",
            "exit": "signal+20 exact market-session close",
            "buy_fillability": "STANDARD",
            "same_symbol": "first signal while flat for portfolio",
            "portfolio": "1% target / 30% max gross",
            "costs": ["20bps", "50bps"],
            "no_parameter_sweep": True,
            "no_regime_filter": True,
            "no_stop_loss": True,
            "no_take_profit": True,
        },
        "universe": {
            "name": "CSI500",
            "accepted_dataset": "CSI500_PIT_ACCEPTED_V1_20260926",
            "expected_members_per_day": 500,
            "union_symbols": len(symbols),
            "benchmark": CSI500.expected_index,
            "start_date": args.start_date,
            "end_date": args.end_date,
        },
        "counts": {
            "raw_events": int(len(events)),
            "complete_events": int(len(complete)),
            "fillable_events": int(len(fillable)),
            "first_signal_while_flat_events": int(len(dedup)),
            "unique_symbols_with_fillable_event": int(fillable["symbol"].nunique()),
            "unique_signal_dates": int(fillable["signal_date"].nunique()),
        },
        "event_all_fillable": open_event,
        "event_first_signal_while_flat": open_event_dedup,
        "base_shared_equity": sims,
        "gate": gate,
        "csi300_comparison": comparison,
    }

    out_path = PROJECT_ROOT / args.output
    out_path.parent.mkdir(parents=True, exist_ok=True)
    save_watchlist_snapshot(out_path, out)

    print(json.dumps(safety, ensure_ascii=False, indent=2))
    print("\nDEEP OVERSOLD — CSI500 UNIVERSE ROBUSTNESS v1")
    print(
        f"PIT symbols={len(symbols)} sessions={len(market_dates)} "
        f"raw={len(events)} fillable={len(fillable)} dedup={len(dedup)}"
    )

    e = open_event_dedup["excess_gross"]
    print("\nEVENT — FIRST SIGNAL WHILE FLAT / T+1 OPEN")
    print(
        f"n={e['n']} avg_excess={e['avg']:+.4%} "
        f"median={e['median']:+.4%} trim5={e['trimmed_mean_5pct']:+.4%} "
        f"win={e['win_rate']:.2%}"
    )
    for f in event_folds:
        print(
            f"  {f['fold']} events={f['events']} "
            f"avg_excess={f['avg_excess']:+.4%}"
        )

    print("\nBASE SHARED EQUITY — 1% / 30%")
    for label in ("20bps", "50bps"):
        r = sims[label]
        print(
            f"  {label}: trades={r['trades']} "
            f"CAGR={r['cagr']:+.2%} MDD={r['max_drawdown']:+.2%} "
            f"Sharpe={r['sharpe']:.3f} avg_gross={r['avg_gross_weight']:.2%} "
            f"cap_eff={r['cagr_per_avg_gross']:.2%}"
        )

    if comparison:
        d = comparison["delta_csi500_minus_csi300"]
        print("\nCSI500 - CSI300 DELTA")
        print(
            f"  CAGR20={d['cagr20']:+.2%} "
            f"MDD20={d['mdd20']:+.2%} "
            f"avg_gross={d['avg_gross20']:+.2%} "
            f"cap_eff={d['capital_efficiency20']:+.2%} "
            f"CAGR50={d['cagr50']:+.2%}"
        )

    print("\nGATE")
    print(json.dumps(gate, ensure_ascii=False, indent=2))
    print("\nNOTE: CSI1000 is intentionally not tested until its 1001-member anomaly is repaired.")
    print(f"OUTPUT: {out_path}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
