
from __future__ import annotations
import re
import numpy as np
import pandas as pd

def norm_bool(s):
    return s.astype("string").str.strip().str.lower().isin(["true","1","yes","y","eligible"])

def code6(s):
    m=re.search(r"(\d{6})",str(s or "").upper())
    return m.group(1) if m else None

def canonical_equity_symbol(s):
    code=code6(s)
    if not code:
        return None
    p2=code[:2]
    if p2 in {"20","90"}:
        return None
    if p2 in {"43","83","87","88","92"}:
        return f"{code}.BJ"
    if p2 in {"00","30"}:
        return f"{code}.SZ"
    if p2 in {"60","68"}:
        return f"{code}.SH"
    return None

def report_period_bucket(s):
    x=s.astype("string").fillna("").str.upper().str.strip()
    out=pd.Series("OTHER",index=s.index,dtype="string")
    for b in ["Q1_Q3","ANNUAL","H1","Q1"]:
        out.loc[x.str.endswith("_"+b)]=b
    return out

def prepare_events(events,cfg):
    x=events.copy()
    x["event_time"]=pd.to_datetime(x["event_time"],errors="coerce")
    x["event_date"]=x["event_time"].dt.normalize()
    x["event_year"]=x["event_time"].dt.year.astype("Int64")
    x["event_kind"]=x["event_kind"].astype("string").str.upper().str.strip()
    x["change_type"]=x["change_type"].astype("string").str.upper().str.strip()
    x["canonical_symbol"]=x["symbol"].map(canonical_equity_symbol)
    x["report_period_bucket"]=report_period_bucket(x["report_period"])

    pos=set(cfg["mechanism"]["positive_change_types"])
    x=x[
        norm_bool(x["strategy_eligible_semantics"])
        & x["event_kind"].eq(cfg["mechanism"]["event_kind"])
        & x["change_type"].isin(pos)
        & x["canonical_symbol"].notna()
        & x["event_time"].notna()
    ].copy()
    x["e5_setup"]=True
    return x

def prepare_market(shadow,allowed):
    x=shadow.copy()
    x["trade_date"]=pd.to_datetime(x["trade_date"],errors="coerce").dt.normalize()
    x["raw_symbol"]=x["symbol"].astype("string").str.upper().str.strip()
    x["symbol"]=x["raw_symbol"].map(canonical_equity_symbol)
    x=x[x["symbol"].isin(allowed)].copy()
    for c in ["open","high","low","close","pre_close"]:
        x[c]=pd.to_numeric(x[c],errors="coerce")

    key=["symbol","trade_date"]
    dup=x.duplicated(key,keep=False)
    conflicts=0
    if dup.any():
        for _,g in x[dup].groupby(key,dropna=False):
            for c in ["open","high","low","close","pre_close"]:
                vals=pd.to_numeric(g[c],errors="coerce").dropna().to_numpy()
                if len(vals)>1 and np.nanmax(vals)-np.nanmin(vals)>0.011:
                    conflicts+=1
                    break
        if conflicts==0:
            x=x.sort_values(key+["raw_symbol"]).drop_duplicates(key,keep="first")
    return x.reset_index(drop=True),int(conflicts)

def synthetic_adjust_ohlc(bars):
    x=bars.copy().sort_values(["symbol","trade_date"]).reset_index(drop=True)
    close=pd.to_numeric(x["close"],errors="coerce")
    pre=pd.to_numeric(x["pre_close"],errors="coerce")
    factor=close/pre
    valid=close.gt(0)&pre.gt(0)&factor.gt(0)&np.isfinite(factor)
    x["_factor_valid"]=valid
    x["_factor"]=factor.where(valid,1.0)
    x["adj_close"]=x.groupby("symbol")["_factor"].cumprod()
    cs=close.where(close.gt(0))
    x["adj_open"]=x["adj_close"]*pd.to_numeric(x["open"],errors="coerce")/cs
    x["adj_high"]=x["adj_close"]*pd.to_numeric(x["high"],errors="coerce")/cs
    x["adj_low"]=x["adj_close"]*pd.to_numeric(x["low"],errors="coerce")/cs
    return x

