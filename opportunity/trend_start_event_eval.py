from __future__ import annotations

import numpy as np
import pandas as pd


def _contiguous_runs(mask: pd.Series) -> list[tuple[int, int]]:
    """Return inclusive [start,end] positional runs where mask is True."""
    a = mask.fillna(False).astype(bool).to_numpy()
    out = []
    start = None
    for i, v in enumerate(a):
        if v and start is None:
            start = i
        elif (not v) and start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(a) - 1))
    return out


def _symbol_event_tables(
    g: pd.DataFrame,
    label_col: str,
    signal_col: str,
    early_window_sessions: int,
):
    g = g.sort_values("trade_date").reset_index(drop=True).copy()

    valid = g[label_col].notna()
    positive = valid & (g[label_col].fillna(0).astype(int) == 1)
    signal = g[signal_col].map(lambda v: bool(v) if pd.notna(v) else False)

    # Episode = one contiguous run of favorable-path daily labels.
    episodes = []
    for eid, (s, e) in enumerate(_contiguous_runs(positive), 1):
        onset = s
        early_end = min(e, onset + early_window_sessions)
        episodes.append({
            "episode_id_local": eid,
            "onset_pos": onset,
            "end_pos": e,
            "early_end_pos": early_end,
            "onset_date": str(g.loc[onset, "trade_date"]),
            "end_date": str(g.loc[e, "trade_date"]),
            "episode_rows": int(e - s + 1),
        })

    # Signal run = one contiguous active period of the candidate detector.
    signal_runs = []
    for rid, (s, e) in enumerate(_contiguous_runs(signal), 1):
        signal_runs.append({
            "signal_run_id_local": rid,
            "start_pos": s,
            "end_pos": e,
            "start_date": str(g.loc[s, "trade_date"]),
            "end_date": str(g.loc[e, "trade_date"]),
            "signal_rows": int(e - s + 1),
        })

    # Greedy one-to-one matching by earliest episode.
    used_runs = set()
    matches = []
    for ep in episodes:
        candidates = []
        for j, run in enumerate(signal_runs):
            if j in used_runs:
                continue
            # Run counts as an early alert if it overlaps onset ... onset+W.
            if run["end_pos"] >= ep["onset_pos"] and run["start_pos"] <= ep["early_end_pos"]:
                candidates.append((j, run))
        if candidates:
            j, run = sorted(candidates, key=lambda x: x[1]["start_pos"])[0]
            used_runs.add(j)
            matches.append({
                "episode_id_local": ep["episode_id_local"],
                "signal_run_id_local": run["signal_run_id_local"],
                "onset_date": ep["onset_date"],
                "signal_start_date": run["start_date"],
                "offset_sessions": int(run["start_pos"] - ep["onset_pos"]),
            })

    return episodes, signal_runs, matches, g


def reconstruct_frozen_v01_conditions(panel: pd.DataFrame) -> pd.DataFrame:
    """Reconstruct only missing frozen V0.1 condition booleans.

    Exact frozen thresholds:
      c_rs_accel: rs_accel > 0
      c_vol_compress: vol_compression <= 0.85
      c_volume_contract: volume_contraction <= 0.90
      c_near_breakout: distance_to_60d_high in [-0.05, 0]
      c_higher_low: higher_low_20_vs_60 >= 0.02

    Existing boolean condition columns are preserved unchanged.
    """
    x = panel.copy()

    specs = {
        "c_rs_accel": (
            {"rs_accel"},
            lambda d: d["rs_accel"] > 0.0,
        ),
        "c_vol_compress": (
            {"vol_compression"},
            lambda d: d["vol_compression"] <= 0.85,
        ),
        "c_volume_contract": (
            {"volume_contraction"},
            lambda d: d["volume_contraction"] <= 0.90,
        ),
        "c_near_breakout": (
            {"distance_to_60d_high"},
            lambda d: d["distance_to_60d_high"].between(-0.05, 0.0, inclusive="both"),
        ),
        "c_higher_low": (
            {"higher_low_20_vs_60"},
            lambda d: d["higher_low_20_vs_60"] >= 0.02,
        ),
    }

    for c, (required, fn) in specs.items():
        if c in x.columns:
            continue
        missing = required - set(x.columns)
        if missing:
            raise ValueError(
                f"cannot reconstruct frozen V0.1 condition {c}; missing={sorted(missing)}"
            )
        x[c] = fn(x).fillna(False).astype(bool)

    return x


