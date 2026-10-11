from __future__ import annotations

import numpy as np
import pandas as pd


def synthetic_adjust_close(g: pd.DataFrame) -> pd.DataFrame:
    x = g.sort_values("trade_date").copy()
    for c in ("open", "high", "low", "close", "pre_close"):
        x[c] = pd.to_numeric(x[c], errors="coerce")
    valid = np.logical_and.reduce(
        [(x[c] > 0) & x[c].notna() for c in ("open", "high", "low", "close", "pre_close")]
    )
    x = x.loc[valid].copy()
    if x.empty:
        return x

    factor = x["close"] / x["pre_close"]
    adj_close = factor.cumprod()
    raw_close = x["close"].copy()
    x["adj_open"] = adj_close * x["open"] / raw_close
    x["adj_low"] = adj_close * x["low"] / raw_close
    x["adj_close"] = adj_close

    x["ret20"] = x["adj_close"] / x["adj_close"].shift(20) - 1.0
    x["ma20"] = x["adj_close"].rolling(20, min_periods=20).mean()
    x["above_ma20"] = x["adj_close"] > x["ma20"]
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
    x = g.sort_values("trade_date").reset_index(drop=True).copy()
    n = len(x)

    for prefix in ("D4_TREND25_60", "D4_TREND40_120"):
        x[f"{prefix}_label"] = np.nan
        x[f"{prefix}_mfe"] = np.nan
        x[f"{prefix}_mae_before_hit"] = np.nan
        x[f"{prefix}_time_to_target"] = np.nan

    close = x["adj_close"].to_numpy(float)
    low = x["adj_low"].to_numpy(float)
    open_ = x["adj_open"].to_numpy(float)

    def one(i, horizon, target, mae_floor):
        entry_idx = i + 1
        end_idx = entry_idx + horizon - 1
        if entry_idx >= n or end_idx >= n:
            return None

        entry = open_[entry_idx]
        if not np.isfinite(entry) or entry <= 0:
            return None

        f_close = close[entry_idx:end_idx + 1]
        f_low = low[entry_idx:end_idx + 1]
        if len(f_close) != horizon or not np.isfinite(f_close).all() or not np.isfinite(f_low).all():
            return None

        ret = f_close / entry - 1.0
        mfe = float(np.max(ret))
        hit = np.where(ret >= target)[0]
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
        p = one(i, primary_horizon, primary_target, primary_mae_floor)
        if p is not None:
            x.loc[i, [
                "D4_TREND25_60_label", "D4_TREND25_60_mfe",
                "D4_TREND25_60_mae_before_hit", "D4_TREND25_60_time_to_target",
            ]] = p

        s = one(i, secondary_horizon, secondary_target, secondary_mae_floor)
        if s is not None:
            x.loc[i, [
                "D4_TREND40_120_label", "D4_TREND40_120_mfe",
                "D4_TREND40_120_mae_before_hit", "D4_TREND40_120_time_to_target",
            ]] = s

    return x


def attach_pit_industry(panel: pd.DataFrame, membership: pd.DataFrame) -> pd.DataFrame:
    """Interval-join validated L1 SW membership to PIT rows.

    Interval semantics:
      effective_from <= trade_date < effective_to_exclusive
      NULL effective_to_exclusive means open-ended.

    sector_key includes taxonomy standard to avoid silently merging SW2014 and
    SW2021 identities across the classification revision.
    """
    p = panel.copy().reset_index(drop=True)
    p["_row_id"] = np.arange(len(p), dtype=np.int64)
    p["_date"] = pd.to_datetime(p["trade_date"].astype(str), format="%Y%m%d", errors="coerce")
    p["_symbol"] = p["symbol"].astype(str).str.upper().str.strip()

    m = membership.copy()
    m["_symbol"] = m["symbol"].astype(str).str.upper().str.strip()
    m["_start"] = pd.to_datetime(m["effective_from"], errors="coerce")
    m["_end"] = pd.to_datetime(m["effective_to_exclusive"], errors="coerce")
    m["industry_code"] = m["industry_code"].astype(str)
    m["industry_name"] = m["industry_name"].astype(str)
    m["standard"] = m["standard"].astype(str)
    m["level"] = m["level"].astype(str)

    keep = [
        "_symbol", "_start", "_end",
        "industry_code", "industry_name", "standard", "level",
    ]
    z = p.merge(m[keep], on="_symbol", how="left")
    active = (
        z["_start"].notna()
        & (z["_date"] >= z["_start"])
        & (z["_end"].isna() | (z["_date"] < z["_end"]))
    )
    a = z.loc[active].copy()

    counts = a.groupby("_row_id").size()
    if len(counts) and int(counts.max()) > 1:
        bad = counts[counts > 1].head(10).index.tolist()
        raise RuntimeError(f"ambiguous PIT industry membership for row ids {bad}")

    assigned = a[
        ["_row_id", "industry_code", "industry_name", "standard", "level"]
    ].drop_duplicates("_row_id")

    out = p.merge(assigned, on="_row_id", how="left", validate="one_to_one")
    out["sector_key"] = np.where(
        out["industry_code"].notna(),
        out["standard"].astype(str) + "|" + out["level"].astype(str) + "|" + out["industry_code"].astype(str),
        pd.NA,
    )
    return out.drop(columns=["_row_id", "_date", "_symbol"])


