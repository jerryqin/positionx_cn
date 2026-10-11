
from __future__ import annotations
import argparse, hashlib, json, math, time
from pathlib import Path
import numpy as np
import pandas as pd
import yaml

ROOT=Path(__file__).resolve().parents[1]
CFG=ROOT/"config/opportunity/full_market_ret120_qmt_v0_2.yaml"

def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))

def sha256_file(path):
    h=hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""):
            h.update(b)
    return h.hexdigest()

def safe(v):
    if v is None or isinstance(v,(str,bool,int)): return v
    if isinstance(v,float): return v if math.isfinite(v) else None
    if isinstance(v,(np.integer,)): return int(v)
    if isinstance(v,(np.floating,)): return safe(float(v))
    if isinstance(v,pd.Timestamp): return v.strftime("%Y-%m-%d")
    if hasattr(v,"item"):
        try:return safe(v.item())
        except Exception:pass
    return str(v)

def json_write(path,obj):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    Path(path).write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=safe),encoding="utf-8")

def input_checks(cfg):
    rows=[]
    items=[
      ("frozen_ret120_panel","frozen_ret120_panel","expected_frozen_ret120_panel_sha256"),
      ("trend_edge_scan","frozen_source_trend_edge_scan","expected_trend_edge_scan_sha256"),
      ("replication_runner","frozen_source_replication_runner","expected_replication_runner_sha256"),
      ("full_market_universe","full_market_universe","expected_full_market_universe_sha256"),
      ("canonical_features","canonical_features","expected_canonical_features_sha256"),
    ]
    for name,key,hkey in items:
        p=Path(cfg["source"][key]); exp=cfg["source"][hkey]
        actual=sha256_file(p) if p.exists() else None
        rows.append({"name":name,"path":str(p),"exists":p.exists(),
                     "sha256":actual,"expected_sha256":exp,"sha_match":actual==exp})
    return rows,all(r["exists"] and r["sha_match"] for r in rows)

def discover_panel_columns(df):
    dc=next((x for x in ["trade_date","signal_date","date"] if x in df.columns),None)
    sc=next((x for x in ["symbol","code","ticker"] if x in df.columns),None)
    if dc is None or sc is None or "ret120" not in df.columns:
        raise RuntimeError(f"unsupported frozen panel schema date={dc} symbol={sc} ret120={'ret120' in df.columns}")
    return dc,sc

def assign_quintiles(df,value_col="ret120"):
    z=df.copy()
    z["signal_date"]=pd.to_datetime(z["signal_date"])
    z=z.sort_values(["signal_date","symbol"],kind="stable").reset_index(drop=True)
    z["bucket"]=None
    for _,idx in z.groupby("signal_date",sort=False).groups.items():
        ii=np.asarray(list(idx))
        vals=pd.to_numeric(z.loc[ii,value_col],errors="coerce")
        good=vals.notna() & np.isfinite(vals.to_numpy(dtype=float))
        vi=ii[good.to_numpy()]
        if len(vi)<5: continue
        ranks=pd.Series(z.loc[vi,value_col].to_numpy(),index=vi).rank(method="first")
        try:
            b=pd.qcut(ranks,5,labels=["Q1","Q2","Q3","Q4","Q5"])
        except ValueError:
            continue
        z.loc[vi,"bucket"]=b.astype(str).to_numpy()
    return z

