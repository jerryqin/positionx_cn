
from __future__ import annotations
import argparse, json, math
from datetime import datetime
from pathlib import Path
import pandas as pd, yaml

ROOT=Path(__file__).resolve().parents[1]
CFG=ROOT/"config/opportunity/e1_market_price_coverage_audit_v0_2.yaml"
KNOWN={"IMPROVING","DETERIORATING","TURN_TO_LOSS","TURNAROUND","LOSS_NARROWING","LOSS_WIDENING","FLAT"}

def duckdb_mod():
    import duckdb
    return duckdb

def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))

def qt(x):
    return ".".join('"' + p.replace('"','""') + '"' for p in x.split("."))

def nb(s):
    return s.astype("string").str.strip().str.lower().isin(["true","1","yes","y","eligible"])

def setup_events(df):
    x=df.copy()
    x["event_time"]=pd.to_datetime(x["event_time"],errors="coerce")
    x["event_date"]=x["event_time"].dt.normalize()
    x["event_year"]=x["event_time"].dt.year.astype("Int64")
    kind=x["event_kind"].astype("string").str.upper().str.strip()
    change=x["change_type"].astype("string").str.upper().str.strip()
    m=nb(x["strategy_eligible_semantics"]) & kind.eq("ORIGINAL_FORECAST") & change.isin(KNOWN) & x["event_time"].notna()
    x=x[m].copy()
    x["symbol_key"]=x["symbol"].astype("string").str.upper().str.strip()
    return x

def exists(con, full):
    s,t=full.split(".",1)
    return bool(con.execute(
        "select count(*) from information_schema.tables where table_schema=? and table_name=?",
        [s,t]
    ).fetchone()[0])

def cols(con, full):
    s,t=full.split(".",1)
    return [r[0] for r in con.execute(
        "select column_name from information_schema.columns where table_schema=? and table_name=? order by ordinal_position",
        [s,t]
    ).fetchall()]

def profile(con, full):
    if not exists(con,full):
        return pd.DataFrame(columns=["symbol_key","min_trade_date","max_trade_date","row_count"])
    need={"symbol","trade_date","open","high","low","close"}
    have={c.lower() for c in cols(con,full)}
    if not need.issubset(have):
        raise RuntimeError(f"{full} missing {sorted(need-have)}")

    z=con.execute(f"""
        select
            upper(trim(cast(symbol as varchar))) as symbol_key,
            min(trade_date) as min_trade_date,
            max(trade_date) as max_trade_date,
            count(*) as row_count
        from {qt(full)}
        group by 1
    """).fetchdf()

    z["min_trade_date"]=pd.to_datetime(z["min_trade_date"],errors="coerce")
    z["max_trade_date"]=pd.to_datetime(z["max_trade_date"],errors="coerce")
    return z

def table_stats(setup, prof, name):
    z=setup.merge(prof,on="symbol_key",how="left",validate="many_to_one")
    z["present"]=z["row_count"].notna()
    z["possible"]=z["present"] & (z["event_date"] < z["max_trade_date"])
    by={}
    for y,g in z[z["event_year"].notna()].groupby("event_year"):
        by[str(int(y))]={
            "setup_rows":int(len(g)),
            "symbol_presence_rate":float(g["present"].mean()),
            "next_bar_possible_rate":float(g["possible"].mean())
        }
    return z,{
        "table":name,
        "market_distinct_symbols":int(prof["symbol_key"].nunique()) if len(prof) else 0,
        "symbol_presence_rate":float(z["present"].mean()) if len(z) else 0,
        "next_bar_possible_rate":float(z["possible"].mean()) if len(z) else 0,
        "by_year":by
    }

def board(s):
    c=str(s).split(".")[0]
    if c.startswith("68"): return "STAR"
    if c.startswith("60"): return "SH_MAIN"
    if c.startswith("30"): return "CHINEXT"
    if c.startswith("00"): return "SZ_MAIN"
    if c.startswith("20"): return "SZ_B"
    if c.startswith("90"): return "SH_B"
    if c[:2] in {"43","83","87","88","92"}: return "BEIJING"
    return "OTHER"

