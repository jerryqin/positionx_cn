from __future__ import annotations

import math
import numpy as np
import pandas as pd


KNOWN_CHANGE_TYPES = {
    "IMPROVING",
    "DETERIORATING",
    "TURN_TO_LOSS",
    "TURNAROUND",
    "LOSS_NARROWING",
    "LOSS_WIDENING",
    "FLAT",
}


def norm_bool(s: pd.Series) -> pd.Series:
    return s.astype("string").str.strip().str.lower().isin(
        ["true", "1", "yes", "y", "eligible"]
    )


def report_period_bucket(s: pd.Series) -> pd.Series:
    x = s.astype("string").fillna("").str.upper().str.strip()
    out = pd.Series("OTHER", index=s.index, dtype="string")
    for bucket in ["ANNUAL", "H1", "Q1", "Q1_Q3"]:
        out.loc[x.str.endswith("_" + bucket)] = bucket
    return out


def prepare_event_universe(events: pd.DataFrame) -> pd.DataFrame:
    x = events.copy()
    x["event_time"] = pd.to_datetime(x["event_time"], errors="coerce")
    x["event_date"] = x["event_time"].dt.normalize()
    x["event_year"] = x["event_time"].dt.year.astype("Int64")

    eligible = norm_bool(x["strategy_eligible_semantics"])
    kind = x["event_kind"].astype("string").str.upper().str.strip()
    change = x["change_type"].astype("string").str.upper().str.strip()

    x["report_period_bucket"] = report_period_bucket(x["report_period"])
    x["e1_setup"] = (
        eligible
        & kind.eq("ORIGINAL_FORECAST")
        & change.isin(KNOWN_CHANGE_TYPES)
        & x["event_time"].notna()
    )
    x["e1_signal"] = x["e1_setup"] & change.eq("TURNAROUND")
    return x


def synthetic_adjust_ohlc(bars: pd.DataFrame) -> pd.DataFrame:
    x = bars.copy()
    x["trade_date"] = pd.to_datetime(x["trade_date"], errors="coerce")
    x = x.dropna(subset=["symbol", "trade_date", "open", "high", "low", "close"])
    x = x.sort_values(["symbol", "trade_date"]).reset_index(drop=True)

    close = pd.to_numeric(x["close"], errors="coerce")
    pre = pd.to_numeric(x["pre_close"], errors="coerce")
    factor = close / pre
    valid = close.gt(0) & pre.gt(0) & factor.gt(0) & np.isfinite(factor)

    x["_factor_valid"] = valid
    x["_factor"] = factor.where(valid, 1.0)

    x["adj_close"] = x.groupby("symbol")["_factor"].cumprod()
    close_safe = close.where(close.gt(0))
    x["adj_open"] = x["adj_close"] * pd.to_numeric(x["open"], errors="coerce") / close_safe
    x["adj_high"] = x["adj_close"] * pd.to_numeric(x["high"], errors="coerce") / close_safe
    x["adj_low"] = x["adj_close"] * pd.to_numeric(x["low"], errors="coerce") / close_safe
    return x


