from __future__ import annotations

from dataclasses import dataclass
import math
import numpy as np
import pandas as pd


OFFSETS = (-20, -10, -5, 0, 5, 10)
FEATURES = (
    "rs20",
    "rs_accel",
    "vol_compression",
    "volume_contraction",
    "distance_to_60d_high",
    "higher_low_20_vs_60",
)


def _bool_series(s: pd.Series) -> pd.Series:
    return s.map(lambda v: bool(v) if pd.notna(v) else False)


def add_episode_onsets(
    panel: pd.DataFrame,
    label_col: str = "TREND25_60_label",
) -> pd.DataFrame:
    """Mark first row of each contiguous positive-label run within symbol."""
    required = {"symbol", "trade_date", label_col}
    missing = required - set(panel.columns)
    if missing:
        raise ValueError(f"missing onset columns: {sorted(missing)}")

    x = panel.sort_values(["symbol", "trade_date"]).copy()
    lab = pd.to_numeric(x[label_col], errors="coerce")
    x["_label_valid"] = lab.notna()
    x["_label_pos"] = lab.eq(1)

    prev = x.groupby("symbol", sort=False)["_label_pos"].shift(1)
    prev_bool = prev.map(lambda v: bool(v) if pd.notna(v) else False)
    x["trend_episode_onset"] = x["_label_pos"] & ~prev_bool
    return x


def add_event_window_features(
    panel: pd.DataFrame,
    features=FEATURES,
    offsets=OFFSETS,
) -> pd.DataFrame:
    """Attach feature values at fixed symbol-session offsets around every anchor row."""
    x = panel.sort_values(["symbol", "trade_date"]).copy()
    missing = set(features) - set(x.columns)
    if missing:
        raise ValueError(f"missing transition features: {sorted(missing)}")

    gb = x.groupby("symbol", sort=False)
    for f in features:
        for off in offsets:
            col = f"{f}__t{off:+d}"
            x[col] = gb[f].shift(-off) if off != 0 else x[f]

        # First derivatives available at T.
        x[f"{f}__d_m20_0"] = x[f"{f}__t+0"] - x[f"{f}__t-20"]
        x[f"{f}__d_m10_0"] = x[f"{f}__t+0"] - x[f"{f}__t-10"]
        x[f"{f}__d_m5_0"] = x[f"{f}__t+0"] - x[f"{f}__t-5"]

        # Local curvature / inflection available at T.
        x[f"{f}__curve_5"] = (
            (x[f"{f}__t+0"] - x[f"{f}__t-5"])
            - (x[f"{f}__t-5"] - x[f"{f}__t-10"])
        )
        x[f"{f}__curve_10"] = (
            (x[f"{f}__t+0"] - x[f"{f}__t-10"])
            - (x[f"{f}__t-10"] - x[f"{f}__t-20"])
        )

        # Descriptive only: future behavior after onset. Never candidate inputs.
        x[f"{f}__post_d_0_5"] = x[f"{f}__t+5"] - x[f"{f}__t+0"]
        x[f"{f}__post_d_0_10"] = x[f"{f}__t+10"] - x[f"{f}__t+0"]

    return x


def transition_metric_columns(features=FEATURES) -> list[str]:
    out = []
    for f in features:
        out.extend([
            f"{f}__d_m20_0",
            f"{f}__d_m10_0",
            f"{f}__d_m5_0",
            f"{f}__curve_5",
            f"{f}__curve_10",
        ])
    return out


def level_columns(features=FEATURES, offsets=OFFSETS) -> list[str]:
    return [f"{f}__t{off:+d}" for f in features for off in offsets]


def post_columns(features=FEATURES) -> list[str]:
    return [
        c
        for f in features
        for c in (f"{f}__post_d_0_5", f"{f}__post_d_0_10")
    ]


