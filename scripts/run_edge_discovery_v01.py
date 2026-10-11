from __future__ import annotations

import argparse, json, sqlite3, sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from xtquant import xtdata

from research.edge_discovery import (
    DiscoveryConfig, FEATURES, HORIZONS, build_panel, edge_studies,
    expand_membership_intervals, market_state, normalize_history,
    recent_factor_reward,
)
from research.watchlist_adapter import AdapterConfig, XtDataWatchlistAdapter, save_watchlist_snapshot

REFERENCE_DB = PROJECT_ROOT / "data" / "research" / "positionx_reference.db"


def load_intervals():
    if not REFERENCE_DB.exists():
        raise SystemExit(f"reference DB missing: {REFERENCE_DB}")
    with sqlite3.connect(REFERENCE_DB) as conn:
        cols=[r[1] for r in conn.execute("PRAGMA table_info(csi300_membership_interval)").fetchall()]
        need={"symbol","valid_from","valid_to_exclusive"}
        if not need.issubset(cols):
            raise SystemExit(f"interval schema mismatch: {cols}")
        return pd.read_sql_query(
            "SELECT symbol, valid_from, valid_to_exclusive "
            "FROM csi300_membership_interval ORDER BY symbol, valid_from", conn
        )


def one_frame(raw, symbol):
    if isinstance(raw, dict) and isinstance(raw.get(symbol), pd.DataFrame):
        return normalize_history(raw[symbol])
    return pd.DataFrame()


def download(symbol, start, end):
    try:
        xtdata.download_history_data(symbol, period="1d", start_time=start, end_time=end)
    except TypeError:
        xtdata.download_history_data(symbol, "1d", start, end)


def load_histories(symbols, start, end, refresh):
    if refresh:
        for i,s in enumerate(symbols,1):
            print(f"[EDGE DATA] refresh {i}/{len(symbols)} {s}")
            try: download(s,start,end)
            except Exception as e: print(f"[EDGE DATA] warning {s}: {e}")
    fields=["open","high","low","close","volume","amount"]
    try:
        raw=xtdata.get_market_data_ex(
            field_list=fields, stock_list=list(symbols), period="1d",
            start_time=start, end_time=end, count=-1,
            dividend_type="front", fill_data=False
        )
    except TypeError:
        raw=xtdata.get_market_data_ex(fields,list(symbols),"1d",start,end,-1,"front",False)
    out={}
    for s in symbols:
        df=one_frame(raw,s)
        if not df.empty: out[s]=df
    missing=[s for s in symbols if s not in out]
    for i,s in enumerate(missing,1):
        print(f"[EDGE DATA] fallback {i}/{len(missing)} {s}")
        try:
            one=xtdata.get_market_data_ex(
                field_list=fields, stock_list=[s], period="1d",
                start_time=start, end_time=end, count=-1,
                dividend_type="front", fill_data=False
            )
        except TypeError:
            one=xtdata.get_market_data_ex(fields,[s],"1d",start,end,-1,"front",False)
        df=one_frame(one,s)
        if not df.empty: out[s]=df
    return out


