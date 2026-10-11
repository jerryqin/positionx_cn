
from __future__ import annotations
import argparse, hashlib, json, math
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT=Path(__file__).resolve().parents[1]
CFG=ROOT/"config/opportunity/full_market_ret120_generalization_v0_1.yaml"

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

def normalize_date(s):
    return pd.to_datetime(s.astype(str),errors="coerce")

def discover_panel_columns(df):
    date_col=next((x for x in ["trade_date","signal_date","date"] if x in df.columns),None)
    symbol_col=next((x for x in ["symbol","code","ticker"] if x in df.columns),None)
    if date_col is None or symbol_col is None or "ret120" not in df.columns:
        raise RuntimeError(f"frozen panel schema unsupported; date={date_col} symbol={symbol_col} ret120={'ret120' in df.columns}")
    return date_col,symbol_col

def assign_quintiles(df,value_col="ret120"):
    """Frozen semantics: symbol-ASC deterministic row order, rank(first), qcut(5)."""
    z=df.copy()
    z=z.sort_values(["signal_date","symbol"],kind="stable").reset_index(drop=True)
    z["bucket"]=None
    for d,idx in z.groupby("signal_date",sort=False).groups.items():
        ii=list(idx)
        vals=pd.to_numeric(z.loc[ii,value_col],errors="coerce")
        ok=vals.notna() & np.isfinite(vals.to_numpy(dtype=float))
        valid_idx=np.asarray(ii)[ok.to_numpy()]
        if len(valid_idx)<5:
            continue
        ranks=pd.Series(z.loc[valid_idx,value_col].to_numpy(),index=valid_idx).rank(method="first")
        try:
            b=pd.qcut(ranks,5,labels=["Q1","Q2","Q3","Q4","Q5"])
        except ValueError:
            continue
        z.loc[valid_idx,"bucket"]=b.astype(str).to_numpy()
    return z

def monotonicity(bucket_means):
    vals=[bucket_means.get(f"Q{i}") for i in range(1,6)]
    if any(v is None or not math.isfinite(float(v)) for v in vals):
        return None
    inc=sum(float(vals[i])>=float(vals[i-1]) for i in range(1,5))
    return inc/4.0

def fold_for_date(d,cfg):
    ts=pd.Timestamp(d)
    for name,bounds in cfg["frozen_folds"].items():
        if name=="source": continue
        a,b=map(pd.Timestamp,bounds)
        if a<=ts<=b:return name
    return None

def validate_inputs(cfg):
    rows=[]
    for name,key,ekey in [
      ("frozen_ret120_panel","frozen_ret120_panel","expected_frozen_ret120_panel_sha256"),
      ("full_market_universe","full_market_universe","expected_full_market_universe_sha256"),
      ("canonical_features","canonical_features","expected_canonical_features_sha256"),
    ]:
        p=Path(cfg["source"][key]);exp=cfg["source"][ekey]
        actual=sha256_file(p) if p.exists() else None
        rows.append({"name":name,"path":str(p),"exists":p.exists(),
                     "sha256":actual,"expected_sha256":exp,"sha_match":actual==exp})
    return rows, all(r["exists"] and r["sha_match"] for r in rows)

def load_frozen_panel(cfg):
    x=pd.read_parquet(cfg["source"]["frozen_ret120_panel"])
    dc,sc=discover_panel_columns(x)
    z=x[[dc,sc,"ret120"]].copy().rename(columns={dc:"signal_date",sc:"symbol"})
    z["signal_date"]=normalize_date(z["signal_date"])
    z["symbol"]=z["symbol"].astype(str)
    z["ret120"]=pd.to_numeric(z["ret120"],errors="coerce")
    a=pd.Timestamp(cfg["window"]["signal_start"]);b=pd.Timestamp(cfg["window"]["signal_cutoff_h120"])
    z=z[(z["signal_date"]>=a)&(z["signal_date"]<=b)].copy()
    if z.duplicated(["signal_date","symbol"]).any():
        raise RuntimeError("duplicate frozen symbol-date rows")
    return z

def spearman_corr_no_scipy(a,b):
    x=pd.DataFrame({"a":a,"b":b}).dropna()
    if len(x)<2:
        return None
    ra=x["a"].rank(method="average").to_numpy(dtype=float)
    rb=x["b"].rank(method="average").to_numpy(dtype=float)
    if np.std(ra)==0 or np.std(rb)==0:
        return None
    return float(np.corrcoef(ra,rb)[0,1])

