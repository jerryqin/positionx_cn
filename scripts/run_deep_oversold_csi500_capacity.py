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

from research.deep_oversold_capacity import (
    ACCOUNT_SIZES,
    PARTICIPATION_RATES,
    capacity_distribution,
    enrich_capacity_events,
    simulate_capacity,
)
from research.deep_oversold_economic_gate import dedup_while_held
from research.deep_oversold_replication import (
    OversoldReplicationConfig,
    apply_standard_fillability,
    generate_events,
    normalize_history,
)
from research.deep_oversold_universe import CSI500, load_accepted_daily
from research.research_store import ResearchStore, sha256_file
from research.watchlist_adapter import AdapterConfig, XtDataWatchlistAdapter, save_watchlist_snapshot


DB_PATH = PROJECT_ROOT / "data/research/positionx_research.duckdb"
ACCEPTED_DIR = PROJECT_ROOT / "data/research/universe_assets/csi500"
CSI500_RESULT = PROJECT_ROOT / "data/research/deep_oversold_csi500_universe_latest.json"
OUT_JSON = PROJECT_ROOT / "data/research/deep_oversold_csi500_capacity_latest.json"
OUT_PARQUET = PROJECT_ROOT / "data/research/deep_oversold_csi500_capacity_events_v1.parquet"


def refresh_symbol(symbol: str, start: str, end: str) -> None:
    try:
        xtdata.download_history_data(symbol, period="1d", start_time=start, end_time=end)
    except TypeError:
        xtdata.download_history_data(symbol, "1d", start, end)


