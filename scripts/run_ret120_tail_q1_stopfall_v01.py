
from __future__ import annotations
import argparse, hashlib, importlib.util, json, math, sys
from pathlib import Path
import numpy as np
import pandas as pd
import yaml

ROOT=Path(__file__).resolve().parents[1]
CFG=ROOT/"config/opportunity/ret120_tail_q1_stopfall_v0_1.yaml"

def C():
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
    return str(v)

def write_json(p,obj):
    Path(p).parent.mkdir(parents=True,exist_ok=True)
    Path(p).write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=safe),encoding="utf-8")

def resolve(paths):
    for p in paths:
        if Path(p).exists():
            return Path(p)
    return None

def load_scanner(c):
    p=Path(c["source"]["exact_scanner_source"])
    sp=importlib.util.spec_from_file_location("px_tail_q1_scanner",p)
    if sp is None or sp.loader is None:
        raise RuntimeError("cannot load frozen scanner")
    m=importlib.util.module_from_spec(sp)
    sys.modules[sp.name]=m
    sp.loader.exec_module(m)
    if not hasattr(m,"assign_quintiles"):
        raise RuntimeError("frozen scanner missing assign_quintiles")
    return m

def panel_cols(df):
    dc=next((x for x in ["signal_date","trade_date","date"] if x in df.columns),None)
    sc=next((x for x in ["symbol","code","ticker"] if x in df.columns),None)
    if not dc or not sc:
        raise RuntimeError("unsupported panel schema")
    return dc,sc

def check_inputs(c):
    fixed=[
      ("scanner",c["source"]["exact_scanner_source"],c["source"]["expected_scanner_source_sha256"]),
      ("ret120",c["source"]["current_qmt_ret120"],c["source"]["expected_current_qmt_ret120_sha256"]),
      ("full_market",c["source"]["full_market_universe"],c["source"]["expected_full_market_universe_sha256"]),
      ("canonical_features",c["source"]["canonical_features"],c["source"]["expected_canonical_features_sha256"]),
    ]
    rows=[];ok=True
    for name,p,exp in fixed:
        P=Path(p);act=sha256_file(P) if P.exists() else None
        good=P.exists() and act==exp
        rows.append({"name":name,"path":str(P),"exists":P.exists(),
                     "sha256":act,"expected_sha256":exp,"pass":good})
        ok &= good

    resolved={}
    for tag in ["csi300","csi500","csi1000"]:
        P=resolve(c["source"][f"{tag}_panel_candidates"])
        resolved[tag]=str(P) if P else None
        ok &= P is not None

    rs=Path(c["source"]["causal_regime_summary"])
    regime_gate=False
    if rs.exists():
        try:
            j=json.loads(rs.read_text(encoding="utf-8"))
            regime_gate=(j.get("status")=="PASS_RET120_CAUSAL_REGIME_DETECTOR_V0_1_RUN")
        except Exception:
            regime_gate=False
    rows.append({"name":"causal_regime_summary","path":str(rs),"exists":rs.exists(),
                 "sha256":sha256_file(rs) if rs.exists() else None,
                 "required_status_pass":regime_gate})
    ok &= regime_gate
    return rows,resolved,ok

def load_membership_panel(path,start,end):
    x=pd.read_parquet(path)
    dc,sc=panel_cols(x)
    x=x[[dc,sc]].rename(columns={dc:"signal_date",sc:"symbol"})
    x["signal_date"]=pd.to_datetime(x["signal_date"])
    x["symbol"]=x["symbol"].astype(str)
    x=x[(x["signal_date"]>=start)&(x["signal_date"]<=end)]
    return x.drop_duplicates(["signal_date","symbol"])