def registry(con,tables):
    if not exists(con,"research.asset_registry_v1"): return []
    have=set(cols(con,"research.asset_registry_v1"))
    wanted=[c for c in [
        "asset_id","asset_version","table_name","source_path",
        "source_sha256","row_count","promoted_at","metadata_json"
    ] if c in have]
    if "table_name" not in wanted: return []
    q=",".join(["?"]*len(tables))
    return con.execute(
        f"select {','.join(wanted)} from research.asset_registry_v1 where table_name in ({q})",
        tables
    ).fetchdf().to_dict(orient="records")

def run_audit(con,cfg):
    ev=con.execute(f"select * from {qt(cfg['event_table'])}").fetchdf()
    setup=setup_events(ev)
    profiles={t:profile(con,t) for t in cfg["price_tables"]}
    summaries={}
    matrices=[]
    for i,t in enumerate(cfg["price_tables"]):
        z,sm=table_stats(setup,profiles[t],t)
        summaries[t]=sm
        matrices.append(
            z[["announcementId","symbol_key","present","possible"]]
            .rename(columns={"present":f"present_{i}","possible":f"possible_{i}"})
        )

    comb=setup.copy()
    for i,m in enumerate(matrices):
        comb=comb.merge(m,on=["announcementId","symbol_key"],how="left",validate="one_to_one")

    pcols=[c for c in comb if c.startswith("present_")]
    xcols=[c for c in comb if c.startswith("possible_")]
    comb["any_present"]=comb[pcols].fillna(False).any(axis=1)
    comb["any_possible"]=comb[xcols].fillna(False).any(axis=1)

    primary_i=cfg["price_tables"].index(cfg["primary_price_table"])
    primary_col=f"possible_{primary_i}"
    primary_cov=float(comb[primary_col].fillna(False).mean())
    union_cov=float(comb["any_possible"].mean())

    other=[c for c in xcols if c!=primary_col]
    recoverable=(~comb[primary_col].fillna(False)) & comb[other].fillna(False).any(axis=1)
    no_any=~comb["any_present"]

    miss=comb[no_any].copy()
    miss["board_bucket"]=miss["symbol_key"].map(board)
    unique=(
        miss.groupby(["symbol_key","board_bucket"],dropna=False)
        .agg(
            setup_events=("announcementId","size"),
            first_event_date=("event_date","min"),
            last_event_date=("event_date","max"),
            first_event_year=("event_year","min"),
            last_event_year=("event_year","max"),
        )
        .reset_index()
        .sort_values(["setup_events","symbol_key"],ascending=[False,True])
    )

    board_sum=(
        unique.groupby("board_bucket")
        .agg(
            missing_symbols=("symbol_key","size"),
            setup_events=("setup_events","sum")
        )
        .reset_index()
        .sort_values("setup_events",ascending=False)
    )

    target=float(cfg["target_entry_mapping_coverage"])
    need=max(
        0,
        math.ceil(target*len(comb))
        - int(comb[primary_col].fillna(False).sum())
    )

    if primary_cov>=target:
        diag="PRIMARY_TABLE_ALREADY_MEETS_GATE"
        nexta="Inspect E1 actual entry join."
    elif union_cov>=target and int(recoverable.sum())>0:
        diag="CATALOG_ASSEMBLY_GAP_RECOVERABLE_FROM_EXISTING_TABLES"
        nexta="Fix daily_bars_v1 assembly from existing base/incremental; no external acquisition."
    else:
        diag="UPSTREAM_HISTORICAL_PRICE_ASSET_GAP"
        nexta="Build scoped historical/delisted price backfill asset for symbols absent from all existing tables."

    audit={
        "setup_rows":int(len(setup)),
        "setup_distinct_symbols":int(setup["symbol_key"].nunique()),
        "target_entry_mapping_coverage":target,
        "primary_price_table":cfg["primary_price_table"],
        "primary_next_bar_possible_rate":primary_cov,
        "union_existing_tables_next_bar_possible_rate":union_cov,
        "recoverable_from_other_existing_tables_rows":int(recoverable.sum()),
        "no_symbol_in_any_price_table_rows":int(no_any.sum()),
        "unique_symbols_absent_from_all_price_tables":int(len(unique)),
        "gap_to_95pct_event_mappings":int(need),
        "table_summaries":[summaries[t] for t in cfg["price_tables"]],
        "missing_board_summary":board_sum.to_dict(orient="records"),
        "diagnosis":diag,
        "next_action":nexta,
    }
    return audit,comb,unique,recoverable

