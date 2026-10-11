
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "config/opportunity/e1_historical_price_backfill_v0_1.yaml"

KNOWN_CHANGE_TYPES = {
    "IMPROVING","DETERIORATING","TURN_TO_LOSS","TURNAROUND",
    "LOSS_NARROWING","LOSS_WIDENING","FLAT",
}

REQ_COLS = ["symbol","trade_date","open","high","low","close","pre_close"]


def duckdb_mod():
    import duckdb
    return duckdb


def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))


def sha256_file(p: Path) -> str:
    h=hashlib.sha256()
    with p.open("rb") as f:
        for ch in iter(lambda:f.read(1024*1024), b""):
            h.update(ch)
    return h.hexdigest()


def qt(x):
    return ".".join('"' + p.replace('"','""') + '"' for p in x.split("."))


def nb(s):
    return s.astype("string").str.strip().str.lower().isin(
        ["true","1","yes","y","eligible"]
    )


def choose(paths):
    for x in paths:
        p=Path(x)
        if p.exists():
            return p
    return None


def setup_events(df):
    x=df.copy()
    x["event_time"]=pd.to_datetime(x["event_time"],errors="coerce")
    x["event_date"]=x["event_time"].dt.normalize()
    kind=x["event_kind"].astype("string").str.upper().str.strip()
    change=x["change_type"].astype("string").str.upper().str.strip()
    m=(
        nb(x["strategy_eligible_semantics"])
        & kind.eq("ORIGINAL_FORECAST")
        & change.isin(KNOWN_CHANGE_TYPES)
        & x["event_time"].notna()
    )
    x=x[m].copy()
    x["symbol"]=x["symbol"].astype("string").str.upper().str.strip()
    return x


def prepare_request(cfg):
    src=Path(cfg["missing_symbols_csv"])
    if not src.exists():
        raise FileNotFoundError(src)

    df=pd.read_csv(src)
    need={"symbol_key","setup_events","first_event_date","last_event_date"}
    missing=need-set(df.columns)
    if missing:
        raise RuntimeError(f"missing-symbol CSV lacks columns {sorted(missing)}")

    df["symbol"]=df["symbol_key"].astype("string").str.upper().str.strip()
    df["first_event_date"]=pd.to_datetime(df["first_event_date"],errors="coerce")
    df["last_event_date"]=pd.to_datetime(df["last_event_date"],errors="coerce")
    df=df.drop_duplicates("symbol").sort_values(
        ["setup_events","symbol"],ascending=[False,True]
    ).reset_index(drop=True)

    out=Path(cfg["request_output_dir"])
    out.mkdir(parents=True,exist_ok=True)

    req=out/"E1_HISTORICAL_PRICE_BACKFILL_REQUEST_V0_1.csv"
    cols=[
        "symbol","board_bucket","setup_events",
        "first_event_date","last_event_date",
        "first_event_year","last_event_year"
    ]
    cols=[c for c in cols if c in df.columns]
    df[cols].to_csv(req,index=False,encoding="utf-8-sig")

    meta={
        "task_id":"E1_HISTORICAL_PRICE_BACKFILL_V0_1",
        "generated_at":datetime.now().isoformat(timespec="seconds"),
        "source_missing_symbols_csv":str(src),
        "source_missing_symbols_csv_sha256":sha256_file(src),
        "requested_symbol_count":int(len(df)),
        "requested_setup_events":int(pd.to_numeric(df["setup_events"],errors="coerce").fillna(0).sum()),
        "request_csv":str(req),
        "request_csv_sha256":sha256_file(req),
        "selection_policy":"ALL symbols absent from all three existing price tables; no outcome-conditioned subset",
    }
    mp=out/"REQUEST_MANIFEST.json"
    mp.write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding="utf-8")
    return meta


def normalize_bars(df):
    x=df.copy()
    missing=[c for c in REQ_COLS if c not in x.columns]
    if missing:
        raise RuntimeError(f"incoming parquet missing columns {missing}")

    x["symbol"]=x["symbol"].astype("string").str.upper().str.strip()
    x["trade_date"]=pd.to_datetime(x["trade_date"],errors="coerce").dt.normalize()
    for c in ["open","high","low","close","pre_close"]:
        x[c]=pd.to_numeric(x[c],errors="coerce")
    return x


