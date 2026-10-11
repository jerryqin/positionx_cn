
from __future__ import annotations
import argparse,hashlib,importlib.util,json,math,sys
from pathlib import Path
import numpy as np
import pandas as pd
import yaml

ROOT=Path(__file__).resolve().parents[1]
CFG=ROOT/"config/opportunity/ret120_index_ladder_attribution_v0_1.yaml"

def C(): return yaml.safe_load(CFG.read_text(encoding="utf-8"))

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
    sp=importlib.util.spec_from_file_location("px_ladder_scanner",p)
    m=importlib.util.module_from_spec(sp);sys.modules[sp.name]=m;sp.loader.exec_module(m)
    return m

def cols(df):
    dc=next((x for x in ["signal_date","trade_date","date"] if x in df.columns),None)
    sc=next((x for x in ["symbol","code","ticker"] if x in df.columns),None)
    if not dc or not sc:raise RuntimeError("panel schema unsupported")
    return dc,sc

def membership_from_panel(p,c,expected):
    x=pd.read_parquet(p);dc,sc=cols(x)
    x=x[[dc,sc]].rename(columns={dc:"signal_date",sc:"symbol"})
    x["signal_date"]=pd.to_datetime(x["signal_date"]);x["symbol"]=x["symbol"].astype(str)
    a=pd.Timestamp(c["window"]["signal_start"]);b=pd.Timestamp(c["window"]["signal_cutoff_h120"])
    x=x[(x["signal_date"]>=a)&(x["signal_date"]<=b)].drop_duplicates(["signal_date","symbol"])
    cnt=x.groupby("signal_date")["symbol"].nunique()
    bad=cnt[cnt!=expected]
    return x,{"path":str(p),"sha256":sha(p),"rows":len(x),"dates":int(x["signal_date"].nunique()),
              "min_members":int(cnt.min()),"max_members":int(cnt.max()),
              "expected_members":expected,"bad_member_count_dates":int(len(bad))}

def full_market(c):
    x=pd.read_parquet(c["source"]["full_market_universe"],columns=["trade_date","symbol"])
    x=x.rename(columns={"trade_date":"signal_date"})
    x["signal_date"]=pd.to_datetime(x["signal_date"]);x["symbol"]=x["symbol"].astype(str)
    a=pd.Timestamp(c["window"]["signal_start"]);b=pd.Timestamp(c["window"]["signal_cutoff_h120"])
    return x[(x["signal_date"]>=a)&(x["signal_date"]<=b)].drop_duplicates(["signal_date","symbol"])

def load_ret(c):
    x=pd.read_parquet(c["source"]["current_qmt_ret120"],columns=["trade_date","symbol","ret120"])
    x["trade_date"]=pd.to_datetime(x["trade_date"]);x["symbol"]=x["symbol"].astype(str)
    return x

def bucket(mem,ret,sc,c):
    z=mem.merge(ret,left_on=["signal_date","symbol"],right_on=["trade_date","symbol"],how="left").drop(columns=["trade_date"])
    z=z.sort_values(["signal_date","symbol"],kind="stable").reset_index(drop=True)
    out=[];sk=0
    for d,g in z.groupby("signal_date",sort=True):
        b=sc.assign_quintiles(g["ret120"])
        need=max(int(c["signal"]["coverage_absolute_min"]),int(round(len(g)*float(c["signal"]["coverage_fraction_min"]))))
        if int(b.notna().sum())<need:sk+=1;continue
        q=g[["signal_date","symbol","ret120"]].copy();q["bucket"]=b.to_numpy();out.append(q)
    r=pd.concat(out,ignore_index=True) if out else pd.DataFrame(columns=["signal_date","symbol","ret120","bucket"])
    return r,{"membership_rows":len(mem),"membership_dates":int(mem["signal_date"].nunique()),
              "bucket_rows":len(r),"bucket_dates":int(r["signal_date"].nunique()) if len(r) else 0,
              "valid_ret120":int(r["bucket"].notna().sum()) if len(r) else 0,"skipped_dates":sk}

def calendar(c):
    x=pd.read_parquet(c["source"]["full_market_universe"],columns=["trade_date"])
    return sorted(pd.to_datetime(x["trade_date"]).dropna().unique().tolist())