def normalize_qmt_frame(symbol,obj):
    if obj is None:return pd.DataFrame(columns=["trade_date","symbol","close"])
    if isinstance(obj,pd.Series):
        df=obj.to_frame("close")
    else:
        df=pd.DataFrame(obj).copy()
    if df.empty:return pd.DataFrame(columns=["trade_date","symbol","close"])
    if "close" not in df.columns:
        close_cols=[c for c in df.columns if str(c).lower()=="close"]
        if not close_cols:return pd.DataFrame(columns=["trade_date","symbol","close"])
        df=df.rename(columns={close_cols[0]:"close"})
    if "time" in df.columns:
        raw=df["time"]
    else:
        raw=pd.Series(df.index,index=df.index)
    if pd.api.types.is_numeric_dtype(raw):
        vals=pd.to_numeric(raw,errors="coerce")
        med=vals.dropna().median() if vals.notna().any() else np.nan
        if pd.notna(med) and med>10**12:
            dt=pd.to_datetime(vals,unit="ms",errors="coerce")
        elif pd.notna(med) and med>10**9:
            dt=pd.to_datetime(vals,unit="s",errors="coerce")
        else:
            dt=pd.to_datetime(vals.astype("Int64").astype(str),format="%Y%m%d",errors="coerce")
    else:
        txt=raw.astype(str).str.replace(r"\.0$","",regex=True)
        dt=pd.to_datetime(txt,format="%Y%m%d",errors="coerce")
        if dt.isna().all():
            dt=pd.to_datetime(txt,errors="coerce")
    out=pd.DataFrame({"trade_date":dt.to_numpy(),
                      "symbol":str(symbol),
                      "close":pd.to_numeric(df["close"],errors="coerce").to_numpy()})
    out=out[out["trade_date"].notna()].copy()
    out=out.sort_values("trade_date").drop_duplicates(["trade_date"],keep="last")
    out.loc[~np.isfinite(out["close"]) | out["close"].le(0),"close"]=np.nan
    return out.reset_index(drop=True)

def qmt_call(xtdata,symbols,cfg):
    kw=dict(field_list=["close"],stock_list=list(symbols),period=cfg["signal_layer"]["period"],
            start_time=cfg["signal_layer"]["start_time"],end_time=cfg["signal_layer"]["end_time"],
            dividend_type=cfg["signal_layer"]["dividend_type"],fill_data=bool(cfg["signal_layer"]["fill_data"]))
    attempts=[
      {**kw,"count":-1},
      kw,
    ]
    last=None
    for a in attempts:
        try:return xtdata.get_market_data_ex(**a)
        except TypeError as e:last=e
    raise last

def universe_symbols(cfg):
    u=pd.read_parquet(cfg["source"]["full_market_universe"],columns=["symbol"])
    return sorted(u["symbol"].astype(str).dropna().unique())

def acquire(cfg,force=False):
    outdir=Path(cfg["output_dir"]);outdir.mkdir(parents=True,exist_ok=True)
    cache=Path(cfg["signal_layer"]["cache_dir"]);cache.mkdir(parents=True,exist_ok=True)
    merged=Path(cfg["signal_layer"]["merged_signal_history"])
    if merged.exists() and not force:
        print(json.dumps({"status":"REUSE_QMT_BACK_RATIO_SIGNAL_HISTORY",
                          "path":str(merged),"sha256":sha256_file(merged)},ensure_ascii=False,indent=2))
        return 0
    from xtquant import xtdata
    try:xtdata.enable_hello=False
    except Exception:pass

    syms=universe_symbols(cfg);bs=int(cfg["signal_layer"]["batch_size"])
    manifests=[]
    for k in range(0,len(syms),bs):
        batch=syms[k:k+bs];bi=k//bs
        p=cache/f"batch_{bi:04d}.parquet"
        if p.exists() and not force:
            manifests.append({"batch":bi,"symbols":len(batch),"path":str(p),"reused":True})
            print(f"[acquire] reuse batch {bi:04d} {k+len(batch)}/{len(syms)}")
            continue
        data=qmt_call(xtdata,batch,cfg)
        frames=[]
        if isinstance(data,dict):
            for s in batch:frames.append(normalize_qmt_frame(s,data.get(s)))
        else:
            raise RuntimeError(f"unexpected QMT get_market_data_ex type: {type(data)}")
        z=pd.concat(frames,ignore_index=True) if frames else pd.DataFrame(columns=["trade_date","symbol","close"])
        z.to_parquet(p,index=False)
        manifests.append({"batch":bi,"symbols":len(batch),"rows":len(z),"path":str(p),"reused":False})
        print(f"[acquire] batch {bi:04d} rows={len(z)} {k+len(batch)}/{len(syms)}")
    allp=sorted(cache.glob("batch_*.parquet"))
    z=pd.concat([pd.read_parquet(p) for p in allp],ignore_index=True)
    z["trade_date"]=pd.to_datetime(z["trade_date"])
    z=z.sort_values(["symbol","trade_date"]).drop_duplicates(["symbol","trade_date"],keep="last").reset_index(drop=True)
    z.to_parquet(merged,index=False)
    result={"status":"PASS_QMT_BACK_RATIO_SIGNAL_HISTORY_ACQUIRED","symbols_requested":len(syms),
            "symbols_returned":int(z["symbol"].nunique()),"rows":len(z),
            "date_min":safe(z["trade_date"].min()),"date_max":safe(z["trade_date"].max()),
            "path":str(merged),"sha256":sha256_file(merged),"batches":len(allp)}
    json_write(outdir/"qmt_signal_history_manifest_v0_2.json",result)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0

