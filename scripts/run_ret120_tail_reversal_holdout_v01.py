
from __future__ import annotations
import argparse,hashlib,importlib.util,json,math,sys
from pathlib import Path
import numpy as np
import pandas as pd
import yaml

ROOT=Path(__file__).resolve().parents[1]
CFG=ROOT/"config/opportunity/ret120_tail_reversal_holdout_v0_1.yaml"

def C():return yaml.safe_load(CFG.read_text(encoding="utf-8"))
def sha(p):
    h=hashlib.sha256()
    with Path(p).open("rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""):h.update(b)
    return h.hexdigest()
def safe(v):
    if v is None or isinstance(v,(str,bool,int)):return v
    if isinstance(v,float):return v if math.isfinite(v) else None
    if isinstance(v,(np.integer,)):return int(v)
    if isinstance(v,(np.floating,)):return safe(float(v))
    if isinstance(v,pd.Timestamp):return v.strftime("%Y-%m-%d")
    return str(v)
def writej(p,x):
    Path(p).parent.mkdir(parents=True,exist_ok=True)
    Path(p).write_text(json.dumps(x,ensure_ascii=False,indent=2,default=safe),encoding="utf-8")
def resolve(paths):
    for p in paths:
        if Path(p).exists():return Path(p)
    return None
def scanner(c):
    p=Path(c["source"]["exact_scanner_source"])
    sp=importlib.util.spec_from_file_location("px_tail_holdout_scanner",p)
    m=importlib.util.module_from_spec(sp);sys.modules[sp.name]=m;sp.loader.exec_module(m)
    return m
def cols(df):
    dc=next((x for x in ["signal_date","trade_date","date"] if x in df.columns),None)
    sc=next((x for x in ["symbol","code","ticker"] if x in df.columns),None)
    if not dc or not sc:raise RuntimeError("panel schema unsupported")
    return dc,sc

def checks(c):
    base=[
      ("scanner",c["source"]["exact_scanner_source"],c["source"]["expected_scanner_source_sha256"]),
      ("ret120",c["source"]["current_qmt_ret120"],c["source"]["expected_current_qmt_ret120_sha256"]),
      ("full_market",c["source"]["full_market_universe"],c["source"]["expected_full_market_universe_sha256"]),
      ("features",c["source"]["canonical_features"],c["source"]["expected_canonical_features_sha256"])]
    rows=[];ok=True
    for n,p,e in base:
        P=Path(p);a=sha(P) if P.exists() else None;g=P.exists() and a==e
        rows.append({"name":n,"path":str(P),"exists":P.exists(),"sha256":a,"expected":e,"pass":g});ok&=g
    panels={}
    for tag in ["csi300","csi500","csi1000"]:
        P=resolve(c["source"][f"{tag}_panel_candidates"]);panels[tag]=str(P) if P else None;ok&=P is not None
    return rows,panels,ok

def membership_panel(p,start,end):
    x=pd.read_parquet(p);dc,sc=cols(x)
    x=x[[dc,sc]].rename(columns={dc:"signal_date",sc:"symbol"})
    x["signal_date"]=pd.to_datetime(x["signal_date"]);x["symbol"]=x["symbol"].astype(str)
    return x[(x["signal_date"]>start)&(x["signal_date"]<=end)].drop_duplicates(["signal_date","symbol"])

def memberships(c):
    start=pd.Timestamp(c["holdout"]["signal_start_exclusive"])
    end=pd.Timestamp(c["holdout"]["market_data_through"])
    idx=[]
    audits={}
    for tag,n in [("csi300",300),("csi500",500),("csi1000",1000)]:
        P=resolve(c["source"][f"{tag}_panel_candidates"])
        z=membership_panel(P,start,end)
        cnt=z.groupby("signal_date")["symbol"].nunique()
        audits[tag]={"rows":len(z),"dates":int(z["signal_date"].nunique()),
                     "min_members":int(cnt.min()) if len(cnt) else 0,
                     "max_members":int(cnt.max()) if len(cnt) else 0,
                     "expected":n,"bad_dates":int((cnt!=n).sum())}
        if audits[tag]["bad_dates"]:raise RuntimeError(f"{tag} bad membership counts")
        idx.append(z)
    csi1800=pd.concat(idx,ignore_index=True)
    if csi1800.duplicated(["signal_date","symbol"]).any():raise RuntimeError("index overlap in holdout")
    fm=pd.read_parquet(c["source"]["full_market_universe"],columns=["trade_date","symbol"]).rename(columns={"trade_date":"signal_date"})
    fm["signal_date"]=pd.to_datetime(fm["signal_date"]);fm["symbol"]=fm["symbol"].astype(str)
    fm=fm[(fm["signal_date"]>start)&(fm["signal_date"]<=end)].drop_duplicates(["signal_date","symbol"])
    ex=fm.merge(csi1800,on=["signal_date","symbol"],how="left",indicator=True)
    ex=ex.loc[ex["_merge"].eq("left_only"),["signal_date","symbol"]]
    return {"CSI1800_HOLDOUT":csi1800,"EX_CSI1800_HOLDOUT":ex,"FULL_MARKET_HOLDOUT":fm},audits

def ret120(c):
    z=pd.read_parquet(c["source"]["current_qmt_ret120"],columns=["trade_date","symbol","ret120"])
    z["trade_date"]=pd.to_datetime(z["trade_date"]);z["symbol"]=z["symbol"].astype(str);return z

def bucket(mem,r,sc,c):
    z=mem.merge(r,left_on=["signal_date","symbol"],right_on=["trade_date","symbol"],how="left").drop(columns=["trade_date"])
    z=z.sort_values(["signal_date","symbol"],kind="stable").reset_index(drop=True)
    out=[];sk=0
    for d,g in z.groupby("signal_date",sort=True):
        b=sc.assign_quintiles(g["ret120"])
        need=max(int(c["signal"]["daily_feature_coverage_min_absolute"]),
                 int(round(len(g)*float(c["signal"]["daily_feature_coverage_min_fraction"]))))
        if int(b.notna().sum())<need:sk+=1;continue
        q=g[["signal_date","symbol","ret120"]].copy();q["bucket"]=b.to_numpy();out.append(q)
    x=pd.concat(out,ignore_index=True) if out else pd.DataFrame(columns=["signal_date","symbol","ret120","bucket"])
    return x,{"rows":len(x),"dates":int(x["signal_date"].nunique()) if len(x) else 0,
              "valid_ret120":int(x["bucket"].notna().sum()) if len(x) else 0,"skipped_dates":sk}

def calendar(c):
    x=pd.read_parquet(c["source"]["full_market_universe"],columns=["trade_date"])
    return sorted(pd.to_datetime(x["trade_date"]).dropna().unique().tolist())

def features(c):
    f=pd.read_parquet(c["source"]["canonical_features"],columns=["trade_date","symbol","adj_open","adj_close"])
    f["trade_date"]=pd.to_datetime(f["trade_date"]);f["symbol"]=f["symbol"].astype(str);return f

def complete_signals(sig,cal,h):
    pos={pd.Timestamp(d):i for i,d in enumerate(cal)}
    maps=[]
    for d in sorted(sig["signal_date"].dropna().unique()):
        d=pd.Timestamp(d);i=pos.get(d)
        if i is not None and i+h<len(cal):
            maps.append({"signal_date":d,"entry_date":pd.Timestamp(cal[i+1]),"exit_date":pd.Timestamp(cal[i+h])})
    dm=pd.DataFrame(maps)
    return sig.merge(dm,on="signal_date",how="inner"),dm

def outcome(sig,f,cal,h):
    s,dm=complete_signals(sig,cal,h)
    e=f[["trade_date","symbol","adj_open"]].rename(columns={"trade_date":"entry_date","adj_open":"entry_open"})
    x=f[["trade_date","symbol","adj_close"]].rename(columns={"trade_date":"exit_date","adj_close":"exit_close"})
    s=s.merge(e,on=["entry_date","symbol"],how="left").merge(x,on=["exit_date","symbol"],how="left")
    ep=pd.to_numeric(s["entry_open"],errors="coerce");xp=pd.to_numeric(s["exit_close"],errors="coerce")
    s["raw"]=np.where((ep>0)&(xp>0),xp/ep-1.0,np.nan)
    return s,{"first_signal":safe(dm["signal_date"].min()) if len(dm) else None,
              "last_signal":safe(dm["signal_date"].max()) if len(dm) else None,
              "complete_signal_dates":len(dm)}

def add_benchmark(outcomes):
    fm=outcomes["FULL_MARKET_HOLDOUT"]
    med=fm.groupby("signal_date")["raw"].median()
    out={}
    for n,x in outcomes.items():
        z=x.copy();z["benchmark"]=z["signal_date"].map(med);z["excess"]=z["raw"]-z["benchmark"];out[n]=z
    return out

def daily_rows(e,h):
    rows=[]
    for d,g in e.groupby("signal_date",sort=True):
        v=g[g["bucket"].notna()&g["excess"].notna()]
        for q,qg in v.groupby("bucket",sort=True):
            if len(qg)>=10:
                rows.append({"signal_date":pd.Timestamp(d),"feature":"ret120","horizon":h,"bucket":str(q),
                             "n":len(qg),"mean_excess":float(qg["excess"].mean()),
                             "median_excess":float(qg["excess"].median())})
    return pd.DataFrame(rows)

def summary(sc,d):
    x=sc.summarize_daily_buckets(d) if len(d) else sc.empty_summary()
    return {"spread":safe(x["q5_minus_q1"]),"monotonicity":safe(x["monotonicity"]),
            "qmeans":{k:safe(v) for k,v in x.get("qmeans",{}).items()},
            "daily_bucket_rows":len(d),"signal_dates":int(d["signal_date"].nunique()) if len(d) else 0}

def gate(R):
    checks={
      "tail_H20_q5_minus_q1_negative":float(R["EX_CSI1800_HOLDOUT"]["H20"]["spread"])<0,
      "tail_H60_q5_minus_q1_negative":float(R["EX_CSI1800_HOLDOUT"]["H60"]["spread"])<0,
      "tail_H20_monotonicity_negative":float(R["EX_CSI1800_HOLDOUT"]["H20"]["monotonicity"])<0,
      "tail_H60_monotonicity_negative":float(R["EX_CSI1800_HOLDOUT"]["H60"]["monotonicity"])<0,
      "tail_H20_spread_less_than_csi1800":float(R["EX_CSI1800_HOLDOUT"]["H20"]["spread"])<float(R["CSI1800_HOLDOUT"]["H20"]["spread"]),
      "tail_H60_spread_less_than_csi1800":float(R["EX_CSI1800_HOLDOUT"]["H60"]["spread"])<float(R["CSI1800_HOLDOUT"]["H60"]["spread"]),
    }
    return {"status":"HOLDOUT_SUPPORTS_TAIL_REVERSAL_HYPOTHESIS" if all(checks.values()) else "HOLDOUT_DOES_NOT_SUPPORT_TAIL_REVERSAL_HYPOTHESIS",
            "checks":checks}

def check(c):
    rows,panels,ok=checks(c)
    out={"status":"PASS_RET120_TAIL_REVERSAL_HOLDOUT_V0_1_CHECK" if ok else "BLOCKED_RET120_TAIL_REVERSAL_HOLDOUT_V0_1_CHECK",
         "inputs":rows,"resolved_index_panels":panels,"holdout":c["holdout"],"hypothesis":c["hypothesis"],"safety":c["safety"]}
    print(json.dumps(out,ensure_ascii=False,indent=2));return 0 if ok else 3

def run(c):
    rows,panels,ok=checks(c)
    if not ok:raise RuntimeError("input gate failed")
    sc=scanner(c);M,aud=memberships(c);r=ret120(c);cal=calendar(c);f=features(c)
    buckets={};cover={}
    for n,m in M.items():buckets[n],cover[n]=bucket(m,r,sc,c)
    R={n:{} for n in M};windows={}
    outdir=Path(c["output_dir"]);outdir.mkdir(parents=True,exist_ok=True)
    for h in [20,60]:
        outs={};win=None
        for n,b in buckets.items():
            outs[n],w=outcome(b,f,cal,h)
            if n=="FULL_MARKET_HOLDOUT":win=w
        outs=add_benchmark(outs);windows[f"H{h}"]=win
        for n,e in outs.items():
            d=daily_rows(e,h);R[n][f"H{h}"]=summary(sc,d)
            d.to_parquet(outdir/f"{n.lower()}_h{h}_daily_bucket_rows.parquet",index=False)
    g=gate(R)
    result={"status":"PASS_RET120_TAIL_REVERSAL_HOLDOUT_V0_1_RUN",
            "research_id":c["research_id"],"discovery_sample":c["discovery_sample"],
            "holdout":c["holdout"],"actual_complete_windows":windows,
            "index_membership_audit":aud,"coverage":cover,"results":R,"holdout_gate":g,
            "interpretation":(
              "Strictly post-discovery H20/H60 signal dates support the predeclared EX_CSI1800 RET120 reversal hypothesis."
              if g["status"].startswith("HOLDOUT_SUPPORTS") else
              "Strictly post-discovery H20/H60 signal dates do not satisfy all predeclared tail-reversal checks."
            ),
            "next_if_support":"Record as a replicated mechanism hypothesis only. Do not backfit a strategy on the discovery+holdout data; future Strategy qualification requires a separately designed history/forward protocol.",
            "next_if_not_support":"Freeze the tail-reversal hypothesis and do not rescue it.",
            "guardrails":["No H120 claim from this holdout.","No transaction-cost or portfolio claim.","No Strategy #2/Forward change.","No post-result threshold tuning."],
            "safety":c["safety"]}
    writej(outdir/"ret120_tail_reversal_holdout_v0_1_summary.json",result)
    print(json.dumps(result,ensure_ascii=False,indent=2,default=safe));print("OUTPUT:",outdir);return 0

def main():
    ap=argparse.ArgumentParser();ap.add_argument("command",choices=["check","run"]);a=ap.parse_args();c=C()
    try:rc=check(c) if a.command=="check" else run(c)
    except Exception as e:
        print(json.dumps({"status":"FAIL_CLOSED_RET120_TAIL_REVERSAL_HOLDOUT_V0_1","error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2));rc=3
    raise SystemExit(rc)
if __name__=="__main__":main()