def main():
    ap=argparse.ArgumentParser(description="PositionX Opportunity / Edge Discovery v0.1")
    ap.add_argument("--start-date",default="20200803")
    ap.add_argument("--end-date",default=None)
    ap.add_argument("--benchmark",default="000300.SH")
    ap.add_argument("--refresh-history",action="store_true")
    ap.add_argument("--output",default="data/research/edge_discovery_latest.json")
    args=ap.parse_args()

    intervals=load_intervals()
    symbols=sorted(intervals["symbol"].astype(str).unique())
    end=args.end_date or datetime.now().strftime("%Y%m%d")
    dl_start=(datetime.strptime(args.start_date,"%Y%m%d")-timedelta(days=220)).strftime("%Y%m%d")
    if args.refresh_history:
        for i,symbol in enumerate(symbols+[args.benchmark],1):
            print(f"[EDGE DATA] refresh {i}/{len(symbols)+1} {symbol}")
            try:
                download(symbol, dl_start, end)
            except Exception as exc:
                print(f"[EDGE DATA] warning {symbol}: {exc}")

    adapter = XtDataWatchlistAdapter(
        xtdata,
        AdapterConfig(sector="", bars=2000, dividend_type="front"),
    )
    loaded = adapter.history(symbols+[args.benchmark])
    loaded = {
        sym: normalize_history(df)
        for sym, df in loaded.items()
        if df is not None and not df.empty
    }
    if args.benchmark not in loaded:
        raise SystemExit("benchmark history missing")
    benchmark=loaded.pop(args.benchmark)
    market_dates=[d for d in benchmark.index if args.start_date<=d<=end]
    missing=[s for s in symbols if s not in loaded]
    if missing:
        raise SystemExit(f"BLOCKED_INCOMPLETE_HISTORY missing={len(missing)} sample={missing[:20]}")
    membership=expand_membership_intervals(intervals,market_dates)
    counts=membership.groupby("date")["symbol"].nunique()
    bad=counts[(counts<295)|(counts>305)]
    if len(bad):
        raise SystemExit(f"BLOCKED_PIT_MEMBERSHIP_COUNT {bad.head().to_dict()}")

    cfg=DiscoveryConfig()
    panel=build_panel(loaded,membership,market_dates,benchmark,cfg)
    studies=edge_studies(panel,cfg)
    reward=recent_factor_reward(panel,cfg)
    latest=max(panel["date"])
    state=market_state(panel.loc[panel["date"]==latest])

    # Ordering only for research triage, never a strategy score.
    for x in studies:
        stability=x["same_sign_year_fraction"] or 0.0
        x["discovery_priority"]=abs(x["spread_top_minus_bottom"])*stability
    studies.sort(key=lambda x:x["discovery_priority"],reverse=True)

    safety={
        "uses_broker":False,"production_db_opened":False,
        "creates_trading_plan":False,"can_submit_order":False,
        "writes_reference_db":False,"changes_strategy_parameters":False,
    }
    out={
        "generated_at":datetime.now().isoformat(timespec="seconds"),
        "mode":"EDGE_DISCOVERY_V01","status":"OK","safety":safety,
        "scope":{
            "universe":"CSI300 PIT interval reference",
            "start_date":args.start_date,"end_date":latest,
            "pit_union_symbols":len(symbols),"history_loaded":len(loaded),
            "market_sessions":len(market_dates),"benchmark":args.benchmark,
            "features":FEATURES,"horizons":HORIZONS,
            "entry":"T close known -> exact next market-session open",
            "purpose":"discover anomalies, not approve strategies",
        },
        "latest_market_state":state,
        "recent_factor_reward":reward,
        "historical_edge_studies":studies,
        "limitations":[
            "v0.1 has no PIT sector, valuation/fundamental or event data",
            "no multiple-testing significance claim",
            "no fillability/capacity model at discovery stage",
        ],
    }
    path=PROJECT_ROOT/args.output
    path.parent.mkdir(parents=True,exist_ok=True)
    save_watchlist_snapshot(path,out)

    pd.DataFrame(studies).to_csv(
        path.with_name("edge_discovery_anomalies_latest.csv"),
        index=False,encoding="utf-8-sig"
    )
    flat=[]
    for r in reward:
        z={"feature":r["feature"],"latest_spread_date":r["latest_spread_date"]}
        for w in (20,60,120):
            for k,v in (r.get(f"w{w}") or {}).items(): z[f"w{w}_{k}"]=v
        flat.append(z)
    pd.DataFrame(flat).to_csv(
        path.with_name("edge_factor_reward_latest.csv"),
        index=False,encoding="utf-8-sig"
    )

    print(json.dumps(safety,ensure_ascii=False,indent=2))
    print("\nEDGE DISCOVERY v0.1")
    print(f"symbols={len(symbols)} sessions={len(market_dates)} latest={latest}")
    print("\nMARKET STATE")
    print(json.dumps(state,ensure_ascii=False,indent=2))
    print("\nRECENT FACTOR REWARD")
    for r in reward:
        parts=[r["feature"]]
        for w in (20,60,120):
            q=r.get(f"w{w}")
            if q: parts.append(f"{w}d={q['cumulative_spread']:+.2%} {q['reward_direction']}")
        print("  "+" | ".join(parts))
    print("\nTOP HISTORICAL ANOMALIES — discovery queue, not strategies")
    for x in studies[:15]:
        print(
            f"  {x['feature']:24s} h={x['horizon']:>2} "
            f"{x['direction']:13s} spread={x['spread_top_minus_bottom']:+.2%} "
            f"stable={x['same_sign_year_fraction']} n={min(x['n_top'],x['n_bottom'])}"
        )
    print(f"\nOUTPUT: {path}")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