def qmt_ret120_history(cfg):
    p=Path(cfg["signal_layer"]["merged_signal_history"])
    if not p.exists():raise RuntimeError("QMT signal history missing; run acquire first")
    x=pd.read_parquet(p)
    x["trade_date"]=pd.to_datetime(x["trade_date"])
    x["symbol"]=x["symbol"].astype(str)
    x=x.sort_values(["symbol","trade_date"]).reset_index(drop=True)
    x["ret120"]=x.groupby("symbol",sort=False)["close"].transform(lambda s:s/s.shift(120)-1.0)
    return x

def lineage(cfg):
    frozen=pd.read_parquet(cfg["source"]["frozen_ret120_panel"])
    dc,sc=discover_panel_columns(frozen)
    f=frozen[[dc,sc,"ret120"]].copy().rename(columns={dc:"signal_date",sc:"symbol","ret120":"frozen_ret120"})
    f["signal_date"]=pd.to_datetime(f["signal_date"]);f["symbol"]=f["symbol"].astype(str)
    a=pd.Timestamp(cfg["window"]["signal_start"]);b=pd.Timestamp(cfg["window"]["signal_cutoff_h120"])
    f=f[(f["signal_date"]>=a)&(f["signal_date"]<=b)].copy()

    # frozen buckets are rebuilt from the frozen panel itself.
    fb=assign_quintiles(f.rename(columns={"frozen_ret120":"ret120"}),"ret120")[["signal_date","symbol","bucket"]]
    fb=fb.rename(columns={"bucket":"frozen_bucket"})
    f=f.merge(fb,on=["signal_date","symbol"],how="left",validate="one_to_one")

    q=qmt_ret120_history(cfg)[["trade_date","symbol","ret120"]].rename(
        columns={"trade_date":"signal_date","ret120":"qmt_ret120"})
    j=f.merge(q,on=["signal_date","symbol"],how="left",validate="one_to_one")

    qb=assign_quintiles(j[["signal_date","symbol","qmt_ret120"]].rename(columns={"qmt_ret120":"ret120"}),"ret120")
    qb=qb[["signal_date","symbol","bucket"]].rename(columns={"bucket":"qmt_bucket"})
    j=j.merge(qb,on=["signal_date","symbol"],how="left",validate="one_to_one")

    missing=int(j["qmt_ret120"].isna().sum())
    fq=set(map(tuple,j.loc[j["frozen_bucket"].eq("Q5"),["signal_date","symbol"]].itertuples(index=False,name=None)))
    qq=set(map(tuple,j.loc[j["qmt_bucket"].eq("Q5"),["signal_date","symbol"]].itertuples(index=False,name=None)))
    mismatch=len(fq.symmetric_difference(qq))
    both=j[["frozen_ret120","qmt_ret120"]].dropna()
    max_abs=float((both["frozen_ret120"]-both["qmt_ret120"]).abs().max()) if len(both) else None
    exact=(missing==0 and mismatch==0)

    outdir=Path(cfg["output_dir"]);outdir.mkdir(parents=True,exist_ok=True)
    sample=j[(j["frozen_bucket"]!=j["qmt_bucket"]) | j["qmt_ret120"].isna()].head(5000)
    sample.to_csv(outdir/"lineage_mismatch_sample.csv",index=False)
    out={"status":"PASS_FULL_MARKET_RET120_QMT_SIGNAL_LINEAGE_EXACT" if exact else "FAIL_CLOSED_FULL_MARKET_RET120_QMT_SIGNAL_LINEAGE",
         "rows":len(j),"signal_dates":int(j["signal_date"].nunique()),
         "missing_qmt_ret120_rows":missing,"q5_membership_mismatch_rows":mismatch,
         "frozen_q5_rows":len(fq),"qmt_q5_rows":len(qq),
         "q5_jaccard":len(fq&qq)/len(fq|qq) if (fq|qq) else None,
         "max_abs_ret120_diff_on_matched_rows":max_abs,
         "frozen_panel_sha256":sha256_file(cfg["source"]["frozen_ret120_panel"]),
         "qmt_signal_history_sha256":sha256_file(cfg["signal_layer"]["merged_signal_history"]),
         "decision":"Full-market RET120 may run only after exact frozen CSI500 lineage reproduction from QMT back_ratio history."}
    json_write(outdir/"qmt_signal_lineage_v0_2.json",out)
    print(json.dumps(out,ensure_ascii=False,indent=2))
    return out

