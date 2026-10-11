
from __future__ import annotations
import numpy as np
import pandas as pd

def add_leader_propagation_features(
    panel: pd.DataFrame,
    sector_min_members: int = 10,
    leader_rank_min: float = 0.80,
    follower_rank_min: float = 0.40,
    follower_rank_max: float = 0.70,
    impulse_delta_sessions: int = 5,
    history_sessions: int = 126,
    ignition_quantile: float = 0.75,
) -> pd.DataFrame:
    x = panel.copy()
    eligible = x["sector_key"].notna() & x["ret20"].notna()

    x["sector_ret20_rank_d5"] = np.nan
    x.loc[eligible, "sector_ret20_rank_d5"] = (
        x.loc[eligible]
        .groupby(["trade_date", "sector_key"])["ret20"]
        .rank(method="average", pct=True)
    )

    base = (
        x.loc[eligible]
        .groupby(["trade_date", "sector_key"], as_index=False)
        .agg(
            sector_members_d5=("symbol", "size"),
            sector_ret20_median=("ret20", "median"),
        )
    )

    leaders = x.loc[
        eligible & (x["sector_ret20_rank_d5"] >= float(leader_rank_min))
    ].copy()

    leader_daily = (
        leaders
        .groupby(["trade_date", "sector_key"], as_index=False)
        .agg(
            leader_count=("symbol", "size"),
            leader_ret20_mean=("ret20", "mean"),
        )
    )

    daily = (
        base.merge(
            leader_daily,
            on=["trade_date", "sector_key"],
            how="left",
            validate="one_to_one",
        )
        .sort_values(["sector_key", "trade_date"])
    )

    daily["leader_spread20"] = (
        daily["leader_ret20_mean"] - daily["sector_ret20_median"]
    )
    daily["leader_impulse5"] = (
        daily.groupby("sector_key")["leader_spread20"]
        .transform(lambda s: s - s.shift(int(impulse_delta_sessions)))
    )
    daily["leader_impulse_prior_q75"] = (
        daily.groupby("sector_key")["leader_impulse5"]
        .transform(
            lambda s: s.shift(1)
            .rolling(int(history_sessions), min_periods=int(history_sessions))
            .quantile(float(ignition_quantile))
        )
    )
    daily["leader_ignition"] = (
        (daily["sector_members_d5"] >= int(sector_min_members))
        & daily["leader_impulse5"].notna()
        & daily["leader_impulse_prior_q75"].notna()
        & (daily["leader_impulse5"] > daily["leader_impulse_prior_q75"])
    )

    x = x.merge(
        daily[
            [
                "trade_date", "sector_key", "sector_members_d5",
                "sector_ret20_median", "leader_count", "leader_ret20_mean",
                "leader_spread20", "leader_impulse5",
                "leader_impulse_prior_q75", "leader_ignition",
            ]
        ],
        on=["trade_date", "sector_key"],
        how="left",
        validate="many_to_one",
    )

    x["follower_setup"] = (
        x["sector_key"].notna()
        & (x["sector_members_d5"] >= int(sector_min_members))
        & (x["sector_ret20_rank_d5"] >= float(follower_rank_min))
        & (x["sector_ret20_rank_d5"] < float(follower_rank_max))
    )
    x["d5_signal"] = x["follower_setup"] & x["leader_ignition"].fillna(False)
    return x

def _by_year(df: pd.DataFrame, label_col: str) -> dict:
    y = pd.to_numeric(df[label_col], errors="coerce")
    valid = df[y.notna()].copy()
    valid["_label"] = y[y.notna()].astype(int).to_numpy()
    valid["year"] = valid["trade_date"].astype(str).str[:4]
    out = {}
    for year, g in valid.groupby("year"):
        setup = g[g["follower_setup"]]
        sig = g[g["d5_signal"]]
        if len(setup) == 0:
            continue
        sr = float(setup["_label"].mean())
        rr = float(sig["_label"].mean()) if len(sig) else None
        lift = rr / sr if rr is not None and sr > 0 else None
        delta = rr - sr if rr is not None else None
        out[str(year)] = {
            "valid_rows": int(len(g)),
            "setup_count": int(len(setup)),
            "signal_count": int(len(sig)),
            "setup_precision": sr,
            "signal_precision": rr,
            "lift_vs_setup": lift,
            "precision_minus_setup": delta,
            "signal_days": int(sig["trade_date"].nunique()) if len(sig) else 0,
        }
    return out

def evaluate_d5(
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

    setup = valid[valid["follower_setup"]].copy()
    sig = valid[valid["d5_signal"]].copy()

    market_rate = float(valid["_label"].mean()) if len(valid) else None
    setup_rate = float(setup["_label"].mean()) if len(setup) else None
    sig_rate = float(sig["_label"].mean()) if len(sig) else None
    lift = sig_rate / setup_rate if sig_rate is not None and setup_rate and setup_rate > 0 else None
    delta = sig_rate - setup_rate if sig_rate is not None and setup_rate is not None else None

    by_year = _by_year(valid, label_col)
    positive_years = sum(
        1 for r in by_year.values()
        if r["lift_vs_setup"] is not None
        and r["precision_minus_setup"] is not None
        and r["lift_vs_setup"] > 1.0
        and r["precision_minus_setup"] > 0.0
    )

    signal_days = int(sig["trade_date"].nunique()) if len(sig) else 0
    avg_signals = float(len(sig) / signal_days) if signal_days else None
    p90_daily = float(sig.groupby("trade_date").size().quantile(0.90)) if signal_days else None

    gate = {
        "selected_min": {"threshold": int(selected_min), "actual": int(len(sig)), "pass": bool(len(sig) >= selected_min)},
        "lift_vs_setup_min": {"threshold": float(lift_min), "actual": lift, "pass": bool(lift is not None and lift >= lift_min)},
        "precision_minus_setup_min": {"threshold": float(precision_delta_min), "actual": delta, "pass": bool(delta is not None and delta >= precision_delta_min)},
        "positive_years_min": {"threshold": int(positive_years_min), "actual": int(positive_years), "pass": bool(positive_years >= positive_years_min)},
    }
    passed = all(v["pass"] for v in gate.values())

    return {
        "all_valid_rows": int(len(valid)),
        "all_market_rate": market_rate,
        "setup_count": int(len(setup)),
        "setup_rate": setup_rate,
        "signal_count": int(len(sig)),
        "signal_rate": sig_rate,
        "lift_vs_setup": lift,
        "precision_minus_setup": delta,
        "signal_share_of_setup": float(len(sig) / len(setup)) if len(setup) else None,
        "signal_days": signal_days,
        "avg_signals_per_signal_day": avg_signals,
        "p90_signals_per_signal_day": p90_daily,
        "positive_years": int(positive_years),
        "by_year": by_year,
        "gate": gate,
        "operational_flow_note": "5-20 names/day is downstream; not part of the scientific gate.",
        "verdict": (
            "PASS_D5_LEADER_LAGGARD_PROPAGATION_V0_1_TO_ROBUSTNESS"
            if passed else
            "FAST_REJECT_D5_LEADER_LAGGARD_PROPAGATION_V0_1"
        ),
    }
