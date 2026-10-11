
from __future__ import annotations
import argparse, hashlib, json, math
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT=Path(__file__).resolve().parents[1]
CFG=ROOT/"config/opportunity/p1_breakout_acceptance_v0_1.yaml"

def load_cfg(): return yaml.safe_load(CFG.read_text(encoding="utf-8"))

def sha256_file(path):
    h=hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""): h.update(b)
    return h.hexdigest()

def safe_float(v):
    try:
        x=float(v)
        return x if math.isfinite(x) else np.nan
    except Exception:return np.nan

def is_target_symbol(s):
    s=str(s).upper()
    if s.endswith(".BJ"):return False
    if s.endswith(".SH"):return not s.split(".")[0].startswith("900")
    if s.endswith(".SZ"):return not s.split(".")[0].startswith("200")
    return False

def prepare_panel(panel):
    req={"trade_date","symbol","compression_ratio","breakout_margin","range_release_ratio"}
    miss=req-set(panel.columns)
    if miss: raise RuntimeError(f"D2 panel missing columns: {sorted(miss)}")
    x=panel.copy()
    x["trade_date"]=pd.to_datetime(x["trade_date"])
    for c in ["compression_ratio","breakout_margin","range_release_ratio"]:
        x[c]=pd.to_numeric(x[c],errors="coerce")
    return x

def d2_signal_with_rank_method(panel,cfg,method):
    """
    D2 compression_rank_max refers to same-day cross-sectional percentile rank
    of compression_ratio (smaller = more compressed), NOT raw ratio <= .25.
    """
    x=prepare_panel(panel)
    rank=x.groupby("trade_date",sort=False)["compression_ratio"].rank(method=method,pct=True,ascending=True)
    c=cfg["frozen_d2_parent"]
    setup=rank.le(float(c["compression_rank_max"])).fillna(False)
    sig=(
        setup
        & x["breakout_margin"].gt(float(c["breakout_margin_min_exclusive"]))
        & x["range_release_ratio"].gt(float(c["range_release_ratio_min_exclusive"]))
    ).fillna(False)
    return rank,setup,sig

def frozen_primary_expected(summary):
    p=summary["primary"]
    years={}
    for y,z in p.get("by_year",{}).items():
        years[str(y)]={
            "valid_rows":int(z["valid_rows"]),
            "compression_setup_count":int(z["compression_setup_count"]),
            "breakout_signal_count":int(z["breakout_signal_count"]),
        }
    return {
        "all_valid_rows":int(p["all_valid_rows"]),
        "compression_setup_count":int(p["compression_setup_count"]),
        "breakout_signal_count":int(p["breakout_signal_count"]),
        "by_year":years,
    }

def identity_for_method(panel,summary,cfg,method):
    x=prepare_panel(panel)
    rank,setup,sig=d2_signal_with_rank_method(x,cfg,method)
    label_col=cfg["frozen_d2_parent"]["primary_label_column"]
    if label_col not in x.columns:
        raise RuntimeError(f"D2 panel missing frozen primary label column {label_col}")
    valid=x[label_col].notna()
    obs={
        "all_valid_rows":int(valid.sum()),
        "compression_setup_count":int((setup&valid).sum()),
        "breakout_signal_count":int((sig&valid).sum()),
        "by_year":{}
    }
    for y,gidx in x[valid].groupby(x.loc[valid,"trade_date"].dt.year).groups.items():
        idx=pd.Index(gidx)
        obs["by_year"][str(int(y))]={
            "valid_rows":len(idx),
            "compression_setup_count":int(setup.loc[idx].sum()),
            "breakout_signal_count":int(sig.loc[idx].sum()),
        }
    exp=frozen_primary_expected(summary)
    top_ok=all(obs[k]==exp[k] for k in ["all_valid_rows","compression_setup_count","breakout_signal_count"])
    year_ok=obs["by_year"]==exp["by_year"]
    return {"method":method,"top_ok":top_ok,"year_ok":year_ok,"exact":top_ok and year_ok,
            "observed":obs,"expected":exp},rank,setup,sig

def bool_mask_sha(mask):
    a=np.asarray(pd.Series(mask).fillna(False).astype(bool).to_numpy(),dtype=np.uint8)
    return hashlib.sha256(a.tobytes()).hexdigest()

