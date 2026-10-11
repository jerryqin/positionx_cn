from __future__ import annotations

import numpy as np
import pandas as pd


def add_sector_repricing_features(
    panel: pd.DataFrame,
    sector_min_members: int = 10,
    follower_rank_min: float = 0.40,
    follower_rank_max: float = 0.70,
    impulse_delta_sessions: int = 5,
    history_sessions: int = 126,
    ignition_quantile: float = 0.75,
) -> pd.DataFrame:
    """Frozen D6 V0.1: sector relative repricing ignition.

    Stock baseline:
      0.40 <= sector-relative RET20 rank < 0.70

    Sector state:
      sector_excess_ret20 =
        median RET20 of PIT sector members - median RET20 of PIT market universe

    Repricing impulse:
      sector_repricing_impulse5 =
        sector_excess_ret20(T) - sector_excess_ret20(T-5)

    Ignition:
      impulse(T) > sector's PRIOR-126-sector-day 75th percentile

    Signal:
      follower_setup AND sector_repricing_ignition

    No parameter scan.
    """
    x = panel.copy()

    required = ["trade_date", "symbol", "sector_key", "ret20"]
    missing = [c for c in required if c not in x.columns]
    if missing:
        raise ValueError(f"missing required columns: {missing}")

    eligible = x["sector_key"].notna() & x["ret20"].notna()

    x["sector_ret20_rank_d6"] = np.nan
    x.loc[eligible, "sector_ret20_rank_d6"] = (
        x.loc[eligible]
        .groupby(["trade_date", "sector_key"])["ret20"]
        .rank(method="average", pct=True)
    )

    sector_daily = (
        x.loc[eligible]
        .groupby(["trade_date", "sector_key"], as_index=False)
        .agg(
            sector_members_d6=("symbol", "size"),
            sector_ret20_median_d6=("ret20", "median"),
        )
        .sort_values(["sector_key", "trade_date"])
    )

    market_daily = (
        x.loc[x["ret20"].notna()]
        .groupby("trade_date", as_index=False)
        .agg(market_ret20_median_d6=("ret20", "median"))
        .sort_values("trade_date")
    )

    sector_daily = sector_daily.merge(
        market_daily,
        on="trade_date",
        how="left",
        validate="many_to_one",
    )

    sector_daily["sector_excess_ret20_d6"] = (
        sector_daily["sector_ret20_median_d6"]
        - sector_daily["market_ret20_median_d6"]
    )

    sector_daily["sector_repricing_impulse5"] = (
        sector_daily.groupby("sector_key")["sector_excess_ret20_d6"]
        .transform(lambda s: s - s.shift(int(impulse_delta_sessions)))
    )

    sector_daily["sector_repricing_prior_q75"] = (
        sector_daily.groupby("sector_key")["sector_repricing_impulse5"]
        .transform(
            lambda s: s.shift(1)
            .rolling(
                int(history_sessions),
                min_periods=int(history_sessions),
            )
            .quantile(float(ignition_quantile))
        )
    )

    sector_daily["sector_repricing_ignition"] = (
        (sector_daily["sector_members_d6"] >= int(sector_min_members))
        & sector_daily["sector_repricing_impulse5"].notna()
        & sector_daily["sector_repricing_prior_q75"].notna()
        & (
            sector_daily["sector_repricing_impulse5"]
            > sector_daily["sector_repricing_prior_q75"]
        )
    )

    x = x.merge(
        sector_daily[
            [
                "trade_date", "sector_key",
                "sector_members_d6", "sector_ret20_median_d6",
                "market_ret20_median_d6", "sector_excess_ret20_d6",
                "sector_repricing_impulse5",
                "sector_repricing_prior_q75",
                "sector_repricing_ignition",
            ]
        ],
        on=["trade_date", "sector_key"],
        how="left",
        validate="many_to_one",
    )

    x["follower_setup_d6"] = (
        x["sector_key"].notna()
        & (x["sector_members_d6"] >= int(sector_min_members))
        & (x["sector_ret20_rank_d6"] >= float(follower_rank_min))
        & (x["sector_ret20_rank_d6"] < float(follower_rank_max))
    )

    ignition = x["sector_repricing_ignition"].astype("boolean").fillna(False)
    x["d6_signal"] = x["follower_setup_d6"] & ignition.astype(bool)

    return x


def _by_year(df: pd.DataFrame, label_col: str) -> dict:
    y = pd.to_numeric(df[label_col], errors="coerce")
    valid = df[y.notna()].copy()
    valid["_label"] = y[y.notna()].astype(int).to_numpy()
    valid["year"] = valid["trade_date"].astype(str).str[:4]

    out = {}
    for year, g in valid.groupby("year"):
        setup = g[g["follower_setup_d6"]]
        sig = g[g["d6_signal"]]
        if len(setup) == 0:
            continue

        setup_rate = float(setup["_label"].mean())
        signal_rate = float(sig["_label"].mean()) if len(sig) else None
        lift = signal_rate / setup_rate if signal_rate is not None and setup_rate > 0 else None
        delta = signal_rate - setup_rate if signal_rate is not None else None

        out[str(year)] = {
            "valid_rows": int(len(g)),
            "setup_count": int(len(setup)),
            "signal_count": int(len(sig)),
            "setup_precision": setup_rate,
            "signal_precision": signal_rate,
            "lift_vs_setup": lift,
            "precision_minus_setup": delta,
            "signal_days": int(sig["trade_date"].nunique()) if len(sig) else 0,
        }

    return out


def evaluate_d6(
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

    setup = valid[valid["follower_setup_d6"]].copy()
    sig = valid[valid["d6_signal"]].copy()

    market_rate = float(valid["_label"].mean()) if len(valid) else None
    setup_rate = float(setup["_label"].mean()) if len(setup) else None
    signal_rate = float(sig["_label"].mean()) if len(sig) else None
    lift = (
        signal_rate / setup_rate
        if signal_rate is not None and setup_rate is not None and setup_rate > 0
        else None
    )
    delta = (
        signal_rate - setup_rate
        if signal_rate is not None and setup_rate is not None
        else None
    )

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
    p90_daily = (
        float(sig.groupby("trade_date").size().quantile(0.90))
        if signal_days else None
    )

    gate = {
        "selected_min": {
            "threshold": int(selected_min),
            "actual": int(len(sig)),
            "pass": bool(len(sig) >= selected_min),
        },
        "lift_vs_setup_min": {
            "threshold": float(lift_min),
            "actual": lift,
            "pass": bool(lift is not None and lift >= lift_min),
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

    return {
        "all_valid_rows": int(len(valid)),
        "all_market_rate": market_rate,
        "setup_count": int(len(setup)),
        "setup_rate": setup_rate,
        "signal_count": int(len(sig)),
        "signal_rate": signal_rate,
        "lift_vs_setup": lift,
        "precision_minus_setup": delta,
        "signal_share_of_setup": float(len(sig) / len(setup)) if len(setup) else None,
        "signal_days": signal_days,
        "avg_signals_per_signal_day": avg_signals,
        "p90_signals_per_signal_day": p90_daily,
        "positive_years": int(positive_years),
        "by_year": by_year,
        "gate": gate,
        "operational_flow_note": (
            "5-20 names/day is a downstream target and is not part of the scientific Fast Reject gate."
        ),
        "verdict": (
            "PASS_D6_SECTOR_REPRICING_V0_1_TO_ROBUSTNESS"
            if passed
            else "FAST_REJECT_D6_SECTOR_REPRICING_V0_1"
        ),
    }