def attach_reaction_and_entry(events,bars):
    x=events.copy()
    x["reaction_date"]=pd.NaT
    x["reaction_close"]=np.nan
    x["reaction_pre_close"]=np.nan
    x["entry_date"]=pd.NaT
    x["entry_adj_open"]=np.nan
    x["_entry_pos"]=pd.Series(pd.NA,index=x.index,dtype="Int64")
    x["e5_signal"]=False

    bg={str(s):g.reset_index(drop=True) for s,g in bars.groupby("symbol",sort=False)}

    for idx,row in x.iterrows():
        g=bg.get(str(row["canonical_symbol"]))
        if g is None or g.empty:
            continue
        dates=g["trade_date"].to_numpy(dtype="datetime64[ns]")
        reaction_pos=int(np.searchsorted(dates,np.datetime64(row["event_date"]),side="right"))
        entry_pos=reaction_pos+1
        if reaction_pos>=len(g) or entry_pos>=len(g):
            continue

        rc=g.loc[reaction_pos,"close"]
        rpc=g.loc[reaction_pos,"pre_close"]
        eo=g.loc[entry_pos,"adj_open"]

        if pd.isna(rc) or pd.isna(rpc) or pd.isna(eo):
            continue
        if float(rc)<=0 or float(rpc)<=0 or float(eo)<=0:
            continue

        x.at[idx,"reaction_date"]=g.loc[reaction_pos,"trade_date"]
        x.at[idx,"reaction_close"]=float(rc)
        x.at[idx,"reaction_pre_close"]=float(rpc)
        x.at[idx,"entry_date"]=g.loc[entry_pos,"trade_date"]
        x.at[idx,"entry_adj_open"]=float(eo)
        x.at[idx,"_entry_pos"]=entry_pos
        x.at[idx,"e5_signal"]=bool(float(rc)>=float(rpc))
    return x

def matured_label(g,pos,price,target,horizon,mae_floor):
    if pos<0 or price<=0:
        return pd.NA
    end=pos+horizon
    if end>len(g):
        return pd.NA
    path=g.iloc[pos:end]
    if len(path)!=horizon:
        return pd.NA

    highs=pd.to_numeric(path["adj_high"],errors="coerce")/price-1
    lows=pd.to_numeric(path["adj_low"],errors="coerce")/price-1
    hits=np.flatnonzero((highs>=target).fillna(False).to_numpy())
    for h in hits:
        pre=lows.iloc[:int(h)+1]
        mae=float(pre.min()) if pre.notna().any() else np.nan
        if np.isfinite(mae) and mae>=mae_floor:
            return 1
    return 0

def add_labels(events,bars,p,s):
    x=events.copy()
    bg={str(sym):g.reset_index(drop=True) for sym,g in bars.groupby("symbol",sort=False)}
    pv=[]; sv=[]
    for _,row in x.iterrows():
        if pd.isna(row["_entry_pos"]):
            pv.append(pd.NA); sv.append(pd.NA); continue
        g=bg.get(str(row["canonical_symbol"]))
        if g is None:
            pv.append(pd.NA); sv.append(pd.NA); continue
        pos=int(row["_entry_pos"])
        price=float(row["entry_adj_open"])
        pv.append(matured_label(g,pos,price,float(p["target"]),int(p["horizon_sessions"]),float(p["mae_floor"])))
        sv.append(matured_label(g,pos,price,float(s["target"]),int(s["horizon_sessions"]),float(s["mae_floor"])))
    x["E5_TREND25_60_label"]=pd.array(pv,dtype="Int64")
    x["E5_TREND40_120_label"]=pd.array(sv,dtype="Int64")
    return x