def data_quality(x):
    n=len(x)
    if not n:
        return {
            "rows":0,
            "distinct_symbols":0,
            "duplicate_symbol_date_rows":0,
            "trade_date_nonnull_rate":0.0,
            "ohlc_nonnull_rate":0.0,
            "positive_price_rate":0.0,
            "high_low_consistency_rate":0.0,
            "pre_close_nonnull_rate":0.0,
        }

    ohlc=x[["open","high","low","close"]]
    ohlc_nonnull=ohlc.notna().all(axis=1)
    positive=ohlc.gt(0).all(axis=1)
    consistent=(
        x["high"].ge(x[["open","close","low"]].max(axis=1))
        & x["low"].le(x[["open","close","high"]].min(axis=1))
    )
    return {
        "rows":int(n),
        "distinct_symbols":int(x["symbol"].nunique(dropna=True)),
        "duplicate_symbol_date_rows":int(x[["symbol","trade_date"]].duplicated().sum()),
        "trade_date_nonnull_rate":float(x["trade_date"].notna().mean()),
        "ohlc_nonnull_rate":float(ohlc_nonnull.mean()),
        "positive_price_rate":float(positive.mean()),
        "high_low_consistency_rate":float(consistent.mean()),
        "pre_close_nonnull_rate":float(x["pre_close"].notna().mean()),
        "trade_date_min":str(x["trade_date"].min()),
        "trade_date_max":str(x["trade_date"].max()),
    }


def read_request(cfg):
    req=Path(cfg["request_output_dir"])/"E1_HISTORICAL_PRICE_BACKFILL_REQUEST_V0_1.csv"
    if not req.exists():
        raise FileNotFoundError("run prepare first")
    x=pd.read_csv(req)
    x["symbol"]=x["symbol"].astype("string").str.upper().str.strip()
    return x,req


def validate_manifest(manifest_path, requested_symbols):
    obj=json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    rows=obj.get("per_symbol_status")
    if not isinstance(rows,list):
        raise RuntimeError("SOURCE_MANIFEST.json missing per_symbol_status list")

    m=pd.DataFrame(rows)
    if "symbol" not in m.columns or "status" not in m.columns:
        raise RuntimeError("per_symbol_status requires symbol/status")

    m["symbol"]=m["symbol"].astype("string").str.upper().str.strip()
    m["status"]=m["status"].astype("string").str.upper().str.strip()
    known={"SUCCESS","UNAVAILABLE","ERROR"}
    invalid=sorted(set(m["status"].dropna())-known)

    requested=set(requested_symbols)
    attempted=set(m["symbol"])
    missing=requested-attempted
    extras=attempted-requested

    return {
        "requested_symbols":len(requested),
        "manifest_status_rows":int(len(m)),
        "attempted_requested_symbols":int(len(requested & attempted)),
        "attempt_rate":float(len(requested & attempted)/len(requested)) if requested else 0,
        "missing_attempt_symbols":sorted(missing)[:100],
        "extra_manifest_symbols":sorted(extras)[:100],
        "invalid_status_values":invalid,
        "status_counts":{
            str(k):int(v) for k,v in m["status"].value_counts(dropna=False).items()
        },
    }, m


def primary_bars(con,cfg):
    return con.execute(f"""
        select
            upper(trim(cast(symbol as varchar))) as symbol,
            trade_date, open, high, low, close, pre_close
        from {qt(cfg['primary_price_table'])}
    """).fetchdf()


def union_shadow(primary, backfill):
    a=normalize_bars(primary)
    b=normalize_bars(backfill)

    a["_source_priority"]=0
    b["_source_priority"]=1

    z=pd.concat([a,b],ignore_index=True,sort=False)
    z=z.sort_values(["symbol","trade_date","_source_priority"])
    # Existing canonical market data wins on overlapping symbol-date rows.
    z=z.drop_duplicates(["symbol","trade_date"],keep="first")
    return z.drop(columns=["_source_priority"])


def entry_mapping_coverage(events,bars):
    ev=setup_events(events)
    b=bars.copy()
    b["symbol"]=b["symbol"].astype("string").str.upper().str.strip()
    b["trade_date"]=pd.to_datetime(b["trade_date"],errors="coerce").dt.normalize()

    maxd=(
        b.dropna(subset=["symbol","trade_date"])
        .groupby("symbol")["trade_date"].max()
    )
    mapped=ev["symbol"].map(maxd)
    possible=mapped.notna() & (ev["event_date"] < mapped)
    return {
        "setup_rows":int(len(ev)),
        "setup_distinct_symbols":int(ev["symbol"].nunique()),
        "next_bar_possible_rows":int(possible.sum()),
        "next_bar_possible_rate":float(possible.mean()) if len(ev) else 0,
        "unmapped_rows":int((~possible).sum()),
    }


