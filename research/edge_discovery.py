from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

FEATURES = (
    "ret_5", "ret_20", "ret_60", "ret_120",
    "momentum_accel", "near_high_60", "near_high_120",
    "volume_ratio_20", "rv_20", "rv_ratio_20_60",
    "ma20_distance", "ma60_distance",
)
HORIZONS = (5, 10, 20, 60)

@dataclass(frozen=True)
class DiscoveryConfig:
    min_price: float = 2.0
    min_avg_amount_20: float = 50_000_000.0
    decile_cut: float = 0.10
    cost_hurdle: float = 0.002

def norm_date(v: Any) -> str:
    s = "".join(c for c in str(v).replace("-", "").replace("/", "") if c.isdigit())
    if len(s) < 8:
        raise ValueError(v)
    return s[:8]

def normalize_history(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    x = df.copy()
    x.columns = [str(c).lower() for c in x.columns]
    x.index = [pd.Timestamp(v).strftime("%Y%m%d") if not str(v).isdigit() else norm_date(v) for v in x.index]
    x = x[~x.index.duplicated(keep="last")].sort_index()
    for c in ("open","high","low","close","volume","amount"):
        if c in x.columns:
            x[c] = pd.to_numeric(x[c], errors="coerce")
    return x

def expand_membership_intervals(intervals: pd.DataFrame, market_dates: Sequence[str]) -> pd.DataFrame:
    need = {"symbol","valid_from","valid_to_exclusive"}
    if not need.issubset(intervals.columns):
        raise ValueError(f"missing interval columns: {sorted(need-set(intervals.columns))}")
    dates = np.asarray([norm_date(d) for d in market_dates], dtype=object)
    rows = []
    for r in intervals.itertuples(index=False):
        start = norm_date(r.valid_from)
        end = norm_date(r.valid_to_exclusive) if pd.notna(r.valid_to_exclusive) and str(r.valid_to_exclusive).strip() else None
        mask = dates >= start
        if end:
            mask &= dates < end
        rows.extend((d, str(r.symbol)) for d in dates[mask])
    out = pd.DataFrame(rows, columns=["date","symbol"])
    if out.duplicated(["date","symbol"]).any():
        raise ValueError("duplicate PIT membership")
    return out

def symbol_frame(symbol: str, history: pd.DataFrame, market_dates: Sequence[str]) -> pd.DataFrame:
    x = normalize_history(history).reindex([norm_date(d) for d in market_dates])
    c = x["close"]
    r1 = c.pct_change(fill_method=None)
    o = pd.DataFrame({"date": x.index, "symbol": symbol, "close": c.to_numpy()})
    for n in (5,20,60,120):
        o[f"ret_{n}"] = (c / c.shift(n) - 1).to_numpy()
    o["momentum_accel"] = o["ret_20"] - o["ret_60"]/3.0
    o["near_high_60"] = (c/c.rolling(60, min_periods=60).max()).to_numpy()
    o["near_high_120"] = (c/c.rolling(120, min_periods=120).max()).to_numpy()
    if "volume" in x.columns:
        base = x["volume"].shift(1).rolling(20, min_periods=20).mean()
        o["volume_ratio_20"] = (x["volume"]/base).to_numpy()
    else:
        o["volume_ratio_20"] = np.nan
    rv20 = r1.rolling(20, min_periods=20).std(ddof=1)*math.sqrt(252)
    rv60 = r1.rolling(60, min_periods=60).std(ddof=1)*math.sqrt(252)
    o["rv_20"] = rv20.to_numpy()
    o["rv_ratio_20_60"] = (rv20/rv60).to_numpy()
    ma20 = c.rolling(20, min_periods=20).mean()
    ma60 = c.rolling(60, min_periods=60).mean()
    o["ma20_distance"] = (c/ma20-1).to_numpy()
    o["ma60_distance"] = (c/ma60-1).to_numpy()
    o["above_ma20"] = (c>ma20).to_numpy()
    o["above_ma60"] = (c>ma60).to_numpy()
    if "amount" in x.columns:
        o["avg_amount_20"] = x["amount"].rolling(20, min_periods=20).mean().to_numpy()
    else:
        o["avg_amount_20"] = np.nan

    entry = x["open"].shift(-1)
    o["fwd_1"] = (x["close"].shift(-1)/entry-1).to_numpy()
    for h in HORIZONS:
        o[f"fwd_{h}"] = (x["close"].shift(-h)/entry-1).to_numpy()
    return o

def benchmark_frame(history: pd.DataFrame, market_dates: Sequence[str]) -> pd.DataFrame:
    x = normalize_history(history).reindex([norm_date(d) for d in market_dates])
    entry = x["open"].shift(-1)
    o = pd.DataFrame({"date": x.index})
    o["bench_fwd_1"] = (x["close"].shift(-1)/entry-1).to_numpy()
    for h in HORIZONS:
        o[f"bench_fwd_{h}"] = (x["close"].shift(-h)/entry-1).to_numpy()
    o["benchmark_close"] = x["close"].to_numpy()
    o["benchmark_ma20"] = x["close"].rolling(20).mean().to_numpy()
    o["benchmark_ma60"] = x["close"].rolling(60).mean().to_numpy()
    return o

def build_panel(histories: Mapping[str,pd.DataFrame], membership: pd.DataFrame,
                market_dates: Sequence[str], benchmark_history: pd.DataFrame,
                cfg: DiscoveryConfig) -> pd.DataFrame:
    frames = [symbol_frame(s, df, market_dates) for s,df in histories.items() if df is not None and not df.empty]
    panel = pd.concat(frames, ignore_index=True).merge(membership, on=["date","symbol"], how="inner")
    panel = panel.merge(benchmark_frame(benchmark_history, market_dates), on="date", how="left")
    panel["candidate"] = panel["close"].notna() & (panel["close"] >= cfg.min_price)
    if panel["avg_amount_20"].notna().any():
        panel["candidate"] &= panel["avg_amount_20"] >= cfg.min_avg_amount_20
    for h in (1,)+HORIZONS:
        panel[f"excess_{h}"] = panel[f"fwd_{h}"] - panel[f"bench_fwd_{h}"]
    for f in FEATURES:
        panel[f"rank_{f}"] = np.nan
        m = panel["candidate"] & panel[f].notna()
        panel.loc[m, f"rank_{f}"] = panel.loc[m].groupby("date")[f].rank(pct=True)
    return panel

def edge_studies(panel: pd.DataFrame, cfg: DiscoveryConfig):
    out=[]
    for f in FEATURES:
        rank=f"rank_{f}"
        for h in HORIZONS:
            col=f"excess_{h}"
            x=panel.loc[panel["candidate"] & panel[rank].notna() & panel[col].notna(), ["date",rank,col]].copy()
            top=x.loc[x[rank]>=1-cfg.decile_cut,col]
            bot=x.loc[x[rank]<=cfg.decile_cut,col]
            if len(top)<30 or len(bot)<30:
                continue
            spread=float(top.mean()-bot.mean())
            yearly=[]
            for y,g in x.assign(year=x["date"].str[:4]).groupby("year"):
                t=g.loc[g[rank]>=1-cfg.decile_cut,col]; b=g.loc[g[rank]<=cfg.decile_cut,col]
                if len(t)>=20 and len(b)>=20:
                    yearly.append(float(t.mean()-b.mean()))
            same=(sum(v*spread>0 for v in yearly)/len(yearly)) if yearly else None
            out.append({
                "feature":f,"horizon":h,"n_top":len(top),"n_bottom":len(bot),
                "top_avg_excess":float(top.mean()),"bottom_avg_excess":float(bot.mean()),
                "spread_top_minus_bottom":spread,
                "top_median_excess":float(top.median()),
                "bottom_median_excess":float(bot.median()),
                "same_sign_year_fraction":same,"years_tested":len(yearly),
                "direction":"HIGH_REWARDED" if spread>0 else "LOW_REWARDED",
                "top_clears_20bp_hurdle":bool(float(top.mean())>cfg.cost_hurdle),
            })
    return out

def recent_factor_reward(panel: pd.DataFrame, cfg: DiscoveryConfig, windows=(20,60,120)):
    out=[]
    for f in FEATURES:
        rank=f"rank_{f}"
        rows=[]
        for d,g in panel.loc[panel["candidate"] & panel[rank].notna() & panel["fwd_1"].notna(),
                             ["date",rank,"fwd_1"]].groupby("date"):
            t=g.loc[g[rank]>=1-cfg.decile_cut,"fwd_1"]; b=g.loc[g[rank]<=cfg.decile_cut,"fwd_1"]
            if len(t)>=3 and len(b)>=3:
                rows.append((d,float(t.mean()-b.mean())))
        if not rows:
            continue
        ts=pd.DataFrame(rows,columns=["date","spread"]).sort_values("date")
        row={"feature":f,"latest_spread_date":str(ts.iloc[-1]["date"])}
        for w in windows:
            z=ts.tail(w)["spread"].to_numpy(float)
            if len(z)<min(10,w):
                row[f"w{w}"]=None
            else:
                cum=float(np.prod(1+z)-1)
                row[f"w{w}"]={"sessions":len(z),"mean_daily_spread":float(z.mean()),
                              "cumulative_spread":cum,
                              "positive_day_rate":float((z>0).mean()),
                              "reward_direction":"HIGH_FEATURE" if cum>0 else "LOW_FEATURE"}
        out.append(row)
    return out

def market_state(latest: pd.DataFrame) -> dict:
    x=latest.loc[latest["candidate"]]
    b20=float(x["above_ma20"].mean()) if len(x) else None
    b60=float(x["above_ma60"].mean()) if len(x) else None
    disp=float(x["ret_20"].std(ddof=1)) if len(x)>1 else None
    r=latest.iloc[0]
    close=r.get("benchmark_close"); ma60=r.get("benchmark_ma60")
    state="UNKNOWN"
    if pd.notna(close) and pd.notna(ma60) and b20 is not None and b60 is not None:
        if close>ma60 and b20>=.60 and b60>=.55: state="UP_BROAD"
        elif close>ma60 and b20<.60: state="UP_NARROW"
        elif close<ma60 and b20<=.40: state="RISK_OFF"
        else: state="MIXED"
    return {"state":state,"breadth_above_ma20":b20,"breadth_above_ma60":b60,
            "ret20_dispersion":disp,"candidate_count":int(len(x)),
            "benchmark_close":float(close) if pd.notna(close) else None,
            "benchmark_ma60":float(ma60) if pd.notna(ma60) else None}
