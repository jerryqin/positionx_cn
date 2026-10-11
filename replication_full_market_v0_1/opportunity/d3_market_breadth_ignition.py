
from __future__ import annotations
import numpy as np
import pandas as pd

def synthetic_adjust_close(g: pd.DataFrame) -> pd.DataFrame:
    x = g.sort_values("trade_date").copy()
    for c in ("open","high","low","close","pre_close"):
        x[c] = pd.to_numeric(x[c], errors="coerce")
    valid = np.logical_and.reduce([(x[c] > 0) & x[c].notna() for c in ("open","high","low","close","pre_close")])
    x = x.loc[valid].copy()
    if x.empty:
        return x
    factor = x["close"] / x["pre_close"]
    adj_close = factor.cumprod()
    raw_close = x["close"].copy()
    for c in ("open","low"):
        x[f"adj_{c}"] = adj_close * x[c] / raw_close
    x["adj_close"] = adj_close
    x["ret20"] = x["adj_close"] / x["adj_close"].shift(20) - 1.0
    x["ma20"] = x["adj_close"].rolling(20, min_periods=20).mean()
    x["above_ma20"] = x["adj_close"] > x["ma20"]
    return x

def add_forward_labels_from_t1(
    g: pd.DataFrame,
    primary_horizon: int = 60, primary_target: float = 0.25, primary_mae_floor: float = -0.10,
    secondary_horizon: int = 120, secondary_target: float = 0.40, secondary_mae_floor: float = -0.12,
) -> pd.DataFrame:
    x = g.sort_values("trade_date").reset_index(drop=True).copy()
    n = len(x)
    for prefix in ("D3_TREND25_60","D3_TREND40_120"):
        x[f"{prefix}_label"] = np.nan
        x[f"{prefix}_mfe"] = np.nan
        x[f"{prefix}_mae_before_hit"] = np.nan
        x[f"{prefix}_time_to_target"] = np.nan
    close = x["adj_close"].to_numpy(float)
    low = x["adj_low"].to_numpy(float)
    open_ = x["adj_open"].to_numpy(float)

    def one(i, horizon, target, mae_floor):
        entry_idx = i + 1
        end_idx = entry_idx + horizon - 1
        if entry_idx >= n or end_idx >= n:
            return None
        entry = open_[entry_idx]
        if not np.isfinite(entry) or entry <= 0:
            return None
        f_close = close[entry_idx:end_idx+1]
        f_low = low[entry_idx:end_idx+1]
        if len(f_close) != horizon or not np.isfinite(f_close).all() or not np.isfinite(f_low).all():
            return None
        ret = f_close / entry - 1.0
        mfe = float(np.max(ret))
        hit = np.where(ret >= target)[0]
        if len(hit):
            h = int(hit[0])
            mae = float(np.min(f_low[:h+1] / entry - 1.0))
            ok = mae >= mae_floor
            ttt = h + 1 if ok else np.nan
        else:
            mae = float(np.min(f_low / entry - 1.0))
            ok = False
            ttt = np.nan
        return int(ok), mfe, mae, ttt

    for i in range(n):
        p = one(i, primary_horizon, primary_target, primary_mae_floor)
        if p is not None:
            x.loc[i, ["D3_TREND25_60_label","D3_TREND25_60_mfe","D3_TREND25_60_mae_before_hit","D3_TREND25_60_time_to_target"]] = p
        s = one(i, secondary_horizon, secondary_target, secondary_mae_floor)
        if s is not None:
            x.loc[i, ["D3_TREND40_120_label","D3_TREND40_120_mfe","D3_TREND40_120_mae_before_hit","D3_TREND40_120_time_to_target"]] = s
    return x

def add_breadth_and_signal(
    panel: pd.DataFrame,
    leader_rank_min: float = 0.75,
    breadth_delta_sessions: int = 5,
    breadth_history_sessions: int = 126,
    breadth_ignition_quantile: float = 0.75,
) -> pd.DataFrame:
    x = panel.copy()
    x["leader_rank20"] = x.groupby("trade_date")["ret20"].rank(method="average", pct=True)
    x["leader_setup"] = x["leader_rank20"] >= float(leader_rank_min)

    daily = (
        x.loc[x["above_ma20"].notna()]
        .groupby("trade_date", as_index=False)
        .agg(breadth20=("above_ma20","mean"), breadth_eligible=("above_ma20","size"))
        .sort_values("trade_date")
    )
    daily["breadth_delta5"] = daily["breadth20"] - daily["breadth20"].shift(int(breadth_delta_sessions))
    hist_q = (
        daily["breadth_delta5"].shift(1)
        .rolling(int(breadth_history_sessions), min_periods=int(breadth_history_sessions))
        .quantile(float(breadth_ignition_quantile))
    )
    daily["breadth_delta5_prior_q75"] = hist_q
    daily["breadth_ignition"] = (
        daily["breadth_delta5"].notna() & hist_q.notna() & (daily["breadth_delta5"] > hist_q)
    )
    x = x.merge(
        daily[["trade_date","breadth20","breadth_eligible","breadth_delta5","breadth_delta5_prior_q75","breadth_ignition"]],
        on="trade_date", how="left", validate="many_to_one"
    )
    x["d3_signal"] = x["leader_setup"] & x["breadth_ignition"].fillna(False)
    return x