def load_tail_membership(c):
    start=pd.Timestamp(c["window"]["signal_start"])
    end=pd.Timestamp(c["window"]["market_data_through"])
    parts={};date_sets=[]
    expected={"csi300":300,"csi500":500,"csi1000":1000}
    audit={}
    for tag in ["csi300","csi500","csi1000"]:
        P=resolve(c["source"][f"{tag}_panel_candidates"])
        z=load_membership_panel(P,start,end)
        cnt=z.groupby("signal_date")["symbol"].nunique()
        valid_dates=set(cnt[cnt==expected[tag]].index)
        audit[tag]={"path":str(P),"rows":len(z),"dates":int(z["signal_date"].nunique()),
                    "valid_count_dates":len(valid_dates),
                    "bad_count_dates":int((cnt!=expected[tag]).sum())}
        parts[tag]=z
        date_sets.append(valid_dates)

    fm=pd.read_parquet(c["source"]["full_market_universe"],columns=["trade_date","symbol"])
    fm=fm.rename(columns={"trade_date":"signal_date"})
    fm["signal_date"]=pd.to_datetime(fm["signal_date"]);fm["symbol"]=fm["symbol"].astype(str)
    fm=fm[(fm["signal_date"]>=start)&(fm["signal_date"]<=end)].drop_duplicates(["signal_date","symbol"])
    fm_dates=set(fm["signal_date"].unique())
    common_dates=set.intersection(*date_sets,fm_dates)
    if not common_dates:
        raise RuntimeError("no common valid membership dates")

    idx=[]
    for z in parts.values():
        idx.append(z[z["signal_date"].isin(common_dates)])
    csi1800=pd.concat(idx,ignore_index=True)
    dup=int(csi1800.duplicated(["signal_date","symbol"]).sum())
    if dup:
        raise RuntimeError(f"CSI300/500/1000 overlap rows={dup}")
    fm=fm[fm["signal_date"].isin(common_dates)]
    ex=fm.merge(csi1800,on=["signal_date","symbol"],how="left",indicator=True)
    ex=ex.loc[ex["_merge"].eq("left_only"),["signal_date","symbol"]].copy()
    audit["common"]={"common_dates":len(common_dates),
                     "first":safe(min(common_dates)),
                     "last":safe(max(common_dates)),
                     "cross_index_duplicate_rows":dup,
                     "full_market_rows":len(fm),
                     "ex_csi1800_rows":len(ex)}
    return ex,audit

def load_ret120(c):
    x=pd.read_parquet(c["source"]["current_qmt_ret120"],columns=["trade_date","symbol","ret120"])
    x["trade_date"]=pd.to_datetime(x["trade_date"]);x["symbol"]=x["symbol"].astype(str)
    return x

def assign_tail_q1(mem,ret,scanner,c):
    z=mem.merge(ret,left_on=["signal_date","symbol"],right_on=["trade_date","symbol"],how="left").drop(columns=["trade_date"])
    z=z.sort_values(["signal_date","symbol"],kind="stable").reset_index(drop=True)
    rows=[];skipped=0
    frac=float(c["candidate_pool"]["daily_feature_coverage_min_fraction"])
    amin=int(c["candidate_pool"]["daily_feature_coverage_min_absolute"])
    for d,g in z.groupby("signal_date",sort=True):
        b=scanner.assign_quintiles(g["ret120"])
        need=max(amin,int(round(len(g)*frac)))
        if int(b.notna().sum())<need:
            skipped+=1;continue
        q=g[["signal_date","symbol","ret120"]].copy()
        q["bucket"]=b.to_numpy()
        rows.append(q[q["bucket"].eq("Q1")])
    out=pd.concat(rows,ignore_index=True) if rows else pd.DataFrame(columns=["signal_date","symbol","ret120","bucket"])
    return out,{"q1_rows":len(out),"q1_dates":int(out["signal_date"].nunique()) if len(out) else 0,
                "skipped_feature_coverage_dates":skipped}

def load_features_with_stopfall(c):
    f=pd.read_parquet(c["source"]["canonical_features"],columns=["trade_date","symbol","adj_open","adj_close"])
    f["trade_date"]=pd.to_datetime(f["trade_date"]);f["symbol"]=f["symbol"].astype(str)
    f["adj_close"]=pd.to_numeric(f["adj_close"],errors="coerce")
    f["adj_open"]=pd.to_numeric(f["adj_open"],errors="coerce")
    f=f.sort_values(["symbol","trade_date"],kind="stable").reset_index(drop=True)
    g=f.groupby("symbol",sort=False)["adj_close"]
    f["prev_close"]=g.shift(1)
    f["prior20_close_min"]=g.transform(lambda s:s.shift(1).rolling(20,min_periods=20).min())
    f["prior_day_is_20d_close_low"]=(
        f["prev_close"].notna() & f["prior20_close_min"].notna()
        & (f["prev_close"]<=f["prior20_close_min"])
    )
    f["current_day_bounces"]=f["adj_close"]>f["prev_close"]
    f["stopfall_confirm"]=f["prior_day_is_20d_close_low"] & f["current_day_bounces"]
    return f

def attach_confirmation(q1,features):
    x=features[["trade_date","symbol","adj_close","prev_close","prior20_close_min",
                "prior_day_is_20d_close_low","current_day_bounces","stopfall_confirm"]].rename(columns={"trade_date":"signal_date"})
    z=q1.merge(x,on=["signal_date","symbol"],how="left")
    z["stopfall_confirm"]=z["stopfall_confirm"].fillna(False).astype(bool)
    return z

def calendar(c):
    u=pd.read_parquet(c["source"]["full_market_universe"],columns=["trade_date"])
    return [pd.Timestamp(x) for x in sorted(pd.to_datetime(u["trade_date"]).dropna().unique())]

