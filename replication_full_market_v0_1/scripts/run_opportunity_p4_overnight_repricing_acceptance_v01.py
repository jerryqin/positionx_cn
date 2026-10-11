
from __future__ import annotations
import argparse, hashlib, json, math
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT=Path(__file__).resolve().parents[1]
CFG=ROOT/"config/opportunity/p4_overnight_repricing_acceptance_v0_1.yaml"

def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))

def sha256_file(path):
    h=hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda:f.read(1024*1024), b""):
            h.update(b)
    return h.hexdigest()

def safe_float(v):
    try:
        x=float(v)
        return x if math.isfinite(x) else np.nan
    except Exception:
        return np.nan

def is_target_symbol(s):
    s=str(s).upper()
    if s.endswith(".BJ"):
        return False
    if s.endswith(".SH"):
        return not s.split(".")[0].startswith("900")
    if s.endswith(".SZ"):
        return not s.split(".")[0].startswith("200")
    return False

def load_universe(cfg):
    p=Path(cfg["source"]["universe_panel"])
    if sha256_file(p)!=cfg["source"]["expected_universe_panel_sha256"]:
        raise RuntimeError("frozen universe panel SHA mismatch")
    x=pd.read_parquet(p,columns=["trade_date","symbol"])
    x["trade_date"]=pd.to_datetime(x["trade_date"])
    return x[x["symbol"].map(is_target_symbol)].drop_duplicates(["trade_date","symbol"]).copy()

def load_market_bars(cfg,symbols,min_date,max_date):
    import duckdb
    con=duckdb.connect(cfg["source"]["market_catalog"],read_only=True)
    table=cfg["source"]["market_table"]
    try:
        cols=[r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()]
        req=["trade_date","symbol","open","high","low","close","pre_close"]
        miss=[c for c in req if c not in cols]
        if miss:
            raise RuntimeError(f"{table} missing required columns: {miss}")
        syms=pd.DataFrame({"symbol":sorted(set(symbols))})
        con.register("p4_symbols",syms)
        q=f"""
        SELECT
          CAST(b.trade_date AS DATE) AS trade_date,
          b.symbol AS symbol,
          CAST(b.open AS DOUBLE) AS open_px,
          CAST(b.high AS DOUBLE) AS high_px,
          CAST(b.low AS DOUBLE) AS low_px,
          CAST(b.close AS DOUBLE) AS close_px,
          CAST(b.pre_close AS DOUBLE) AS pre_close_px
        FROM {table} b
        INNER JOIN p4_symbols s ON b.symbol=s.symbol
        WHERE CAST(b.trade_date AS DATE) BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
        ORDER BY b.symbol, CAST(b.trade_date AS DATE)
        """
        z=con.execute(q,[min_date,max_date]).fetchdf()
        return z.rename(columns={
            "open_px":"open","high_px":"high","low_px":"low",
            "close_px":"close","pre_close_px":"pre_close"
        })
    finally:
        con.close()

def synthetic_adjust(g):
    z=g.sort_values("trade_date").copy()
    for c in ["open","high","low","close","pre_close"]:
        z[c]=pd.to_numeric(z[c],errors="coerce")
    valid=z["open"].gt(0)&z["high"].gt(0)&z["low"].gt(0)&z["close"].gt(0)&z["pre_close"].gt(0)
    factor=(z["close"]/z["pre_close"]).where(valid)
    bad=factor.isna().cummax()
    cum=factor.fillna(1.0).cumprod().where(~bad)
    z["adj_close"]=cum
    z["adj_open"]=cum*z["open"]/z["close"]
    z["adj_high"]=cum*z["high"]/z["close"]
    z["adj_low"]=cum*z["low"]/z["close"]
    return z

