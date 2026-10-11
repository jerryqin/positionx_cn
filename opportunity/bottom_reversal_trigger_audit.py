from __future__ import annotations

import numpy as np
import pandas as pd

CONTINUOUS = (
    "ret1",
    "prior5_return",
    "intraday_return",
    "close_location",
    "lower_wick_share",
    "upper_wick_share",
    "range_vs_prior20",
    "volume_vs_prior20",
    "recovery_from_low",
    "distance_to_prior20_low",
)

BOOLEAN = (
    "failed_breakdown_5d",
    "failed_breakdown_20d",
    "bullish_reversal_day",
    "lower_wick_dominant",
)


def synthetic_adjust_ohlc(g: pd.DataFrame) -> pd.DataFrame:
    """Corporate-action-safe synthetic OHLC using PositionX close/pre_close convention."""
    x = g.sort_values("trade_date").copy()
    req = ["open", "high", "low", "close", "pre_close"]
    for c in req:
        x[c] = pd.to_numeric(x[c], errors="coerce")
    valid = np.logical_and.reduce([(x[c] > 0) & x[c].notna() for c in req])
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


def add_trigger_features(g: pd.DataFrame) -> pd.DataFrame:
    """Features known by T close. No forward inputs."""
    x = synthetic_adjust_ohlc(g)
    if x.empty:
        return x

    prev_close = x["adj_close"].shift(1)
    x["ret1"] = x["adj_close"] / prev_close - 1.0
    x["prior5_return"] = x["adj_close"].shift(1) / x["adj_close"].shift(6) - 1.0
    x["intraday_return"] = x["adj_close"] / x["adj_open"] - 1.0

    day_range = (x["adj_high"] - x["adj_low"]).replace(0, np.nan)
    x["close_location"] = (x["adj_close"] - x["adj_low"]) / day_range
    x["lower_wick_share"] = (
        np.minimum(x["adj_open"], x["adj_close"]) - x["adj_low"]
    ) / day_range
    x["upper_wick_share"] = (
        x["adj_high"] - np.maximum(x["adj_open"], x["adj_close"])
    ) / day_range
    x["recovery_from_low"] = x["adj_close"] / x["adj_low"] - 1.0

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

    vol = pd.to_numeric(x["volume"], errors="coerce")
    prior20_vol = vol.shift(1).rolling(20, min_periods=15).median()
    x["volume_vs_prior20"] = vol / prior20_vol.replace(0, np.nan)

    prior5_low = x["adj_low"].shift(1).rolling(5, min_periods=5).min()
    prior20_low = x["adj_low"].shift(1).rolling(20, min_periods=15).min()
    x["distance_to_prior20_low"] = x["adj_close"] / prior20_low - 1.0

    x["failed_breakdown_5d"] = (
        (x["adj_low"] < prior5_low) & (x["adj_close"] > prior5_low)
    )
    x["failed_breakdown_20d"] = (
        (x["adj_low"] < prior20_low) & (x["adj_close"] > prior20_low)
    )

    x["bullish_reversal_day"] = (
        (x["ret1"] > 0)
        & (x["intraday_return"] > 0)
        & (x["close_location"] >= 0.67)
    )
    x["lower_wick_dominant"] = (
        (x["lower_wick_share"] > x["upper_wick_share"])
        & (x["close_location"] >= 0.5)
    )
    return x


def build_boundary_anchors(
    panel: pd.DataFrame,
    label_col: str = "TREND25_60_label",
) -> pd.DataFrame:
    x = panel.sort_values(["symbol", "trade_date"]).copy()
    lab = pd.to_numeric(x[label_col], errors="coerce")
    pos = lab.eq(1)
    prev = pos.groupby(x["symbol"], sort=False).shift(1)
    prev_bool = prev.map(lambda v: bool(v) if pd.notna(v) else False)
    onset = pos & ~prev_bool
    return x.loc[onset, ["trade_date", "symbol"]].copy()