def load_features(cfg):
    z=pd.read_parquet(cfg["source"]["canonical_features"],columns=["trade_date","symbol","adj_open","adj_close"])
    z["trade_date"]=pd.to_datetime(z["trade_date"]);z["symbol"]=z["symbol"].astype(str)
    return z.sort_values(["symbol","trade_date"]).reset_index(drop=True)

def load_universe(cfg):
    u=pd.read_parquet(cfg["source"]["full_market_universe"],columns=["trade_date","symbol"])
    u["trade_date"]=pd.to_datetime(u["trade_date"]);u["symbol"]=u["symbol"].astype(str)
    a=pd.Timestamp(cfg["window"]["signal_start"]);b=pd.Timestamp(cfg["window"]["signal_cutoff_h120"])
    return u[(u["trade_date"]>=a)&(u["trade_date"]<=b)].drop_duplicates(["trade_date","symbol"])

def monotonicity(means):
    vals=[means.get(f"Q{i}") for i in range(1,6)]
    if any(v is None or not math.isfinite(float(v)) for v in vals):return None
    return sum(float(vals[i])>=float(vals[i-1]) for i in range(1,5))/4.0

def build_events(cfg):
    q=qmt_ret120_history(cfg)[["trade_date","symbol","ret120"]]
    u=load_universe(cfg)
    s=u.merge(q,on=["trade_date","symbol"],how="left").rename(columns={"trade_date":"signal_date"})
    s=assign_quintiles(s,"ret120")
    feat=load_features(cfg)
    dates=sorted(feat["trade_date"].dropna().unique())
    pos={pd.Timestamp(d):i for i,d in enumerate(dates)}
    fi=feat.set_index(["symbol","trade_date"])[["adj_open","adj_close"]]
    rows=[]
    for r in s.itertuples(index=False):
        if r.bucket is None or pd.isna(r.ret120):continue
        i=pos.get(pd.Timestamp(r.signal_date))
        if i is None or i+120>=len(dates):continue
        d1=pd.Timestamp(dates[i+1]);d20=pd.Timestamp(dates[i+20]);d60=pd.Timestamp(dates[i+60]);d120=pd.Timestamp(dates[i+120])
        try:
            ep=float(fi.loc[(r.symbol,d1),"adj_open"]);c20=float(fi.loc[(r.symbol,d20),"adj_close"])
            c60=float(fi.loc[(r.symbol,d60),"adj_close"]);c120=float(fi.loc[(r.symbol,d120),"adj_close"])
        except Exception:continue
        if not all(math.isfinite(v) and v>0 for v in [ep,c20,c60,c120]):continue
        rows.append({"signal_date":pd.Timestamp(r.signal_date),"symbol":r.symbol,"ret120":float(r.ret120),"bucket":r.bucket,
                     "raw20":c20/ep-1,"raw60":c60/ep-1,"raw120":c120/ep-1})
    e=pd.DataFrame(rows)
    for h in [20,60,120]:
        med=e.groupby("signal_date")[f"raw{h}"].median().rename(f"market{h}")
        e=e.merge(med,on="signal_date",how="left")
        e[f"adj{h}"]=e[f"raw{h}"]-e[f"market{h}"]
    return s,e

def summarize(events,h,cfg):
    col=f"adj{h}"
    means={str(k):float(v) for k,v in events.groupby("bucket")[col].mean().items()}
    spread=means.get("Q5",np.nan)-means.get("Q1",np.nan)
    mono=monotonicity(means)
    folds=[]
    for name,bounds in cfg["frozen_folds"].items():
        a,b=map(pd.Timestamp,bounds)
        g=events[(events["signal_date"]>=a)&(events["signal_date"]<=b)]
        m=g.groupby("bucket")[col].mean().to_dict()
        sp=float(m["Q5"]-m["Q1"]) if "Q5" in m and "Q1" in m else None
        folds.append({"fold":name,"rows":len(g),"q5_minus_q1":sp,"positive":bool(sp is not None and sp>0)})
    return {"horizon":h,"bucket_adjusted_means":means,"q5_minus_q1":float(spread),
            "monotonicity":mono,"positive_folds":sum(x["positive"] for x in folds),"folds":folds}

