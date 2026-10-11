
from __future__ import annotations
import re
import numpy as np
import pandas as pd

def report_period_bucket(s: pd.Series) -> pd.Series:
    x=s.astype("string").fillna("").str.upper().str.strip()
    out=pd.Series("OTHER",index=s.index,dtype="string")
    # Longest suffix first.
    for bucket in ["Q1_Q3","ANNUAL","H1","Q1"]:
        out.loc[x.str.endswith("_"+bucket)]=bucket
    return out

def code6(s):
    m=re.search(r"(\d{6})",str(s or "").upper())
    return m.group(1) if m else None

def canonical_equity_symbol(s):
    code=code6(s)
    if not code:
        return None
    p2=code[:2]
    if p2 in {"43","83","87","88","92"}:
        return f"{code}.BJ"
    if p2 in {"00","30"}:
        return f"{code}.SZ"
    if p2 in {"60","68"}:
        return f"{code}.SH"
    return None

def prepare_identity_events(identity: pd.DataFrame) -> pd.DataFrame:
    x=identity.copy()
    x["event_time"]=pd.to_datetime(x["event_time"],errors="coerce")
    x["event_date"]=x["event_time"].dt.normalize()
    x["event_year"]=x["event_time"].dt.year.astype("Int64")
    x["identity_eligible"]=(
        x["identity_eligible"].astype("string").str.lower().isin(["true","1","yes","y"])
    )
    x=x[x["identity_eligible"]].copy()
    x["symbol"]=x["canonical_symbol"].astype("string").str.upper().str.strip()
    x["e1_setup"]=True
    x["e1_signal"]=x["change_type"].astype("string").str.upper().str.strip().eq("TURNAROUND")
    x["report_period_bucket"]=report_period_bucket(x["report_period"])
    return x

def prepare_shadow_market(shadow: pd.DataFrame, allowed_symbols: set[str]):
    x=shadow.copy()
    x["trade_date"]=pd.to_datetime(x["trade_date"],errors="coerce").dt.normalize()
    x["raw_symbol"]=x["symbol"].astype("string").str.upper().str.strip()
    x["symbol"]=x["raw_symbol"].map(canonical_equity_symbol)
    x=x[x["symbol"].isin(allowed_symbols)].copy()

    for c in ["open","high","low","close","pre_close"]:
        x[c]=pd.to_numeric(x[c],errors="coerce")

    # Canonicalization may collapse aliases such as a mistaken .SH BSE request into .BJ.
    # If duplicate symbol-date rows appear, accept deduplication only when price values agree.
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
    return x.reset_index(drop=True), int(conflicts)

def synthetic_adjust_ohlc(bars: pd.DataFrame) -> pd.DataFrame:
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

