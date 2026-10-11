
from __future__ import annotations
import argparse, hashlib, json, math
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT=Path(__file__).resolve().parents[1]
CFG=ROOT/"config/opportunity/p2_trend_pullback_reclaim_v0_1.yaml"

def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))

def sha256_file(path):
    h=hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""):
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
    x=x[x["symbol"].map(is_target_symbol)].drop_duplicates(["trade_date","symbol"]).copy()
    return x

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
        con.register("p2_symbols",syms)
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
        INNER JOIN p2_symbols s ON b.symbol=s.symbol
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
    # Each symbol path is valid only after first valid point; later invalid points break continuity.
    if factor.isna().any():
        # Keep strict semantics: invalid factor makes that row and downstream rolling values invalid
        bad=factor.isna().cummax()
        cum=factor.fillna(1.0).cumprod().where(~bad)
    else:
        cum=factor.cumprod()
    z["adj_close"]=cum
    z["adj_open"]=cum*z["open"]/z["close"]
    z["adj_high"]=cum*z["high"]/z["close"]
    z["adj_low"]=cum*z["low"]/z["close"]
    return z

def build_features(bars):
    parts=[]
    for _,g in bars.groupby("symbol",sort=False):
        z=synthetic_adjust(g)
        c=z["adj_close"]
        z["ma20"]=c.rolling(20,min_periods=20).mean()
        z["ma60"]=c.rolling(60,min_periods=60).mean()
        z["ma60_lag20"]=z["ma60"].shift(20)
        above=(c>=z["ma20"])
        z["prior5_all_above_ma20"]=(
            above.shift(1).rolling(5,min_periods=5).sum().eq(5)
        )
        parts.append(z)
    return pd.concat(parts,ignore_index=True)

def build_setups(features,eligible_keys):
    e=eligible_keys.copy()
    e["_eligible"]=True
    x=features.merge(e,on=["trade_date","symbol"],how="left")
    x["_eligible"]=x["_eligible"].fillna(False)

    setup=(
        x["_eligible"]
        & x["ma20"].notna()
        & x["ma60"].notna()
        & x["ma60_lag20"].notna()
        & x["ma20"].gt(x["ma60"])
        & x["ma60"].gt(x["ma60_lag20"])
        & x["prior5_all_above_ma20"].fillna(False)
        & x["adj_close"].lt(x["ma20"])
        & x["adj_close"].gt(x["ma60"])
    )
    x["p2_setup"]=setup

    rows=[]
    for sym,g in x.groupby("symbol",sort=False):
        g=g.reset_index(drop=True)
        idxs=np.flatnonzero(g["p2_setup"].to_numpy())
        for i in idxs:
            if i+2>=len(g):
                continue
            # S setup; T=i+1 decision; entry=i+2 open.
            t=g.iloc[i+1]
            ent=g.iloc[i+2]
            if not bool(t["_eligible"]):
                continue
            signal=bool(
                math.isfinite(safe_float(t["adj_close"]))
                and math.isfinite(safe_float(t["ma20"]))
                and safe_float(t["adj_close"])>safe_float(t["ma20"])
            )
            rows.append({
                "symbol":sym,
                "setup_date":g.iloc[i]["trade_date"],
                "decision_date":t["trade_date"],
                "entry_date":ent["trade_date"],
                "signal_reclaim":signal,
                "setup_close_to_ma20":safe_float(g.iloc[i]["adj_close"]/g.iloc[i]["ma20"]-1.0),
                "setup_close_to_ma60":safe_float(g.iloc[i]["adj_close"]/g.iloc[i]["ma60"]-1.0),
                "decision_close_to_ma20":safe_float(t["adj_close"]/t["ma20"]-1.0),
                "entry_adj_open":safe_float(ent["adj_open"]),
                "_setup_index":int(i),
            })
    return pd.DataFrame(rows),x

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

