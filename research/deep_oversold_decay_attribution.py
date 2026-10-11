from __future__ import annotations

from typing import Any, Dict, Iterable, Sequence

import pandas as pd


FOLDS = (
    ("F1", "20200803", "20221017"),
    ("F2", "20221018", "20240919"),
    ("F3", "20240920", "20260924"),
)


def fold_of(date: str) -> str | None:
    d = str(date)
    for name, start, end in FOLDS:
        if start <= d <= end:
            return name
    return None


def trimmed_mean(s: pd.Series, frac: float = 0.05) -> float | None:
    x = pd.to_numeric(s, errors="coerce").dropna().sort_values()
    if x.empty:
        return None
    k = int(len(x) * frac)
    if k > 0 and len(x) > 2*k:
        x = x.iloc[k:-k]
    return float(x.mean())


def make_daily_signal_frame(events: pd.DataFrame, return_col: str) -> pd.DataFrame:
    regime_cols = [
        "panic_regime", "trend_regime", "breadth_regime", "vol_regime",
        "panic_breadth", "breadth_above_ma60", "benchmark_close",
        "benchmark_ma60", "benchmark_rv20",
        "benchmark_rv20_trailing252_median",
    ]
    available = [c for c in regime_cols if c in events.columns]
    rows = []
    for d, g in events.groupby("signal_date", sort=True):
        row = {
            "signal_date": str(d),
            "fold": fold_of(str(d)),
            "event_count": int(len(g)),
            "daily_equal_return": float(pd.to_numeric(g[return_col], errors="coerce").mean()),
        }
        for c in available:
            vals = g[c].dropna()
            if len(vals):
                # Regime/state columns are identical within a signal date by construction.
                row[c] = vals.iloc[0]
        rows.append(row)
    return pd.DataFrame(rows)


def fold_summary(
    events: pd.DataFrame,
    daily: pd.DataFrame,
    state_days: pd.DataFrame,
    return_col: str,
) -> list[Dict[str, Any]]:
    out = []
    for name, start, end in FOLDS:
        e = events[(events["signal_date"] >= start) & (events["signal_date"] <= end)].copy()
        d = daily[daily["fold"] == name].copy()
        s = state_days[
            (state_days["trade_date"] >= start) & (state_days["trade_date"] <= end)
        ]
        r = pd.to_numeric(e[return_col], errors="coerce").dropna()
        out.append({
            "fold": name,
            "market_sessions": int(len(s)),
            "events": int(len(e)),
            "signal_dates": int(len(d)),
            "signal_day_rate": float(len(d)/len(s)) if len(s) else None,
            "events_per_signal_day": float(len(e)/len(d)) if len(d) else None,
            "event_weighted_avg": float(r.mean()) if len(r) else None,
            "event_median": float(r.median()) if len(r) else None,
            "event_trim5": trimmed_mean(r, 0.05),
            "tail_lift_avg_minus_trim5": (
                float(r.mean()) - trimmed_mean(r, 0.05)
                if len(r) else None
            ),
            "date_equal_avg": float(d["daily_equal_return"].mean()) if len(d) else None,
        })
    return out


def bucket_table(
    daily: pd.DataFrame,
    dims: Sequence[str],
) -> pd.DataFrame:
    x = daily.dropna(subset=list(dims)).copy()
    if x.empty:
        return pd.DataFrame()
    keys = list(dims) + ["fold"]
    g = x.groupby(keys, sort=True).agg(
        signal_dates=("signal_date","count"),
        mean_return=("daily_equal_return","mean"),
        median_return=("daily_equal_return","median"),
        mean_event_count=("event_count","mean"),
    ).reset_index()
    totals = g.groupby("fold")["signal_dates"].transform("sum")
    g["weight"] = g["signal_dates"] / totals
    return g


def pooled_bucket_means(
    daily: pd.DataFrame,
    dims: Sequence[str],
) -> pd.DataFrame:
    x = daily.dropna(subset=list(dims)).copy()
    if x.empty:
        return pd.DataFrame()
    return (
        x.groupby(list(dims), sort=True)
        .agg(
            pooled_mean=("daily_equal_return","mean"),
            pooled_signal_dates=("signal_date","count"),
        )
        .reset_index()
    )


def composition_attribution(
    daily: pd.DataFrame,
    dims: Sequence[str],
    fold_a: str,
    fold_b: str,
) -> Dict[str, Any]:
    """
    Attribution using pooled bucket means.

    composition_effect:
      change caused only by fold B having a different regime mix, if each regime
      kept the pooled historical mean return.

    residual_within_regime_and_other:
      observed date-equal return change minus the composition effect.
      This is deliberately not claimed as a pure causal "within-regime" effect.
    """
    bt = bucket_table(daily, dims)
    pooled = pooled_bucket_means(daily, dims)
    if bt.empty or pooled.empty:
        return {}

    join_cols = list(dims)
    a = bt[bt["fold"] == fold_a][join_cols + ["weight"]].rename(columns={"weight":"w_a"})
    b = bt[bt["fold"] == fold_b][join_cols + ["weight"]].rename(columns={"weight":"w_b"})
    z = pooled.merge(a, on=join_cols, how="left").merge(b, on=join_cols, how="left")
    z["w_a"] = z["w_a"].fillna(0.0)
    z["w_b"] = z["w_b"].fillna(0.0)

    comp_a = float((z["w_a"] * z["pooled_mean"]).sum())
    comp_b = float((z["w_b"] * z["pooled_mean"]).sum())
    composition_effect = comp_b - comp_a

    ra = float(daily.loc[daily["fold"] == fold_a, "daily_equal_return"].mean())
    rb = float(daily.loc[daily["fold"] == fold_b, "daily_equal_return"].mean())
    observed_change = rb - ra
    residual = observed_change - composition_effect

    buckets = []
    for _, r in z.sort_values("pooled_signal_dates", ascending=False).iterrows():
        rec = {d: str(r[d]) for d in dims}
        rec.update({
            "pooled_mean": float(r["pooled_mean"]),
            "w_a": float(r["w_a"]),
            "w_b": float(r["w_b"]),
            "mix_contribution": float((r["w_b"]-r["w_a"]) * r["pooled_mean"]),
        })
        buckets.append(rec)

    return {
        "dimensions": list(dims),
        "from_fold": fold_a,
        "to_fold": fold_b,
        "observed_date_equal_change": observed_change,
        "composition_effect_using_pooled_bucket_means": composition_effect,
        "residual_within_regime_and_other": residual,
        "composition_share_of_observed_change": (
            composition_effect / observed_change
            if observed_change != 0 else None
        ),
        "buckets": buckets,
    }


def regime_mix(daily: pd.DataFrame, dim: str) -> list[Dict[str, Any]]:
    out = []
    for fold, fg in daily.groupby("fold", sort=True):
        n = len(fg)
        for regime, g in fg.groupby(dim, dropna=False, sort=True):
            out.append({
                "fold": str(fold),
                "dimension": dim,
                "regime": str(regime),
                "signal_dates": int(len(g)),
                "weight": float(len(g)/n) if n else None,
                "date_equal_avg": float(g["daily_equal_return"].mean()),
                "avg_events_per_signal_day": float(g["event_count"].mean()),
            })
    return out