def event_membership(daily: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    x = daily[(daily["trade_date"] >= start) & (daily["trade_date"] <= end)].copy()
    out = x[["trade_date","symbol"]].rename(columns={"trade_date":"signal_date"})
    counts = out.groupby("signal_date")["symbol"].nunique()
    bad = counts[counts != 500]
    if len(bad):
        raise SystemExit(
            "BLOCKED_CSI500_MEMBERSHIP_SIZE "
            + json.dumps({str(k):int(v) for k,v in bad.head(20).items()}, ensure_ascii=False)
        )
    return out


def load_histories(symbols, dividend_type: str, bars=2000):
    adapter = XtDataWatchlistAdapter(
        xtdata,
        AdapterConfig(sector="", bars=bars, dividend_type=dividend_type),
    )
    raw = adapter.history(symbols)
    return {
        s: normalize_history(df)
        for s,df in raw.items()
        if df is not None and not df.empty
    }


def load_status(store: ResearchStore, events: pd.DataFrame) -> pd.DataFrame:
    keys = events[["entry_date","symbol"]].drop_duplicates().copy()
    store.con.register("capacity_keys", keys)
    try:
        return store.con.execute("""
        SELECT
          strftime(CAST(s.trade_date AS DATE), '%Y%m%d') AS trade_date,
          s.symbol,
          s.is_suspended,
          s.new_listing_special,
          s.can_buy_at_open,
          s.st_known
        FROM foundation_a_share_daily_trading_status s
        INNER JOIN capacity_keys k
          ON strftime(CAST(s.trade_date AS DATE), '%Y%m%d') = k.entry_date
         AND s.symbol = k.symbol
        """).fetchdf()
    finally:
        store.con.unregister("capacity_keys")


def save_parquet(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.register("capacity_events_df", df)
        pp = str(path).replace("\\","/").replace("'","''")
        con.execute(
            f"COPY capacity_events_df TO '{pp}' "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )
    finally:
        con.close()


def baseline_reference():
    if not CSI500_RESULT.exists():
        return None
    x = json.loads(CSI500_RESULT.read_text(encoding="utf-8"))
    r20 = x["base_shared_equity"]["20bps"]
    r50 = x["base_shared_equity"]["50bps"]
    return {
        "fractional_20bps_cagr": r20["cagr"],
        "fractional_20bps_mdd": r20["max_drawdown"],
        "fractional_20bps_avg_gross": r20["avg_gross_weight"],
        "fractional_50bps_cagr": r50["cagr"],
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Deep Oversold CSI500 Capacity Audit v1")
    p.add_argument("--start-date", default="20200803")
    p.add_argument("--end-date", default="20260924")
    p.add_argument("--refresh-missing-history", action="store_true")
    p.add_argument("--db", default=str(DB_PATH))
    args = p.parse_args()

    daily = load_accepted_daily(ACCEPTED_DIR, CSI500)
    membership = event_membership(daily, args.start_date, args.end_date)
    symbols = sorted(membership["symbol"].unique().tolist())
    names = symbols + [CSI500.expected_index]

    adj = load_histories(names, "front", 2000)
    raw = load_histories(symbols, "none", 2000)
    missing = sorted(set(names) - set(adj) | (set(symbols) - set(raw)))

    if missing and args.refresh_missing_history:
        dl_start = (
            datetime.strptime(args.start_date, "%Y%m%d") - timedelta(days=180)
        ).strftime("%Y%m%d")
        print(f"[DATA] refreshing missing only: {len(missing)}")
        for i,symbol in enumerate(missing,1):
            print(f"[DATA] refresh {i}/{len(missing)} {symbol}")
            try:
                refresh_symbol(symbol, dl_start, args.end_date)
            except Exception as exc:
                print(f"[DATA] warning {symbol}: {type(exc).__name__}: {exc}")
        adj = load_histories(names, "front", 2000)
        raw = load_histories(symbols, "none", 2000)
        missing = sorted(set(names) - set(adj) | (set(symbols) - set(raw)))

    if missing:
        raise SystemExit(
            f"BLOCKED_INCOMPLETE_CAPACITY_HISTORY missing={len(missing)} "
            f"sample={missing[:30]}\n"
            "Rerun with --refresh-missing-history."
        )

    benchmark = adj.pop(CSI500.expected_index)
    market_dates = [
        d for d in benchmark.index if args.start_date <= d <= args.end_date
    ]
    cfg = OversoldReplicationConfig()
    events = generate_events(adj, benchmark, membership, market_dates, cfg)
    complete = events[events["complete_outcome"] == True].copy()

    with ResearchStore(args.db) as store:
        status = load_status(store, complete)
        screened = apply_standard_fillability(complete, status)
        if int(screened["status_found"].sum()) == 0:
            raise SystemExit("BLOCKED_STATUS_JOIN_ZERO_MATCH")
        fillable = screened[screened["standard_fillable"] == True].copy()

        cap_events, prep = enrich_capacity_events(fillable, raw)
        if len(cap_events) < 500:
            raise SystemExit(
                f"BLOCKED_TOO_FEW_CAPACITY_READY_EVENTS n={len(cap_events)}"
            )

        distribution = capacity_distribution(cap_events)

        # Reconcile the familiar dedup count; simulator itself uses dynamic
        # actual-holding duplicate handling.
        frozen_dedup_count = len(dedup_while_held(fillable))

        sim_dates = [
            d for d in market_dates
            if cap_events["entry_date"].min() <= d <= cap_events["exit_date"].max()
        ]

        policies = [("LOT_ONLY", None)] + [
            (f"ADV_{int(round(p*10000))}BP", p) for p in PARTICIPATION_RATES
        ]
        simulations = []
        for name, participation in policies:
            for account in ACCOUNT_SIZES:
                for cost_label, cost in (("20bps",0.002),("50bps",0.005)):
                    r = simulate_capacity(
                        cap_events, adj, sim_dates,
                        initial_equity=account,
                        participation_rate=participation,
                        round_trip_cost=cost,
                    )
                    r["policy"] = name
                    r["cost_label"] = cost_label
                    simulations.append(r)

        # Durable derived asset: Parquet + DuckDB external view, no physical copy.
        save_parquet(cap_events, OUT_PARQUET)
        store.register_external_view(
            asset_id="DEEP_OVERSOLD_CSI500_CAPACITY_EVENTS_V1",
            view_name="deep_oversold_csi500_capacity_events_v1",
            path=OUT_PARQUET,
            source_system="POSITIONX_DERIVED_RESEARCH",
            file_kind="parquet",
            asof_date=args.end_date,
            row_count=len(cap_events),
            metadata={
                "signal":"ret20<=-25%",
                "liquidity":"ADV20 last 20 positive-amount traded days ending T",
                "target_weight":0.01,
                "lot_model":"688/689=200 shares; other SH/SZ=100",
            },
        )

        safety = {
            "uses_broker": False,
            "production_db_opened": False,
            "creates_trading_plan": False,
            "can_submit_order": False,
            "changes_forward_protocol": False,
        }
        out = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "mode":"DEEP_OVERSOLD_CSI500_CAPACITY_AUDIT_V1",
            "status":"OK",
            "safety":safety,
            "frozen_edge":{
                "ret20_threshold":-0.25,
                "universe":"CSI500 PIT",
                "entry":"T+1 exact OPEN",
                "exit":"S20 CLOSE",
                "target_weight":0.01,
                "max_total_weight":0.30,
            },
            "liquidity_model":{
                "version":"DAILY_ADV_PROXY_V1",
                "adv20":"last 20 positive-amount traded days ending signal T",
                "future_data_used":False,
                "participation_rates":list(PARTICIPATION_RATES),
                "warning":"Daily ADV is a capacity proxy, not opening-auction liquidity.",
            },
            "lot_model":{
                "version":"A_SHARE_BOARD_LOT_V1",
                "STAR_688_689":200,
                "other_SH_SZ":100,
                "forward_execution_uses":"QMT MinLimitOrderVolume",
            },
            "counts":{
                "raw_events":len(events),
                "complete_events":len(complete),
                "fillable_events":len(fillable),
                "frozen_first_signal_while_flat_events":frozen_dedup_count,
                **prep,
            },
            "distribution":distribution,
            "fractional_reference":baseline_reference(),
            "simulations":simulations,
            "derived_asset":{
                "parquet":str(OUT_PARQUET),
                "sha256":sha256_file(OUT_PARQUET),
                "duckdb_view":"deep_oversold_csi500_capacity_events_v1",
            },
        }
        save_watchlist_snapshot(OUT_JSON, out)

    print(json.dumps(safety, ensure_ascii=False, indent=2))
    print("\nDEEP OVERSOLD CSI500 CAPACITY AUDIT v1")
    print(
        f"events raw={len(events)} fillable={len(fillable)} "
        f"capacity_ready={len(cap_events)} frozen_dedup={frozen_dedup_count}"
    )

    print("\nLOT-SIZE ACCOUNT FLOOR (1% target)")
    lotq = distribution["min_account_for_one_lot_at_1pct"]
    for k in ("p50","p90","p95","p99"):
        print(f"  {k}: ¥{lotq[k]:,.0f}")

    print("\nLIQUIDITY ACCOUNT CAPACITY — full 1% target")
    for name, block in distribution["participation"].items():
        cq = block["account_capacity_full_1pct"]
        print(
            f"  {name}: p10=¥{cq['p10']:,.0f} "
            f"p25=¥{cq['p25']:,.0f} p50=¥{cq['p50']:,.0f} "
            f"p90=¥{cq['p90']:,.0f}"
        )

    print("\nACCOUNT-SIZE CURVE — 20bps")
    print(
        " policy      account      exec%   target-fill   CAGR     MDD     "
        "avg-gross  lot-skip  liq<lot"
    )
    print("-"*96)
    for r in simulations:
        if r["cost_label"] != "20bps":
            continue
        print(
            f" {r['policy']:10s} ¥{r['initial_equity']/1e6:7.1f}m "
            f"{r['execution_rate']:7.1%} {r['target_notional_fill_ratio']:11.1%} "
            f"{r['cagr']:+8.2%} {r['max_drawdown']:+8.2%} "
            f"{r['avg_gross_weight']:9.2%} {r['skipped_lot']:9d} "
            f"{r['skipped_liquidity_below_lot']:8d}"
        )

    print("\nNOTES")
    print("  - Do not select account size/participation rate by highest historical CAGR.")
    print("  - Use the curve to identify where lot size or liquidity starts degrading the frozen Edge.")
    print("  - Daily ADV is only a first capacity proxy; opening-auction liquidity is a later refinement.")
    print(f"\nJSON: {OUT_JSON}")
    print(f"PARQUET: {OUT_PARQUET}")
    print("DuckDB view: deep_oversold_csi500_capacity_events_v1")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
