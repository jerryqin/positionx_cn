from __future__ import annotations

import numpy as np
import pandas as pd

FEATURES = (
    "rs20",
    "rs_accel",
    "vol_compression",
    "volume_contraction",
    "distance_to_60d_high",
    "higher_low_20_vs_60",
)
OFFSETS = (-20, -10, -5, 0, 5, 10)


def _mark_episodes(panel: pd.DataFrame, label_col: str) -> pd.DataFrame:
    required = {"symbol", "trade_date", label_col}
    missing = required - set(panel.columns)
    if missing:
        raise ValueError(f"missing episode columns: {sorted(missing)}")

    x = panel.sort_values(["symbol", "trade_date"]).copy()
    lab = pd.to_numeric(x[label_col], errors="coerce")
    x["_positive"] = lab.eq(1)

    prev = x.groupby("symbol", sort=False)["_positive"].shift(1)
    prev_bool = prev.map(lambda v: bool(v) if pd.notna(v) else False)
    x["_episode_start"] = x["_positive"] & ~prev_bool

    # Per-symbol cumulative episode number; non-positive rows get NA episode key.
    x["_episode_num"] = x.groupby("symbol", sort=False)["_episode_start"].cumsum()
    x["_episode_key"] = np.where(
        x["_positive"],
        x["symbol"].astype(str) + "#" + x["_episode_num"].astype(int).astype(str),
        None,
    )
    x["_symbol_pos"] = x.groupby("symbol", sort=False).cumcount()
    return x


def extract_episode_anchors(
    panel: pd.DataFrame,
    label_col: str = "TREND25_60_label",
    mfe_col: str = "TREND25_60_mfe",
) -> pd.DataFrame:
    """One row per favorable-path episode with two ex-post anchors.

    boundary anchor:
      first row where the row-level future-path label flips into a positive run.

    max-MFE anchor:
      row inside the same positive run with maximum ex-post MFE, earliest date on ties.
      This is a diagnostic 'best convexity inside the already-qualified episode',
      not a tradable signal and not a new label threshold.
    """
    x = _mark_episodes(panel, label_col)
    if mfe_col not in x.columns:
        raise ValueError(f"missing MFE column: {mfe_col}")

    pos = x[x["_positive"]].copy()
    rows = []
    for key, g in pos.groupby("_episode_key", sort=False):
        g = g.sort_values("trade_date")
        b = g.iloc[0]

        mfe = pd.to_numeric(g[mfe_col], errors="coerce")
        if mfe.notna().any():
            max_mfe = mfe.max()
            candidates = g[mfe.eq(max_mfe)].sort_values("trade_date")
            m = candidates.iloc[0]
        else:
            m = b

        rows.append({
            "episode_key": key,
            "symbol": str(b["symbol"]),
            "episode_start_date": str(b["trade_date"]),
            "episode_end_date": str(g.iloc[-1]["trade_date"]),
            "episode_rows": int(len(g)),
            "boundary_date": str(b["trade_date"]),
            "boundary_symbol_pos": int(b["_symbol_pos"]),
            "max_mfe_date": str(m["trade_date"]),
            "max_mfe_symbol_pos": int(m["_symbol_pos"]),
            "max_mfe_value": float(pd.to_numeric(m[mfe_col], errors="coerce"))
                             if pd.notna(pd.to_numeric(m[mfe_col], errors="coerce"))
                             else None,
            "max_mfe_offset_from_boundary": int(m["_symbol_pos"] - b["_symbol_pos"]),
        })
    return pd.DataFrame(rows)