def check(cfg):
    req=Path(cfg["request_output_dir"])/"E1_HISTORICAL_PRICE_BACKFILL_REQUEST_V0_1.csv"
    incoming=choose(cfg["incoming_candidates"])
    manifest=choose(cfg["incoming_manifest_candidates"])
    out={
        "status":"PASS_E1_HISTORICAL_PRICE_BACKFILL_V0_1_CHECK",
        "request_exists":req.exists(),
        "request_path":str(req),
        "incoming_selected":str(incoming) if incoming else None,
        "manifest_selected":str(manifest) if manifest else None,
        "primary_market_read_only":True,
    }
    print(json.dumps(out,ensure_ascii=False,indent=2))
    return 0


def validate(cfg):
    request,req_path=read_request(cfg)
    requested=set(request["symbol"])

    incoming=choose(cfg["incoming_candidates"])
    manifest_path=choose(cfg["incoming_manifest_candidates"])
    if incoming is None:
        print(json.dumps({
            "status":"BLOCKED_E1_HISTORICAL_PRICE_BACKFILL_INPUT_NOT_FOUND",
            "incoming_candidates":cfg["incoming_candidates"]
        },ensure_ascii=False,indent=2))
        return 3
    if manifest_path is None:
        print(json.dumps({
            "status":"BLOCKED_E1_HISTORICAL_PRICE_BACKFILL_MANIFEST_NOT_FOUND",
            "manifest_candidates":cfg["incoming_manifest_candidates"]
        },ensure_ascii=False,indent=2))
        return 3

    bars=normalize_bars(pd.read_parquet(incoming))
    q=data_quality(bars)
    man,man_df=validate_manifest(manifest_path,requested)

    incoming_symbols=set(bars["symbol"].dropna())
    extra_bar_symbols=sorted(incoming_symbols-requested)
    successful_manifest=set(
        man_df.loc[man_df["status"].eq("SUCCESS"),"symbol"]
    )
    success_with_bars=len(successful_manifest & incoming_symbols)

    g=cfg["gate"]
    checks={
        "requested_symbols_expected":len(requested)==int(g["requested_symbols_expected"]),
        "request_symbol_attempt_rate_min":man["attempt_rate"]>=float(g["request_symbol_attempt_rate_min"]),
        "manifest_status_values_valid":len(man["invalid_status_values"])==0,
        "duplicate_symbol_date_max":q["duplicate_symbol_date_rows"]<=int(g["duplicate_symbol_date_max"]),
        "trade_date_nonnull_min":q["trade_date_nonnull_rate"]>=float(g["trade_date_nonnull_min"]),
        "ohlc_nonnull_min":q["ohlc_nonnull_rate"]>=float(g["ohlc_nonnull_min"]),
        "positive_price_rate_min":q["positive_price_rate"]>=float(g["positive_price_rate_min"]),
        "high_low_consistency_min":q["high_low_consistency_rate"]>=float(g["high_low_consistency_min"]),
        "pre_close_nonnull_min":q["pre_close_nonnull_rate"]>=float(g["pre_close_nonnull_min"]),
        "no_extra_bar_symbols":len(extra_bar_symbols)==0,
    }

    duckdb=duckdb_mod()
    con=duckdb.connect(cfg["catalog"],read_only=True)
    try:
        events=con.execute(f"select * from {qt(cfg['event_table'])}").fetchdf()
        primary=primary_bars(con,cfg)
    finally:
        con.close()

    before=entry_mapping_coverage(events,primary)
    shadow=union_shadow(primary,bars)
    after=entry_mapping_coverage(events,shadow)

    checks["union_entry_mapping_coverage_min"]=(
        after["next_bar_possible_rate"]
        >= float(g["union_entry_mapping_coverage_min"])
    )

    accepted=all(checks.values())

    outdir=Path(cfg["output_dir"])
    outdir.mkdir(parents=True,exist_ok=True)

    # Freeze accepted input as research-only durable asset, but never mutate primary market.
    artifacts={}
    if accepted:
        durable_dir=Path(cfg["shadow_asset"]["directory"])
        durable_dir.mkdir(parents=True,exist_ok=True)

        durable=durable_dir/cfg["shadow_asset"]["parquet_name"]
        shutil.copy2(incoming,durable)
        if sha256_file(durable)!=sha256_file(incoming):
            raise RuntimeError("durable backfill SHA mismatch")

        shadow_path=durable_dir/cfg["shadow_asset"]["shadow_union_parquet_name"]
        shadow.to_parquet(shadow_path,index=False)

        asset_manifest={
            "asset_id":"E1_HISTORICAL_PRICE_BACKFILL_V0_1",
            "created_at":datetime.now().isoformat(timespec="seconds"),
            "request_csv":str(req_path),
            "request_csv_sha256":sha256_file(req_path),
            "incoming_path":str(incoming),
            "incoming_sha256":sha256_file(incoming),
            "source_manifest_path":str(manifest_path),
            "source_manifest_sha256":sha256_file(manifest_path),
            "durable_backfill_path":str(durable),
            "durable_backfill_sha256":sha256_file(durable),
            "shadow_union_path":str(shadow_path),
            "shadow_union_sha256":sha256_file(shadow_path),
            "before_mapping":before,
            "after_mapping":after,
            "checks":checks,
            "note":"research-only shadow asset; market.daily_bars_v1 unchanged",
        }
        amp=durable_dir/cfg["shadow_asset"]["manifest_name"]
        amp.write_text(json.dumps(asset_manifest,ensure_ascii=False,indent=2),encoding="utf-8")

        artifacts={
            "durable_backfill":str(durable),
            "durable_backfill_sha256":sha256_file(durable),
            "shadow_union":str(shadow_path),
            "shadow_union_sha256":sha256_file(shadow_path),
            "asset_manifest":str(amp),
            "asset_manifest_sha256":sha256_file(amp),
        }

    result={
        "status":"PASS_E1_HISTORICAL_PRICE_BACKFILL_V0_1_RUN",
        "generated_at":datetime.now().isoformat(timespec="seconds"),
        "research_id":cfg["research_id"],
        "request":{
            "path":str(req_path),
            "sha256":sha256_file(req_path),
            "requested_symbols":len(requested),
        },
        "incoming":{
            "path":str(incoming),
            "sha256":sha256_file(incoming),
            "manifest_path":str(manifest_path),
            "manifest_sha256":sha256_file(manifest_path),
            "quality":q,
            "manifest_validation":man,
            "incoming_distinct_symbols":len(incoming_symbols),
            "successful_manifest_symbols_with_bars":success_with_bars,
            "extra_bar_symbols":extra_bar_symbols[:100],
        },
        "coverage":{
            "before":before,
            "shadow_union":after,
            "improvement_pp":(
                after["next_bar_possible_rate"]-before["next_bar_possible_rate"]
            )*100.0,
        },
        "checks":checks,
        "accepted":accepted,
        "verdict":(
            "ACCEPT_E1_HISTORICAL_PRICE_BACKFILL_V0_1"
            if accepted else
            "REJECT_OR_HOLD_E1_HISTORICAL_PRICE_BACKFILL_V0_1"
        ),
        "artifacts":artifacts,
        "next_action":(
            "If accepted: use the frozen shadow union to rerun E1 without changing the E1 mechanism or gates; promote market data only after research result is independently reproduced."
            if accepted else
            "Do not rerun E1 yet. Repair source coverage/quality; do not relax the 95% mapping gate."
        ),
        "safety":cfg["safety"],
    }
    sp=outdir/"e1_historical_price_backfill_v0_1_summary.json"
    sp.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False,indent=2))
    print(f"OUTPUT: {outdir}")
    return 0


def main():
    p=argparse.ArgumentParser()
    p.add_argument("command",choices=["prepare","check","run"])
    a=p.parse_args()
    cfg=load_cfg()
    if a.command=="prepare":
        try:
            r=prepare_request(cfg)
            print(json.dumps({
                "status":"PASS_E1_HISTORICAL_PRICE_BACKFILL_V0_1_PREPARE",
                "request":r,
                "handoff":"Use the package handoff/EVA_E1_HISTORICAL_PRICE_BACKFILL_TASK_V0_1.md"
            },ensure_ascii=False,indent=2))
            return 0
        except Exception as e:
            print(json.dumps({
                "status":"FAIL_CLOSED_E1_HISTORICAL_PRICE_BACKFILL_V0_1_PREPARE",
                "error":f"{type(e).__name__}: {e}"
            },ensure_ascii=False,indent=2))
            return 3
    if a.command=="check":
        return check(cfg)
    return validate(cfg)


if __name__=="__main__":
    raise SystemExit(main())
