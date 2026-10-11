from __future__ import annotations

import numpy as np
import pandas as pd

SHOCK_DIMS = ("ret1", "intraday_return", "close_location", "range_vs_prior20")
CONTINUOUS_OUTCOMES = (
    "gap_1",
    "fwd_close_ret_1",
    "fwd_close_ret_2",
    "fwd_close_ret_3",
    "fwd_close_ret_5",
    "next1_intraday_return",
    "next1_close_location",
)
BOOLEAN_OUTCOMES = (
    "close_above_t_close_1",
    "close_above_t_high_1",
    "close_above_t_high_3",
)


def synthetic_adjust_ohlc(g: pd.DataFrame) -> pd.DataFrame:
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


def add_sequence_features(g: pd.DataFrame) -> pd.DataFrame:
    """T shock features + descriptive T+1..T+5 recovery outcomes."""
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

    t_close = x["adj_close"]
    t_high = x["adj_high"]

    x["gap_1"] = x["adj_open"].shift(-1) / t_close - 1.0
    for h in (1, 2, 3, 5):
        x[f"fwd_close_ret_{h}"] = x["adj_close"].shift(-h) / t_close - 1.0

    x["next1_intraday_return"] = (
        x["adj_close"].shift(-1) / x["adj_open"].shift(-1) - 1.0
    )
    next1_range = (x["adj_high"].shift(-1) - x["adj_low"].shift(-1)).replace(0, np.nan)
    x["next1_close_location"] = (
        x["adj_close"].shift(-1) - x["adj_low"].shift(-1)
    ) / next1_range

    x["close_above_t_close_1"] = x["adj_close"].shift(-1) > t_close
    x["close_above_t_high_1"] = x["adj_close"].shift(-1) > t_high

    next3_close_max = pd.concat(
        [x["adj_close"].shift(-h) for h in (1, 2, 3)],
        axis=1,
    ).max(axis=1)
    x["close_above_t_high_3"] = next3_close_max > t_high
    return x


def boundary_events(panel: pd.DataFrame, label_col: str = "TREND25_60_label") -> pd.DataFrame:
    x = panel.sort_values(["symbol", "trade_date"]).copy()
    lab = pd.to_numeric(x[label_col], errors="coerce")
    pos = lab.eq(1)
    prev = pos.groupby(x["symbol"], sort=False).shift(1)
    prev_bool = prev.map(lambda v: bool(v) if pd.notna(v) else False)
    onset = pos & ~prev_bool
    return x.loc[onset, ["trade_date", "symbol"]].copy()