def check(cfg):
    p=Path(cfg["catalog"])
    out={
        "status":"PASS_E1_MARKET_PRICE_COVERAGE_AUDIT_V0_2_1_CHECK" if p.exists() else "BLOCKED_E1_MARKET_PRICE_COVERAGE_AUDIT_V0_2_1_CHECK",
        "catalog":str(p),
        "catalog_exists":p.exists(),
        "price_tables":cfg["price_tables"],
        "primary_price_table":cfg["primary_price_table"],
        "target_entry_mapping_coverage":cfg["target_entry_mapping_coverage"],
        "read_only":True,
    }
    print(json.dumps(out,ensure_ascii=False,indent=2))
    return 0 if p.exists() else 3

def run(cfg):
    try:
        p=Path(cfg["catalog"])
        if not p.exists(): return check(cfg)

        duckdb=duckdb_mod()
        con=duckdb.connect(str(p),read_only=True)
        try:
            audit,comb,unique,recoverable=run_audit(con,cfg)
            reg=registry(con,cfg["price_tables"])
        finally:
            con.close()

        out=Path(cfg["output_dir"])
        out.mkdir(parents=True,exist_ok=True)

        p1=out/"symbols_absent_from_all_price_tables_v0_2.csv"
        unique.to_csv(p1,index=False,encoding="utf-8-sig")

        p2=out/"events_recoverable_from_other_price_tables_v0_2.csv"
        comb.loc[
            recoverable,
            ["announcementId","symbol","event_time","event_year","change_type"]
        ].to_csv(p2,index=False,encoding="utf-8-sig")

        res={
            "status":"PASS_E1_MARKET_PRICE_COVERAGE_AUDIT_V0_2_1_RUN",
            "generated_at":datetime.now().isoformat(timespec="seconds"),
            "research_id":cfg["research_id"],
            "audit":audit,
            "asset_registry_rows_for_price_tables":reg,
            "artifacts":{
                "symbols_absent_from_all_price_tables_csv":str(p1),
                "events_recoverable_from_other_price_tables_csv":str(p2)
            },
            "safety":{
                "read_only":True,
                "research_logic_changed":False,
                "gate_changed":False
            }
        }

        sp=out/"e1_market_price_coverage_audit_v0_2_summary.json"
        sp.write_text(
            json.dumps(res,ensure_ascii=False,indent=2,default=str),
            encoding="utf-8"
        )

        print(json.dumps(res,ensure_ascii=False,indent=2,default=str))
        print(f"OUTPUT: {out}")
        return 0

    except Exception as e:
        print(json.dumps({
            "status":"FAIL_CLOSED_E1_MARKET_PRICE_COVERAGE_AUDIT_V0_2_1",
            "error":f"{type(e).__name__}: {e}"
        },ensure_ascii=False,indent=2))
        return 3

def main():
    a=argparse.ArgumentParser()
    a.add_argument("command",choices=["check","run"])
    args=a.parse_args()
    cfg=load_cfg()
    raise SystemExit(check(cfg) if args.command=="check" else run(cfg))

if __name__=="__main__":
    main()