def _window_values(panel: pd.DataFrame, anchors: pd.DataFrame, anchor_date_col: str) -> pd.DataFrame:
    x = panel.sort_values(["symbol", "trade_date"]).copy()
    for f in FEATURES:
        if f not in x.columns:
            raise ValueError(f"missing feature: {f}")

    x["_symbol_pos"] = x.groupby("symbol", sort=False).cumcount()
    lookup = {}
    for _, r in x[["symbol", "_symbol_pos", "trade_date", *FEATURES]].iterrows():
        lookup[(str(r["symbol"]), int(r["_symbol_pos"]))] = r

    date_to_pos = {}
    for _, r in x[["symbol", "_symbol_pos", "trade_date"]].iterrows():
        date_to_pos[(str(r["symbol"]), str(r["trade_date"]))] = int(r["_symbol_pos"])

    out = []
    for a in anchors.itertuples(index=False):
        date = str(getattr(a, anchor_date_col))
        pos = date_to_pos.get((str(a.symbol), date))
        if pos is None:
            continue
        rec = {
            "episode_key": a.episode_key,
            "symbol": str(a.symbol),
            "anchor_date": date,
        }
        for f in FEATURES:
            for off in OFFSETS:
                rr = lookup.get((str(a.symbol), pos + off))
                rec[f"{f}__t{off:+d}"] = rr[f] if rr is not None else np.nan
            rec[f"{f}__d_m5_0"] = rec[f"{f}__t+0"] - rec[f"{f}__t-5"]
            rec[f"{f}__post_d_0_10"] = rec[f"{f}__t+10"] - rec[f"{f}__t+0"]
        out.append(rec)
    return pd.DataFrame(out)


def _same_day_percentile(events: pd.DataFrame, controls: pd.DataFrame, metric: str) -> pd.Series:
    out = pd.Series(np.nan, index=events.index, dtype=float)
    cg = {}
    for d, g in controls.groupby("trade_date", sort=False):
        a = pd.to_numeric(g[metric], errors="coerce").dropna().to_numpy(float)
        if len(a):
            cg[str(d)] = np.sort(a)

    for d, g in events.groupby("anchor_date", sort=False):
        a = cg.get(str(d))
        if a is None or len(a) == 0:
            continue
        vals = pd.to_numeric(g[metric], errors="coerce")
        valid = vals.notna()
        if not valid.any():
            continue
        v = vals[valid].to_numpy(float)
        left = np.searchsorted(a, v, side="left")
        right = np.searchsorted(a, v, side="right")
        out.loc[vals[valid].index] = (left + right) / (2.0 * len(a))
    return out


def _attach_control_metrics(panel: pd.DataFrame) -> pd.DataFrame:
    """Create only the metrics needed for same-date controls."""
    x = panel.sort_values(["symbol", "trade_date"]).copy()
    gb = x.groupby("symbol", sort=False)
    for f in FEATURES:
        x[f"{f}__t-5"] = gb[f].shift(5)
        x[f"{f}__t+10"] = gb[f].shift(-10)
        x[f"{f}__t+0"] = x[f]
        x[f"{f}__d_m5_0"] = x[f"{f}__t+0"] - x[f"{f}__t-5"]
        x[f"{f}__post_d_0_10"] = x[f"{f}__t+10"] - x[f"{f}__t+0"]
    return x


def _trajectory_summary(events: pd.DataFrame, controls: pd.DataFrame) -> dict:
    result = {}
    for f in FEATURES:
        fr = {}
        for off in OFFSETS:
            metric = f"{f}__t{off:+d}"
            # Controls only have -5,0,+10. Other offsets are summarized without percentile.
            vals = pd.to_numeric(events[metric], errors="coerce")
            item = {
                "event_n": int(vals.notna().sum()),
                "event_median": float(vals.median()) if vals.notna().any() else None,
            }
            if off in (-5, 0, 10):
                cmetric = metric
                if cmetric in controls.columns:
                    pct = _same_day_percentile(events, controls, metric).dropna()
                    item["median_percentile_vs_same_day_controls"] = (
                        float(pct.median()) if len(pct) else None
                    )
            fr[f"t{off:+d}"] = item

        for metric_name in (f"{f}__d_m5_0", f"{f}__post_d_0_10"):
            vals = pd.to_numeric(events[metric_name], errors="coerce")
            pct = _same_day_percentile(events, controls, metric_name).dropna()
            fr[metric_name.split("__", 1)[1]] = {
                "event_n": int(vals.notna().sum()),
                "event_median": float(vals.median()) if vals.notna().any() else None,
                "median_percentile_vs_same_day_controls": (
                    float(pct.median()) if len(pct) else None
                ),
            }
        result[f] = fr
    return result