def add_gap_features(x):
    z=x.copy()
    for c in ["open","high","low","close","pre_close"]:
        z[c]=pd.to_numeric(z[c],errors="coerce")
    valid=z["open"].gt(0)&z["low"].gt(0)&z["pre_close"].gt(0)
    z["opening_gap"]=(z["open"]/z["pre_close"]-1.0).where(valid)
    z["gap_rank"]=(
        z.groupby("trade_date",sort=False)["opening_gap"]
         .rank(method="average",pct=True,ascending=True)
    )
    z["parent_event"]=z["opening_gap"].gt(0)&z["gap_rank"].ge(0.95)
    z["accepted_gap"]=z["parent_event"]&z["low"].gt(z["pre_close"])
    z["rejected_gap"]=z["parent_event"]&~z["accepted_gap"]
    z["gap_hold_margin"]=z["low"]/z["pre_close"]-1.0
    rng=z["high"]-z["low"]
    z["close_location"]=((z["close"]-z["low"])/rng).where(rng.gt(0))
    return z

def build_event_panel(bars,eligible_keys):
    parts=[synthetic_adjust(g) for _,g in bars.groupby("symbol",sort=False)]
    full=pd.concat(parts,ignore_index=True)
    e=eligible_keys.copy()
    e["_eligible"]=True
    eligible=full.merge(e,on=["trade_date","symbol"],how="inner")
    eligible=add_gap_features(eligible)
    return eligible,full

def make_events(gap_panel,full_features):
    groups={s:g.sort_values("trade_date").reset_index(drop=True) for s,g in full_features.groupby("symbol",sort=False)}
    maps={s:{d:i for i,d in enumerate(g["trade_date"])} for s,g in groups.items()}
    rows=[]
    for _,r in gap_panel[gap_panel["parent_event"].fillna(False)].iterrows():
        sym=r["symbol"];td=pd.Timestamp(r["trade_date"])
        g=groups.get(sym);idx=maps.get(sym,{}).get(td)
        if g is None or idx is None or idx+1>=len(g):
            continue
        ent=g.iloc[idx+1]
        rows.append({
            "symbol":sym,
            "event_date":td,
            "entry_date":ent["trade_date"],
            "opening_gap":safe_float(r["opening_gap"]),
            "gap_rank":safe_float(r["gap_rank"]),
            "accepted_gap":bool(r["accepted_gap"]),
            "rejected_gap":bool(r["rejected_gap"]),
            "gap_hold_margin":safe_float(r["gap_hold_margin"]),
            "close_location":safe_float(r["close_location"]),
            "entry_adj_open":safe_float(ent["adj_open"]),
        })
    return pd.DataFrame(rows)

def trend_label(g,entry_index,entry_price,target,horizon,mae_floor):
    if not math.isfinite(entry_price) or entry_price<=0:
        return None
    if entry_index+horizon>len(g):
        return None
    z=g.iloc[entry_index:entry_index+horizon]
    hi=pd.to_numeric(z["adj_high"],errors="coerce")/entry_price-1.0
    lo=pd.to_numeric(z["adj_low"],errors="coerce")/entry_price-1.0
    if hi.isna().any() or lo.isna().any():
        return None
    hits=np.flatnonzero(hi.to_numpy()>=target)
    if len(hits)==0:
        return {"label":0,"time_to_target":None,"mfe":float(hi.max()),"mae_before_hit":float(lo.min())}
    k=int(hits[0])
    mae=float(lo.iloc[:k+1].min())
    return {
        "label":int(mae>=mae_floor),
        "time_to_target":k+1,
        "mfe":float(hi.iloc[:k+1].max()),
        "mae_before_hit":mae,
    }