def add_sector_ignition_features(
    panel: pd.DataFrame,
    sector_min_members: int = 10,
    setup_rank_min: float = 0.50,
    setup_rank_max: float = 0.80,
    breadth_delta_sessions: int = 5,
    history_sessions: int = 126,
    ignition_quantile: float = 0.75,
) -> pd.DataFrame:
    """Frozen D4 V0.1: sector-local breadth ignition.

    Baseline stock setup:
      sector-relative RET20 rank in [0.50, 0.80), i.e. participating but not
      already the hottest sector leaders.

    Sector breadth:
      share of PIT sector members above their own MA20.

    Local ignition:
      [sector breadth(T)-sector breadth(T-5)]
        - [market breadth(T)-market breadth(T-5)]
      exceeds that sector's own PRIOR-126-day 75th percentile.

    Current day is excluded from the rolling ignition threshold.
    """
    x = panel.copy()

    eligible = x["sector_key"].notna() & x["ret20"].notna() & x["above_ma20"].notna()

    x["sector_ret20_rank"] = np.nan
    x.loc[eligible, "sector_ret20_rank"] = (
        x.loc[eligible]
        .groupby(["trade_date", "sector_key"])["ret20"]
        .rank(method="average", pct=True)
    )

    sector_daily = (
        x.loc[eligible]
        .groupby(["trade_date", "sector_key"], as_index=False)
        .agg(
            sector_breadth20=("above_ma20", "mean"),
            sector_members=("symbol", "size"),
        )
        .sort_values(["sector_key", "trade_date"])
    )

    market_daily = (
        x.loc[x["above_ma20"].notna()]
        .groupby("trade_date", as_index=False)
        .agg(market_breadth20=("above_ma20", "mean"))
        .sort_values("trade_date")
    )
    market_daily["market_breadth_delta5"] = (
        market_daily["market_breadth20"]
        - market_daily["market_breadth20"].shift(int(breadth_delta_sessions))
    )

    sector_daily["sector_breadth_delta5"] = (
        sector_daily.groupby("sector_key")["sector_breadth20"]
        .transform(lambda s: s - s.shift(int(breadth_delta_sessions)))
    )

    sector_daily = sector_daily.merge(
        market_daily[["trade_date", "market_breadth20", "market_breadth_delta5"]],
        on="trade_date",
        how="left",
        validate="many_to_one",
    )
    sector_daily["sector_excess_breadth_delta5"] = (
        sector_daily["sector_breadth_delta5"]
        - sector_daily["market_breadth_delta5"]
    )

    sector_daily["sector_excess_prior_q75"] = (
        sector_daily.groupby("sector_key")["sector_excess_breadth_delta5"]
        .transform(
            lambda s: s.shift(1)
            .rolling(int(history_sessions), min_periods=int(history_sessions))
            .quantile(float(ignition_quantile))
        )
    )

    sector_daily["sector_ignition"] = (
        (sector_daily["sector_members"] >= int(sector_min_members))
        & sector_daily["sector_excess_breadth_delta5"].notna()
        & sector_daily["sector_excess_prior_q75"].notna()
        & (
            sector_daily["sector_excess_breadth_delta5"]
            > sector_daily["sector_excess_prior_q75"]
        )
    )

    x = x.merge(
        sector_daily[
            [
                "trade_date", "sector_key", "sector_breadth20", "sector_members",
                "sector_breadth_delta5", "market_breadth20", "market_breadth_delta5",
                "sector_excess_breadth_delta5", "sector_excess_prior_q75",
                "sector_ignition",
            ]
        ],
        on=["trade_date", "sector_key"],
        how="left",
        validate="many_to_one",
    )

    x["second_tier_setup"] = (
        x["sector_key"].notna()
        & (x["sector_members"] >= int(sector_min_members))
        & (x["sector_ret20_rank"] >= float(setup_rank_min))
        & (x["sector_ret20_rank"] < float(setup_rank_max))
    )

    x["d4_signal"] = (
        x["second_tier_setup"]
        & x["sector_ignition"].fillna(False)
    )
    return x


def _by_year(df: pd.DataFrame, label_col: str) -> dict:
    y = pd.to_numeric(df[label_col], errors="coerce")
    valid = df[y.notna()].copy()
    valid["_label"] = y[y.notna()].astype(int).to_numpy()
    valid["year"] = valid["trade_date"].astype(str).str[:4]

    out = {}
    for year, g in valid.groupby("year"):
        setup = g[g["second_tier_setup"]]
        sig = g[g["d4_signal"]]
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


def evaluate_d4(
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

    setup = valid[valid["second_tier_setup"]].copy()
    sig = valid[valid["d4_signal"]].copy()

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
    p90_daily = None
    if signal_days:
        daily_counts = sig.groupby("trade_date").size()
        p90_daily = float(daily_counts.quantile(0.90))

    gate = {
        "selected_min": {
            "threshold": int(selected_min), "actual": int(len(sig)),
            "pass": bool(len(sig) >= selected_min),
        },
        "lift_vs_setup_min": {
            "threshold": float(lift_min), "actual": lift,
            "pass": bool(lift is not None and lift >= lift_min),
        },
        "precision_minus_setup_min": {
            "threshold": float(precision_delta_min), "actual": delta,
            "pass": bool(delta is not None and delta >= precision_delta_min),
        },
        "positive_years_min": {
            "threshold": int(positive_years_min), "actual": int(positive_years),
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
        "operational_flow_note": (
            "5-20 names/day is a downstream production target, not part of the frozen scientific Fast Reject gate."
        ),
        "verdict": (
            "PASS_D4_SECTOR_IGNITION_V0_1_TO_ROBUSTNESS"
            if passed
            else "FAST_REJECT_D4_SECTOR_IGNITION_V0_1"
        ),
    }
