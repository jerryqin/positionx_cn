
from __future__ import annotations
import argparse, hashlib, importlib.util, json, math, sys
from pathlib import Path
import numpy as np
import pandas as pd
import yaml

ROOT=Path(__file__).resolve().parents[1]
CFG=ROOT/"config/opportunity/ret120_current_vintage_paired_universe_v0_4.yaml"

def cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))

def sha256_file(p):
    h=hashlib.sha256()
    with Path(p).open("rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""):
            h.update(b)
    return h.hexdigest()

def qpath(p):
    return str(Path(p)).replace("\\","/").replace("'","''")

def safe(v):
    if v is None or isinstance(v,(str,bool,int)):return v
    if isinstance(v,float):return v if math.isfinite(v) else None
    if isinstance(v,(np.integer,)):return int(v)
    if isinstance(v,(np.floating,)):return safe(float(v))
    if isinstance(v,pd.Timestamp):return v.strftime("%Y-%m-%d")
    if hasattr(v,"item"):
        try:return safe(v.item())
        except Exception:pass
    return str(v)

def json_write(p,obj):
    Path(p).parent.mkdir(parents=True,exist_ok=True)
    Path(p).write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=safe),encoding="utf-8")

def load_scanner(c):
    p=Path(c["source"]["exact_scanner_source"])
    spec=importlib.util.spec_from_file_location("px_exact_trend_scanner",p)
    if spec is None or spec.loader is None:raise RuntimeError("cannot load exact scanner")
    m=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=m
    spec.loader.exec_module(m)
    for name in ["assign_quintiles","summarize_daily_buckets","empty_summary","fast_reject","monotonicity_score"]:
        if not hasattr(m,name):raise RuntimeError(f"exact scanner missing {name}")
    return m

def input_checks(c):
    items=[
      ("frozen_csi500_panel","frozen_csi500_panel","expected_frozen_csi500_panel_sha256"),
      ("exact_scanner_source","exact_scanner_source","expected_scanner_source_sha256"),
      ("current_qmt_back_ratio_close","current_qmt_back_ratio_close","expected_current_qmt_sha256"),
      ("full_market_universe","full_market_universe","expected_full_market_universe_sha256"),
      ("canonical_features","canonical_features","expected_canonical_features_sha256"),
    ]
    rows=[]
    for name,key,hkey in items:
        p=Path(c["source"][key]);exp=c["source"][hkey]
        act=sha256_file(p) if p.exists() else None
        rows.append({"name":name,"path":str(p),"exists":p.exists(),"sha256":act,
                     "expected_sha256":exp,"sha_match":act==exp})
    return rows,all(x["exists"] and x["sha_match"] for x in rows)

def panel_cols(df):
    dc=next((x for x in ["trade_date","signal_date","date"] if x in df.columns),None)
    sc=next((x for x in ["symbol","code","ticker"] if x in df.columns),None)
    if not dc or not sc:raise RuntimeError("frozen panel date/symbol schema unsupported")
    return dc,sc

def verify_frozen_row_order(c):
    f=pd.read_parquet(c["source"]["frozen_csi500_panel"])
    dc,sc=panel_cols(f)
    z=f[[dc,sc]].copy()
    z[dc]=pd.to_datetime(z[dc]);z[sc]=z[sc].astype(str)
    bad=0
    for _,g in z.groupby(dc,sort=False):
        vals=g[sc].tolist()
        if vals!=sorted(vals):bad+=1
    return {"signal_dates":int(z[dc].nunique()),"non_symbol_ascending_dates":bad,
            "pass":bad==0}