def evaluate_event_onsets(
    panel: pd.DataFrame,
    label_col: str = "TREND25_60_label",
    signal_col: str = "trend_ignition_v01",
    score_col: str = "ignition_score_0_5",
    condition_cols: tuple[str, ...] = (
        "c_rs_accel",
        "c_vol_compress",
        "c_volume_contract",
        "c_near_breakout",
        "c_higher_low",
    ),
    early_window_sessions: int = 10,
) -> dict:
    panel = reconstruct_frozen_v01_conditions(panel)

    required = {"symbol", "trade_date", label_col, signal_col}
    missing = required - set(panel.columns)
    if missing:
        raise ValueError(f"missing event-eval columns: {sorted(missing)}")

    episode_rows = []
    run_rows = []
    match_rows = []
    onset_feature_rows = []

    for symbol, g in panel.groupby("symbol", sort=False):
        episodes, runs, matches, gs = _symbol_event_tables(
            g, label_col, signal_col, early_window_sessions
        )
        for x in episodes:
            x["symbol"] = symbol
            episode_rows.append(x)
            onset_feature_rows.append(gs.loc[x["onset_pos"]].to_dict())
        for x in runs:
            x["symbol"] = symbol
            run_rows.append(x)
        for x in matches:
            x["symbol"] = symbol
            match_rows.append(x)

    eps = pd.DataFrame(episode_rows)
    runs = pd.DataFrame(run_rows)
    matches = pd.DataFrame(match_rows)
    onsets = pd.DataFrame(onset_feature_rows)

    n_ep = len(eps)
    n_runs = len(runs)
    matched_ep = len(matches)
    matched_runs = matches[["symbol", "signal_run_id_local"]].drop_duplicates().shape[0] if len(matches) else 0

    recall = matched_ep / n_ep if n_ep else None
    precision = matched_runs / n_runs if n_runs else None

    score_diag = {}
    if score_col in panel.columns and score_col in onsets.columns and len(onsets):
        base = panel.loc[panel[label_col].notna(), score_col].dropna()
        onset = onsets[score_col].dropna()
        for k in sorted(set(base.astype(int).tolist()) | set(onset.astype(int).tolist())):
            b = float((base.astype(int) == k).mean()) if len(base) else None
            o = float((onset.astype(int) == k).mean()) if len(onset) else None
            score_diag[str(k)] = {
                "all_valid_rows_share": b,
                "episode_onsets_share": o,
                "onset_vs_all_ratio": (o / b) if b not in (None, 0) and o is not None else None,
            }

    cond_diag = {}
    if len(onsets):
        valid_panel = panel[panel[label_col].notna()]
        for c in condition_cols:
            if c not in panel.columns or c not in onsets.columns:
                continue
            b = float(valid_panel[c].map(lambda v: bool(v) if pd.notna(v) else False).mean())
            o = float(onsets[c].map(lambda v: bool(v) if pd.notna(v) else False).mean())
            cond_diag[c] = {
                "all_valid_rows_rate": b,
                "episode_onsets_rate": o,
                "onset_vs_all_ratio": (o / b) if b > 0 else None,
            }

    by_year = {}
    if len(eps):
        eps2 = eps.copy()
        eps2["year"] = eps2["onset_date"].astype(str).str[:4]
        if len(matches):
            mkeys = set(zip(matches["symbol"], matches["episode_id_local"]))
        else:
            mkeys = set()
        for year, gy in eps2.groupby("year"):
            total = len(gy)
            hit = sum((r.symbol, r.episode_id_local) in mkeys for r in gy.itertuples())
            by_year[str(year)] = {
                "episodes": int(total),
                "matched_early": int(hit),
                "event_recall": float(hit / total) if total else None,
            }

    offsets = matches["offset_sessions"] if len(matches) else pd.Series(dtype=float)

    return {
        "definition": {
            "episode": "contiguous run of row-level favorable-path labels",
            "onset": "first row of each episode",
            "signal_event": "contiguous run where detector is active",
            "match": f"signal run overlaps episode onset through onset+{early_window_sessions} sessions",
            "matching": "one signal run may match at most one episode",
            "purpose": "diagnostic/event-level ruler; does not rescue rejected V0.1 mechanism",
        },
        "event_metrics": {
            "episodes": int(n_ep),
            "signal_runs": int(n_runs),
            "matched_episodes": int(matched_ep),
            "matched_signal_runs": int(matched_runs),
            "event_recall": float(recall) if recall is not None else None,
            "event_precision": float(precision) if precision is not None else None,
            "median_signal_offset_sessions": float(offsets.median()) if len(offsets) else None,
        },
        "score_diagnostic": score_diag,
        "condition_diagnostic": cond_diag,
        "by_onset_year": by_year,
    }