def attach_next_tradable_entry(events: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    """Entry is the first available symbol bar strictly AFTER event calendar date."""
    x = events.copy()
    x["entry_date"] = pd.NaT
    x["entry_adj_open"] = np.nan
    x["_entry_pos"] = pd.Series(pd.NA, index=x.index, dtype="Int64")

    by_symbol = {
        str(sym): g.reset_index(drop=True)
        for sym, g in bars.groupby("symbol", sort=False)
    }

    for idx, row in x.loc[x["e1_setup"]].iterrows():
        sym = str(row["symbol"])
        g = by_symbol.get(sym)
        if g is None or g.empty or pd.isna(row["event_date"]):
            continue

        dates = g["trade_date"].to_numpy(dtype="datetime64[ns]")
        pos = int(np.searchsorted(dates, np.datetime64(row["event_date"]), side="right"))
        if pos >= len(g):
            continue
        op = g.loc[pos, "adj_open"]
        if pd.isna(op) or float(op) <= 0:
            continue

        x.at[idx, "entry_date"] = g.loc[pos, "trade_date"]
        x.at[idx, "entry_adj_open"] = float(op)
        x.at[idx, "_entry_pos"] = pos

    return x


def _event_label(
    g: pd.DataFrame,
    entry_pos: int,
    entry_price: float,
    target: float,
    horizon: int,
    mae_floor: float,
):
    """Return 1/0/NA.

    1 can be known once target is hit with acceptable pre-hit MAE.
    0 requires a complete horizon.
    """
    if entry_pos < 0 or entry_pos >= len(g) or entry_price <= 0:
        return pd.NA

    end = min(len(g), entry_pos + horizon)
    path = g.iloc[entry_pos:end].copy()
    if path.empty:
        return pd.NA

    highs = pd.to_numeric(path["adj_high"], errors="coerce") / entry_price - 1.0
    lows = pd.to_numeric(path["adj_low"], errors="coerce") / entry_price - 1.0

    hit_positions = np.flatnonzero((highs >= target).fillna(False).to_numpy())
    if len(hit_positions):
        h = int(hit_positions[0])
        pre_hit_lows = lows.iloc[: h + 1]
        mae = float(pre_hit_lows.min()) if pre_hit_lows.notna().any() else np.nan
        if np.isfinite(mae) and mae >= mae_floor:
            return 1
        # Target hit only after violating MAE constraint => positive label impossible
        # under "MAE before first hit" definition, but a later hit after recovery could
        # satisfy a different path. Continue scanning later target hits.
        for h in hit_positions[1:]:
            pre = lows.iloc[: int(h) + 1]
            mae2 = float(pre.min()) if pre.notna().any() else np.nan
            if np.isfinite(mae2) and mae2 >= mae_floor:
                return 1

    # A negative label is only safe when the complete horizon exists.
    if len(path) < horizon:
        return pd.NA
    return 0


def add_forward_labels(
    events: pd.DataFrame,
    bars: pd.DataFrame,
    primary_target: float = 0.25,
    primary_horizon: int = 60,
    primary_mae_floor: float = -0.10,
    secondary_target: float = 0.40,
    secondary_horizon: int = 120,
    secondary_mae_floor: float = -0.12,
) -> pd.DataFrame:
    x = events.copy()

    bar_groups = {
        str(sym): g.reset_index(drop=True)
        for sym, g in bars.groupby("symbol", sort=False)
    }

    pvals, svals = [], []
    for _, row in x.iterrows():
        if not bool(row.get("e1_setup", False)) or pd.isna(row.get("_entry_pos")):
            pvals.append(pd.NA)
            svals.append(pd.NA)
            continue

        g = bar_groups.get(str(row["symbol"]))
        if g is None:
            pvals.append(pd.NA)
            svals.append(pd.NA)
            continue

        pos = int(row["_entry_pos"])
        price = float(row["entry_adj_open"])
        pvals.append(
            _event_label(
                g, pos, price,
                primary_target, primary_horizon, primary_mae_floor
            )
        )
        svals.append(
            _event_label(
                g, pos, price,
                secondary_target, secondary_horizon, secondary_mae_floor
            )
        )

    x["E1_TREND25_60_label"] = pd.array(pvals, dtype="Int64")
    x["E1_TREND40_120_label"] = pd.array(svals, dtype="Int64")
    return x


def _matched_rate(
    valid_setup: pd.DataFrame,
    valid_signal: pd.DataFrame,
    label_col: str,
    min_setup_per_stratum: int,
):
    """Signal-weighted matched baseline by event_year x report_period_bucket.

    The baseline includes the signal subset, making it conservative.
    """
    setup_stats = (
        valid_setup.groupby(["event_year", "report_period_bucket"], dropna=False)
        .agg(
            setup_count=(label_col, "size"),
            setup_rate=(label_col, "mean"),
        )
        .reset_index()
    )

    signal_stats = (
        valid_signal.groupby(["event_year", "report_period_bucket"], dropna=False)
        .agg(
            signal_count=(label_col, "size"),
            signal_rate=(label_col, "mean"),
        )
        .reset_index()
    )

    z = signal_stats.merge(
        setup_stats,
        on=["event_year", "report_period_bucket"],
        how="left",
        validate="many_to_one",
    )
    z = z[z["setup_count"] >= int(min_setup_per_stratum)].copy()

    if z.empty or int(z["signal_count"].sum()) == 0:
        return None, 0, z

    matched_rate = float(
        (z["signal_count"] * z["setup_rate"]).sum() / z["signal_count"].sum()
    )
    matched_signal_count = int(z["signal_count"].sum())
    return matched_rate, matched_signal_count, z


def evaluate_e1(
    panel: pd.DataFrame,
    label_col: str,
    selected_min: int = 200,
    lift_min: float = 1.25,
    precision_delta_min: float = 0.03,
    positive_years_min: int = 5,
    min_setup_per_stratum: int = 30,
):
    y = pd.to_numeric(panel[label_col], errors="coerce")
    valid = panel[y.notna()].copy()
    valid["_label"] = y[y.notna()].astype(int).to_numpy()

    setup = valid[valid["e1_setup"]].copy()
    signal = valid[valid["e1_signal"]].copy()

    raw_setup_rate = float(setup["_label"].mean()) if len(setup) else None
    signal_rate = float(signal["_label"].mean()) if len(signal) else None

    matched_rate, matched_signal_count, strata = _matched_rate(
        setup, signal, "_label", min_setup_per_stratum
    )

    if matched_rate is not None and signal_rate is not None and matched_rate > 0:
        # Recompute signal rate only on strata retained by matched baseline.
        keys = strata[["event_year", "report_period_bucket"]].drop_duplicates()
        sig_matched = signal.merge(
            keys,
            on=["event_year", "report_period_bucket"],
            how="inner",
        )
        signal_rate_matched = (
            float(sig_matched["_label"].mean()) if len(sig_matched) else None
        )
    else:
        sig_matched = signal.iloc[0:0].copy()
        signal_rate_matched = None

    lift = (
        signal_rate_matched / matched_rate
        if signal_rate_matched is not None and matched_rate is not None and matched_rate > 0
        else None
    )
    delta = (
        signal_rate_matched - matched_rate
        if signal_rate_matched is not None and matched_rate is not None
        else None
    )

    by_year = {}
    positive_years = 0
    years = sorted(int(v) for v in setup["event_year"].dropna().unique())
    for year in years:
        sy = setup[setup["event_year"] == year]
        gy = signal[signal["event_year"] == year]
        my, my_n, st = _matched_rate(
            sy, gy, "_label", min_setup_per_stratum
        )
        if my is None or my_n == 0:
            continue
        keys = st[["event_year", "report_period_bucket"]].drop_duplicates()
        gym = gy.merge(keys, on=["event_year", "report_period_bucket"], how="inner")
        sr = float(gym["_label"].mean()) if len(gym) else None
        yl = sr / my if sr is not None and my > 0 else None
        yd = sr - my if sr is not None else None
        if yl is not None and yd is not None and yl > 1.0 and yd > 0:
            positive_years += 1

        by_year[str(year)] = {
            "setup_valid": int(len(sy)),
            "signal_valid": int(len(gy)),
            "matched_signal_count": int(my_n),
            "signal_rate": sr,
            "matched_setup_rate": my,
            "lift_vs_matched_setup": yl,
            "precision_minus_matched_setup": yd,
        }

    gate = {
        "selected_min": {
            "threshold": int(selected_min),
            "actual": int(matched_signal_count),
            "pass": bool(matched_signal_count >= selected_min),
        },
        "lift_vs_matched_setup_min": {
            "threshold": float(lift_min),
            "actual": lift,
            "pass": bool(lift is not None and lift >= lift_min),
        },
        "precision_minus_matched_setup_min": {
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

    return {
        "valid_label_rows": int(len(valid)),
        "setup_valid": int(len(setup)),
        "signal_valid": int(len(signal)),
        "raw_setup_rate": raw_setup_rate,
        "matched_signal_count": int(matched_signal_count),
        "signal_rate_matched": signal_rate_matched,
        "matched_setup_rate": matched_rate,
        "lift_vs_matched_setup": lift,
        "precision_minus_matched_setup": delta,
        "positive_years": int(positive_years),
        "by_year": by_year,
        "gate": gate,
        "verdict": (
            "PASS_E1_FORECAST_TURNAROUND_V0_1_TO_ROBUSTNESS"
            if passed else
            "FAST_REJECT_E1_FORECAST_TURNAROUND_V0_1"
        ),
    }