def load_features(cfg):
    z=pd.read_parquet(cfg["source"]["canonical_features"])
    req=["trade_date","symbol","adj_close","adj_open"]
    miss=[c for c in req if c not in z.columns]
    if miss:raise RuntimeError(f"canonical features missing {miss}")
    z=z[req].copy()
    z["trade_date"]=pd.to_datetime(z["trade_date"])
    z["symbol"]=z["symbol"].astype(str)
    z["adj_close"]=pd.to_numeric(z["adj_close"],errors="coerce")
    z["adj_open"]=pd.to_numeric(z["adj_open"],errors="coerce")
    z=z.sort_values(["symbol","trade_date"]).reset_index(drop=True)
    z["canonical_ret120"]=z.groupby("symbol",sort=False)["adj_close"].pct_change(120,fill_method=None)
    return z

def bridge(cfg,write=True):
    frozen=load_frozen_panel(cfg)
    frozen_q=assign_quintiles(frozen.rename(columns={"ret120":"ret120"}),"ret120")
    feat=load_features(cfg)

    can=feat[["trade_date","symbol","canonical_ret120"]].rename(columns={"trade_date":"signal_date"})
    j=frozen_q.merge(can,on=["signal_date","symbol"],how="left",validate="one_to_one")
    missing=int(j["canonical_ret120"].isna().sum())

    can_bucket=assign_quintiles(
        j[["signal_date","symbol","canonical_ret120"]].rename(columns={"canonical_ret120":"ret120"}),
        "ret120"
    )[["signal_date","symbol","bucket"]].rename(columns={"bucket":"canonical_bucket"})
    j=j.merge(can_bucket,on=["signal_date","symbol"],how="left",validate="one_to_one")

    fq=set(map(tuple,j.loc[j["bucket"].eq("Q5"),["signal_date","symbol"]].itertuples(index=False,name=None)))
    cq=set(map(tuple,j.loc[j["canonical_bucket"].eq("Q5"),["signal_date","symbol"]].itertuples(index=False,name=None)))
    mismatch=len(fq.symmetric_difference(cq))
    overlap=len(fq & cq)
    union=len(fq | cq)
    q5_jaccard=overlap/union if union else None

    both=j[["ret120","canonical_ret120"]].dropna()
    corr=spearman_corr_no_scipy(both["ret120"],both["canonical_ret120"]) if len(both)>1 else None

    by_year=[]
    tmp=j.copy();tmp["year"]=tmp["signal_date"].dt.year
    for y,g in tmp.groupby("year"):
        f=set(map(tuple,g.loc[g["bucket"].eq("Q5"),["signal_date","symbol"]].itertuples(index=False,name=None)))
        c=set(map(tuple,g.loc[g["canonical_bucket"].eq("Q5"),["signal_date","symbol"]].itertuples(index=False,name=None)))
        by_year.append({"year":int(y),"frozen_q5":len(f),"canonical_q5":len(c),
                        "mismatch_rows":len(f.symmetric_difference(c)),
                        "jaccard":len(f&c)/len(f|c) if (f|c) else None})

    exact=(missing==int(cfg["bridge"]["required_missing_canonical_keys"])
           and mismatch==int(cfg["bridge"]["required_q5_key_mismatch_rows"]))

    out={
      "status":"PASS_RET120_CANONICAL_SIGNAL_BRIDGE_EXACT" if exact else "FAIL_CLOSED_RET120_CANONICAL_SIGNAL_BRIDGE",
      "frozen_panel_sha256":sha256_file(cfg["source"]["frozen_ret120_panel"]),
      "canonical_features_sha256":sha256_file(cfg["source"]["canonical_features"]),
      "window":cfg["window"],
      "rows":len(j),"signal_dates":int(j["signal_date"].nunique()),
      "frozen_q5_rows":len(fq),"canonical_q5_rows":len(cq),
      "missing_canonical_ret120_rows":missing,
      "q5_membership_mismatch_rows":mismatch,
      "q5_jaccard":q5_jaccard,
      "ret120_spearman":corr,
      "by_year":by_year,
      "contract":cfg["bridge"],
      "decision":"Full-market RET120 may be run only if this bridge is exact. A near-match is not promoted by tolerance."
    }
    if write:
        od=Path(cfg["output_dir"]);od.mkdir(parents=True,exist_ok=True)
        json_write(od/"ret120_canonical_signal_bridge_v0_1.json",out)
    return out

