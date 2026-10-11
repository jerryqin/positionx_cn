
from __future__ import annotations
import argparse, hashlib, importlib.util, json, math, sys
from pathlib import Path
import numpy as np
import pandas as pd
import yaml

ROOT=Path(__file__).resolve().parents[1]
CFG=ROOT/"config/opportunity/ret120_universe_complement_attribution_v0_1.yaml"

def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))

def sha256_file(p):
    h=hashlib.sha256()
    with Path(p).open("rb") as f:
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

def json_write(p,obj):
    Path(p).parent.mkdir(parents=True,exist_ok=True)
    Path(p).write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=safe),encoding="utf-8")

def input_checks(c):
    items=[
      ("frozen_csi500_panel","frozen_csi500_panel","expected_frozen_csi500_panel_sha256"),
      ("exact_scanner_source","exact_scanner_source","expected_scanner_source_sha256"),
      ("current_qmt_ret120","current_qmt_ret120","expected_current_qmt_ret120_sha256"),
      ("full_market_universe","full_market_universe","expected_full_market_universe_sha256"),
      ("canonical_features","canonical_features","expected_canonical_features_sha256"),
    ]
    rows=[]
    for name,key,hkey in items:
        p=Path(c["source"][key]);exp=c["source"][hkey]
        act=sha256_file(p) if p.exists() else None
        rows.append({"name":name,"path":str(p),"exists":p.exists(),
                     "sha256":act,"expected_sha256":exp,"sha_match":act==exp})
    return rows,all(x["exists"] and x["sha_match"] for x in rows)

def load_scanner(c):
    p=Path(c["source"]["exact_scanner_source"])
    spec=importlib.util.spec_from_file_location("px_exact_scanner_attr",p)
    if spec is None or spec.loader is None:raise RuntimeError("cannot load exact scanner")
    m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)
    for n in ["assign_quintiles","summarize_daily_buckets","empty_summary","fast_reject"]:
        if not hasattr(m,n):raise RuntimeError(f"exact scanner missing {n}")
    return m

def panel_cols(df):
    dc=next((x for x in ["trade_date","signal_date","date"] if x in df.columns),None)
    sc=next((x for x in ["symbol","code","ticker"] if x in df.columns),None)
    if not dc or not sc:raise RuntimeError("frozen panel schema unsupported")
    return dc,sc

def load_memberships(c):
    a=pd.Timestamp(c["window"]["signal_start"]);b=pd.Timestamp(c["window"]["signal_cutoff_h120"])
    f=pd.read_parquet(c["source"]["frozen_csi500_panel"])
    dc,sc=panel_cols(f)
    csi=f[[dc,sc]].rename(columns={dc:"signal_date",sc:"symbol"}).copy()
    csi["signal_date"]=pd.to_datetime(csi["signal_date"]);csi["symbol"]=csi["symbol"].astype(str)
    csi=csi[(csi["signal_date"]>=a)&(csi["signal_date"]<=b)].drop_duplicates(["signal_date","symbol"])

    fm=pd.read_parquet(c["source"]["full_market_universe"],columns=["trade_date","symbol"])
    fm=fm.rename(columns={"trade_date":"signal_date"})
    fm["signal_date"]=pd.to_datetime(fm["signal_date"]);fm["symbol"]=fm["symbol"].astype(str)
    fm=fm[(fm["signal_date"]>=a)&(fm["signal_date"]<=b)].drop_duplicates(["signal_date","symbol"])

    ex=fm.merge(csi,on=["signal_date","symbol"],how="left",indicator=True)
    ex=ex.loc[ex["_merge"].eq("left_only"),["signal_date","symbol"]].copy()
    return {"CSI500_CURRENT":csi,"FULL_MARKET_CURRENT":fm,"EX_CSI500_CURRENT":ex}

def load_ret120(c):
    z=pd.read_parquet(c["source"]["current_qmt_ret120"],columns=["trade_date","symbol","ret120"])
    z["trade_date"]=pd.to_datetime(z["trade_date"]);z["symbol"]=z["symbol"].astype(str)
    return z

def bucket_one(mem,ret,scanner,c):
    z=mem.merge(ret,left_on=["signal_date","symbol"],right_on=["trade_date","symbol"],how="left")
    z=z.drop(columns=["trade_date"]).sort_values(["signal_date","symbol"],kind="stable").reset_index(drop=True)
    rows=[];skipped=0
    frac=float(c["signal"]["daily_feature_coverage_min_fraction"]);amin=int(c["signal"]["daily_feature_coverage_min_absolute"])
    for d,g in z.groupby("signal_date",sort=True):
        b=scanner.assign_quintiles(g["ret120"])
        min_feature=max(amin,int(round(len(g)*frac)))
        if int(b.notna().sum())<min_feature:
            skipped+=1;continue
        gg=g[["signal_date","symbol","ret120"]].copy()
        gg["bucket"]=b.to_numpy()
        rows.append(gg)
    out=pd.concat(rows,ignore_index=True) if rows else pd.DataFrame(columns=["signal_date","symbol","ret120","bucket"])
    audit={"membership_rows":len(mem),"membership_dates":int(mem["signal_date"].nunique()),
           "bucket_rows":len(out),"bucket_dates":int(out["signal_date"].nunique()) if len(out) else 0,
           "valid_ret120_rows":int(out["bucket"].notna().sum()) if len(out) else 0,
           "skipped_feature_coverage_dates":skipped}
    return out,audit

