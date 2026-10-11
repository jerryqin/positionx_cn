from __future__ import annotations

import numpy as np
import pandas as pd


SHOCK_COMPONENTS = (
    "ret1",
    "intraday_return",
    "close_location",
    "range_vs_prior20",
)


def synthetic_adjust_ohlc(g: pd.DataFrame) -> pd.DataFrame:
    """PositionX corporate-action-safe synthetic adjusted OHLC."""
    x = g.sort_values("trade_date").copy()
    required = ["open", "high", "low", "close", "pre_close"]
    for c in required:
        x[c] = pd.to_numeric(x[c], errors="coerce")
    valid = np.logical_and.reduce([(x[c] > 0) & x[c].notna() for c in required])
    x = x.loc[valid].copy()
    if x.empty:
        return x

    daily_factor = x["close"] / x["pre_close"]
    adj_close = daily_factor.cumprod()
    raw_close = x["close"].copy()
    for c in ("open", "high", "low"):
        x[f"adj_{c}"] = adj_close * x[c] / raw_close
    x["adj_close"] = adj_close
    return x


def add_t_features(g: pd.DataFrame) -> pd.DataFrame:
    """Build only T-known shock features plus future prices needed for fixed timing."""
    x = synthetic_adjust_ohlc(g)
    if x.empty:
        return x

    prev_close = x["adj_close"].shift(1)
    day_range = (x["adj_high"] - x["adj_low"]).replace(0, np.nan)

    x["ret1"] = x["adj_close"] / prev_close - 1.0
    x["intraday_return"] = x["adj_close"] / x["adj_open"] - 1.0
    x["close_location"] = (x["adj_close"] - x["adj_low"]) / day_range

    true_range = pd.concat(
        [
            x["adj_high"] - x["adj_low"],
            (x["adj_high"] - prev_close).abs(),
            (x["adj_low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    prior20_tr = true_range.shift(1).rolling(20, min_periods=15).median()
    x["range_vs_prior20"] = true_range / prior20_tr.replace(0, np.nan)

    # Fixed confirmation / entry timing.
    x["close_t3"] = x["adj_close"].shift(-3)
    x["open_t4"] = x["adj_open"].shift(-4)
    x["recovery_3d"] = x["close_t3"] / x["adj_close"] - 1.0

    return x


def add_same_day_shock_score(df: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional adverse percentile score on T.

    Higher score = more capitulation-like:
      low ret1, low intraday_return, low close_location, high range_vs_prior20.
    """
    x = df.copy()
    rank_ret1 = x.groupby("trade_date")["ret1"].rank(method="average", pct=True)
    rank_intraday = x.groupby("trade_date")["intraday_return"].rank(method="average", pct=True)
    rank_close_loc = x.groupby("trade_date")["close_location"].rank(method="average", pct=True)
    rank_range = x.groupby("trade_date")["range_vs_prior20"].rank(method="average", pct=True)

    x["shock_ret1"] = 1.0 - rank_ret1
    x["shock_intraday"] = 1.0 - rank_intraday
    x["shock_close_location"] = 1.0 - rank_close_loc
    x["shock_range"] = rank_range

    x["capitulation_score"] = x[
        ["shock_ret1", "shock_intraday", "shock_close_location", "shock_range"]
    ].mean(axis=1, skipna=False)
    return x


def mark_setup_and_signal(
    df: pd.DataFrame,
    setup_score_min: float = 0.75,
) -> pd.DataFrame:
    """Frozen D1 V0.1 mechanism.

    Setup at T close:
      capitulation_score >= 0.75

    Confirmation at T+3 close:
      close(T+3) > close(T)

    Hypothetical entry:
      T+4 open
    """
    x = df.copy()
    x["d1_setup"] = x["capitulation_score"] >= float(setup_score_min)
    x["d1_recovery_confirm"] = x["recovery_3d"] > 0.0
    x["d1_signal"] = x["d1_setup"] & x["d1_recovery_confirm"]
    return x


def add_forward_labels_from_t4(
    g: pd.DataFrame,
    primary_horizon: int = 60,
    primary_target: float = 0.25,
    primary_mae_floor: float = -0.10,
    secondary_horizon: int = 120,
    secondary_target: float = 0.40,
    secondary_mae_floor: float = -0.12,
) -> pd.DataFrame:
    """Ex-post labels for a signal decided at T+3 close, entered T+4 open.

    All labels are recomputed from T+4 open. No T-based TREND25_60 label is reused.
    """
    x = g.sort_values("trade_date").reset_index(drop=True).copy()
    n = len(x)

    for prefix in ("D1_TREND25_60", "D1_TREND40_120"):
        x[f"{prefix}_label"] = np.nan
        x[f"{prefix}_mfe"] = np.nan
        x[f"{prefix}_mae_before_hit"] = np.nan
        x[f"{prefix}_time_to_target"] = np.nan

    close = x["adj_close"].to_numpy(float)
    low = x["adj_low"].to_numpy(float)
    open_ = x["adj_open"].to_numpy(float)

    def compute_one(i, horizon, target, mae_floor):
        entry_idx = i + 4
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
                "D1_TREND25_60_label",
                "D1_TREND25_60_mfe",
                "D1_TREND25_60_mae_before_hit",
                "D1_TREND25_60_time_to_target",
            ]] = p

        s = compute_one(i, secondary_horizon, secondary_target, secondary_mae_floor)
        if s is not None:
            x.loc[i, [
                "D1_TREND40_120_label",
                "D1_TREND40_120_mfe",
                "D1_TREND40_120_mae_before_hit",
                "D1_TREND40_120_time_to_target",
            ]] = s

    return x


def _year_stats(df: pd.DataFrame, label_col: str) -> dict:
    out = {}
    y = pd.to_numeric(df[label_col], errors="coerce")
    valid = df[y.notna()].copy()
    valid["_label"] = y[y.notna()].astype(int).to_numpy()
    valid["year"] = valid["trade_date"].astype(str).str[:4]

    for year, g in valid.groupby("year"):
        setup = g[g["d1_setup"]]
        sig = g[g["d1_signal"]]
        if len(setup) == 0:
            continue
        setup_rate = float(setup["_label"].mean())
        sig_rate = float(sig["_label"].mean()) if len(sig) else None
        lift = sig_rate / setup_rate if sig_rate is not None and setup_rate > 0 else None
        delta = sig_rate - setup_rate if sig_rate is not None else None
        out[str(year)] = {
            "valid_rows": int(len(g)),
            "setup_count": int(len(setup)),
            "signal_count": int(len(sig)),
            "setup_precision": setup_rate,
            "signal_precision": sig_rate,
            "lift_vs_setup": lift,
            "precision_minus_setup": delta,
        }
    return out


def evaluate_d1(
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

    all_rate = float(valid["_label"].mean()) if len(valid) else None
    setup = valid[valid["d1_setup"]].copy()
    sig = valid[valid["d1_signal"]].copy()

    setup_rate = float(setup["_label"].mean()) if len(setup) else None
    sig_rate = float(sig["_label"].mean()) if len(sig) else None
    lift_vs_setup = (
        sig_rate / setup_rate
        if sig_rate is not None and setup_rate is not None and setup_rate > 0
        else None
    )
    delta = (
        sig_rate - setup_rate
        if sig_rate is not None and setup_rate is not None
        else None
    )

    by_year = _year_stats(valid, label_col)
    positive_years = sum(
        1
        for r in by_year.values()
        if r["lift_vs_setup"] is not None
        and r["precision_minus_setup"] is not None
        and r["lift_vs_setup"] > 1.0
        and r["precision_minus_setup"] > 0.0
    )

    gate = {
        "selected_min": {
            "threshold": int(selected_min),
            "actual": int(len(sig)),
            "pass": bool(len(sig) >= selected_min),
        },
        "lift_vs_setup_min": {
            "threshold": float(lift_min),
            "actual": lift_vs_setup,
            "pass": bool(lift_vs_setup is not None and lift_vs_setup >= lift_min),
        },
        "precision_minus_setup_min": {
            "threshold": float(precision_delta_min),
            "actual": delta,
            "pass": bool(delta is not None and delta >= precision_delta_min),
        },
        "positive_years_min": {
            "threshold": int(positive_years_min),
            "actual": int(positive_years),
            "pass": bool(positive_years >= positive_years_min),
        },
    }
    passed = all(v["pass"] for v in gate.values())

    ttt = pd.to_numeric(
        sig.loc[sig["_label"].eq(1), label_col.replace("_label", "_time_to_target")],
        errors="coerce",
    ).dropna()
    mfe = pd.to_numeric(
        sig[label_col.replace("_label", "_mfe")], errors="coerce"
    ).dropna()
    mae = pd.to_numeric(
        sig[label_col.replace("_label", "_mae_before_hit")], errors="coerce"
    ).dropna()

    return {
        "all_valid_rows": int(len(valid)),
        "all_market_rate": all_rate,
        "setup_count": int(len(setup)),
        "setup_rate": setup_rate,
        "signal_count": int(len(sig)),
        "signal_rate": sig_rate,
        "lift_vs_setup": lift_vs_setup,
        "precision_minus_setup": delta,
        "signal_share_of_setup": float(len(sig) / len(setup)) if len(setup) else None,
        "positive_years": int(positive_years),
        "median_time_to_target": float(ttt.median()) if len(ttt) else None,
        "median_mfe": float(mfe.median()) if len(mfe) else None,
        "median_mae_before_hit": float(mae.median()) if len(mae) else None,
        "by_year": by_year,
        "gate": gate,
        "verdict": (
            "PASS_D1_CAPITULATION_RECOVERY_MECHANISM_V0_1_TO_ROBUSTNESS"
            if passed
            else "FAST_REJECT_D1_CAPITULATION_RECOVERY_MECHANISM_V0_1"
        ),
    }