def load_universe(cfg):
    u=pd.read_parquet(cfg["source"]["full_market_universe"],columns=["trade_date","symbol"])
    u["trade_date"]=pd.to_datetime(u["trade_date"])
    u["symbol"]=u["symbol"].astype(str)
    a=pd.Timestamp(cfg["window"]["signal_start"]);b=pd.Timestamp(cfg["window"]["signal_cutoff_h120"])
    return u[(u["trade_date"]>=a)&(u["trade_date"]<=b)].drop_duplicates(["trade_date","symbol"])

def build_calendar(features):
    return sorted(pd.to_datetime(features["trade_date"].dropna().unique()).tolist())

def make_signal_panel(cfg,features,universe):
    x=features.merge(universe,left_on=["trade_date","symbol"],right_on=["trade_date","symbol"],how="inner")
    x=x[(x["trade_date"]>=pd.Timestamp(cfg["window"]["signal_start"]))&
        (x["trade_date"]<=pd.Timestamp(cfg["window"]["signal_cutoff_h120"]))].copy()
    x=x.rename(columns={"trade_date":"signal_date","canonical_ret120":"ret120"})
    q=assign_quintiles(x[["signal_date","symbol","ret120"]],"ret120")
    return q

def event_panel(cfg,signals,features):
    dates=build_calendar(features); pos={d:i for i,d in enumerate(dates)}
    feat_idx=features.set_index(["symbol","trade_date"])[["adj_open","adj_close"]]
    rows=[]
    for r in signals.itertuples(index=False):
        if r.bucket is None or pd.isna(r.ret120): continue
        i=pos.get(pd.Timestamp(r.signal_date))
        if i is None or i+120>=len(dates):continue
        ed=dates[i+1]; d20=dates[i+20];d60=dates[i+60];d120=dates[i+120]
        try:
            entry=float(feat_idx.loc[(r.symbol,ed),"adj_open"])
            c20=float(feat_idx.loc[(r.symbol,d20),"adj_close"])
            c60=float(feat_idx.loc[(r.symbol,d60),"adj_close"])
            c120=float(feat_idx.loc[(r.symbol,d120),"adj_close"])
        except Exception:
            continue
        if not all(math.isfinite(v) and v>0 for v in [entry,c20,c60,c120]):continue
        rows.append({"signal_date":pd.Timestamp(r.signal_date),"symbol":r.symbol,"ret120":float(r.ret120),
                     "bucket":r.bucket,"entry_date":ed,"exit20":d20,"exit60":d60,"exit120":d120,
                     "raw20":c20/entry-1.0,"raw60":c60/entry-1.0,"raw120":c120/entry-1.0})
    return pd.DataFrame(rows)

def add_market_adjustment(events):
    z=events.copy()
    for h in [20,60,120]:
        med=z.groupby("signal_date")[f"raw{h}"].median().rename(f"market{h}")
        z=z.merge(med,on="signal_date",how="left")
        z[f"adj{h}"]=z[f"raw{h}"]-z[f"market{h}"]
    return z

def summarize_horizon(events,h,cfg):
    col=f"adj{h}"
    means=events.groupby("bucket")[col].mean().to_dict()
    q5=means.get("Q5");q1=means.get("Q1")
    spread=float(q5-q1) if q5 is not None and q1 is not None else None
    mono=monotonicity({k:float(v) for k,v in means.items()})

    folds=[]
    for name,bounds in cfg["frozen_folds"].items():
        if name=="source":continue
        a,b=map(pd.Timestamp,bounds)
        g=events[(events["signal_date"]>=a)&(events["signal_date"]<=b)]
        gm=g.groupby("bucket")[col].mean().to_dict()
        s=None
        if "Q5" in gm and "Q1" in gm:s=float(gm["Q5"]-gm["Q1"])
        folds.append({"fold":name,"start":bounds[0],"end":bounds[1],
                      "rows":len(g),"q5_minus_q1":s,"positive":bool(s is not None and s>0)})
    return {"horizon":h,"bucket_adjusted_means":{str(k):safe(float(v)) for k,v in means.items()},
            "q5_minus_q1":spread,"monotonicity":mono,
            "positive_folds":sum(x["positive"] for x in folds),"folds":folds}

def verdict(metrics,cfg):
    all_spread=all(m["q5_minus_q1"] is not None and m["q5_minus_q1"]>0 for m in metrics)
    mono_n=sum(m["monotonicity"] is not None and m["monotonicity"]>=float(cfg["frozen_fast_reject_gate"]["at_least_2_horizons_monotonicity_ge"]) for m in metrics)
    each_fold=all(m["positive_folds"]>=int(cfg["frozen_fast_reject_gate"]["each_horizon_positive_folds_min"]) for m in metrics)
    checks={"all_horizon_q5_minus_q1_positive":all_spread,
            "at_least_2_horizons_monotonicity_ge_0_5":mono_n>=2,
            "each_horizon_at_least_2_of_3_positive_folds":each_fold}
    return ("CANDIDATE" if all(checks.values()) else "REJECT"),checks