def attach_same_day_ranks(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    for c in SHOCK_DIMS:
        x[f"{c}__rank"] = x.groupby("trade_date")[c].rank(
            method="average", pct=True
        )
    return x


def nearest_control_matches(
    events: pd.DataFrame,
    controls: pd.DataFrame,
    k: int = 5,
) -> pd.DataFrame:
    """Same-day nearest neighbors in four T-known shock-rank dimensions.

    Controls may be reused across events. No future outcome enters matching.
    """
    rank_cols = [f"{c}__rank" for c in SHOCK_DIMS]
    rows = []

    controls_by_date = {str(d): g.copy() for d, g in controls.groupby("trade_date", sort=False)}
    for d, eg in events.groupby("trade_date", sort=False):
        cg = controls_by_date.get(str(d))
        if cg is None or cg.empty:
            continue
        cvalid = cg[rank_cols].notna().all(axis=1)
        cg = cg.loc[cvalid].copy()
        if cg.empty:
            continue
        cm = cg[rank_cols].to_numpy(float)

        for ei, er in eg.iterrows():
            ev = er[rank_cols]
            if ev.isna().any():
                continue
            v = ev.to_numpy(float)
            dist = np.sqrt(((cm - v) ** 2).sum(axis=1))
            kk = min(k, len(cg))
            take = np.argpartition(dist, kk - 1)[:kk] if kk < len(cg) else np.arange(len(cg))
            take = take[np.argsort(dist[take])]
            for rank, pos in enumerate(take, 1):
                cr = cg.iloc[int(pos)]
                rows.append({
                    "event_index": int(ei),
                    "event_trade_date": str(er["trade_date"]),
                    "event_symbol": str(er["symbol"]),
                    "control_trade_date": str(cr["trade_date"]),
                    "control_symbol": str(cr["symbol"]),
                    "neighbor_rank": int(rank),
                    "shock_rank_distance": float(dist[int(pos)]),
                })
    return pd.DataFrame(rows)


def _event_control_means(
    matches: pd.DataFrame,
    rows: pd.DataFrame,
    outcome: str,
) -> pd.DataFrame:
    lookup = rows.set_index(["trade_date", "symbol"])[outcome]

    rec = []
    for event_index, mg in matches.groupby("event_index", sort=False):
        erow = rows.loc[event_index]
        ev = erow[outcome]
        vals = []
        for r in mg.itertuples(index=False):
            key = (str(r.control_trade_date), str(r.control_symbol))
            try:
                cv = lookup.loc[key]
            except KeyError:
                continue
            if pd.notna(cv):
                vals.append(float(cv))
        if pd.isna(ev) or not vals:
            continue
        rec.append({
            "event_index": int(event_index),
            "trade_date": str(erow["trade_date"]),
            "symbol": str(erow["symbol"]),
            "event_value": float(ev),
            "matched_control_mean": float(np.mean(vals)),
            "pair_effect": float(ev) - float(np.mean(vals)),
        })
    return pd.DataFrame(rec)


def summarize_continuous_matched(
    matches: pd.DataFrame,
    rows: pd.DataFrame,
    outcome: str,
) -> dict:
    p = _event_control_means(matches, rows, outcome)
    if p.empty:
        return {
            "event_n": 0,
            "median_event": None,
            "median_matched_control": None,
            "median_pair_effect": None,
            "event_win_rate": None,
            "positive_effect_years": 0,
            "by_year": {},
        }

    p["year"] = p["trade_date"].str[:4]
    by_year = {}
    positive_years = 0
    for y, g in p.groupby("year"):
        med = float(g["pair_effect"].median())
        win = float((g["pair_effect"] > 0).mean())
        pos = med > 0
        positive_years += int(pos)
        by_year[str(y)] = {
            "event_n": int(len(g)),
            "median_pair_effect": med,
            "event_win_rate": win,
            "median_event": float(g["event_value"].median()),
            "median_matched_control": float(g["matched_control_mean"].median()),
            "positive_median_effect": bool(pos),
        }

    return {
        "event_n": int(len(p)),
        "median_event": float(p["event_value"].median()),
        "median_matched_control": float(p["matched_control_mean"].median()),
        "median_pair_effect": float(p["pair_effect"].median()),
        "mean_pair_effect": float(p["pair_effect"].mean()),
        "event_win_rate": float((p["pair_effect"] > 0).mean()),
        "positive_effect_years": int(positive_years),
        "by_year": by_year,
    }


def summarize_boolean_matched(
    matches: pd.DataFrame,
    rows: pd.DataFrame,
    outcome: str,
) -> dict:
    p = _event_control_means(matches, rows, outcome)
    if p.empty:
        return {
            "event_n": 0,
            "event_rate": None,
            "matched_control_rate": None,
            "lift": None,
            "positive_lift_years": 0,
            "by_year": {},
        }

    # event_value is 0/1; matched_control_mean is neighbor rate.
    p["year"] = p["trade_date"].str[:4]
    er = float(p["event_value"].mean())
    cr = float(p["matched_control_mean"].mean())
    lift = er / cr if cr > 0 else None

    by_year = {}
    pos_years = 0
    for y, g in p.groupby("year"):
        ger = float(g["event_value"].mean())
        gcr = float(g["matched_control_mean"].mean())
        glift = ger / gcr if gcr > 0 else None
        pos = glift is not None and glift > 1.0
        pos_years += int(pos)
        by_year[str(y)] = {
            "event_n": int(len(g)),
            "event_rate": ger,
            "matched_control_rate": gcr,
            "lift": glift,
            "lift_gt_1": bool(pos),
        }

    return {
        "event_n": int(len(p)),
        "event_rate": er,
        "matched_control_rate": cr,
        "lift": lift,
        "positive_lift_years": int(pos_years),
        "by_year": by_year,
    }


def matching_diagnostics(
    matches: pd.DataFrame,
    ranked_rows: pd.DataFrame,
) -> dict:
    if matches.empty:
        return {"matched_events": 0}
    lookup = ranked_rows.set_index(["trade_date", "symbol"])
    first = matches[matches["neighbor_rank"] == 1]
    diffs = {c: [] for c in SHOCK_DIMS}
    for r in first.itertuples(index=False):
        e = lookup.loc[(str(r.event_trade_date), str(r.event_symbol))]
        c = lookup.loc[(str(r.control_trade_date), str(r.control_symbol))]
        for d in SHOCK_DIMS:
            diffs[d].append(abs(float(e[f"{d}__rank"]) - float(c[f"{d}__rank"])))
    return {
        "matched_events": int(matches["event_index"].nunique()),
        "match_rows": int(len(matches)),
        "neighbors_per_event_target": 5,
        "nearest_distance_median": float(first["shock_rank_distance"].median()),
        "nearest_distance_p90": float(first["shock_rank_distance"].quantile(0.90)),
        "nearest_abs_rank_diff_median": {
            d: float(np.median(v)) if v else None for d, v in diffs.items()
        },
    }


def evaluate_capitulation_recovery(
    panel: pd.DataFrame,
    sequence_rows: pd.DataFrame,
    label_col: str = "TREND25_60_label",
) -> tuple[dict, pd.DataFrame]:
    key = ["trade_date", "symbol"]
    needed = set(key) | set(SHOCK_DIMS) | set(CONTINUOUS_OUTCOMES) | set(BOOLEAN_OUTCOMES)
    missing = needed - set(sequence_rows.columns)
    if missing:
        raise ValueError(f"sequence rows missing columns: {sorted(missing)}")

    base = panel[key + [label_col]].merge(
        sequence_rows[list(needed)],
        on=key,
        how="left",
        validate="one_to_one",
    )
    base = attach_same_day_ranks(base)

    anchors = boundary_events(panel, label_col=label_col)
    event_keys = set(zip(anchors["trade_date"].astype(str), anchors["symbol"].astype(str)))
    event_mask = [
        (str(d), str(s)) in event_keys
        for d, s in zip(base["trade_date"], base["symbol"])
    ]
    events = base.loc[event_mask].copy()
    lab = pd.to_numeric(base[label_col], errors="coerce")
    controls = base.loc[lab.eq(0)].copy()

    valid_shock = [f"{c}__rank" for c in SHOCK_DIMS]
    events = events[events[valid_shock].notna().all(axis=1)].copy()
    controls = controls[controls[valid_shock].notna().all(axis=1)].copy()

    matches = nearest_control_matches(events, controls, k=5)
    diag = matching_diagnostics(matches, base)

    continuous = {
        o: summarize_continuous_matched(matches, base, o)
        for o in CONTINUOUS_OUTCOMES
    }
    boolean = {
        o: summarize_boolean_matched(matches, base, o)
        for o in BOOLEAN_OUTCOMES
    }

    cont_candidates = []
    for o, r in continuous.items():
        if (
            r["event_n"] >= 500
            and r["median_pair_effect"] is not None
            and r["median_pair_effect"] > 0
            and r["event_win_rate"] is not None
            and r["event_win_rate"] >= 0.60
            and r["positive_effect_years"] >= 5
        ):
            cont_candidates.append({
                "outcome": o,
                "median_pair_effect": r["median_pair_effect"],
                "event_win_rate": r["event_win_rate"],
                "positive_effect_years": r["positive_effect_years"],
                "research_effect": "CANDIDATE_RECOVERY_CONFIRMATION_FOR_FORMAL_D1_TEST",
            })

    bool_candidates = []
    for o, r in boolean.items():
        if (
            r["event_n"] >= 500
            and r["event_rate"] is not None
            and r["event_rate"] >= 0.05
            and r["lift"] is not None
            and r["lift"] >= 1.50
            and r["positive_lift_years"] >= 5
        ):
            bool_candidates.append({
                "outcome": o,
                "event_rate": r["event_rate"],
                "matched_control_rate": r["matched_control_rate"],
                "lift": r["lift"],
                "positive_lift_years": r["positive_lift_years"],
                "research_effect": "CANDIDATE_RECOVERY_CONFIRMATION_FOR_FORMAL_D1_TEST",
            })

    return {
        "definition": {
            "event": "TREND25_60 favorable-path episode boundary",
            "control_pool": "same-date TREND25_60_label=0 PIT rows",
            "matching": (
                "5 nearest same-day controls in percentile-rank space of T-known "
                "ret1, intraday_return, close_location, range_vs_prior20"
            ),
            "control_reuse": True,
            "future_metrics": "diagnostic only; never used in matching",
            "continuous_candidate_gate": (
                "event_n>=500, median matched effect>0, win rate>=60%, "
                "positive median effect in >=5 years"
            ),
            "boolean_candidate_gate": (
                "event_n>=500, event rate>=5%, matched lift>=1.50, "
                "lift>1 in >=5 years"
            ),
            "not_strategy_qualification": True,
        },
        "cohort": {
            "events_available": int(len(events)),
            "controls_available": int(len(controls)),
            "event_dates": int(events["trade_date"].nunique()),
        },
        "matching_diagnostics": diag,
        "continuous_recovery": continuous,
        "boolean_recovery": boolean,
        "continuous_confirmation_candidates": cont_candidates,
        "boolean_confirmation_candidates": bool_candidates,
        "interpretation_contract": (
            "This study separates shock intensity from subsequent recovery. "
            "Any candidate only motivates a separately pre-registered D1 detector. "
            "A formal trigger at T+1 close must recompute its outcome label from T+2 open; "
            "the original T-based TREND25_60 label may not be reused for qualification."
        ),
    }, matches