def event_returns(q1,features,cal,h):
    pos={d:i for i,d in enumerate(cal)}
    maps=[]
    for d in sorted(q1["signal_date"].dropna().unique()):
        d=pd.Timestamp(d);i=pos.get(d)
        if i is not None and i+h<len(cal):
            maps.append({"signal_date":d,"entry_date":cal[i+1],"exit_date":cal[i+h]})
    dm=pd.DataFrame(maps)
    z=q1.merge(dm,on="signal_date",how="inner")
    e=features[["trade_date","symbol","adj_open"]].rename(columns={"trade_date":"entry_date","adj_open":"entry_open"})
    x=features[["trade_date","symbol","adj_close"]].rename(columns={"trade_date":"exit_date","adj_close":"exit_close"})
    z=z.merge(e,on=["entry_date","symbol"],how="left").merge(x,on=["exit_date","symbol"],how="left")
    ep=pd.to_numeric(z["entry_open"],errors="coerce");xp=pd.to_numeric(z["exit_close"],errors="coerce")
    z["raw_return"]=np.where((ep>0)&(xp>0),xp/ep-1.0,np.nan)
    z["horizon"]=h
    return z[z["raw_return"].notna()].copy()

def date_equal_stats(ev):
    rows=[]
    for d,g in ev.groupby("signal_date",sort=True):
        base=g["raw_return"]
        sel=g.loc[g["stopfall_confirm"],"raw_return"]
        if sel.empty:continue
        rows.append({"signal_date":pd.Timestamp(d),
                     "baseline_q1_n":int(len(base)),
                     "selected_n":int(len(sel)),
                     "baseline_q1_mean":float(base.mean()),
                     "selected_mean":float(sel.mean()),
                     "lift":float(sel.mean()-base.mean()),
                     "selected_win_rate":float((sel>0).mean())})
    return pd.DataFrame(rows)

def horizon_summary(ev,daily,c,h):
    selected=ev[ev["stopfall_confirm"]].copy()
    folds={}
    for name,bounds in c["frozen_folds"].items():
        a,b=map(pd.Timestamp,bounds)
        g=daily[(daily["signal_date"]>=a)&(daily["signal_date"]<=b)]
        folds[name]={"signal_dates":len(g),
                     "date_equal_lift":float(g["lift"].mean()) if len(g) else None,
                     "selected_raw_mean":float(g["selected_mean"].mean()) if len(g) else None,
                     "positive_lift":bool(len(g) and g["lift"].mean()>0)}
    positive_folds=sum(1 for x in folds.values() if x["positive_lift"])
    return {
      "horizon":h,
      "complete_q1_events":len(ev),
      "complete_q1_signal_dates":int(ev["signal_date"].nunique()),
      "selected_events":len(selected),
      "selected_signal_dates":int(selected["signal_date"].nunique()),
      "event_weighted_selected_raw_mean":float(selected["raw_return"].mean()) if len(selected) else None,
      "event_weighted_baseline_q1_raw_mean":float(ev["raw_return"].mean()) if len(ev) else None,
      "date_equal_selected_raw_mean":float(daily["selected_mean"].mean()) if len(daily) else None,
      "date_equal_baseline_q1_raw_mean":float(daily["baseline_q1_mean"].mean()) if len(daily) else None,
      "date_equal_lift":float(daily["lift"].mean()) if len(daily) else None,
      "median_daily_lift":float(daily["lift"].median()) if len(daily) else None,
      "daily_lift_positive_fraction":float((daily["lift"]>0).mean()) if len(daily) else None,
      "date_equal_selected_win_rate":float(daily["selected_win_rate"].mean()) if len(daily) else None,
      "positive_lift_folds":positive_folds,
      "folds":folds,
      "first_complete_signal":safe(ev["signal_date"].min()) if len(ev) else None,
      "last_complete_signal":safe(ev["signal_date"].max()) if len(ev) else None,
    }

def segment_summary(daily,c):
    cut=pd.Timestamp(c["window"]["prior_research_cut"])
    out={}
    for name,mask in [
      ("PRE_2026_04_08",daily["signal_date"]<=cut),
      ("POST_2026_04_08",daily["signal_date"]>cut),
    ]:
        g=daily[mask]
        out[name]={"signal_dates":len(g),
                   "date_equal_selected_raw_mean":float(g["selected_mean"].mean()) if len(g) else None,
                   "date_equal_baseline_q1_raw_mean":float(g["baseline_q1_mean"].mean()) if len(g) else None,
                   "date_equal_lift":float(g["lift"].mean()) if len(g) else None,
                   "note":"Descriptive only; POST segment is not an independent holdout for this newly proposed confirmation."}
    return out