def evaluate_events(events,features,cfg):
    groups={s:g.reset_index(drop=True) for s,g in features.groupby("symbol",sort=False)}
    maps={s:{d:i for i,d in enumerate(g["trade_date"])} for s,g in groups.items()}
    out=[]
    for _,e in events.iterrows():
        sym=e["symbol"];g=groups[sym]
        entry_idx=maps[sym].get(pd.Timestamp(e["entry_date"]))
        r=e.to_dict()
        if entry_idx is None:
            r["evaluation_valid"]=False
            r["invalid_reason"]="ENTRY_DATE_NOT_FOUND"
            out.append(r);continue
        entry=safe_float(g.iloc[entry_idx]["adj_open"])
        r["entry_adj_open"]=entry
        r["evaluation_valid"]=math.isfinite(entry) and entry>0
        r["invalid_reason"]=None if r["evaluation_valid"] else "INVALID_ENTRY_OPEN"
        for lab in [cfg["labels"]["primary"],cfg["labels"]["secondary"]]:
            z=trend_label(
                g,entry_idx,entry,
                float(lab["target"]),
                int(lab["horizon_sessions"]),
                float(lab["mae_floor"])
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
    sig=x[x["signal_reclaim"].fillna(False)]
    setup=x
    sp=float(sig[lab].mean()) if len(sig) else None
    bp=float(setup[lab].mean()) if len(setup) else None
    years={}
    for y,g in x.groupby(pd.to_datetime(x["decision_date"]).dt.year):
        sg=g[g["signal_reclaim"].fillna(False)]
        if len(sg) and len(g):
            s=float(sg[lab].mean());b=float(g[lab].mean())
            years[str(int(y))]={
                "setup_n":len(g),
                "signal_n":len(sg),
                "setup_precision":b,
                "signal_precision":s,
                "lift":s/b if b>0 else None,
                "precision_minus_setup":s-b,
            }
    return {
        "matured_setup_rows":len(setup),
        "matured_signal_rows":len(sig),
        "setup_precision":bp,
        "signal_precision":sp,
        "lift_vs_setup":sp/bp if sp is not None and bp not in (None,0) else None,
        "precision_minus_setup":sp-bp if sp is not None and bp is not None else None,
        "signal_share_of_setup":len(sig)/len(setup) if len(setup) else None,
        "positive_years":sum(v["precision_minus_setup"]>0 for v in years.values()),
        "by_year":years,
    }

def check(cfg):
    ps=[
        Path(cfg["source"]["universe_panel"]),
        Path(cfg["source"]["market_catalog"]),
    ]
    panel_sha=sha256_file(ps[0]) if ps[0].exists() else None
    good=all(p.exists() for p in ps) and panel_sha==cfg["source"]["expected_universe_panel_sha256"]
    out={
        "status":"PASS_P2_TREND_PULLBACK_RECLAIM_V0_1_CHECK" if good else "BLOCKED_P2_TREND_PULLBACK_RECLAIM_V0_1_CHECK",
        "inputs":[{"path":str(p),"exists":p.exists()} for p in ps],
        "universe_panel_sha256":panel_sha,
        "sha_match":panel_sha==cfg["source"]["expected_universe_panel_sha256"],
        "mechanism":cfg["mechanism"],
        "labels":cfg["labels"],
        "gate":cfg["gate"],
        "research_domain":"SH/SZ A-shares only",
    }
    print(json.dumps(out,ensure_ascii=False,indent=2))
    return 0 if good else 3

def run(cfg):
    universe=load_universe(cfg)
    min_d=(universe["trade_date"].min().date()-timedelta(days=220)).isoformat()
    max_d=(universe["trade_date"].max().date()+timedelta(days=220)).isoformat()
    bars=load_market_bars(cfg,universe["symbol"].unique(),min_d,max_d)
    bars["trade_date"]=pd.to_datetime(bars["trade_date"])
    features=build_features(bars)
    events,feature_panel=build_setups(features,universe)
    study=evaluate_events(events,features,cfg)

    pn=cfg["labels"]["primary"]["name"]
    sn=cfg["labels"]["secondary"]["name"]
    p=metric_block(study,pn)
    s=metric_block(study,sn)
    gc=cfg["gate"]
    gate={
        "selected_min":{
            "threshold":int(gc["selected_min"]),
            "actual":p["matured_signal_rows"],
            "pass":p["matured_signal_rows"]>=int(gc["selected_min"]),
        },
        "primary_lift_vs_setup_min":{
            "threshold":float(gc["primary_lift_vs_setup_min"]),
            "actual":p["lift_vs_setup"],
            "pass":p["lift_vs_setup"] is not None and p["lift_vs_setup"]>=float(gc["primary_lift_vs_setup_min"]),
        },
        "primary_precision_minus_setup_min":{
            "threshold":float(gc["primary_precision_minus_setup_min"]),
            "actual":p["precision_minus_setup"],
            "pass":p["precision_minus_setup"] is not None and p["precision_minus_setup"]>=float(gc["primary_precision_minus_setup_min"]),
        },
        "positive_years_min":{
            "threshold":int(gc["positive_years_min"]),
            "actual":p["positive_years"],
            "pass":p["positive_years"]>=int(gc["positive_years_min"]),
        },
    }
    verdict=(
        "PASS_P2_TREND_PULLBACK_RECLAIM_TO_ROBUSTNESS"
        if all(v["pass"] for v in gate.values())
        else "FAST_REJECT_P2_TREND_PULLBACK_RECLAIM_V0_1"
    )

    outdir=Path(cfg["output_dir"])
    outdir.mkdir(parents=True,exist_ok=True)
    po=outdir/"p2_trend_pullback_reclaim_v0_1_panel.parquet"
    study.to_parquet(po,index=False)

    result={
        "status":"PASS_P2_TREND_PULLBACK_RECLAIM_V0_1_RUN",
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
            "setup_episodes":len(events),
            "evaluation_valid":int(study["evaluation_valid"].fillna(False).sum()) if len(study) else 0,
        },
        "primary":p,
        "secondary":s,
        "gate":gate,
        "overall_verdict":verdict,
        "interpretation_contract":"Primary question only: inside a pre-registered established uptrend, does a next-session MA20 reclaim after the first MA20 pullback add predictive value versus all same-timing pullback setups? No parameter scan and no post-result rescue.",
        "artifacts":{"panel":str(po),"panel_sha256":sha256_file(po)},
        "safety":cfg["safety"],
    }
    (outdir/"p2_trend_pullback_reclaim_v0_1_summary.json").write_text(
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
            "status":"FAIL_CLOSED_P2_TREND_PULLBACK_RECLAIM_V0_1",
            "error":f"{type(e).__name__}: {e}",
        },ensure_ascii=False,indent=2))
        raise SystemExit(3)

if __name__=="__main__":
    main()