def _same_day_control_percentiles(
    events: pd.DataFrame,
    controls: pd.DataFrame,
    metric: str,
) -> pd.Series:
    """Empirical percentile of each event value versus same-date label=0 controls."""
    out = pd.Series(np.nan, index=events.index, dtype=float)
    control_groups = {}
    for d, g in controls.groupby("trade_date", sort=False):
        a = pd.to_numeric(g[metric], errors="coerce").dropna().to_numpy(dtype=float)
        if len(a):
            control_groups[str(d)] = np.sort(a)

    for d, g in events.groupby("trade_date", sort=False):
        a = control_groups.get(str(d))
        if a is None or len(a) == 0:
            continue
        vals = pd.to_numeric(g[metric], errors="coerce")
        valid = vals.notna()
        if not valid.any():
            continue
        v = vals[valid].to_numpy(dtype=float)
        left = np.searchsorted(a, v, side="left")
        right = np.searchsorted(a, v, side="right")
        pct = (left + right) / (2.0 * len(a))
        out.loc[vals[valid].index] = pct
    return out


def _summarize_metric(
    events: pd.DataFrame,
    controls: pd.DataFrame,
    metric: str,
) -> dict:
    e = pd.to_numeric(events[metric], errors="coerce")
    c = pd.to_numeric(controls[metric], errors="coerce")
    ev = events.loc[e.notna(), ["trade_date", "symbol"]].copy()
    ev["value"] = e[e.notna()]
    co = controls.loc[c.notna(), ["trade_date", "symbol"]].copy()
    co["value"] = c[c.notna()]

    if ev.empty or co.empty:
        return {
            "event_n": int(len(ev)),
            "control_n": int(len(co)),
            "median_event_percentile_vs_same_day_controls": None,
            "mean_event_percentile_vs_same_day_controls": None,
            "date_equal_median_diff": None,
            "date_equal_positive_share": None,
            "year_consistency_count": 0,
            "by_year": {},
        }

    # Percentile versus same-day controls.
    tmp_events = events.loc[e.notna()].copy()
    tmp_controls = controls.loc[c.notna()].copy()
    pct = _same_day_control_percentiles(tmp_events, tmp_controls, metric)
    pct_valid = pct.dropna()

    # Date-equal effect: median(event) - median(control), then equal-weight dates.
    em = ev.groupby("trade_date")["value"].median()
    cm = co.groupby("trade_date")["value"].median()
    date_diff = (em - cm).dropna()

    by_year = {}
    year_consistency_count = 0
    if len(pct_valid):
        pp = tmp_events.loc[pct_valid.index, ["trade_date"]].copy()
        pp["pct"] = pct_valid
        pp["year"] = pp["trade_date"].astype(str).str[:4]
        overall_dir = 1 if float(pct_valid.median()) > 0.5 else -1
        for year, gy in pp.groupby("year"):
            med = float(gy["pct"].median())
            same_dir = (med > 0.5 and overall_dir > 0) or (med < 0.5 and overall_dir < 0)
            if same_dir:
                year_consistency_count += 1
            by_year[str(year)] = {
                "event_n": int(len(gy)),
                "median_percentile": med,
                "same_direction_as_overall": bool(same_dir),
            }

    return {
        "event_n": int(len(ev)),
        "control_n": int(len(co)),
        "event_median": float(ev["value"].median()) if len(ev) else None,
        "control_median": float(co["value"].median()) if len(co) else None,
        "median_event_percentile_vs_same_day_controls": (
            float(pct_valid.median()) if len(pct_valid) else None
        ),
        "mean_event_percentile_vs_same_day_controls": (
            float(pct_valid.mean()) if len(pct_valid) else None
        ),
        "event_top_quartile_share": (
            float((pct_valid >= 0.75).mean()) if len(pct_valid) else None
        ),
        "event_bottom_quartile_share": (
            float((pct_valid <= 0.25).mean()) if len(pct_valid) else None
        ),
        "date_equal_dates": int(len(date_diff)),
        "date_equal_median_diff": (
            float(date_diff.median()) if len(date_diff) else None
        ),
        "date_equal_positive_share": (
            float((date_diff > 0).mean()) if len(date_diff) else None
        ),
        "year_consistency_count": int(year_consistency_count),
        "by_year": by_year,
    }


def _candidate_flag(summary: dict) -> bool:
    """Exploratory shortlist gate, not a strategy gate."""
    p = summary.get("median_event_percentile_vs_same_day_controls")
    n = summary.get("event_n", 0)
    yc = summary.get("year_consistency_count", 0)
    if p is None:
        return False
    return bool(n >= 500 and abs(float(p) - 0.5) >= 0.10 and yc >= 4)