def resolve_d2_rank_semantics(panel,summary,cfg):
    """
    Several pandas tie methods may all reproduce the frozen D2 aggregates.
    That is not ambiguous if they also produce the exact same setup/signal masks.

    Accept iff:
      1) at least one method exactly reproduces frozen D2 Primary totals + yearly counts;
      2) every exact-matching method has identical setup membership and signal membership.

    In that case tie handling is empirically irrelevant for this dataset. Use 'average'
    as canonical when available, otherwise the first exact method.
    """
    attempts=[]
    matches=[]
    payload={}
    fingerprints={}

    for method in cfg["frozen_d2_parent"]["rank_methods_to_identity_check"]:
        ident,rank,setup,sig=identity_for_method(panel,summary,cfg,method)
        ident["setup_mask_sha256"]=bool_mask_sha(setup)
        ident["signal_mask_sha256"]=bool_mask_sha(sig)
        attempts.append(ident)
        if ident["exact"]:
            matches.append(method)
            payload[method]=(rank,setup,sig)
            fingerprints[method]=(
                ident["setup_mask_sha256"],
                ident["signal_mask_sha256"],
            )

    if not matches:
        raise RuntimeError(
            "BLOCKED_D2_RANK_SEMANTICS_NO_IDENTITY_MATCH "
            f"attempts={json.dumps(attempts,ensure_ascii=False)}"
        )

    unique_fingerprints=set(fingerprints.values())
    if len(unique_fingerprints)!=1:
        raise RuntimeError(
            "BLOCKED_D2_RANK_SEMANTICS_MEMBER_AMBIGUITY "
            f"exact_methods={matches} fingerprints={json.dumps(fingerprints,ensure_ascii=False)}"
        )

    canonical="average" if "average" in matches else matches[0]
    rank,setup,sig=payload[canonical]
    resolution={
        "canonical_method":canonical,
        "exact_methods":matches,
        "membership_equivalent":True,
        "setup_mask_sha256":fingerprints[canonical][0],
        "signal_mask_sha256":fingerprints[canonical][1],
        "interpretation":"All exact-matching rank methods select identical D2 setup/signal memberships; tie convention is immaterial on the frozen panel."
    }
    return canonical,rank,setup,sig,attempts,resolution

def breakout_level_relative(margin):
    m=safe_float(margin)
    return np.nan if (not math.isfinite(m) or m<=-1) else 1.0/(1.0+m)

def normalized_future_path(future):
    z=future.copy()
    for c in ["open","high","low","close","pre_close"]:
        z[c]=pd.to_numeric(z[c],errors="coerce")
    valid=z["open"].gt(0)&z["high"].gt(0)&z["low"].gt(0)&z["close"].gt(0)&z["pre_close"].gt(0)
    factor=(z["close"]/z["pre_close"]).where(valid)
    bad=factor.isna().cummax()
    cum=factor.fillna(1.0).cumprod().where(~bad)
    z["rel_close"]=cum
    z["rel_open"]=(cum*z["open"]/z["close"]).where(~bad)
    z["rel_high"]=(cum*z["high"]/z["close"]).where(~bad)
    z["rel_low"]=(cum*z["low"]/z["close"]).where(~bad)
    return z

def classify_acceptance(path3,level):
    if len(path3)<3 or not math.isfinite(level):return None
    cl=pd.to_numeric(path3["rel_close"],errors="coerce")
    lo=pd.to_numeric(path3["rel_low"],errors="coerce")
    hi=pd.to_numeric(path3["rel_high"],errors="coerce")
    if cl.isna().any() or lo.isna().any() or hi.isna().any():return None
    accepted=bool((cl>=level).all())
    return {
        "accepted_3d":accepted,"rejected_3d":not accepted,
        "retest_hold":accepted and bool((lo<=level).any()),
        "no_touch_hold":accepted and bool((lo>level).all()),
        "reacceleration":accepted and bool(cl.iloc[2]>max(hi.iloc[0],hi.iloc[1])),
        "min_close_margin":float(cl.min()/level-1),
        "min_low_margin":float(lo.min()/level-1),
        "t3_close_margin":float(cl.iloc[2]/level-1),
    }

