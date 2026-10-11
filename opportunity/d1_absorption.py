from __future__ import annotations

import numpy as np
import pandas as pd


def synthetic_adjust_ohlc(g: pd.DataFrame) -> pd.DataFrame:
    """Corporate-action-safe synthetic OHLC using PositionX close/pre_close convention."""
    x = g.sort_values("trade_date").copy()
    required = ["open", "high", "low", "close", "pre_close"]
    for c in required:
        x[c] = pd.to_numeric(x[c], errors="coerce")
    x["volume"] = pd.to_numeric(x["volume"], errors="coerce")

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


def add_absorption_features(g: pd.DataFrame) -> pd.DataFrame:
    """Build T-known 5-session selling-pressure / price-impact features."""
    x = synthetic_adjust_ohlc(g)
    if x.empty:
        return x

    prev_close = x["adj_close"].shift(1)
    x["ret1"] = x["adj_close"] / prev_close - 1.0

    prior20_vol = x["volume"].shift(1).rolling(20, min_periods=15).median()
    x["volume_ratio20"] = x["volume"] / prior20_vol.replace(0, np.nan)

    neg = x["ret1"] < 0
    down_vol = x["volume_ratio20"].where(neg, 0.0)
    downside = (-x["ret1"]).where(neg, 0.0)

    x["down_volume_intensity_5"] = down_vol.rolling(5, min_periods=5).mean()
    x["downside_damage_5"] = downside.rolling(5, min_periods=5).sum()
    x["damage_per_pressure_5"] = (
        x["downside_damage_5"] / x["down_volume_intensity_5"].replace(0, np.nan)
    )
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
    """Ex-post labels for a signal known at T close and entered T+1 open."""
    x = g.sort_values("trade_date").reset_index(drop=True).copy()
    n = len(x)

    for prefix in ("D1A_TREND25_60", "D1A_TREND40_120"):
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
                "D1A_TREND25_60_label",
                "D1A_TREND25_60_mfe",
                "D1A_TREND25_60_mae_before_hit",
                "D1A_TREND25_60_time_to_target",
            ]] = p
        s = compute_one(i, secondary_horizon, secondary_target, secondary_mae_floor)
        if s is not None:
            x.loc[i, [
                "D1A_TREND40_120_label",
                "D1A_TREND40_120_mfe",
                "D1A_TREND40_120_mae_before_hit",
                "D1A_TREND40_120_time_to_target",
            ]] = s
    return x


def mark_pressure_and_absorption(
    panel: pd.DataFrame,
    pressure_rank_min: float = 0.75,
    absorption_rank_min: float = 0.75,
) -> pd.DataFrame:
    """Frozen D1 Absorption V0.1 selection."""
    x = panel.copy()
    x["pressure_rank"] = x.groupby("trade_date")["down_volume_intensity_5"].rank(
        method="average", pct=True
    )
    x["pressure_setup"] = x["pressure_rank"] >= float(pressure_rank_min)

    x["absorption_score"] = np.nan
    mask = x["pressure_setup"] & x["damage_per_pressure_5"].notna()
    if mask.any():
        ranked = (
            x.loc[mask]
            .groupby("trade_date")["damage_per_pressure_5"]
            .rank(method="average", pct=True)
        )
        x.loc[mask, "absorption_score"] = 1.0 - ranked

    x["d1_absorption_signal"] = (
        x["pressure_setup"]
        & (x["absorption_score"] >= float(absorption_rank_min))
    )
    return x


def _by_year(df: pd.DataFrame, label_col: str) -> dict:
    y = pd.to_numeric(df[label_col], errors="coerce")
    valid = df[y.notna()].copy()
    valid["_label"] = y[y.notna()].astype(int).to_numpy()
    valid["year"] = valid["trade_date"].astype(str).str[:4]

    out = {}
    for year, g in valid.groupby("year"):
        setup = g[g["pressure_setup"]]
        sig = g[g["d1_absorption_signal"]]
        if len(setup) == 0:
            continue
        setup_rate = float(setup["_label"].mean())
        sig_rate = float(sig["_label"].mean()) if len(sig) else None
        lift = sig_rate / setup_rate if sig_rate is not None and setup_rate > 0 else None
        delta = sig_rate - setup_rate if sig_rate is not None else None
        out[str(year)] = {
            "valid_rows": int(len(g)),
            "pressure_setup_count": int(len(setup)),
            "absorption_signal_count": int(len(sig)),
            "pressure_setup_precision": setup_rate,
            "absorption_signal_precision": sig_rate,
            "lift_vs_pressure_setup": lift,
            "precision_minus_pressure_setup": delta,
        }
    return out


def evaluate_absorption(
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
    setup = valid[valid["pressure_setup"]].copy()
    sig = valid[valid["d1_absorption_signal"]].copy()

    setup_rate = float(setup["_label"].mean()) if len(setup) else None
    sig_rate = float(sig["_label"].mean()) if len(sig) else None
    lift = sig_rate / setup_rate if sig_rate is not None and setup_rate and setup_rate > 0 else None
    delta = sig_rate - setup_rate if sig_rate is not None and setup_rate is not None else None

    by_year = _by_year(valid, label_col)
    positive_years = sum(
        1 for r in by_year.values()
        if r["lift_vs_pressure_setup"] is not None
        and r["precision_minus_pressure_setup"] is not None
        and r["lift_vs_pressure_setup"] > 1.0
        and r["precision_minus_pressure_setup"] > 0.0
    )

    gate = {
        "selected_min": {"threshold": int(selected_min), "actual": int(len(sig)), "pass": bool(len(sig) >= selected_min)},
        "lift_vs_pressure_setup_min": {"threshold": float(lift_min), "actual": lift, "pass": bool(lift is not None and lift >= lift_min)},
        "precision_minus_pressure_setup_min": {"threshold": float(precision_delta_min), "actual": delta, "pass": bool(delta is not None and delta >= precision_delta_min)},
        "positive_years_min": {"threshold": int(positive_years_min), "actual": int(positive_years), "pass": bool(positive_years >= positive_years_min)},
    }
    passed = all(v["pass"] for v in gate.values())

    ttt = pd.to_numeric(sig.loc[sig["_label"].eq(1), label_col.replace("_label", "_time_to_target")], errors="coerce").dropna()
    mfe = pd.to_numeric(sig[label_col.replace("_label", "_mfe")], errors="coerce").dropna()
    mae = pd.to_numeric(sig[label_col.replace("_label", "_mae_before_hit")], errors="coerce").dropna()

    return {
        "all_valid_rows": int(len(valid)),
        "all_market_rate": market_rate,
        "pressure_setup_count": int(len(setup)),
        "pressure_setup_rate": setup_rate,
        "absorption_signal_count": int(len(sig)),
        "absorption_signal_rate": sig_rate,
        "lift_vs_pressure_setup": lift,
        "precision_minus_pressure_setup": delta,
        "signal_share_of_pressure_setup": float(len(sig) / len(setup)) if len(setup) else None,
        "positive_years": int(positive_years),
        "median_time_to_target": float(ttt.median()) if len(ttt) else None,
        "median_mfe": float(mfe.median()) if len(mfe) else None,
        "median_mae_before_hit": float(mae.median()) if len(mae) else None,
        "by_year": by_year,
        "gate": gate,
        "verdict": "PASS_D1_ABSORPTION_MECHANISM_V0_1_TO_ROBUSTNESS" if passed else "FAST_REJECT_D1_ABSORPTION_MECHANISM_V0_1",
    }