def audit_episode_anchors(
    panel: pd.DataFrame,
    label_col: str = "TREND25_60_label",
    mfe_col: str = "TREND25_60_mfe",
) -> tuple[dict, pd.DataFrame]:
    required = {"symbol", "trade_date", label_col, mfe_col, *FEATURES}
    missing = required - set(panel.columns)
    if missing:
        raise ValueError(f"missing anchor-audit columns: {sorted(missing)}")

    anchors = extract_episode_anchors(panel, label_col=label_col, mfe_col=mfe_col)
    boundary = _window_values(panel, anchors, "boundary_date")
    best = _window_values(panel, anchors, "max_mfe_date")

    control_panel = _attach_control_metrics(panel)
    lab = pd.to_numeric(control_panel[label_col], errors="coerce")
    controls = control_panel[lab.eq(0)].copy()

    offsets = pd.to_numeric(anchors["max_mfe_offset_from_boundary"], errors="coerce")
    by_year = {}
    tmp = anchors.copy()
    tmp["year"] = tmp["boundary_date"].astype(str).str[:4]
    for year, g in tmp.groupby("year"):
        o = pd.to_numeric(g["max_mfe_offset_from_boundary"], errors="coerce")
        by_year[str(year)] = {
            "episodes": int(len(g)),
            "median_offset_sessions": float(o.median()),
            "same_day_share": float((o == 0).mean()),
            "within_2_sessions_share": float((o <= 2).mean()),
            "within_5_sessions_share": float((o <= 5).mean()),
        }

    result = {
        "semantics": {
            "boundary_anchor": "first positive row of the contiguous TREND25_60 favorable-path episode",
            "max_mfe_anchor": "within the same positive episode, row with maximum TREND25_60_mfe; earliest date breaks ties",
            "purpose": (
                "Test whether the old 'onset' is merely an eligibility-boundary artifact. "
                "No new detector, no new threshold, no Strategy qualification."
            ),
        },
        "cohort": {
            "episodes": int(len(anchors)),
            "boundary_windows": int(len(boundary)),
            "max_mfe_windows": int(len(best)),
        },
        "anchor_offset": {
            "median_sessions": float(offsets.median()),
            "p25_sessions": float(offsets.quantile(0.25)),
            "p75_sessions": float(offsets.quantile(0.75)),
            "same_day_share": float((offsets == 0).mean()),
            "within_2_sessions_share": float((offsets <= 2).mean()),
            "within_5_sessions_share": float((offsets <= 5).mean()),
            "by_boundary_year": by_year,
        },
        "boundary_anchor_trajectory": _trajectory_summary(boundary, controls),
        "max_mfe_anchor_trajectory": _trajectory_summary(best, controls),
        "interpretation_contract": (
            "If the sharp pre-T weakening / post-T recovery exists mainly at the boundary anchor "
            "but materially weakens at max-MFE anchor, treat it as label-boundary artifact. "
            "If it persists at both anchors, it is evidence that favorable episodes themselves "
            "cluster around pullback/reversal structures; that still does not make it a tradable detector."
        ),
    }
    export = anchors.merge(
        boundary.add_prefix("boundary_"),
        left_on="episode_key",
        right_on="boundary_episode_key",
        how="left",
    ).merge(
        best.add_prefix("maxmfe_"),
        left_on="episode_key",
        right_on="maxmfe_episode_key",
        how="left",
    )
    return result, export