def trend_label(path,entry,target,horizon,mae_floor):
    if len(path)<horizon or not math.isfinite(entry) or entry<=0:return None
    z=path.iloc[:horizon]
    hi=pd.to_numeric(z["rel_high"],errors="coerce")/entry-1
    lo=pd.to_numeric(z["rel_low"],errors="coerce")/entry-1
    if hi.isna().any() or lo.isna().any():return None
    hit=np.flatnonzero(hi.to_numpy()>=target)
    if len(hit)==0:return {"label":0,"time_to_target":None,"mfe":float(hi.max()),"mae_before_hit":float(lo.min())}
    k=int(hit[0]);mae=float(lo.iloc[:k+1].min())
    return {"label":int(mae>=mae_floor),"time_to_target":k+1,
            "mfe":float(hi.iloc[:k+1].max()),"mae_before_hit":mae}

def load_market_bars(cfg,symbols,min_date):
    import duckdb
    con=duckdb.connect(cfg["source"]["market_catalog"],read_only=True)
    table=cfg["source"]["market_table"]
    try:
        cols=[r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()]
        req=["trade_date","symbol","open","high","low","close","pre_close"]
        miss=[c for c in req if c not in cols]
        if miss:raise RuntimeError(f"{table} missing {miss}; columns={cols}")
        syms=pd.DataFrame({"symbol":sorted(set(symbols))});con.register("p1_symbols",syms)
        q=f"""SELECT
        CAST(b.trade_date AS DATE) AS trade_date,
        b.symbol AS symbol,
        CAST(b.open AS DOUBLE) AS open_px,
        CAST(b.high AS DOUBLE) AS high_px,
        CAST(b.low AS DOUBLE) AS low_px,
        CAST(b.close AS DOUBLE) AS close_px,
        CAST(b.pre_close AS DOUBLE) AS pre_close_px
        FROM {table} b INNER JOIN p1_symbols s ON b.symbol=s.symbol
        WHERE CAST(b.trade_date AS DATE)>=CAST(? AS DATE)
        ORDER BY b.symbol,CAST(b.trade_date AS DATE)"""
        out=con.execute(q,[min_date]).fetchdf()
        return out.rename(columns={
            "open_px":"open",
            "high_px":"high",
            "low_px":"low",
            "close_px":"close",
            "pre_close_px":"pre_close",
        })
    finally:con.close()

def event_study(events,bars,cfg):
    p1=cfg["labels"]["primary"];p2=cfg["labels"]["secondary"]
    max_h=max(int(p1["horizon_sessions"]),int(p2["horizon_sessions"]))
    bars=bars.copy();bars["trade_date"]=pd.to_datetime(bars["trade_date"])
    groups={s:g.reset_index(drop=True) for s,g in bars.groupby("symbol",sort=False)}
    maps={s:{d:i for i,d in enumerate(g["trade_date"])} for s,g in groups.items()}
    rows=[]
    for _,e in events.iterrows():
        sym=e["symbol"];td=pd.Timestamp(e["trade_date"]);g=groups.get(sym);idx=maps.get(sym,{}).get(td)
        base={"trade_date":td,"symbol":sym,"compression_rank":safe_float(e["compression_rank"]),
              "compression_ratio":safe_float(e["compression_ratio"]),"breakout_margin":safe_float(e["breakout_margin"]),
              "range_release_ratio":safe_float(e["range_release_ratio"])}
        if g is None or idx is None:
            base.update(state_valid=False,invalid_reason="EVENT_DATE_NOT_IN_MARKET_TABLE");rows.append(base);continue
        path=normalized_future_path(g.iloc[idx+1:idx+1+4+max_h].copy())
        if len(path)<4:
            base.update(state_valid=False,invalid_reason="INSUFFICIENT_T_PLUS_4");rows.append(base);continue
        level=breakout_level_relative(e["breakout_margin"]);state=classify_acceptance(path.iloc[:3],level)
        if state is None:
            base.update(state_valid=False,invalid_reason="INVALID_ACCEPTANCE_PATH");rows.append(base);continue
        entry=safe_float(path.iloc[3]["rel_open"])
        if not math.isfinite(entry) or entry<=0:
            base.update(state_valid=False,invalid_reason="INVALID_T_PLUS_4_OPEN");rows.append(base);continue
        base.update(state);base.update(state_valid=True,invalid_reason=None,breakout_level_rel=level,
            confirmation_date=path.iloc[2]["trade_date"],entry_date=path.iloc[3]["trade_date"],entry_rel=entry)
        ep=path.iloc[3:]
        for lab in [p1,p2]:
            r=trend_label(ep,entry,float(lab["target"]),int(lab["horizon_sessions"]),float(lab["mae_floor"]))
            k=lab["name"];base[k+"_matured"]=r is not None;base[k+"_label"]=r["label"] if r else np.nan
            base[k+"_time_to_target"]=r["time_to_target"] if r else np.nan
            base[k+"_mfe"]=r["mfe"] if r else np.nan;base[k+"_mae_before_hit"]=r["mae_before_hit"] if r else np.nan
        rows.append(base)
    return pd.DataFrame(rows)

