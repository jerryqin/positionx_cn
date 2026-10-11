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

from research.deep_oversold_economic_gate import dedup_while_held
from research.deep_oversold_regime_map import (
    compute_daily_state,
    summarize_dimension,
    summarize_joint,
)
from research.deep_oversold_universe import CSI500, load_accepted_daily
from research.research_store import ResearchStore, sha256_file
from research.watchlist_adapter import AdapterConfig, XtDataWatchlistAdapter, save_watchlist_snapshot


DB_PATH = PROJECT_ROOT/"data/research/positionx_research.duckdb"
ACCEPTED_DIR = PROJECT_ROOT/"data/research/universe_assets/csi500"
CAP_EVENTS = PROJECT_ROOT/"data/research/deep_oversold_csi500_capacity_events_v1.parquet"
OUT_JSON = PROJECT_ROOT/"data/research/deep_oversold_csi500_regime_map_latest.json"
OUT_STATE = PROJECT_ROOT/"data/research/csi500_market_state_daily_v1.parquet"
OUT_EVENTS = PROJECT_ROOT/"data/research/deep_oversold_csi500_regime_events_v1.parquet"


def load_histories(symbols, bars=2000):
    adapter = XtDataWatchlistAdapter(
        xtdata,
        AdapterConfig(sector="", bars=bars, dividend_type="front"),
    )
    raw = adapter.history(symbols)
    return {
        s: df for s,df in raw.items()
        if df is not None and not df.empty
    }


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    x.columns = [str(c).lower() for c in x.columns]
    idx = []
    for v in x.index:
        try:
            idx.append(pd.Timestamp(v).strftime("%Y%m%d"))
        except Exception:
            s = "".join(ch for ch in str(v) if ch.isdigit())
            idx.append(s[:8])
    x.index = idx
    x = x[~x.index.duplicated(keep="last")].sort_index()
    for c in ("open","high","low","close","volume","amount"):
        if c in x.columns:
            x[c] = pd.to_numeric(x[c], errors="coerce")
    return x