def _same_day_percentiles(
    events: pd.DataFrame, controls: pd.DataFrame, metric: str
) -> pd.Series:
    out = pd.Series(np.nan, index=events.index, dtype=float)
    control_map = {}
    for d, g in controls.groupby("trade_date", sort=False):
        a = pd.to_numeric(g[metric], errors="coerce").dropna().to_numpy(float)
        if len(a):
            control_map[str(d)] = np.sort(a)

    for d, g in events.groupby("trade_date", sort=False):
        a = control_map.get(str(d))
        if a is None or len(a) == 0:
            continue
        vals = pd.to_numeric(g[metric], errors="coerce")
        ok = vals.notna()
        if not ok.any():
            continue
        v = vals[ok].to_numpy(float)
        left = np.searchsorted(a, v, side="left")
        right = np.searchsorted(a, v, side="right")
        out.loc[vals[ok].index] = (left + right) / (2.0 * len(a))
    return out


def summarize_continuous(
    events: pd.DataFrame, controls: pd.DataFrame, metric: str
) -> dict:
    e = pd.to_numeric(events[metric], errors="coerce")
    c = pd.to_numeric(controls[metric], errors="coerce")
    ev = events.loc[e.notna()].copy()
    co = controls.loc[c.notna()].copy()
    pct = _same_day_percentiles(ev, co, metric).dropna()

    by_year = {}
    overall_dir = None
    if len(pct):
        overall_dir = 1 if float(pct.median()) > 0.5 else -1
        tmp = ev.loc[pct.index, ["trade_date"]].copy()
        tmp["pct"] = pct
        tmp["year"] = tmp["trade_date"].astype(str).str[:4]
        for y, gy in tmp.groupby("year"):
            med = float(gy["pct"].median())
            same = (med > 0.5 and overall_dir > 0) or (med < 0.5 and overall_dir < 0)
            by_year[str(y)] = {
                "event_n": int(len(gy)),
                "median_percentile": med,
                "same_direction_as_overall": bool(same),
            }

    date_equal = []
    em = ev.groupby("trade_date")[metric].median()
    cm = co.groupby("trade_date")[metric].median()
    for d in em.index.intersection(cm.index):
        if pd.notna(em.loc[d]) and pd.notna(cm.loc[d]):
            date_equal.append(float(em.loc[d] - cm.loc[d]))

    return {
        "event_n": int(e.notna().sum()),
        "control_n": int(c.notna().sum()),
        "event_median": float(e.median()) if e.notna().any() else None,
        "control_median": float(c.median()) if c.notna().any() else None,
        "median_event_percentile_vs_same_day_controls": (
            float(pct.median()) if len(pct) else None
        ),
        "mean_event_percentile_vs_same_day_controls": (
            float(pct.mean()) if len(pct) else None
        ),
        "event_top_quartile_share": float((pct >= 0.75).mean()) if len(pct) else None,
        "event_bottom_quartile_share": float((pct <= 0.25).mean()) if len(pct) else None,
        "date_equal_dates": int(len(date_equal)),
        "date_equal_median_diff": float(np.median(date_equal)) if date_equal else None,
        "year_consistency_count": int(
            sum(v["same_direction_as_overall"] for v in by_year.values())
        ),
        "by_year": by_year,
    }


def summarize_boolean(
    events: pd.DataFrame, controls: pd.DataFrame, metric: str
) -> dict:
    e = events[metric].map(lambda v: bool(v) if pd.notna(v) else False)
    c = controls[metric].map(lambda v: bool(v) if pd.notna(v) else False)
    er = float(e.mean()) if len(e) else None
    cr = float(c.mean()) if len(c) else None
    lift = er / cr if cr and cr > 0 else None

    by_year = {}
    years_positive = 0
    for y, gy in events.assign(year=events["trade_date"].astype(str).str[:4]).groupby("year"):
        cd = controls[controls["trade_date"].astype(str).str[:4] == str(y)]
        if len(cd) == 0:
            continue
        ger = float(gy[metric].map(lambda v: bool(v) if pd.notna(v) else False).mean())
        gcr = float(cd[metric].map(lambda v: bool(v) if pd.notna(v) else False).mean())
        glift = ger / gcr if gcr > 0 else None
        positive = glift is not None and glift > 1.0
        years_positive += int(positive)
        by_year[str(y)] = {
            "event_n": int(len(gy)),
            "event_rate": ger,
            "control_rate": gcr,
            "lift": glift,
            "lift_gt_1": positive,
        }

    return {
        "event_n": int(len(events)),
        "control_n": int(len(controls)),
        "event_rate": er,
        "control_rate": cr,
        "lift": lift,
        "positive_lift_years": int(years_positive),
        "by_year": by_year,
    }