def metric_block(df,name):
    lab=name+"_label";mat=name+"_matured"
    x=df[df["state_valid"].fillna(False)&df[mat].fillna(False)].copy()
    a=x[x["accepted_3d"].fillna(False)];r=x[x["rejected_3d"].fillna(False)]
    ap=float(a[lab].mean()) if len(a) else None;rp=float(r[lab].mean()) if len(r) else None
    yrs={}
    for y,g in x.groupby(x["trade_date"].dt.year):
        aa=g[g["accepted_3d"].fillna(False)];rr=g[g["rejected_3d"].fillna(False)]
        if len(aa) and len(rr):
            av=float(aa[lab].mean());rv=float(rr[lab].mean())
            yrs[str(int(y))]={"accepted_n":len(aa),"rejected_n":len(rr),"accepted_precision":av,
                              "rejected_precision":rv,"lift":av/rv if rv>0 else None,
                              "precision_minus_rejected":av-rv}
    return {"matured_rows":len(x),"accepted_n":len(a),"rejected_n":len(r),
            "accepted_precision":ap,"rejected_precision":rp,
            "lift_vs_rejected":ap/rp if ap is not None and rp not in (None,0) else None,
            "precision_minus_rejected":ap-rp if ap is not None and rp is not None else None,
            "positive_years":sum(z["precision_minus_rejected"]>0 for z in yrs.values()),"by_year":yrs}

def diagnostic_block(df,name):
    lab=name+"_label";mat=name+"_matured"
    x=df[df["state_valid"].fillna(False)&df[mat].fillna(False)&df["accepted_3d"].fillna(False)]
    out={}
    for n,c in [("RETEST_HOLD","retest_hold"),("NO_TOUCH_HOLD","no_touch_hold"),("REACCELERATION","reacceleration")]:
        g=x[x[c].fillna(False)];out[n]={"n":len(g),"precision":float(g[lab].mean()) if len(g) else None}
    return out

def check(cfg):
    ps=[Path(cfg["source"]["d2_summary"]),Path(cfg["source"]["d2_panel"]),Path(cfg["source"]["market_catalog"])]
    sha=sha256_file(ps[1]) if ps[1].exists() else None;exp=cfg["source"]["expected_d2_panel_sha256"]
    good=all(p.exists() for p in ps) and sha==exp
    out={"status":"PASS_P1_BREAKOUT_ACCEPTANCE_V0_1_3_CHECK" if good else "BLOCKED_P1_BREAKOUT_ACCEPTANCE_V0_1_1_CHECK",
         "inputs":[{"path":str(p),"exists":p.exists()} for p in ps],"d2_panel_sha256":sha,"sha_match":sha==exp,
          "hotfix":["compression_rank is reconstructed as same-day cross-sectional percentile rank",
                   "5120 is used only as frozen D2 primary identity check, not total parent-event count",
                   "P1 parent uses all D2 mechanism signals before old D2 maturity filtering",
                   "multiple exact rank methods are accepted only when their setup/signal membership masks are identical",
                   "DuckDB market-bar SQL uses non-reserved *_px aliases then renames in pandas"],
         "research_logic_changed":False,"p1_gate_changed":False,"research_domain":"SH/SZ A-shares only"}
    print(json.dumps(out,ensure_ascii=False,indent=2));return 0 if good else 3

