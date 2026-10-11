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

from research.deep_oversold_decay_attribution import (
    composition_attribution,
    fold_summary,
    make_daily_signal_frame,
    regime_mix,
)
from research.research_store import ResearchStore, sha256_file
from research.watchlist_adapter import save_watchlist_snapshot


EVENTS = PROJECT_ROOT/"data/research/deep_oversold_csi500_regime_events_v1.parquet"
STATE = PROJECT_ROOT/"data/research/csi500_market_state_daily_v1.parquet"
OUT_JSON = PROJECT_ROOT/"data/research/deep_oversold_csi500_decay_attribution_latest.json"
OUT_PARQUET = PROJECT_ROOT/"data/research/deep_oversold_csi500_decay_daily_v1.parquet"
RETURN_COL = "open_excess_gross"


def read_parquet(path: Path) -> pd.DataFrame:
    con = duckdb.connect()
    try:
        p = str(path).replace("\\","/").replace("'","''")
        return con.execute(f"SELECT * FROM read_parquet('{p}')").fetchdf()
    finally:
        con.close()


def save_parquet(df: pd.DataFrame, path: Path) -> None:
    con = duckdb.connect()
    try:
        con.register("dfv", df)
        p = str(path).replace("\\","/").replace("'","''")
        con.execute(f"COPY dfv TO '{p}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    finally:
        con.close()


def main() -> int:
    p = argparse.ArgumentParser(description="Deep Oversold CSI500 temporal decay attribution v1")
    p.add_argument("--db", default=str(PROJECT_ROOT/"data/research/positionx_research.duckdb"))
    args = p.parse_args()

    if not EVENTS.exists() or not STATE.exists():
        raise SystemExit("missing regime-map assets; run Regime Map v1 first")

    events = read_parquet(EVENTS)
    state = read_parquet(STATE)
    for c in ("signal_date","entry_date","exit_date"):
        if c in events.columns:
            events[c] = events[c].astype(str).str.replace("-","",regex=False).str[:8]
    state["trade_date"] = state["trade_date"].astype(str).str.replace("-","",regex=False).str[:8]

    if RETURN_COL not in events.columns:
        raise SystemExit(f"events missing {RETURN_COL}")

    daily = make_daily_signal_frame(events, RETURN_COL)
    fold = fold_summary(events, daily, state, RETURN_COL)

    dims = [
        ("panic_regime",),
        ("vol_regime",),
        ("breadth_regime",),
        ("trend_regime",),
        ("panic_regime","vol_regime"),
    ]
    mix = {}
    attribution = {}
    for ds in dims:
        key = "_x_".join(ds)
        if len(ds) == 1:
            mix[key] = regime_mix(daily, ds[0])
        attribution[key] = {
            "F1_to_F2": composition_attribution(daily, ds, "F1", "F2"),
            "F1_to_F3": composition_attribution(daily, ds, "F1", "F3"),
            "F2_to_F3": composition_attribution(daily, ds, "F2", "F3"),
        }

    out = {
        "generated_at":datetime.now().isoformat(timespec="seconds"),
        "mode":"DEEP_OVERSOLD_CSI500_DECAY_ATTRIBUTION_V1",
        "status":"OK",
        "safety":{
            "uses_broker":False,
            "changes_forward_protocol":False,
            "creates_regime_filter":False,
            "changes_strategy_parameters":False,
        },
        "question":(
            "Why is CSI500 Deep Oversold stronger in F1 and thinner in F2/F3? "
            "Separate regime-mix change, within-regime/residual decay, opportunity "
            "density, and tail concentration."
        ),
        "method":{
            "primary_return":"signal-date equal-weighted T+1-open excess",
            "event_return":"open_excess_gross",
            "composition_effect":(
                "Change implied only by fold-specific regime weights, holding each "
                "regime at its pooled historical mean return."
            ),
            "residual":(
                "Observed fold change minus composition effect; this contains "
                "within-regime return changes and other non-composition effects, "
                "and is not claimed causal."
            ),
        },
        "fold_summary":fold,
        "regime_mix":mix,
        "attribution":attribution,
    }

    save_parquet(daily, OUT_PARQUET)
    with ResearchStore(args.db) as store:
        store.register_external_view(
            asset_id="DEEP_OVERSOLD_CSI500_DECAY_DAILY_V1",
            view_name="deep_oversold_csi500_decay_daily_v1",
            path=OUT_PARQUET,
            source_system="POSITIONX_DERIVED_RESEARCH",
            file_kind="parquet",
            asof_date="20260924",
            row_count=len(daily),
            metadata={"live_gate":False,"purpose":"temporal decay attribution"},
        )

    save_watchlist_snapshot(OUT_JSON,out)

    print(json.dumps(out["safety"], ensure_ascii=False, indent=2))
    print("\nDEEP OVERSOLD CSI500 — TEMPORAL DECAY ATTRIBUTION v1")
    print("\nFOLD SUMMARY")
    for r in fold:
        print(
            f"  {r['fold']}: sessions={r['market_sessions']} events={r['events']} "
            f"signal_dates={r['signal_dates']} signal_day_rate={r['signal_day_rate']:.1%} "
            f"events/day={r['events_per_signal_day']:.2f} "
            f"event_avg={r['event_weighted_avg']:+.2%} "
            f"date_eq={r['date_equal_avg']:+.2%} "
            f"trim5={r['event_trim5']:+.2%} "
            f"tail_lift={r['tail_lift_avg_minus_trim5']:+.2%}"
        )

    print("\nATTRIBUTION — negative means later fold is weaker")
    for key, pairs in attribution.items():
        print(f"  [{key}]")
        for pair_name, r in pairs.items():
            if not r:
                continue
            print(
                f"    {pair_name}: observed={r['observed_date_equal_change']:+.2%} "
                f"mix={r['composition_effect_using_pooled_bucket_means']:+.2%} "
                f"residual={r['residual_within_regime_and_other']:+.2%}"
            )

    print("\nINTERPRETATION RULE")
    print("  mix explains most -> state frequency changed")
    print("  residual explains most -> Edge weakened/strengthened inside similar states")
    print("  large tail_lift drop -> fewer extreme rebound winners")
    print("\nNO FORWARD RULE WAS CHANGED.")
    print(f"JSON: {OUT_JSON}")
    print(f"DAILY: {OUT_PARQUET}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