def matched_rate(setup,signal,label_col,min_setup):
    keys=["event_year","report_period_bucket","change_type"]
    bs=(setup.groupby(keys,dropna=False)
        .agg(setup_count=(label_col,"size"),setup_rate=(label_col,"mean")).reset_index())
    ss=(signal.groupby(keys,dropna=False)
        .agg(signal_count=(label_col,"size"),signal_rate=(label_col,"mean")).reset_index())
    z=ss.merge(bs,on=keys,how="left",validate="many_to_one")
    z=z[z["setup_count"]>=int(min_setup)].copy()
    if z.empty or int(z["signal_count"].sum())==0:
        return None,0,z
    rate=float((z["signal_count"]*z["setup_rate"]).sum()/z["signal_count"].sum())
    return rate,int(z["signal_count"].sum()),z

def evaluate(panel,label_col,gates,min_setup):
    y=pd.to_numeric(panel[label_col],errors="coerce")
    valid=panel[y.notna()].copy()
    valid["_label"]=y[y.notna()].astype(int).to_numpy()

    setup=valid[valid["e5_setup"]].copy()
    signal=valid[valid["e5_signal"]].copy()

    br,n,strata=matched_rate(setup,signal,"_label",min_setup)
    keys=["event_year","report_period_bucket","change_type"]
    if br is not None and n:
        keep=strata[keys].drop_duplicates()
        sig=signal.merge(keep,on=keys,how="inner")
        sr=float(sig["_label"].mean()) if len(sig) else None
    else:
        sig=signal.iloc[:0]
        sr=None

    lift=sr/br if sr is not None and br is not None and br>0 else None
    delta=sr-br if sr is not None and br is not None else None

    by_year={}
    pos_years=0
    for year in sorted(int(v) for v in setup["event_year"].dropna().unique()):
        by=setup[setup["event_year"]==year]
        sy=signal[signal["event_year"]==year]
        ybr,yn,yst=matched_rate(by,sy,"_label",min_setup)
        if ybr is None or yn==0:
            continue
        keep=yst[keys].drop_duplicates()
        sm=sy.merge(keep,on=keys,how="inner")
        rr=float(sm["_label"].mean()) if len(sm) else None
        yl=rr/ybr if rr is not None and ybr>0 else None
        yd=rr-ybr if rr is not None else None
        if yl is not None and yd is not None and yl>1 and yd>0:
            pos_years+=1
        by_year[str(year)]={
            "setup_valid":int(len(by)),
            "signal_valid":int(len(sy)),
            "matched_signal_count":int(yn),
            "signal_rate":rr,
            "matched_setup_rate":ybr,
            "lift_vs_matched_setup":yl,
            "precision_minus_matched_setup":yd,
        }

    gate={
        "selected_min":{
            "threshold":int(gates["selected_min"]),
            "actual":int(n),
            "pass":n>=int(gates["selected_min"]),
        },
        "lift_vs_matched_setup_min":{
            "threshold":float(gates["primary_lift_vs_matched_setup_min"]),
            "actual":lift,
            "pass":lift is not None and lift>=float(gates["primary_lift_vs_matched_setup_min"]),
        },
        "precision_minus_matched_setup_min":{
            "threshold":float(gates["primary_precision_minus_matched_setup_min"]),
            "actual":delta,
            "pass":delta is not None and delta>=float(gates["primary_precision_minus_matched_setup_min"]),
        },
        "positive_years_min":{
            "threshold":int(gates["positive_years_min"]),
            "actual":int(pos_years),
            "pass":pos_years>=int(gates["positive_years_min"]),
        },
    }
    passed=all(v["pass"] for v in gate.values())

    return {
        "evaluation_population":"MATURED_ONLY",
        "setup_valid":int(len(setup)),
        "signal_valid":int(len(signal)),
        "matched_signal_count":int(n),
        "signal_rate_matched":sr,
        "matched_setup_rate":br,
        "lift_vs_matched_setup":lift,
        "precision_minus_matched_setup":delta,
        "positive_years":int(pos_years),
        "by_year":by_year,
        "gate":gate,
        "verdict":"PASS_E5_POSITIVE_NEWS_CONFIRMATION_V0_1_TO_ROBUSTNESS" if passed else "FAST_REJECT_E5_POSITIVE_NEWS_CONFIRMATION_V0_1",
    }