def _by_year(df: pd.DataFrame, label_col: str) -> dict:
    y = pd.to_numeric(df[label_col], errors="coerce")
    valid = df[y.notna()].copy()
    valid["_label"] = y[y.notna()].astype(int).to_numpy()
    valid["year"] = valid["trade_date"].astype(str).str[:4]
    out = {}
    for year, g in valid.groupby("year"):
        setup = g[g["leader_setup"]]
        sig = g[g["d3_signal"]]
        if len(setup) == 0:
            continue
        sr = float(setup["_label"].mean())
        rr = float(sig["_label"].mean()) if len(sig) else None
        lift = rr / sr if rr is not None and sr > 0 else None
        delta = rr - sr if rr is not None else None
        out[str(year)] = {
            "valid_rows": int(len(g)),
            "leader_setup_count": int(len(setup)),
            "breadth_signal_count": int(len(sig)),
            "leader_setup_precision": sr,
            "breadth_signal_precision": rr,
            "lift_vs_leader_setup": lift,
            "precision_minus_leader_setup": delta,
            "signal_days": int(sig["trade_date"].nunique()) if len(sig) else 0,
        }
    return out

def evaluate_d3(
    panel: pd.DataFrame,
    label_col: str,
    selected_min: int = 200,
    lift_min: float = 1.25,
    precision_delta_min: float = 0.03,
    positive_years_min: int = 5,
) -> dict:
    y = pd.to_numeric(panel[label_col], errors="coerce")
    valid = panel[y.notna()].copy()
    valid["_label"] = y[y.notna()].astype(int).to_numpy()
    setup = valid[valid["leader_setup"]].copy()
    sig = valid[valid["d3_signal"]].copy()
    market_rate = float(valid["_label"].mean()) if len(valid) else None
    setup_rate = float(setup["_label"].mean()) if len(setup) else None
    sig_rate = float(sig["_label"].mean()) if len(sig) else None
    lift = sig_rate / setup_rate if sig_rate is not None and setup_rate and setup_rate > 0 else None
    delta = sig_rate - setup_rate if sig_rate is not None and setup_rate is not None else None
    by_year = _by_year(valid, label_col)
    positive_years = sum(
        1 for r in by_year.values()
        if r["lift_vs_leader_setup"] is not None
        and r["precision_minus_leader_setup"] is not None
        and r["lift_vs_leader_setup"] > 1.0
        and r["precision_minus_leader_setup"] > 0.0
    )
    signal_days = int(sig["trade_date"].nunique()) if len(sig) else 0
    avg_signals = float(len(sig) / signal_days) if signal_days else None
    gate = {
        "selected_min": {"threshold": int(selected_min), "actual": int(len(sig)), "pass": bool(len(sig) >= selected_min)},
        "lift_vs_leader_setup_min": {"threshold": float(lift_min), "actual": lift, "pass": bool(lift is not None and lift >= lift_min)},
        "precision_minus_leader_setup_min": {"threshold": float(precision_delta_min), "actual": delta, "pass": bool(delta is not None and delta >= precision_delta_min)},
        "positive_years_min": {"threshold": int(positive_years_min), "actual": int(positive_years), "pass": bool(positive_years >= positive_years_min)},
    }
    passed = all(v["pass"] for v in gate.values())
    return {
        "all_valid_rows": int(len(valid)),
        "all_market_rate": market_rate,
        "leader_setup_count": int(len(setup)),
        "leader_setup_rate": setup_rate,
        "breadth_signal_count": int(len(sig)),
        "breadth_signal_rate": sig_rate,
        "lift_vs_leader_setup": lift,
        "precision_minus_leader_setup": delta,
        "signal_share_of_leader_setup": float(len(sig) / len(setup)) if len(setup) else None,
        "signal_days": signal_days,
        "avg_signals_per_signal_day": avg_signals,
        "positive_years": int(positive_years),
        "by_year": by_year,
        "gate": gate,
        "verdict": "PASS_D3_MARKET_BREADTH_IGNITION_V0_1_TO_ROBUSTNESS" if passed else "FAST_REJECT_D3_MARKET_BREADTH_IGNITION_V0_1",
    }