def outcomes(sig,c,cal):
    f=pd.read_parquet(c["source"]["canonical_features"],columns=["trade_date","symbol","adj_open","adj_close"])
    f["trade_date"]=pd.to_datetime(f["trade_date"]);f["symbol"]=f["symbol"].astype(str)
    pos={pd.Timestamp(d):i for i,d in enumerate(cal)}
    maps=[]
    for d in sorted(sig["signal_date"].dropna().unique()):
        d=pd.Timestamp(d);i=pos.get(d)
        if i is not None and i+120<len(cal):
            maps.append({"signal_date":d,"e":pd.Timestamp(cal[i+1]),"d20":pd.Timestamp(cal[i+20]),
                         "d60":pd.Timestamp(cal[i+60]),"d120":pd.Timestamp(cal[i+120])})
    s=sig.merge(pd.DataFrame(maps),on="signal_date",how="inner")
    def J(df,datecol,col,new):
        q=f[["trade_date","symbol",col]].rename(columns={"trade_date":datecol,col:new})
        return df.merge(q,on=[datecol,"symbol"],how="left")
    s=J(s,"e","adj_open","entry");s=J(s,"d20","adj_close","x20")
    s=J(s,"d60","adj_close","x60");s=J(s,"d120","adj_close","x120")
    ep=pd.to_numeric(s["entry"],errors="coerce")
    for h,col in [(20,"x20"),(60,"x60"),(120,"x120")]:
        xp=pd.to_numeric(s[col],errors="coerce")
        s[f"excess_{h}"]=np.where((ep>0)&(xp>0),xp/ep-1.0,np.nan)
    return s

def daily_rows(e):
    rows=[]
    for h in [20,60,120]:
        col=f"excess_{h}"
        for d,g in e.groupby("signal_date",sort=True):
            v=g[g["bucket"].notna()&g[col].notna()]
            for q,qg in v.groupby("bucket",sort=True):
                if len(qg)>=10:
                    rows.append({"signal_date":pd.Timestamp(d),"feature":"ret120","horizon":h,
                                 "bucket":str(q),"n":int(len(qg)),
                                 "mean_excess":float(qg[col].mean()),
                                 "median_excess":float(qg[col].median())})
    return pd.DataFrame(rows)

def result(sc,d,c):
    full={};folds={k:{} for k in c["frozen_folds"]}
    for h in [20,60,120]:
        x=d[d["horizon"]==h]
        full[str(h)]=sc.summarize_daily_buckets(x) if len(x) else sc.empty_summary()
        for name,ab in c["frozen_folds"].items():
            a,b=map(pd.Timestamp,ab);y=x[(x["signal_date"]>=a)&(x["signal_date"]<=b)]
            folds[name][str(h)]=sc.summarize_daily_buckets(y) if len(y) else sc.empty_summary()
    r={"feature":"ret120","full_sample":full,"folds":folds};r["fast_reject"]=sc.fast_reject(r)
    fp=r["fast_reject"]["positive_folds_by_horizon"];o={}
    for h in [20,60,120]:
        x=full[str(h)]
        o[f"H{h}"]={"spread":safe(x["q5_minus_q1"]),"mono":safe(x["monotonicity"]),
                    "folds":int(fp[str(h)]),"qmeans_raw":{k:safe(v) for k,v in x.get("qmeans",{}).items()}}
    o["candidate"]=bool(r["fast_reject"]["fast_reject_candidate"])
    return o

def repro(R,c):
    checks=[];tol=float(c["v04_reproduction_reference"]["tolerance"])
    for u in ["CSI500_CURRENT","FULL_MARKET_CURRENT"]:
        for h in ["H20","H60","H120"]:
            a=R[u][h];b=c["v04_reproduction_reference"][u][h]
            for x,y in [("spread","spread"),("mono","mono")]:
                checks.append({"universe":u,"horizon":h,"field":x,"pass":abs(float(a[x])-float(b[y]))<=tol})
            checks.append({"universe":u,"horizon":h,"field":"folds","pass":int(a["folds"])==int(b["folds"])})
    return {"pass":all(x["pass"] for x in checks),"checks":checks}

def verdict(R):
    core=["CSI300_CURRENT","CSI500_CURRENT","CSI1000_CURRENT"]
    core_pos={u:sum(float(R[u][f"H{h}"]["spread"])>0 for h in [20,60,120]) for u in core}
    tail_neg=sum(float(R["EX_CSI1800_CURRENT"][f"H{h}"]["spread"])<0 for h in [20,60,120])
    if all(v>=2 for v in core_pos.values()) and tail_neg==3:return "INVERSION_LOCALIZED_BELOW_CSI1800"
    if tail_neg>=2:return "TAIL_MOSTLY_INVERSE"
    return "NO_SIMPLE_INDEX_LADDER_LOCALIZATION"