def check(cfg):
    inputs,ok=validate_inputs(cfg)
    out={"status":"PASS_FULL_MARKET_RET120_GENERALIZATION_V0_1_CHECK" if ok else "BLOCKED_FULL_MARKET_RET120_GENERALIZATION_V0_1_CHECK",
         "inputs":inputs,"bridge_contract":cfg["bridge"],"definition":cfg["definition"],
         "frozen_fast_reject_gate":cfg["frozen_fast_reject_gate"],"safety":cfg["safety"]}
    print(json.dumps(out,ensure_ascii=False,indent=2))
    return 0 if ok else 3

def do_bridge(cfg):
    inputs,ok=validate_inputs(cfg)
    if not ok:raise RuntimeError("input hash/existence validation failed")
    out=bridge(cfg,True)
    print(json.dumps(out,ensure_ascii=False,indent=2))
    return 0 if out["status"].startswith("PASS_") else 3

def run(cfg):
    inputs,ok=validate_inputs(cfg)
    if not ok:raise RuntimeError("input hash/existence validation failed")
    br=bridge(cfg,True)
    if br["status"]!="PASS_RET120_CANONICAL_SIGNAL_BRIDGE_EXACT":
        raise RuntimeError("canonical ret120 does not exactly reproduce frozen CSI500 Q5 membership; full-market transfer blocked")

    features=load_features(cfg)
    universe=load_universe(cfg)
    signals=make_signal_panel(cfg,features,universe)
    events=event_panel(cfg,signals,features)
    if events.empty:raise RuntimeError("no complete full-market RET120 events")
    events=add_market_adjustment(events)
    metrics=[summarize_horizon(events,h,cfg) for h in [20,60,120]]
    v,checks=verdict(metrics,cfg)

    od=Path(cfg["output_dir"]);od.mkdir(parents=True,exist_ok=True)
    ep=od/"full_market_ret120_events_v0_1.parquet"
    events.to_parquet(ep,index=False)
    result={
      "status":"PASS_FULL_MARKET_RET120_GENERALIZATION_V0_1_RUN",
      "generated_at":datetime.now().isoformat(timespec="seconds"),
      "research_id":cfg["research_id"],
      "bridge":br,
      "definition":cfg["definition"],
      "window":cfg["window"],
      "frozen_folds":cfg["frozen_folds"],
      "coverage":{"signal_rows":len(signals),"signal_dates":int(signals["signal_date"].nunique()),
                  "symbols":int(signals["symbol"].nunique()),"complete_event_rows":len(events),
                  "q5_rows":int((events["bucket"]=="Q5").sum()),"q1_rows":int((events["bucket"]=="Q1").sum())},
      "metrics":{f"H{m['horizon']}":m for m in metrics},
      "gate":{"checks":checks,"verdict":v,"rule":cfg["frozen_fast_reject_gate"]},
      "interpretation_contract":"Universe-transfer test only. Existing CSI500 RET120 Strategy #2 / Forward are unchanged regardless of this result.",
      "next_if_candidate":"Run a full-market RET120 H120 standalone portfolio with the already-frozen 1%/30%/30-position policy; do not alter CSI500 Strategy #2.",
      "next_if_reject":"Freeze full-market RET120 transfer. Keep existing CSI500 RET120 Strategy #2 unchanged; do not tune bucket thresholds or add filters.",
      "artifacts":{"events":str(ep),"events_sha256":sha256_file(ep)},
      "safety":cfg["safety"],
    }
    sp=od/"full_market_ret120_generalization_v0_1_summary.json";json_write(sp,result)
    print(json.dumps(result,ensure_ascii=False,indent=2,default=safe))
    print("OUTPUT:",od)
    return 0

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("command",choices=["check","bridge","run"])
    a=ap.parse_args();cfg=load_cfg()
    try:
        if a.command=="check":rc=check(cfg)
        elif a.command=="bridge":rc=do_bridge(cfg)
        else:rc=run(cfg)
    except Exception as e:
        print(json.dumps({"status":"FAIL_CLOSED_FULL_MARKET_RET120_GENERALIZATION_V0_1",
                          "error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2))
        rc=3
    raise SystemExit(rc)

if __name__=="__main__":
    main()