def build_qmt_ret120(c,outdir):
    import duckdb
    p=outdir/"current_qmt_ret120.parquet"
    if p.exists():return p
    src=qpath(c["source"]["current_qmt_back_ratio_close"]);dst=qpath(p)
    con=duckdb.connect()
    try:
        con.execute(f"""
        COPY (
          SELECT trade_date,symbol,close,
                 CASE WHEN close>0 AND lag(close,120) OVER(PARTITION BY symbol ORDER BY trade_date)>0
                      THEN close/lag(close,120) OVER(PARTITION BY symbol ORDER BY trade_date)-1.0
                 END AS ret120
          FROM read_parquet('{src}')
          ORDER BY symbol,trade_date
        ) TO '{dst}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
    finally:con.close()
    return p

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
    return {"CSI500_CURRENT":csi,"FULL_MARKET_CURRENT":fm}

def bucket_one(membership,ret120,scanner,c):
    z=membership.merge(ret120,left_on=["signal_date","symbol"],right_on=["trade_date","symbol"],how="left")
    z=z.drop(columns=["trade_date"])
    z=z.sort_values(["signal_date","symbol"],kind="stable").reset_index(drop=True)
    rows=[]
    frac=float(c["signal"]["daily_feature_coverage_min_fraction"])
    amin=int(c["signal"]["daily_feature_coverage_min_absolute"])
    skipped=0
    for d,g in z.groupby("signal_date",sort=True):
        # exact scanner function; symbol-ascending input reproduces frozen runner order.
        buckets=scanner.assign_quintiles(g["ret120"])
        min_feature=max(amin,int(round(len(g)*frac)))
        valid=int(buckets.notna().sum())
        if valid<min_feature:
            skipped+=1
            continue
        gg=g[["signal_date","symbol","ret120"]].copy()
        gg["bucket"]=buckets.to_numpy()
        rows.append(gg)
    out=pd.concat(rows,ignore_index=True) if rows else pd.DataFrame(columns=["signal_date","symbol","ret120","bucket"])
    audit={"membership_rows":len(membership),"membership_dates":int(membership["signal_date"].nunique()),
           "bucket_rows":len(out),"bucket_dates":int(out["signal_date"].nunique()) if len(out) else 0,
           "valid_ret120_rows":int(out["bucket"].notna().sum()) if len(out) else 0,
           "skipped_feature_coverage_dates":skipped}
    return out,audit

def build_calendar(c):
    u=pd.read_parquet(c["source"]["full_market_universe"],columns=["trade_date"])
    return sorted(pd.to_datetime(u["trade_date"]).dropna().unique().tolist())

def attach_outcomes(signal_df,c,calendar):
    feat=pd.read_parquet(c["source"]["canonical_features"],
                         columns=["trade_date","symbol","adj_open","adj_close"])
    feat["trade_date"]=pd.to_datetime(feat["trade_date"]);feat["symbol"]=feat["symbol"].astype(str)
    pos={pd.Timestamp(d):i for i,d in enumerate(calendar)}
    date_map=[]
    for d in sorted(signal_df["signal_date"].dropna().unique()):
        d=pd.Timestamp(d);i=pos.get(d)
        if i is None or i+120>=len(calendar):continue
        date_map.append({"signal_date":d,"entry_date":pd.Timestamp(calendar[i+1]),
                         "d20":pd.Timestamp(calendar[i+20]),"d60":pd.Timestamp(calendar[i+60]),
                         "d120":pd.Timestamp(calendar[i+120])})
    dm=pd.DataFrame(date_map)
    s=signal_df.merge(dm,on="signal_date",how="inner")
    if s.empty:return s
    fi=feat.set_index(["symbol","trade_date"])
    # vectorized joins
    def join_price(df,datecol,pricecol,newcol):
        x=feat[["trade_date","symbol",pricecol]].rename(columns={"trade_date":datecol,pricecol:newcol})
        return df.merge(x,on=[datecol,"symbol"],how="left")
    s=join_price(s,"entry_date","adj_open","entry_open")
    s=join_price(s,"d20","adj_close","close20")
    s=join_price(s,"d60","adj_close","close60")
    s=join_price(s,"d120","adj_close","close120")
    for h,col in [(20,"close20"),(60,"close60"),(120,"close120")]:
        ok=(pd.to_numeric(s["entry_open"],errors="coerce")>0)&(pd.to_numeric(s[col],errors="coerce")>0)
        s[f"raw_{h}"]=np.where(ok,pd.to_numeric(s[col],errors="coerce")/pd.to_numeric(s["entry_open"],errors="coerce")-1.0,np.nan)
    return s

def add_common_benchmark(outcomes_by_u):
    fm=outcomes_by_u["FULL_MARKET_CURRENT"]
    meds={}
    for h in [20,60,120]:
        meds[h]=fm.groupby("signal_date")[f"raw_{h}"].median()
    result={}
    for name,x in outcomes_by_u.items():
        z=x.copy()
        for h in [20,60,120]:
            z[f"benchmark_{h}"]=z["signal_date"].map(meds[h])
            z[f"excess_{h}"]=z[f"raw_{h}"]-z[f"benchmark_{h}"]
        result[name]=z
    return result

def daily_bucket_rows(events):
    rows=[]
    for h in [20,60,120]:
        col=f"excess_{h}"
        for d,g in events.groupby("signal_date",sort=True):
            valid=g[g["bucket"].notna() & g[col].notna()]
            for q,qg in valid.groupby("bucket",sort=True):
                if len(qg)<10:continue
                rows.append({"signal_date":pd.Timestamp(d),"feature":"ret120","horizon":h,
                             "bucket":str(q),"n":int(len(qg)),
                             "mean_excess":float(qg[col].mean()),
                             "median_excess":float(qg[col].median())})
    return pd.DataFrame(rows)

def scanner_result(scanner,buckets,c):
    full={}
    folds={k:{} for k in c["frozen_folds"]}
    for h in [20,60,120]:
        x=buckets[buckets["horizon"]==h]
        full[str(h)]=scanner.summarize_daily_buckets(x) if len(x) else scanner.empty_summary()
        for name,bounds in c["frozen_folds"].items():
            a,b=map(pd.Timestamp,bounds)
            y=x[(x["signal_date"]>=a)&(x["signal_date"]<=b)]
            folds[name][str(h)]=scanner.summarize_daily_buckets(y) if len(y) else scanner.empty_summary()
    r={"feature":"ret120","full_sample":full,"folds":folds}
    r["fast_reject"]=scanner.fast_reject(r)
    return r

def compact_result(r):
    out={}
    fp=r["fast_reject"]["positive_folds_by_horizon"]
    for h in [20,60,120]:
        x=r["full_sample"][str(h)]
        out[f"H{h}"]={"q5_minus_q1":safe(x["q5_minus_q1"]),
                      "monotonicity":safe(x["monotonicity"]),
                      "positive_folds":int(fp[str(h)]),
                      "qmeans":{k:safe(v) for k,v in x.get("qmeans",{}).items()}}
    out["candidate"]=bool(r["fast_reject"]["fast_reject_candidate"])
    out["fast_reject"]=r["fast_reject"]
    return out

def compare_to_frozen(cur,c):
    ref=c["frozen_csi500_reference"]
    out={}
    for h in [20,60,120]:
        a=cur[f"H{h}"];b=ref[f"H{h}"]
        out[f"H{h}"]={
          "q5_minus_q1_delta":safe(float(a["q5_minus_q1"])-float(b["q5_minus_q1"])),
          "monotonicity_delta":safe(float(a["monotonicity"])-float(b["monotonicity"])),
          "positive_folds_delta":int(a["positive_folds"])-int(b["positive_folds"])
        }
    return out

def check(c):
    rows,ok=input_checks(c)
    order=verify_frozen_row_order(c) if ok else {}
    ok=ok and order.get("pass",False)
    scanner=load_scanner(c) if ok else None
    source_semantics={}
    if scanner is not None:
        source_semantics={
          "PRICE_ADJUSTMENT_MODE":getattr(scanner,"PRICE_ADJUSTMENT_MODE",None),
          "monotonicity_probe":scanner.monotonicity_score({"Q1":1,"Q2":2,"Q3":4,"Q4":3,"Q5":5}),
          "expected_probe_not_quarter_step":True,
        }
    out={"status":"PASS_RET120_CURRENT_VINTAGE_PAIRED_V0_4_CHECK" if ok else "BLOCKED_RET120_CURRENT_VINTAGE_PAIRED_V0_4_CHECK",
         "inputs":rows,"frozen_panel_row_order":order,"exact_source_semantics":source_semantics,
         "paired_design":c["paired_design"],"safety":c["safety"]}
    print(json.dumps(out,ensure_ascii=False,indent=2,default=safe))
    return 0 if ok else 3

def run(c,force=False):
    rows,ok=input_checks(c)
    if not ok:raise RuntimeError("input hash/existence validation failed")
    order=verify_frozen_row_order(c)
    if not order["pass"]:raise RuntimeError(f"frozen panel is not symbol-ascending by date: {order}")
    scanner=load_scanner(c)
    outdir=Path(c["output_dir"]);outdir.mkdir(parents=True,exist_ok=True)

    retp=build_qmt_ret120(c,outdir)
    ret=pd.read_parquet(retp,columns=["trade_date","symbol","ret120"])
    ret["trade_date"]=pd.to_datetime(ret["trade_date"]);ret["symbol"]=ret["symbol"].astype(str)

    memberships=load_memberships(c)
    buckets={};audits={}
    for name,m in memberships.items():
        print("[bucket]",name)
        b,a=bucket_one(m,ret,scanner,c)
        buckets[name]=b;audits[name]=a
        b.to_parquet(outdir/f"{name.lower()}_signal_buckets.parquet",index=False)

    cal=build_calendar(c)
    outcomes={}
    for name,b in buckets.items():
        print("[outcomes]",name)
        outcomes[name]=attach_outcomes(b,c,cal)
    outcomes=add_common_benchmark(outcomes)

    results={};daily={}
    for name,x in outcomes.items():
        print("[summarize]",name)
        d=daily_bucket_rows(x);daily[name]=d
        d.to_parquet(outdir/f"{name.lower()}_daily_bucket_rows.parquet",index=False)
        results[name]=compact_result(scanner_result(scanner,d,c))

    control=results["CSI500_CURRENT"];treat=results["FULL_MARKET_CURRENT"]
    if not control["candidate"]:
        verdict="BASELINE_NOT_REPRODUCED_CURRENT_VINTAGE"
        interpretation="Do not attribute the Full-Market result to universe transfer because the same-current-vintage CSI500 control no longer passes the frozen scanner structure."
    elif treat["candidate"]:
        verdict="GENERALIZES_TO_FULL_MARKET_CURRENT_VINTAGE"
        interpretation="Current-vintage CSI500 control passes and Full Market also passes under the same signal/economic pipeline."
    else:
        verdict="UNIVERSE_DILUTION_CURRENT_VINTAGE"
        interpretation="Current-vintage CSI500 control passes while Full Market rejects; this isolates universe broadening as the material change in the paired experiment."

    cmp={}
    for h in [20,60,120]:
        a=control[f"H{h}"];b=treat[f"H{h}"]
        cmp[f"H{h}"]={"csi500_q5_minus_q1":a["q5_minus_q1"],
                      "full_market_q5_minus_q1":b["q5_minus_q1"],
                      "full_minus_csi500_spread":safe(float(b["q5_minus_q1"])-float(a["q5_minus_q1"])),
                      "csi500_monotonicity":a["monotonicity"],
                      "full_market_monotonicity":b["monotonicity"],
                      "csi500_positive_folds":a["positive_folds"],
                      "full_market_positive_folds":b["positive_folds"]}

    summary={
      "status":"PASS_RET120_CURRENT_VINTAGE_PAIRED_UNIVERSE_V0_4_RUN",
      "research_id":c["research_id"],
      "design":c["paired_design"],
      "signal":c["signal"],
      "economic_measurement":c["economic_measurement"],
      "frozen_panel_row_order":order,
      "coverage":audits,
      "results":{"CSI500_CURRENT":control,"FULL_MARKET_CURRENT":treat},
      "csi500_current_vs_frozen_reference":compare_to_frozen(control,c),
      "paired_comparison":cmp,
      "verdict":verdict,
      "interpretation":interpretation,
      "guardrails":[
        "This does not overwrite or requalify existing RET120 Strategy #2.",
        "This does not claim the 2026-10 current QMT vintage is identical to the frozen v0.2.2 signal vintage.",
        "No post-result threshold tuning is allowed."
      ],
      "artifacts":{
        "current_qmt_ret120":str(retp),"current_qmt_ret120_sha256":sha256_file(retp)
      },
      "safety":c["safety"]
    }
    sp=outdir/"ret120_current_vintage_paired_universe_v0_4_summary.json"
    json_write(sp,summary)
    print(json.dumps(summary,ensure_ascii=False,indent=2,default=safe))
    print("OUTPUT:",outdir)
    return 0

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("command",choices=["check","run"])
    ap.add_argument("--force",action="store_true")
    a=ap.parse_args();c=cfg()
    try:rc=check(c) if a.command=="check" else run(c,a.force)
    except Exception as e:
        print(json.dumps({"status":"FAIL_CLOSED_RET120_CURRENT_VINTAGE_PAIRED_V0_4",
                          "error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2))
        rc=3
    raise SystemExit(rc)

if __name__=="__main__":
    main()