def check(c):
    base=[
      ("scanner",c["source"]["exact_scanner_source"],c["source"]["expected_scanner_source_sha256"]),
      ("ret120",c["source"]["current_qmt_ret120"],c["source"]["expected_current_qmt_ret120_sha256"]),
      ("full_market",c["source"]["full_market_universe"],c["source"]["expected_full_market_universe_sha256"]),
      ("features",c["source"]["canonical_features"],c["source"]["expected_canonical_features_sha256"]),
    ]
    rows=[];ok=True
    for n,p,e in base:
        P=Path(p);a=sha(P) if P.exists() else None;good=P.exists() and a==e
        rows.append({"name":n,"path":str(P),"exists":P.exists(),"sha256":a,"expected":e,"pass":good});ok&=good
    resolved={}
    for tag in ["csi300","csi500","csi1000"]:
        P=resolve(c["source"][f"{tag}_panel_candidates"]);resolved[tag]=str(P) if P else None;ok&=P is not None
    out={"status":"PASS_RET120_INDEX_LADDER_ATTRIBUTION_V0_1_CHECK" if ok else "BLOCKED_RET120_INDEX_LADDER_ATTRIBUTION_V0_1_CHECK",
         "inputs":rows,"resolved_index_panels":resolved,"window":c["window"],"safety":c["safety"]}
    print(json.dumps(out,ensure_ascii=False,indent=2));return 0 if ok else 3

def run(c):
    sc=scanner(c);ret=load_ret(c);fm=full_market(c);cal=calendar(c)
    mem={};audit={}
    for tag,name in [("csi300","CSI300_CURRENT"),("csi500","CSI500_CURRENT"),("csi1000","CSI1000_CURRENT")]:
        P=resolve(c["source"][f"{tag}_panel_candidates"])
        if P is None:raise RuntimeError(f"missing {tag} frozen panel")
        mem[name],audit[name]=membership_from_panel(P,c,int(c["expected_members"][name]))
        if audit[name]["bad_member_count_dates"]!=0:raise RuntimeError(f"{name} membership count gate failed: {audit[name]}")
    # require index ladder disjoint on the same signal date
    union=pd.concat([mem["CSI300_CURRENT"],mem["CSI500_CURRENT"],mem["CSI1000_CURRENT"]],ignore_index=True)
    dup=int(union.duplicated(["signal_date","symbol"]).sum())
    if dup:raise RuntimeError(f"CSI300/500/1000 overlap rows={dup}")
    mem["CSI1800_CURRENT"]=union.drop_duplicates(["signal_date","symbol"])
    ex=fm.merge(mem["CSI1800_CURRENT"],on=["signal_date","symbol"],how="left",indicator=True)
    mem["EX_CSI1800_CURRENT"]=ex.loc[ex["_merge"].eq("left_only"),["signal_date","symbol"]]
    mem["FULL_MARKET_CURRENT"]=fm

    outdir=Path(c["output_dir"]);outdir.mkdir(parents=True,exist_ok=True)
    R={};cover={}
    for name,M in mem.items():
        print("[universe]",name)
        b,a=bucket(M,ret,sc,c);cover[name]=a
        e=outcomes(b,c,cal);d=daily_rows(e)
        d.to_parquet(outdir/f"{name.lower()}_daily_bucket_rows.parquet",index=False)
        R[name]=result(sc,d,c)
    rg=repro(R,c)
    if not rg["pass"]:raise RuntimeError("V0.4 reproduction failed")
    v=verdict(R)
    summary={"status":"PASS_RET120_INDEX_LADDER_ATTRIBUTION_V0_1_RUN",
             "research_id":c["research_id"],"window":c["window"],
             "index_membership_audit":audit,"cross_index_duplicate_rows":dup,
             "coverage":cover,"v04_reproduction_gate":rg,"results":R,
             "diagnostic_verdict":v,
             "interpretation":{
               "INVERSION_LOCALIZED_BELOW_CSI1800":"CSI300/CSI500/CSI1000 each retain positive RET120 spreads in at least two horizons, while the market outside their union is inverse at all three horizons.",
               "TAIL_MOSTLY_INVERSE":"The market outside CSI300/500/1000 is inverse at at least two horizons, but the index ladder is not uniformly positive enough to localize the effect cleanly.",
               "NO_SIMPLE_INDEX_LADDER_LOCALIZATION":"The inversion cannot be cleanly localized by the pre-existing CSI300/500/1000 ladder."
             }[v],
             "next_if_localized":"Freeze RET120 full-market expansion. Treat index eligibility / investability as part of the usable universe. Do not optimize a new liquidity threshold from this result.",
             "next_otherwise":"Freeze the attribution and do not search filters post hoc.",
             "guardrails":["diagnostic only","no Strategy #2 change","no Forward change","no post-result threshold tuning"],
             "safety":c["safety"]}
    writej(outdir/"ret120_index_ladder_attribution_v0_1_summary.json",summary)
    print(json.dumps(summary,ensure_ascii=False,indent=2,default=safe));print("OUTPUT:",outdir);return 0

def main():
    ap=argparse.ArgumentParser();ap.add_argument("command",choices=["check","run"]);a=ap.parse_args();c=C()
    try:rc=check(c) if a.command=="check" else run(c)
    except Exception as e:
        print(json.dumps({"status":"FAIL_CLOSED_RET120_INDEX_LADDER_ATTRIBUTION_V0_1","error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2));rc=3
    raise SystemExit(rc)

if __name__=="__main__":main()