def load_calendar(c):
    u=pd.read_parquet(c["source"]["full_market_universe"],columns=["trade_date"])
    return sorted(pd.to_datetime(u["trade_date"]).dropna().unique().tolist())

def attach_outcomes(sig,c,calendar):
    feat=pd.read_parquet(c["source"]["canonical_features"],columns=["trade_date","symbol","adj_open","adj_close"])
    feat["trade_date"]=pd.to_datetime(feat["trade_date"]);feat["symbol"]=feat["symbol"].astype(str)
    pos={pd.Timestamp(d):i for i,d in enumerate(calendar)}
    maps=[]
    for d in sorted(sig["signal_date"].dropna().unique()):
        d=pd.Timestamp(d);i=pos.get(d)
        if i is None or i+120>=len(calendar):continue
        maps.append({"signal_date":d,"entry_date":pd.Timestamp(calendar[i+1]),
                     "d20":pd.Timestamp(calendar[i+20]),"d60":pd.Timestamp(calendar[i+60]),
                     "d120":pd.Timestamp(calendar[i+120])})
    dm=pd.DataFrame(maps)
    s=sig.merge(dm,on="signal_date",how="inner")
    def jp(df,datecol,pricecol,newcol):
        x=feat[["trade_date","symbol",pricecol]].rename(columns={"trade_date":datecol,pricecol:newcol})
        return df.merge(x,on=[datecol,"symbol"],how="left")
    s=jp(s,"entry_date","adj_open","entry_open")
    s=jp(s,"d20","adj_close","close20")
    s=jp(s,"d60","adj_close","close60")
    s=jp(s,"d120","adj_close","close120")
    ep=pd.to_numeric(s["entry_open"],errors="coerce")
    for h,col in [(20,"close20"),(60,"close60"),(120,"close120")]:
        xp=pd.to_numeric(s[col],errors="coerce");ok=(ep>0)&(xp>0)
        s[f"raw_{h}"]=np.where(ok,xp/ep-1.0,np.nan)
    return s

def add_full_market_benchmark(outcomes):
    fm=outcomes["FULL_MARKET_CURRENT"]
    med={h:fm.groupby("signal_date")[f"raw_{h}"].median() for h in [20,60,120]}
    out={}
    for name,x in outcomes.items():
        z=x.copy()
        for h in [20,60,120]:
            z[f"benchmark_{h}"]=z["signal_date"].map(med[h])
            z[f"excess_{h}"]=z[f"raw_{h}"]-z[f"benchmark_{h}"]
        out[name]=z
    return out

def daily_bucket_rows(events):
    rows=[]
    for h in [20,60,120]:
        col=f"excess_{h}"
        for d,g in events.groupby("signal_date",sort=True):
            valid=g[g["bucket"].notna()&g[col].notna()]
            for q,qg in valid.groupby("bucket",sort=True):
                if len(qg)<10:continue
                rows.append({"signal_date":pd.Timestamp(d),"feature":"ret120","horizon":h,
                             "bucket":str(q),"n":int(len(qg)),
                             "mean_excess":float(qg[col].mean()),
                             "median_excess":float(qg[col].median())})
    return pd.DataFrame(rows)

def exact_result(scanner,buckets,c):
    full={};folds={k:{} for k in c["frozen_folds"]}
    for h in [20,60,120]:
        x=buckets[buckets["horizon"]==h]
        full[str(h)]=scanner.summarize_daily_buckets(x) if len(x) else scanner.empty_summary()
        for name,bounds in c["frozen_folds"].items():
            a,b=map(pd.Timestamp,bounds);y=x[(x["signal_date"]>=a)&(x["signal_date"]<=b)]
            folds[name][str(h)]=scanner.summarize_daily_buckets(y) if len(y) else scanner.empty_summary()
    r={"feature":"ret120","full_sample":full,"folds":folds}
    r["fast_reject"]=scanner.fast_reject(r)
    return r

def compact(r):
    fp=r["fast_reject"]["positive_folds_by_horizon"];out={}
    for h in [20,60,120]:
        x=r["full_sample"][str(h)]
        out[f"H{h}"]={"q5_minus_q1":safe(x["q5_minus_q1"]),
                      "monotonicity":safe(x["monotonicity"]),
                      "positive_folds":int(fp[str(h)]),
                      "qmeans":{k:safe(v) for k,v in x.get("qmeans",{}).items()}}
    out["candidate"]=bool(r["fast_reject"]["fast_reject_candidate"])
    return out