def evaluate_bottom_reversal_trigger_audit(
    panel: pd.DataFrame,
    trigger_rows: pd.DataFrame,
    label_col: str = "TREND25_60_label",
) -> tuple[dict, pd.DataFrame]:
    """Compare T-known trigger features at favorable-path boundaries vs same-day negatives."""
    key = ["trade_date", "symbol"]
    needed = set(key) | set(CONTINUOUS) | set(BOOLEAN)
    missing = needed - set(trigger_rows.columns)
    if missing:
        raise ValueError(f"trigger rows missing: {sorted(missing)}")

    p = panel[key + [label_col]].copy()
    x = p.merge(trigger_rows[list(needed)], on=key, how="left", validate="one_to_one")
    anchors = build_boundary_anchors(panel, label_col=label_col)
    events = anchors.merge(x, on=key, how="left", validate="one_to_one")
    lab = pd.to_numeric(x[label_col], errors="coerce")
    controls = x[lab.eq(0)].copy()

    continuous = {
        m: summarize_continuous(events, controls, m) for m in CONTINUOUS
    }
    boolean = {
        m: summarize_boolean(events, controls, m) for m in BOOLEAN
    }

    continuous_shortlist = []
    for m, r in continuous.items():
        pctl = r["median_event_percentile_vs_same_day_controls"]
        if (
            r["event_n"] >= 500
            and pctl is not None
            and abs(float(pctl) - 0.5) >= 0.10
            and r["year_consistency_count"] >= 4
        ):
            continuous_shortlist.append({
                "metric": m,
                "direction": "HIGHER_AT_BOUNDARY" if pctl > 0.5 else "LOWER_AT_BOUNDARY",
                "median_percentile": pctl,
                "distance_from_neutral": abs(float(pctl) - 0.5),
                "year_consistency_count": r["year_consistency_count"],
                "research_effect": "SHORTLIST_FOR_D1_MECHANISM_DESIGN_ONLY",
            })
    continuous_shortlist.sort(key=lambda z: -z["distance_from_neutral"])

    boolean_shortlist = []
    for m, r in boolean.items():
        if (
            r["event_n"] >= 500
            and r["event_rate"] is not None
            and r["event_rate"] >= 0.02
            and r["lift"] is not None
            and r["lift"] >= 1.50
            and r["positive_lift_years"] >= 4
        ):
            boolean_shortlist.append({
                "metric": m,
                "event_rate": r["event_rate"],
                "control_rate": r["control_rate"],
                "lift": r["lift"],
                "positive_lift_years": r["positive_lift_years"],
                "research_effect": "SHORTLIST_FOR_D1_MECHANISM_DESIGN_ONLY",
            })
    boolean_shortlist.sort(key=lambda z: -z["lift"])

    export_cols = key + list(CONTINUOUS) + list(BOOLEAN)
    return {
        "definition": {
            "event": "first positive row of a contiguous TREND25_60 favorable-path episode",
            "control": "same-date TREND25_60_label=0 PIT rows",
            "all_trigger_features_known_by": "T close",
            "continuous_shortlist_gate": (
                "event_n>=500, |median same-day percentile-0.5|>=0.10, "
                "same direction in >=4 years"
            ),
            "boolean_shortlist_gate": (
                "event_n>=500, event rate>=2%, lift>=1.50, positive lift in >=4 years"
            ),
            "not_strategy_qualification": True,
        },
        "cohort": {
            "events": int(len(events)),
            "controls": int(len(controls)),
            "event_dates": int(events["trade_date"].nunique()),
        },
        "continuous_metrics": continuous,
        "boolean_patterns": boolean,
        "continuous_shortlist": continuous_shortlist,
        "boolean_shortlist": boolean_shortlist,
        "interpretation_contract": (
            "A shortlist item only motivates a separately pre-registered D1 Bottom Reversal "
            "mechanism. Do not combine features post hoc and do not tune Trend Ignition V0.1."
        ),
    }, events[export_cols].copy()