def evaluate_events(events,full_features,cfg):
    groups={s:g.sort_values("trade_date").reset_index(drop=True) for s,g in full_features.groupby("symbol",sort=False)}
    maps={s:{d:i for i,d in enumerate(g["trade_date"])} for s,g in groups.items()}
    out=[]
    for _,e in events.iterrows():
        sym=e["symbol"];g=groups[sym]
        idx=maps[sym].get(pd.Timestamp(e["entry_date"]))
        r=e.to_dict()
        if idx is None:
            r.update(evaluation_valid=False,invalid_reason="ENTRY_DATE_NOT_FOUND")
            out.append(r);continue
        entry=safe_float(g.iloc[idx]["adj_open"])
        r["entry_adj_open"]=entry
        r["evaluation_valid"]=math.isfinite(entry) and entry>0
        r["invalid_reason"]=None if r["evaluation_valid"] else "INVALID_ENTRY_OPEN"
        for lab in [cfg["labels"]["primary"],cfg["labels"]["secondary"]]:
            z=trend_label(
                g,idx,entry,float(lab["target"]),
                int(lab["horizon_sessions"]),float(lab["mae_floor"])
            ) if r["evaluation_valid"] else None
            k=lab["name"]
            r[k+"_matured"]=z is not None
            r[k+"_label"]=z["label"] if z else np.nan
            r[k+"_time_to_target"]=z["time_to_target"] if z else np.nan
            r[k+"_mfe"]=z["mfe"] if z else np.nan
            r[k+"_mae_before_hit"]=z["mae_before_hit"] if z else np.nan
        out.append(r)
    return pd.DataFrame(out)

def metric_block(df,name):
    lab=name+"_label";mat=name+"_matured"
    x=df[df["evaluation_valid"].fillna(False)&df[mat].fillna(False)].copy()
    a=x[x["accepted_gap"].fillna(False)]
    r=x[x["rejected_gap"].fillna(False)]
    ap=float(a[lab].mean()) if len(a) else None
    rp=float(r[lab].mean()) if len(r) else None
    years={}
    for y,g in x.groupby(pd.to_datetime(x["event_date"]).dt.year):
        aa=g[g["accepted_gap"].fillna(False)]
        rr=g[g["rejected_gap"].fillna(False)]
        if len(aa) and len(rr):
            av=float(aa[lab].mean());rv=float(rr[lab].mean())
            years[str(int(y))]={
                "accepted_n":len(aa),
                "rejected_n":len(rr),
                "accepted_precision":av,
                "rejected_precision":rv,
                "lift":av/rv if rv>0 else None,
                "precision_minus_rejected":av-rv,
            }
    return {
        "matured_rows":len(x),
        "accepted_n":len(a),
        "rejected_n":len(r),
        "accepted_precision":ap,
        "rejected_precision":rp,
        "lift_vs_rejected":ap/rp if ap is not None and rp not in (None,0) else None,
        "precision_minus_rejected":ap-rp if ap is not None and rp is not None else None,
        "positive_years":sum(v["precision_minus_rejected"]>0 for v in years.values()),
        "by_year":years,
    }

def check(cfg):
    ps=[Path(cfg["source"]["universe_panel"]),Path(cfg["source"]["market_catalog"])]
    sha=sha256_file(ps[0]) if ps[0].exists() else None
    good=all(p.exists() for p in ps) and sha==cfg["source"]["expected_universe_panel_sha256"]
    out={
        "status":"PASS_P4_OVERNIGHT_REPRICING_ACCEPTANCE_V0_1_CHECK" if good else "BLOCKED_P4_OVERNIGHT_REPRICING_ACCEPTANCE_V0_1_CHECK",
        "inputs":[{"path":str(p),"exists":p.exists()} for p in ps],
        "universe_panel_sha256":sha,
        "sha_match":sha==cfg["source"]["expected_universe_panel_sha256"],
        "mechanism":cfg["mechanism"],
        "labels":cfg["labels"],
        "gate":cfg["gate"],
        "research_domain":"SH/SZ A-shares only",
    }
    print(json.dumps(out,ensure_ascii=False,indent=2))
    return 0 if good else 3