def save_parquet(df: pd.DataFrame, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.register("dfv", df)
        p = str(path).replace("\\","/").replace("'","''")
        con.execute(f"COPY dfv TO '{p}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    finally:
        con.close()


def main() -> int:
    p = argparse.ArgumentParser(description="Deep Oversold CSI500 Regime Map v1")
    p.add_argument("--start-date", default="20200803")
    p.add_argument("--end-date", default="20260924")
    p.add_argument("--db", default=str(DB_PATH))
    args = p.parse_args()

    if not CAP_EVENTS.exists():
        raise SystemExit(
            f"missing capacity asset: {CAP_EVENTS}\n"
            "Run run_deep_oversold_csi500_capacity.py first."
        )

    # Event asset is already frozen STANDARD-fillable and contains no future
    # liquidity in its signal definition.
    con = duckdb.connect()
    try:
        cp = str(CAP_EVENTS).replace("\\","/").replace("'","''")
        events = con.execute(f"SELECT * FROM read_parquet('{cp}')").fetchdf()
    finally:
        con.close()
    for c in ("signal_date","entry_date","exit_date"):
        events[c] = events[c].astype(str).str.replace("-","",regex=False).str[:8]

    dedup = dedup_while_held(events)
    value_col = "open_excess_gross"
    if value_col not in dedup.columns:
        raise SystemExit(f"capacity events missing required return column: {value_col}")

    daily = load_accepted_daily(ACCEPTED_DIR, CSI500)
    membership = daily[
        (daily["trade_date"] >= args.start_date)
        & (daily["trade_date"] <= args.end_date)
    ][["trade_date","symbol"]].rename(columns={"trade_date":"signal_date"})
    symbols = sorted(membership["symbol"].unique().tolist())

    raw = load_histories(symbols + [CSI500.expected_index], 2000)
    missing = [s for s in symbols + [CSI500.expected_index] if s not in raw]
    if missing:
        raise SystemExit(
            f"BLOCKED_INCOMPLETE_REGIME_HISTORY missing={len(missing)} "
            f"sample={missing[:30]}"
        )
    histories = {s:normalize(raw[s]) for s in symbols}
    benchmark = normalize(raw[CSI500.expected_index])
    market_dates = [
        d for d in benchmark.index if args.start_date <= d <= args.end_date
    ]

    state = compute_daily_state(histories, membership, benchmark, market_dates)
    regime_events = dedup.merge(
        state,
        left_on="signal_date",
        right_on="trade_date",
        how="left",
        validate="many_to_one",
    )
    if regime_events["panic_regime"].isna().any():
        raise SystemExit("BLOCKED_REGIME_JOIN_MISSING")

    dimensions = []
    for dim in ("panic_regime","trend_regime","breadth_regime","vol_regime"):
        dimensions.extend(summarize_dimension(regime_events, dim, value_col))

    joint = {
        "panic_x_trend": summarize_joint(
            regime_events, ("panic_regime","trend_regime"), value_col, 30
        ),
        "panic_x_vol": summarize_joint(
            regime_events, ("panic_regime","vol_regime"), value_col, 30
        ),
        "trend_x_vol": summarize_joint(
            regime_events, ("trend_regime","vol_regime"), value_col, 30
        ),
    }

    # Persistence: descriptive only. A regime is not promoted to a live gate.
    persistent = []
    for row in dimensions:
        pos = sum(
            1 for f in row["folds"]
            if f["date_equal_avg"] is not None and f["date_equal_avg"] > 0
        )
        persistent.append({
            "dimension":row["dimension"],
            "regime":row["regime"],
            "events":row["events"],
            "positive_folds":pos,
            "folds_total":3,
            "date_equal_avg":row["date_equal_avg"],
            "trim5":row["trim5"],
        })

    save_parquet(state, OUT_STATE)
    save_parquet(regime_events, OUT_EVENTS)

    with ResearchStore(args.db) as store:
        store.register_external_view(
            asset_id="CSI500_MARKET_STATE_DAILY_V1",
            view_name="csi500_market_state_daily_v1",
            path=OUT_STATE,
            source_system="POSITIONX_DERIVED_RESEARCH",
            file_kind="parquet",
            asof_date=args.end_date,
            row_count=len(state),
            metadata={
                "purpose":"T-observable descriptive regime map",
                "live_gate":False,
            },
        )
        store.register_external_view(
            asset_id="DEEP_OVERSOLD_CSI500_REGIME_EVENTS_V1",
            view_name="deep_oversold_csi500_regime_events_v1",
            path=OUT_EVENTS,
            source_system="POSITIONX_DERIVED_RESEARCH",
            file_kind="parquet",
            asof_date=args.end_date,
            row_count=len(regime_events),
            metadata={
                "edge":"ret20<=-25%",
                "live_gate":False,
                "event_set":"first signal while flat",
            },
        )

    out = {
        "generated_at":datetime.now().isoformat(timespec="seconds"),
        "mode":"DEEP_OVERSOLD_CSI500_REGIME_MAP_V1",
        "status":"OK",
        "safety":{
            "uses_broker":False,
            "changes_forward_protocol":False,
            "creates_live_regime_gate":False,
            "changes_strategy_parameters":False,
        },
        "purpose":(
            "Describe where the frozen Edge historically earned/decayed. "
            "No regime is used to filter Forward or alter capital."
        ),
        "event_set":{
            "input_capacity_events":int(len(events)),
            "first_signal_while_flat":int(len(dedup)),
            "joined_events":int(len(regime_events)),
            "return_metric":value_col,
        },
        "state_definitions":{
            "panic_breadth":{
                "ISOLATED_LT_1PCT":"<1% of PIT CSI500 members have ret20<=-25%",
                "BROAD_1_TO_5PCT":"1% to <5%",
                "SYSTEMIC_GE_5PCT":">=5%",
            },
            "trend":"CSI500 close vs T-close MA60",
            "breadth":"fraction PIT CSI500 members above own MA60, split at 50%",
            "vol":"CSI500 RV20 vs its own trailing 252-session median, min 126 observations",
            "all_features":"T-close observable only",
        },
        "dimensions":dimensions,
        "joint":joint,
        "persistence_summary":persistent,
        "protocol_note":(
            "This report is descriptive. Do not convert the best historical bucket "
            "into a live filter without a separately predeclared allocator experiment."
        ),
        "assets":{
            "daily_state_parquet":str(OUT_STATE),
            "daily_state_sha256":sha256_file(OUT_STATE),
            "event_regime_parquet":str(OUT_EVENTS),
            "event_regime_sha256":sha256_file(OUT_EVENTS),
            "duckdb_views":[
                "csi500_market_state_daily_v1",
                "deep_oversold_csi500_regime_events_v1",
            ],
        },
    }
    save_watchlist_snapshot(OUT_JSON,out)

    print(json.dumps(out["safety"], ensure_ascii=False, indent=2))
    print("\nDEEP OVERSOLD CSI500 REGIME MAP v1")
    print(
        f"events input={len(events)} first-signal-flat={len(dedup)} "
        f"state_days={len(state)}"
    )
    print("\nSINGLE-DIMENSION MAP — date-equal OPEN excess")
    for row in dimensions:
        pos = sum(
            1 for f in row["folds"]
            if f["date_equal_avg"] is not None and f["date_equal_avg"] > 0
        )
        print(
            f"  {row['dimension']:14s} {row['regime']:22s} "
            f"events={row['events']:4d} dates={row['signal_dates']:3d} "
            f"date_eq={row['date_equal_avg']:+.2%} "
            f"median={row['median']:+.2%} trim5={row['trim5']:+.2%} "
            f"folds+={pos}/3"
        )

    print("\nJOINT MAP — minimum 30 events")
    for name,rows in joint.items():
        print(f"  [{name}]")
        for row in rows:
            label = " | ".join(
                f"{k}={v}" for k,v in row.items()
                if k in ("panic_regime","trend_regime","breadth_regime","vol_regime")
            )
            print(
                f"    {label} events={row['events']} "
                f"date_eq={row['date_equal_avg']:+.2%} "
                f"trim5={row['trim5']:+.2%}"
            )

    print("\nNO REGIME FILTER WAS CREATED.")
    print(f"JSON: {OUT_JSON}")
    print(f"STATE: {OUT_STATE}")
    print(f"EVENTS: {OUT_EVENTS}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
