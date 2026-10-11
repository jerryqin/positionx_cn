from __future__ import annotations

import numpy as np
import pandas as pd


def synthetic_adjust_ohlc(g: pd.DataFrame) -> pd.DataFrame:
    x = g.sort_values("trade_date").copy()
    required = ["open", "high", "low", "close", "pre_close"]
    for c in required:
        x[c] = pd.to_numeric(x[c], errors="coerce")
    valid = np.logical_and.reduce([(x[c] > 0) & x[c].notna() for c in required])
    x = x.loc[valid].copy()
    if x.empty:
        return x

    factor = x["close"] / x["pre_close"]
    adj_close = factor.cumprod()
    raw_close = x["close"].copy()
    for c in ("open", "high", "low"):
        x[f"adj_{c}"] = adj_close * x[c] / raw_close
    x["adj_close"] = adj_close
    return x


def add_compression_breakout_features(g: pd.DataFrame) -> pd.DataFrame:
    """T-known state-transition features.

    Compression setup excludes T itself:
      recent_tr10 = median true-range-pct over T-10..T-1
      older_tr50  = median true-range-pct over T-60..T-11
      compression_ratio = recent_tr10 / older_tr50

    T confirmation:
      close(T) > max high(T-20..T-1)
      true_range_pct(T) > recent_tr10
    """
    x = synthetic_adjust_ohlc(g)
    if x.empty:
        return x

    prev_close = x["adj_close"].shift(1)
    true_range = pd.concat([
        x["adj_high"] - x["adj_low"],
        (x["adj_high"] - prev_close).abs(),
        (x["adj_low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    x["true_range_pct"] = true_range / prev_close.replace(0, np.nan)

    x["recent_tr10"] = x["true_range_pct"].shift(1).rolling(10, min_periods=10).median()
    x["older_tr50"] = x["true_range_pct"].shift(11).rolling(50, min_periods=40).median()
    x["compression_ratio"] = x["recent_tr10"] / x["older_tr50"].replace(0, np.nan)

    x["prior20_high"] = x["adj_high"].shift(1).rolling(20, min_periods=20).max()
    x["breakout_margin"] = x["adj_close"] / x["prior20_high"] - 1.0
    x["range_release_ratio"] = x["true_range_pct"] / x["recent_tr10"].replace(0, np.nan)
    return x


def add_forward_labels_from_t1(
    g: pd.DataFrame,
    primary_horizon: int = 60,
    primary_target: float = 0.25,
    primary_mae_floor: float = -0.10,
    secondary_horizon: int = 120,
    secondary_target: float = 0.40,
    secondary_mae_floor: float = -0.12,
) -> pd.DataFrame:
    x = g.sort_values("trade_date").reset_index(drop=True).copy()
    n = len(x)

    for prefix in ("D2_TREND25_60", "D2_TREND40_120"):
        x[f"{prefix}_label"] = np.nan
        x[f"{prefix}_mfe"] = np.nan
        x[f"{prefix}_mae_before_hit"] = np.nan
        x[f"{prefix}_time_to_target"] = np.nan

    close = x["adj_close"].to_numpy(float)
    low = x["adj_low"].to_numpy(float)
    open_ = x["adj_open"].to_numpy(float)

    def compute_one(i, horizon, target, mae_floor):
        entry_idx = i + 1
        end_idx = entry_idx + horizon - 1
        if entry_idx >= n or end_idx >= n:
            return None
        entry = open_[entry_idx]
        if not np.isfinite(entry) or entry <= 0:
            return None
        f_close = close[entry_idx:end_idx + 1]
        f_low = low[entry_idx:end_idx + 1]
        if len(f_close) != horizon or len(f_low) != horizon:
            return None
        if not np.isfinite(f_close).all() or not np.isfinite(f_low).all():
            return None

        ret_close = f_close / entry - 1.0
        mfe = float(np.max(ret_close))
        hit = np.where(ret_close >= target)[0]
        if len(hit):
            h = int(hit[0])
            mae = float(np.min(f_low[:h + 1] / entry - 1.0))
            ok = mae >= mae_floor
            ttt = h + 1 if ok else np.nan
        else:
            mae = float(np.min(f_low / entry - 1.0))
            ok = False
            ttt = np.nan
        return int(ok), mfe, mae, ttt

    for i in range(n):
        p = compute_one(i, primary_horizon, primary_target, primary_mae_floor)
        if p is not None:
            x.loc[i, [
                "D2_TREND25_60_label", "D2_TREND25_60_mfe",
                "D2_TREND25_60_mae_before_hit", "D2_TREND25_60_time_to_target",
            ]] = p
        s = compute_one(i, secondary_horizon, secondary_target, secondary_mae_floor)
        if s is not None:
            x.loc[i, [
                "D2_TREND40_120_label", "D2_TREND40_120_mfe",
                "D2_TREND40_120_mae_before_hit", "D2_TREND40_120_time_to_target",
            ]] = s
    return x


def mark_setup_and_signal(
    panel: pd.DataFrame,
    compression_rank_max: float = 0.25,
) -> pd.DataFrame:
    """Frozen D2 V0.1 selection, all known at T close."""
    x = panel.copy()
    x["compression_rank"] = x.groupby("trade_date")["compression_ratio"].rank(
        method="average", pct=True
    )
    x["d2_setup"] = x["compression_rank"] <= float(compression_rank_max)
    x["breakout_confirm"] = x["breakout_margin"] > 0.0
    x["range_release_confirm"] = x["range_release_ratio"] > 1.0
    x["d2_signal"] = x["d2_setup"] & x["breakout_confirm"] & x["range_release_confirm"]
    return x


def _by_year(df: pd.DataFrame, label_col: str) -> dict:
    y = pd.to_numeric(df[label_col], errors="coerce")
    valid = df[y.notna()].copy()
    valid["_label"] = y[y.notna()].astype(int).to_numpy()
    valid["year"] = valid["trade_date"].astype(str).str[:4]
    out = {}
    for year, g in valid.groupby("year"):
        setup = g[g["d2_setup"]]
        sig = g[g["d2_signal"]]
        if len(setup) == 0:
            continue
        setup_rate = float(setup["_label"].mean())
        sig_rate = float(sig["_label"].mean()) if len(sig) else None
        lift = sig_rate / setup_rate if sig_rate is not None and setup_rate > 0 else None
        delta = sig_rate - setup_rate if sig_rate is not None else None
        out[str(year)] = {
            "valid_rows": int(len(g)),
            "compression_setup_count": int(len(setup)),
            "breakout_signal_count": int(len(sig)),
            "compression_setup_precision": setup_rate,
            "breakout_signal_precision": sig_rate,
            "lift_vs_compression_setup": lift,
            "precision_minus_compression_setup": delta,
        }
    return out


def evaluate_d2(
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

    market_rate = float(valid["_label"].mean()) if len(valid) else None
    setup = valid[valid["d2_setup"]].copy()
    sig = valid[valid["d2_signal"]].copy()

    setup_rate = float(setup["_label"].mean()) if len(setup) else None
    sig_rate = float(sig["_label"].mean()) if len(sig) else None
    lift = sig_rate / setup_rate if sig_rate is not None and setup_rate and setup_rate > 0 else None
    delta = sig_rate - setup_rate if sig_rate is not None and setup_rate is not None else None

    by_year = _by_year(valid, label_col)
    positive_years = sum(
        1 for r in by_year.values()
        if r["lift_vs_compression_setup"] is not None
        and r["precision_minus_compression_setup"] is not None
        and r["lift_vs_compression_setup"] > 1.0
        and r["precision_minus_compression_setup"] > 0.0
    )

    gate = {
        "selected_min": {"threshold": int(selected_min), "actual": int(len(sig)), "pass": bool(len(sig) >= selected_min)},
        "lift_vs_compression_setup_min": {"threshold": float(lift_min), "actual": lift, "pass": bool(lift is not None and lift >= lift_min)},
        "precision_minus_compression_setup_min": {"threshold": float(precision_delta_min), "actual": delta, "pass": bool(delta is not None and delta >= precision_delta_min)},
        "positive_years_min": {"threshold": int(positive_years_min), "actual": int(positive_years), "pass": bool(positive_years >= positive_years_min)},
    }
    passed = all(v["pass"] for v in gate.values())

    ttt = pd.to_numeric(sig.loc[sig["_label"].eq(1), label_col.replace("_label", "_time_to_target")], errors="coerce").dropna()
    mfe = pd.to_numeric(sig[label_col.replace("_label", "_mfe")], errors="coerce").dropna()
    mae = pd.to_numeric(sig[label_col.replace("_label", "_mae_before_hit")], errors="coerce").dropna()

    return {
        "all_valid_rows": int(len(valid)),
        "all_market_rate": market_rate,
        "compression_setup_count": int(len(setup)),
        "compression_setup_rate": setup_rate,
        "breakout_signal_count": int(len(sig)),
        "breakout_signal_rate": sig_rate,
        "lift_vs_compression_setup": lift,
        "precision_minus_compression_setup": delta,
        "signal_share_of_compression_setup": float(len(sig) / len(setup)) if len(setup) else None,
        "positive_years": int(positive_years),
        "median_time_to_target": float(ttt.median()) if len(ttt) else None,
        "median_mfe": float(mfe.median()) if len(mfe) else None,
        "median_mae_before_hit": float(mae.median()) if len(mae) else None,
        "by_year": by_year,
        "gate": gate,
        "verdict": "PASS_D2_COMPRESSION_BREAKOUT_MECHANISM_V0_1_TO_ROBUSTNESS" if passed else "FAST_REJECT_D2_COMPRESSION_BREAKOUT_MECHANISM_V0_1",
    }