def run(cfg):
    summary=json.loads(Path(cfg["source"]["d2_summary"]).read_text(encoding="utf-8"))
    pp=Path(cfg["source"]["d2_panel"])
    if sha256_file(pp)!=cfg["source"]["expected_d2_panel_sha256"]:raise RuntimeError("D2 panel SHA mismatch")
    panel=pd.read_parquet(pp)
    method,rank,setup,sig,attempts,rank_resolution=resolve_d2_rank_semantics(panel,summary,cfg)
    x=prepare_panel(panel);x["compression_rank"]=rank
    events=x.loc[sig,["trade_date","symbol","compression_rank","compression_ratio","breakout_margin","range_release_ratio"]].copy()
    total_parent=len(events)
    events=events[events["symbol"].map(is_target_symbol)].copy()
    if events.duplicated(["trade_date","symbol"]).any():raise RuntimeError("duplicate parent event keys")
    bars=load_market_bars(cfg,events["symbol"].unique(),events["trade_date"].min().date().isoformat())
    study=event_study(events,bars,cfg)
    pn=cfg["labels"]["primary"]["name"];sn=cfg["labels"]["secondary"]["name"]
    p=metric_block(study,pn);s=metric_block(study,sn);gc=cfg["gate"]
    gate={
      "accepted_min":{"threshold":int(gc["selected_min"]),"actual":p["accepted_n"],"pass":p["accepted_n"]>=int(gc["selected_min"])},
      "primary_lift_vs_rejected_min":{"threshold":float(gc["primary_lift_vs_rejected_min"]),"actual":p["lift_vs_rejected"],
        "pass":p["lift_vs_rejected"] is not None and p["lift_vs_rejected"]>=float(gc["primary_lift_vs_rejected_min"])},
      "primary_precision_minus_rejected_min":{"threshold":float(gc["primary_precision_minus_rejected_min"]),"actual":p["precision_minus_rejected"],
        "pass":p["precision_minus_rejected"] is not None and p["precision_minus_rejected"]>=float(gc["primary_precision_minus_rejected_min"])},
      "positive_years_min":{"threshold":int(gc["positive_years_min"]),"actual":p["positive_years"],"pass":p["positive_years"]>=int(gc["positive_years_min"])}
    }
    verdict="PASS_P1_BREAKOUT_ACCEPTANCE_TO_ROBUSTNESS" if all(v["pass"] for v in gate.values()) else "FAST_REJECT_P1_BREAKOUT_ACCEPTANCE_V0_1"
    outdir=Path(cfg["output_dir"]);outdir.mkdir(parents=True,exist_ok=True)
    po=outdir/"p1_breakout_acceptance_v0_1_panel.parquet";study.to_parquet(po,index=False)
    result={"status":"PASS_P1_BREAKOUT_ACCEPTANCE_V0_1_3_RUN","generated_at":datetime.now().isoformat(timespec="seconds"),
      "research_id":cfg["research_id"],"parent_integrity":{"resolved_rank_method":method,"rank_semantics_resolution":rank_resolution,"identity_attempts":attempts,
      "all_d2_mechanism_parent_events":total_parent,"sh_sz_parent_events":len(events)},
      "mechanism":cfg["mechanism"],"labels":cfg["labels"],
      "coverage":{"parent_events":len(events),"state_valid":int(study["state_valid"].fillna(False).sum()),
      "state_valid_rate":float(study["state_valid"].fillna(False).mean()),"accepted_3d":int(study["accepted_3d"].fillna(False).sum()),
      "rejected_3d":int(study["rejected_3d"].fillna(False).sum())},
      "primary":p,"secondary":s,"diagnostics_primary":diagnostic_block(study,pn),"gate":gate,"overall_verdict":verdict,
      "interpretation_contract":"Primary only: frozen D2 mechanism breakout -> 3-session acceptance vs rejection -> common T+4 open. Diagnostics cannot rescue a failed primary gate.",
      "artifacts":{"panel":str(po),"panel_sha256":sha256_file(po)},"safety":cfg["safety"]}
    (outdir/"p1_breakout_acceptance_v0_1_summary.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False,indent=2));print("OUTPUT:",outdir);return 0

def main():
    p=argparse.ArgumentParser();p.add_argument("command",choices=["check","run"]);a=p.parse_args();cfg=load_cfg()
    try:raise SystemExit(check(cfg) if a.command=="check" else run(cfg))
    except Exception as e:
        print(json.dumps({"status":"FAIL_CLOSED_P1_BREAKOUT_ACCEPTANCE_V0_1_3","error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2));raise SystemExit(3)
if __name__=="__main__":main()