def reproduce_v04(results,c):
    tol=float(c["v04_reproduction_reference"]["numeric_tolerance"]);checks=[]
    for uni in ["CSI500_CURRENT","FULL_MARKET_CURRENT"]:
        for h in ["H20","H60","H120"]:
            got=results[uni][h];ref=c["v04_reproduction_reference"][uni][h]
            for k in ["q5_minus_q1","monotonicity"]:
                checks.append({"universe":uni,"horizon":h,"field":k,"got":got[k],"expected":ref[k],
                               "pass":abs(float(got[k])-float(ref[k]))<=tol})
            checks.append({"universe":uni,"horizon":h,"field":"positive_folds","got":got["positive_folds"],
                           "expected":ref["positive_folds"],"pass":int(got["positive_folds"])==int(ref["positive_folds"])})
    return {"pass":all(x["pass"] for x in checks),"checks":checks}

def diagnose(ex):
    neg=sum(float(ex[f"H{h}"]["q5_minus_q1"])<0 for h in [20,60,120])
    if neg==3:return "COMPLEMENT_INVERSE"
    if neg==2:return "COMPLEMENT_MOSTLY_INVERSE"
    return "NO_CLEAR_COMPLEMENT_INVERSION"

def check(c):
    rows,ok=input_checks(c)
    out={"status":"PASS_RET120_UNIVERSE_COMPLEMENT_ATTRIBUTION_V0_1_CHECK" if ok else "BLOCKED_RET120_UNIVERSE_COMPLEMENT_ATTRIBUTION_V0_1_CHECK",
         "inputs":rows,"window":c["window"],"universes":c["universes"],
         "diagnostic_interpretation":c["diagnostic_interpretation"],"safety":c["safety"]}
    print(json.dumps(out,ensure_ascii=False,indent=2,default=safe))
    return 0 if ok else 3

def run(c):
    rows,ok=input_checks(c)
    if not ok:raise RuntimeError("input hash/existence validation failed")
    scanner=load_scanner(c);ret=load_ret120(c);mems=load_memberships(c);cal=load_calendar(c)
    audits={};buckets={};outcomes={}
    for name,mem in mems.items():
        print("[bucket]",name)
        buckets[name],audits[name]=bucket_one(mem,ret,scanner,c)
        print("[outcome]",name)
        outcomes[name]=attach_outcomes(buckets[name],c,cal)
    outcomes=add_full_market_benchmark(outcomes)
    results={}
    outdir=Path(c["output_dir"]);outdir.mkdir(parents=True,exist_ok=True)
    for name,x in outcomes.items():
        d=daily_bucket_rows(x)
        d.to_parquet(outdir/f"{name.lower()}_daily_bucket_rows.parquet",index=False)
        results[name]=compact(exact_result(scanner,d,c))
    repro=reproduce_v04(results,c)
    if not repro["pass"]:
        raise RuntimeError("V0.4 reproduction gate failed; attribution blocked")
    ex=results["EX_CSI500_CURRENT"]
    verdict=diagnose(ex)
    summary={
      "status":"PASS_RET120_UNIVERSE_COMPLEMENT_ATTRIBUTION_V0_1_RUN",
      "research_id":c["research_id"],"window":c["window"],
      "coverage":audits,"v04_reproduction_gate":repro,
      "results":results,"diagnostic_verdict":verdict,
      "interpretation":{
        "COMPLEMENT_INVERSE":"The non-CSI500 complement is inverse at all H20/H60/H120 under the same current-vintage signal and economic pipeline.",
        "COMPLEMENT_MOSTLY_INVERSE":"The non-CSI500 complement is inverse at two of three horizons; broadening likely weakens RET120 but attribution is not uniformly inverse.",
        "NO_CLEAR_COMPLEMENT_INVERSION":"The non-CSI500 complement does not show a uniform inverse pattern; do not infer a simple CSI500-vs-rest explanation."
      }[verdict],
      "next_if_complement_inverse":"Stop RET120 full-market expansion. Next diagnostic, if desired, is a single predeclared investable-universe attribution (liquidity/maturity) on EX_CSI500 only; not a strategy rescue.",
      "next_otherwise":"Do not add post-hoc filters. Freeze this attribution result and move to a different opportunity family.",
      "guardrails":["No Strategy #2 change.","No Forward change.","No threshold rescue.","No filter selection in this study."],
      "safety":c["safety"]
    }
    json_write(outdir/"ret120_universe_complement_attribution_v0_1_summary.json",summary)
    print(json.dumps(summary,ensure_ascii=False,indent=2,default=safe))
    print("OUTPUT:",outdir)
    return 0

def main():
    ap=argparse.ArgumentParser();ap.add_argument("command",choices=["check","run"]);a=ap.parse_args();c=load_cfg()
    try:rc=check(c) if a.command=="check" else run(c)
    except Exception as e:
        print(json.dumps({"status":"FAIL_CLOSED_RET120_UNIVERSE_COMPLEMENT_ATTRIBUTION_V0_1",
                          "error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2));rc=3
    raise SystemExit(rc)

if __name__=="__main__":
    main()