def attach_entry(events: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    x=events.copy()
    x["entry_date"]=pd.NaT
    x["entry_adj_open"]=np.nan
    x["_entry_pos"]=pd.Series(pd.NA,index=x.index,dtype="Int64")

    bg={str(s):g.reset_index(drop=True) for s,g in bars.groupby("symbol",sort=False)}
    for idx,row in x.iterrows():
        g=bg.get(str(row["symbol"]))
        if g is None or g.empty or pd.isna(row["event_date"]):
            continue
        dates=g["trade_date"].to_numpy(dtype="datetime64[ns]")
        pos=int(np.searchsorted(dates,np.datetime64(row["event_date"]),side="right"))
        if pos>=len(g):
            continue
        op=g.loc[pos,"adj_open"]
        if pd.isna(op) or float(op)<=0:
            continue
        x.at[idx,"entry_date"]=g.loc[pos,"trade_date"]
        x.at[idx,"entry_adj_open"]=float(op)
        x.at[idx,"_entry_pos"]=pos
    return x

def matured_label(g, entry_pos, entry_price, target, horizon, mae_floor):
    if entry_pos<0 or entry_price<=0:
        return pd.NA
    end=entry_pos+horizon
    if end>len(g):
        return pd.NA
    path=g.iloc[entry_pos:end]
    if len(path)!=horizon:
        return pd.NA

    highs=pd.to_numeric(path["adj_high"],errors="coerce")/entry_price-1.0
    lows=pd.to_numeric(path["adj_low"],errors="coerce")/entry_price-1.0
    hits=np.flatnonzero((highs>=target).fillna(False).to_numpy())
    for h in hits:
        pre=lows.iloc[:int(h)+1]
        mae=float(pre.min()) if pre.notna().any() else np.nan
        if np.isfinite(mae) and mae>=mae_floor:
            return 1
    return 0

def add_matured_labels(events,bars,primary,secondary):
    x=events.copy()
    bg={str(s):g.reset_index(drop=True) for s,g in bars.groupby("symbol",sort=False)}
    pvals=[]; svals=[]
    for _,row in x.iterrows():
        if pd.isna(row.get("_entry_pos")):
            pvals.append(pd.NA); svals.append(pd.NA); continue
        g=bg.get(str(row["symbol"]))
        if g is None:
            pvals.append(pd.NA); svals.append(pd.NA); continue
        pos=int(row["_entry_pos"]); price=float(row["entry_adj_open"])
        pvals.append(matured_label(
            g,pos,price,float(primary["target"]),int(primary["horizon_sessions"]),float(primary["mae_floor"])
        ))
        svals.append(matured_label(
            g,pos,price,float(secondary["target"]),int(secondary["horizon_sessions"]),float(secondary["mae_floor"])
        ))
    x["E1_TREND25_60_label"]=pd.array(pvals,dtype="Int64")
    x["E1_TREND40_120_label"]=pd.array(svals,dtype="Int64")
    return x

def matched_rate(setup,signal,label_col,min_setup):
    ss=(setup.groupby(["event_year","report_period_bucket"],dropna=False)
        .agg(setup_count=(label_col,"size"),setup_rate=(label_col,"mean")).reset_index())
    gs=(signal.groupby(["event_year","report_period_bucket"],dropna=False)
        .agg(signal_count=(label_col,"size"),signal_rate=(label_col,"mean")).reset_index())
    z=gs.merge(ss,on=["event_year","report_period_bucket"],how="left",validate="many_to_one")
    z=z[z["setup_count"]>=int(min_setup)].copy()
    if z.empty or int(z["signal_count"].sum())==0:
        return None,0,z
    rate=float((z["signal_count"]*z["setup_rate"]).sum()/z["signal_count"].sum())
    return rate,int(z["signal_count"].sum()),z

def evaluate(panel,label_col,gates,min_setup):
    y=pd.to_numeric(panel[label_col],errors="coerce")
    valid=panel[y.notna()].copy()
    valid["_label"]=y[y.notna()].astype(int).to_numpy()
    setup=valid[valid["e1_setup"]].copy()
    signal=valid[valid["e1_signal"]].copy()

    base,n,strata=matched_rate(setup,signal,"_label",min_setup)
    if base is not None and n:
        keys=strata[["event_year","report_period_bucket"]].drop_duplicates()
        sm=signal.merge(keys,on=["event_year","report_period_bucket"],how="inner")
        sr=float(sm["_label"].mean()) if len(sm) else None
    else:
        sm=signal.iloc[:0]; sr=None

    lift=sr/base if sr is not None and base is not None and base>0 else None
    delta=sr-base if sr is not None and base is not None else None

    by_year={}
    pos_years=0
    for year in sorted(int(v) for v in setup["event_year"].dropna().unique()):
        sy=setup[setup["event_year"]==year]
        gy=signal[signal["event_year"]==year]
        br,bn,st=matched_rate(sy,gy,"_label",min_setup)
        if br is None or bn==0:
            continue
        keys=st[["event_year","report_period_bucket"]].drop_duplicates()
        gym=gy.merge(keys,on=["event_year","report_period_bucket"],how="inner")
        rr=float(gym["_label"].mean()) if len(gym) else None
        yl=rr/br if rr is not None and br>0 else None
        yd=rr-br if rr is not None else None
        if yl is not None and yd is not None and yl>1 and yd>0:
            pos_years+=1
        by_year[str(year)]={
            "setup_valid":int(len(sy)),
            "signal_valid":int(len(gy)),
            "matched_signal_count":int(bn),
            "signal_rate":rr,
            "matched_setup_rate":br,
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
        "valid_label_rows":int(len(valid)),
        "setup_valid":int(len(setup)),
        "signal_valid":int(len(signal)),
        "matched_signal_count":int(n),
        "signal_rate_matched":sr,
        "matched_setup_rate":base,
        "lift_vs_matched_setup":lift,
        "precision_minus_matched_setup":delta,
        "positive_years":int(pos_years),
        "by_year":by_year,
        "gate":gate,
        "verdict":"PASS_E1_FORECAST_TURNAROUND_V0_2_TO_ROBUSTNESS" if passed else "FAST_REJECT_E1_FORECAST_TURNAROUND_V0_2",
    }