def evaluate_transition_study(
    panel: pd.DataFrame,
    label_col: str = "TREND25_60_label",
) -> tuple[dict, pd.DataFrame]:
    x = add_episode_onsets(panel, label_col=label_col)
    x = add_event_window_features(x)

    pre_metrics = transition_metric_columns()
    levels = level_columns()
    posts = post_columns()

    # Pre-onset cohort needs T-20..T. Full trajectory additionally needs T+10.
    pre_required = [f"{f}__t-20" for f in FEATURES] + [f"{f}__t+0" for f in FEATURES]
    full_required = [f"{f}__t-20" for f in FEATURES] + [f"{f}__t+10" for f in FEATURES]
    x["_pre_window_ok"] = x[pre_required].notna().all(axis=1)
    x["_full_window_ok"] = x[full_required].notna().all(axis=1)

    lab = pd.to_numeric(x[label_col], errors="coerce")
    events_pre = x[x["trend_episode_onset"] & x["_pre_window_ok"]].copy()
    controls_pre = x[lab.eq(0) & x["_pre_window_ok"]].copy()

    events_full = x[x["trend_episode_onset"] & x["_full_window_ok"]].copy()
    controls_full = x[lab.eq(0) & x["_full_window_ok"]].copy()

    metric_summaries = {}
    for m in pre_metrics:
        metric_summaries[m] = _summarize_metric(events_pre, controls_pre, m)

    # Levels and post-onset behavior use full cohort for complete comparability.
    level_summaries = {m: _summarize_metric(events_full, controls_full, m) for m in levels}
    post_summaries = {m: _summarize_metric(events_full, controls_full, m) for m in posts}

    shortlist = []
    for m, s in metric_summaries.items():
        if _candidate_flag(s):
            p = float(s["median_event_percentile_vs_same_day_controls"])
            shortlist.append({
                "metric": m,
                "direction": "HIGHER_AT_ONSET" if p > 0.5 else "LOWER_AT_ONSET",
                "median_percentile": p,
                "distance_from_neutral": abs(p - 0.5),
                "year_consistency_count": int(s["year_consistency_count"]),
                "event_n": int(s["event_n"]),
                "research_effect": "SHORTLIST_FOR_NEW_INDEPENDENT_MECHANISM_ONLY",
            })
    shortlist.sort(key=lambda r: (-r["distance_from_neutral"], -r["year_consistency_count"], r["metric"]))

    event_export_cols = [
        "trade_date", "symbol", label_col, "trend_episode_onset",
        *[c for c in pre_metrics if c in events_pre.columns],
        *[c for c in levels if c in events_pre.columns],
    ]
    event_export = events_pre[event_export_cols].copy()

    result = {
        "definition": {
            "primary_episode_label": label_col,
            "episode_onset": "first row of each contiguous positive TREND25_60 episode",
            "offsets_sessions": list(OFFSETS),
            "features": list(FEATURES),
            "control": "same-date rows with TREND25_60_label=0",
            "pre_onset_metrics": [
                "delta T-20->T", "delta T-10->T", "delta T-5->T",
                "curve_5=(T-T-5)-(T-5-T-10)",
                "curve_10=(T-T-10)-(T-10-T-20)",
            ],
            "future_offsets": "T+5/T+10 descriptive only; never candidate inputs",
            "percentile": "event value empirical percentile versus same-date label=0 controls",
            "date_equal": "per-date event median minus control median, then equal-weight dates",
            "shortlist_gate": (
                "exploratory only: event_n>=500, |median percentile-0.5|>=0.10, "
                "same direction in >=4 calendar years"
            ),
            "not_strategy_qualification": True,
        },
        "cohort": {
            "panel_rows": int(len(x)),
            "episodes_total": int(x["trend_episode_onset"].sum()),
            "events_pre_window": int(len(events_pre)),
            "controls_pre_window": int(len(controls_pre)),
            "events_full_window": int(len(events_full)),
            "controls_full_window": int(len(controls_full)),
            "event_dates_pre_window": int(events_pre["trade_date"].nunique()),
        },
        "transition_metrics": metric_summaries,
        "level_trajectory": level_summaries,
        "post_onset_descriptive": post_summaries,
        "transition_shortlist": shortlist,
    }
    return result, event_export