def fast_reject(summary,c):
    gate=c["fast_reject_gate"]
    checks={}
    for h in [20,60]:
        s=summary[f"H{h}"]
        checks[f"H{h}_selected_events_min"]=s["selected_events"]>=int(gate["selected_events_min_each_horizon"])
        checks[f"H{h}_selected_signal_dates_min"]=s["selected_signal_dates"]>=int(gate["selected_signal_dates_min_each_horizon"])
        checks[f"H{h}_selected_raw_positive"]=(s["date_equal_selected_raw_mean"] is not None and s["date_equal_selected_raw_mean"]>0)
        checks[f"H{h}_lift_positive"]=(s["date_equal_lift"] is not None and s["date_equal_lift"]>0)
        checks[f"H{h}_positive_lift_folds"]=s["positive_lift_folds"]>=int(gate["positive_lift_folds_min_each_horizon"])
    return {"status":"CANDIDATE" if all(checks.values()) else "REJECT",
            "checks":checks}

def check(c):
    rows,resolved,ok=check_inputs(c)
    out={"status":"PASS_RET120_TAIL_Q1_STOPFALL_V0_1_CHECK" if ok else "BLOCKED_RET120_TAIL_Q1_STOPFALL_V0_1_CHECK",
         "inputs":rows,"resolved_index_panels":resolved,
         "candidate_pool":c["candidate_pool"],"confirmation":c["confirmation"],
         "fast_reject_gate":c["fast_reject_gate"],"safety":c["safety"]}
    print(json.dumps(out,ensure_ascii=False,indent=2))
    return 0 if ok else 3

def run(c):
    rows,resolved,ok=check_inputs(c)
    if not ok:raise RuntimeError("input/upstream gate failed")
    scanner=load_scanner(c)
    mem,mem_audit=load_tail_membership(c)
    ret=load_ret120(c)
    q1,q1_audit=assign_tail_q1(mem,ret,scanner,c)
    features=load_features_with_stopfall(c)
    cand=attach_confirmation(q1,features)
    cal=calendar(c)
    outdir=Path(c["output_dir"]);outdir.mkdir(parents=True,exist_ok=True)

    cand.to_parquet(outdir/"tail_q1_candidates_with_stopfall.parquet",index=False)

    results={};segment={}
    for h in [20,60]:
        ev=event_returns(cand,features,cal,h)
        daily=date_equal_stats(ev)
        ev.to_parquet(outdir/f"tail_q1_stopfall_h{h}_events.parquet",index=False)
        daily.to_csv(outdir/f"tail_q1_stopfall_h{h}_date_equal.csv",index=False)
        results[f"H{h}"]=horizon_summary(ev,daily,c,h)
        segment[f"H{h}"]=segment_summary(daily,c)

    verdict=fast_reject(results,c)
    summary={
      "status":"PASS_RET120_TAIL_Q1_STOPFALL_V0_1_RUN",
      "research_id":c["research_id"],
      "market_data_through":c["window"]["market_data_through"],
      "candidate_pool":c["candidate_pool"],
      "confirmation":c["confirmation"],
      "membership_audit":mem_audit,
      "q1_audit":q1_audit,
      "confirmation_counts":{
        "q1_candidates":len(cand),
        "confirmed":int(cand["stopfall_confirm"].sum()),
        "confirmation_rate":float(cand["stopfall_confirm"].mean()) if len(cand) else None
      },
      "results":results,
      "pre_post_descriptive":segment,
      "fast_reject":verdict,
      "interpretation":c["interpretation"]["pass"] if verdict["status"]=="CANDIDATE" else c["interpretation"]["fail"],
      "methodological_guardrails":[
        "Primary comparison is selected versus all RET120-Q1 stocks on the SAME signal dates.",
        "H20 and H60 use separate exact complete cohorts.",
        "The post-2026-04-08 segment is descriptive, not an independent holdout for this newly proposed confirmation.",
        "No alternative lookback window, bounce threshold, or second confirmation rule is scanned in V0.1.",
        "A CANDIDATE verdict advances only to immutable Forward capture."
      ],
      "artifacts":{
        "candidate_panel":str(outdir/"tail_q1_candidates_with_stopfall.parquet"),
        "h20_events":str(outdir/"tail_q1_stopfall_h20_events.parquet"),
        "h60_events":str(outdir/"tail_q1_stopfall_h60_events.parquet")
      },
      "safety":c["safety"]
    }
    write_json(outdir/"ret120_tail_q1_stopfall_v0_1_summary.json",summary)
    print(json.dumps(summary,ensure_ascii=False,indent=2,default=safe))
    print("OUTPUT:",outdir)
    return 0

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("command",choices=["check","run"])
    a=ap.parse_args();c=C()
    try:
        rc=check(c) if a.command=="check" else run(c)
    except Exception as e:
        print(json.dumps({"status":"FAIL_CLOSED_RET120_TAIL_Q1_STOPFALL_V0_1",
                          "error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2))
        rc=3
    raise SystemExit(rc)

if __name__=="__main__":
    main()