def run(cfg):
    universe=load_universe(cfg)
    min_d=(universe["trade_date"].min().date()-timedelta(days=10)).isoformat()
    max_d=(universe["trade_date"].max().date()+timedelta(days=220)).isoformat()
    bars=load_market_bars(cfg,universe["symbol"].unique(),min_d,max_d)
    bars["trade_date"]=pd.to_datetime(bars["trade_date"])
    gap_panel,full_features=build_event_panel(bars,universe)
    events=make_events(gap_panel,full_features)
    study=evaluate_events(events,full_features,cfg)

    pn=cfg["labels"]["primary"]["name"]
    sn=cfg["labels"]["secondary"]["name"]
    p=metric_block(study,pn)
    s=metric_block(study,sn)
    gc=cfg["gate"]
    gate={
        "selected_min":{
            "threshold":int(gc["selected_min"]),
            "actual":p["accepted_n"],
            "pass":p["accepted_n"]>=int(gc["selected_min"]),
        },
        "primary_lift_vs_rejected_min":{
            "threshold":float(gc["primary_lift_vs_rejected_min"]),
            "actual":p["lift_vs_rejected"],
            "pass":p["lift_vs_rejected"] is not None and p["lift_vs_rejected"]>=float(gc["primary_lift_vs_rejected_min"]),
        },
        "primary_precision_minus_rejected_min":{
            "threshold":float(gc["primary_precision_minus_rejected_min"]),
            "actual":p["precision_minus_rejected"],
            "pass":p["precision_minus_rejected"] is not None and p["precision_minus_rejected"]>=float(gc["primary_precision_minus_rejected_min"]),
        },
        "positive_years_min":{
            "threshold":int(gc["positive_years_min"]),
            "actual":p["positive_years"],
            "pass":p["positive_years"]>=int(gc["positive_years_min"]),
        },
    }
    verdict=(
        "PASS_P4_OVERNIGHT_REPRICING_ACCEPTANCE_TO_ROBUSTNESS"
        if all(v["pass"] for v in gate.values())
        else "FAST_REJECT_P4_OVERNIGHT_REPRICING_ACCEPTANCE_V0_1"
    )

    outdir=Path(cfg["output_dir"])
    outdir.mkdir(parents=True,exist_ok=True)
    po=outdir/"p4_overnight_repricing_acceptance_v0_1_panel.parquet"
    study.to_parquet(po,index=False)

    result={
        "status":"PASS_P4_OVERNIGHT_REPRICING_ACCEPTANCE_V0_1_RUN",
        "generated_at":datetime.now().isoformat(timespec="seconds"),
        "research_id":cfg["research_id"],
        "source":{
            "universe_panel":cfg["source"]["universe_panel"],
            "universe_panel_sha256":sha256_file(cfg["source"]["universe_panel"]),
            "market_catalog":cfg["source"]["market_catalog"],
            "market_table":cfg["source"]["market_table"],
        },
        "mechanism":cfg["mechanism"],
        "price_adjustment":cfg["price_adjustment"],
        "coverage":{
            "eligible_universe_keys":len(universe),
            "symbols":int(universe["symbol"].nunique()),
            "parent_gap_events":len(events),
            "accepted_gap_events":int(events["accepted_gap"].sum()) if len(events) else 0,
            "rejected_gap_events":int(events["rejected_gap"].sum()) if len(events) else 0,
            "evaluation_valid":int(study["evaluation_valid"].fillna(False).sum()) if len(study) else 0,
        },
        "primary":p,
        "secondary":s,
        "gate":gate,
        "overall_verdict":verdict,
        "interpretation_contract":"Primary question only: among same-day top-5%-ranked positive opening gaps, does an unfilled gap through the entire session add predictive value versus gaps that fully retrace intraday, with common T+1 open evaluation? No parameter scan and no post-result rescue.",
        "artifacts":{"panel":str(po),"panel_sha256":sha256_file(po)},
        "safety":cfg["safety"],
    }
    (outdir/"p4_overnight_repricing_acceptance_v0_1_summary.json").write_text(
        json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8"
    )
    print(json.dumps(result,ensure_ascii=False,indent=2))
    print("OUTPUT:",outdir)
    return 0

def main():
    p=argparse.ArgumentParser()
    p.add_argument("command",choices=["check","run"])
    a=p.parse_args()
    cfg=load_cfg()
    try:
        raise SystemExit(check(cfg) if a.command=="check" else run(cfg))
    except Exception as e:
        print(json.dumps({
            "status":"FAIL_CLOSED_P4_OVERNIGHT_REPRICING_ACCEPTANCE_V0_1",
            "error":f"{type(e).__name__}: {e}",
        },ensure_ascii=False,indent=2))
        raise SystemExit(3)

if __name__=="__main__":
    main()