def verdict(metrics,cfg):
    g=cfg["frozen_fast_reject_gate"]
    checks={
      "all_horizon_q5_minus_q1_positive":all(m["q5_minus_q1"]>0 for m in metrics),
      "at_least_2_horizons_monotonicity_ge_0_5":sum(m["monotonicity"] is not None and m["monotonicity"]>=float(g["at_least_2_horizons_monotonicity_ge"]) for m in metrics)>=2,
      "each_horizon_at_least_2_of_3_positive_folds":all(m["positive_folds"]>=int(g["each_horizon_positive_folds_min"]) for m in metrics),
    }
    return ("CANDIDATE" if all(checks.values()) else "REJECT"),checks

def run(cfg):
    lin=lineage(cfg)
    if lin["status"]!="PASS_FULL_MARKET_RET120_QMT_SIGNAL_LINEAGE_EXACT":
        raise RuntimeError("exact QMT frozen lineage not proven")
    s,e=build_events(cfg)
    ms=[summarize(e,h,cfg) for h in [20,60,120]]
    v,checks=verdict(ms,cfg)
    outdir=Path(cfg["output_dir"]);outdir.mkdir(parents=True,exist_ok=True)
    ep=outdir/"full_market_ret120_qmt_events_v0_2.parquet";e.to_parquet(ep,index=False)
    result={"status":"PASS_FULL_MARKET_RET120_QMT_V0_2_RUN","research_id":cfg["research_id"],
            "signal_lineage":lin,"definition":cfg["definition"],"coverage":{
              "signal_panel_rows":len(s),"signal_dates":int(s["signal_date"].nunique()),
              "symbols":int(s["symbol"].nunique()),"complete_event_rows":len(e),
              "q5_rows":int((e["bucket"]=="Q5").sum()),"q1_rows":int((e["bucket"]=="Q1").sum())},
            "metrics":{f"H{m['horizon']}":m for m in ms},
            "gate":{"verdict":v,"checks":checks,"rule":cfg["frozen_fast_reject_gate"]},
            "interpretation_contract":"Signal layer uses exact QMT back_ratio semantics. Canonical synthetic prices are economic measurement only. Existing CSI500 RET120 Strategy #2 / Forward remain unchanged.",
            "next_if_candidate":"Only then consider a full-market RET120 standalone portfolio under frozen 1%/30%/30-position policy.",
            "next_if_reject":"Freeze full-market RET120 transfer; do not add filters or tune bucket rules.",
            "artifacts":{"events":str(ep),"events_sha256":sha256_file(ep)},"safety":cfg["safety"]}
    json_write(outdir/"full_market_ret120_qmt_v0_2_summary.json",result)
    print(json.dumps(result,ensure_ascii=False,indent=2,default=safe))
    return 0

def check(cfg):
    rows,ok=input_checks(cfg)
    out={"status":"PASS_FULL_MARKET_RET120_QMT_V0_2_CHECK" if ok else "BLOCKED_FULL_MARKET_RET120_QMT_V0_2_CHECK",
         "inputs":rows,"signal_layer":cfg["signal_layer"],"definition":cfg["definition"],
         "frozen_fast_reject_gate":cfg["frozen_fast_reject_gate"],"safety":cfg["safety"]}
    print(json.dumps(out,ensure_ascii=False,indent=2))
    return 0 if ok else 3

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("command",choices=["check","acquire","lineage","run"])
    ap.add_argument("--force",action="store_true")
    a=ap.parse_args();cfg=load_cfg()
    try:
        if a.command=="check":rc=check(cfg)
        elif a.command=="acquire":rc=acquire(cfg,a.force)
        elif a.command=="lineage":
            out=lineage(cfg);rc=0 if out["status"].startswith("PASS_") else 3
        else:rc=run(cfg)
    except Exception as e:
        print(json.dumps({"status":"FAIL_CLOSED_FULL_MARKET_RET120_QMT_V0_2",
                          "error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2))
        rc=3
    raise SystemExit(rc)

if __name__=="__main__":
    main()
