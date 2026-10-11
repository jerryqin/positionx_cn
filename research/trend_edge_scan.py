from __future__ import annotations

from typing import Any, Dict, Mapping, Sequence
import math

import numpy as np
import pandas as pd


# Frozen from v0.1. This patch does NOT change the economic hypothesis.
FOLDS = (
    ("F1", "20200803", "20221017"),
    ("F2", "20221018", "20240919"),
    ("F3", "20240920", "20260924"),
)
HORIZONS = (20, 60, 120)
FEATURES = (
    "ret20", "ret60", "ret120",
    "rs20", "rs60",
    "dist_ma20", "dist_ma60",
    "drawdown20",
)

# Data-integrity guardrails, not alpha filters.
# An adjusted established A-share history that jumps >10x or <0.1x
# from one valid observation to the next is treated as broken/reused.
ADJ_GROSS_RATIO_MIN = 0.10
ADJ_GROSS_RATIO_MAX = 10.0

# Endpoint safety net. It is deliberately much wider than any plausible
# 20/60/120-session return for an index constituent. We do not winsorize:
# bad labels become NaN and are counted/reported.
FORWARD_GROSS_RATIO_MIN = 0.01
FORWARD_GROSS_RATIO_MAX = 100.0

# Ratio-style back adjustment is used for discovery returns/features.
# It keeps adjusted levels positive while preserving corporate-action-adjusted
# price ratios. This is a data representation choice, not an alpha parameter.
PRICE_ADJUSTMENT_MODE = "back_ratio"


def fold_of(d: str) -> str | None:
    for name, start, end in FOLDS:
        if start <= d <= end:
            return name
    return None


def add_stock_features(
    df: pd.DataFrame,
    benchmark_close: pd.Series,
) -> pd.DataFrame:
    x = df.copy().sort_index()
    c = pd.to_numeric(x["close"], errors="coerce")

    x["ret20"] = c / c.shift(20) - 1.0
    x["ret60"] = c / c.shift(60) - 1.0
    x["ret120"] = c / c.shift(120) - 1.0

    b20 = benchmark_close / benchmark_close.shift(20) - 1.0
    b60 = benchmark_close / benchmark_close.shift(60) - 1.0
    x["rs20"] = x["ret20"] - b20.reindex(x.index)
    x["rs60"] = x["ret60"] - b60.reindex(x.index)

    x["dist_ma20"] = c / c.rolling(20, min_periods=20).mean() - 1.0
    x["dist_ma60"] = c / c.rolling(60, min_periods=60).mean() - 1.0
    x["drawdown20"] = c / c.rolling(20, min_periods=20).max() - 1.0
    return x



def sanitize_history_prices(df: pd.DataFrame) -> tuple[pd.DataFrame, Dict[str, Any]]:
    """
    Normalize QMT sparse/suspension rows without changing valid prices.

    Accepted sparse forms:
      - one side missing (open or close): kept as NaN; unusable labels/features
        naturally become NaN.
      - open==0 and close==0 on the same row: treated as a suspension/no-price
        row and converted to NaN/NaN.

    Rejected:
      - one side zero while the other is positive;
      - negative/infinite prices;
      - non-numeric non-null price payloads.

    This is data cleaning, not return clipping/winsorization.
    """
    x = df.copy().sort_index()
    raw_open = x["open"]
    raw_close = x["close"]
    op = pd.to_numeric(raw_open, errors="coerce")
    cl = pd.to_numeric(raw_close, errors="coerce")

    bad_parse_open = raw_open.notna() & op.isna()
    bad_parse_close = raw_close.notna() & cl.isna()

    pair_zero = op.eq(0) & cl.eq(0)
    single_zero = (op.eq(0) ^ cl.eq(0))

    # Pair-zero is a common no-trade/suspension encoding. Convert only this
    # symmetric form to missing. A one-sided zero is structurally suspicious.
    op = op.mask(pair_zero)
    cl = cl.mask(pair_zero)

    bad_open_numeric = op.notna() & (~np.isfinite(op) | (op < 0))
    bad_close_numeric = cl.notna() & (~np.isfinite(cl) | (cl < 0))

    x["open"] = op
    x["close"] = cl

    return x, {
        "paired_zero_rows_sanitized": int(pair_zero.sum()),
        "single_zero_rows": int(single_zero.sum()),
        "bad_parse_open_rows": int(bad_parse_open.sum()),
        "bad_parse_close_rows": int(bad_parse_close.sum()),
        "bad_open_numeric_rows": int(bad_open_numeric.sum()),
        "bad_close_numeric_rows": int(bad_close_numeric.sum()),
        "has_structural_price_error": bool(
            single_zero.any()
            or bad_parse_open.any()
            or bad_parse_close.any()
            or bad_open_numeric.any()
            or bad_close_numeric.any()
        ),
    }


def history_integrity(
    df: pd.DataFrame,
    *,
    start_date: str | None = None,
) -> Dict[str, Any]:
    """
    Audit an adjusted price series for catastrophic discontinuities.

    v0.2.1 fixes an over-strict v0.2 rule: a row with only one missing endpoint
    was incorrectly interpreted as a bad price because the earlier check
    required open AND close to be finite/positive whenever either was present.

    Sparse/missing observations are allowed; they simply cannot create a return
    label. True structural errors are still fail-closed.
    """
    required = {"open", "close"}
    if df is None or df.empty or required - set(df.columns):
        return {
            "ok": False,
            "reason": "MISSING_OPEN_CLOSE",
            "paired_zero_rows_sanitized": 0,
            "max_close_gross": None,
            "min_close_gross": None,
            "max_open_prevclose_gross": None,
            "min_open_prevclose_gross": None,
        }

    x = df.copy().sort_index()
    if start_date is not None:
        x = x.loc[x.index >= start_date]
    if x.empty:
        return {
            "ok": False,
            "reason": "EMPTY_ANALYSIS_WINDOW",
            "paired_zero_rows_sanitized": 0,
            "max_close_gross": None,
            "min_close_gross": None,
            "max_open_prevclose_gross": None,
            "min_open_prevclose_gross": None,
        }

    clean, sanitation = sanitize_history_prices(x)
    if sanitation["has_structural_price_error"]:
        reason = (
            "MISMATCHED_ZERO_PRICE"
            if sanitation["single_zero_rows"] > 0
            else "NONNUMERIC_OR_INVALID_PRICE"
        )
        return {
            "ok": False,
            "reason": reason,
            **sanitation,
            "max_close_gross": None,
            "min_close_gross": None,
            "max_open_prevclose_gross": None,
            "min_open_prevclose_gross": None,
        }

    op = pd.to_numeric(clean["open"], errors="coerce")
    cl = pd.to_numeric(clean["close"], errors="coerce")

    # Compare each valid observation with the last valid prior close, even
    # across a suspension/missing row. This preserves the catastrophic-gap
    # defense while allowing sparse QMT rows.
    prev_valid_close = cl.ffill().shift(1)
    close_gross = cl / prev_valid_close
    open_prevclose = op / prev_valid_close

    cg = close_gross.replace([np.inf, -np.inf], np.nan).dropna()
    og = open_prevclose.replace([np.inf, -np.inf], np.nan).dropna()

    bad_close = cg[
        (cg < ADJ_GROSS_RATIO_MIN) | (cg > ADJ_GROSS_RATIO_MAX)
    ]
    bad_open = og[
        (og < ADJ_GROSS_RATIO_MIN) | (og > ADJ_GROSS_RATIO_MAX)
    ]

    reason = None
    if len(bad_close):
        reason = "ABSURD_ADJUSTED_CLOSE_GAP"
    elif len(bad_open):
        reason = "ABSURD_ADJUSTED_OPEN_GAP"

    return {
        "ok": reason is None,
        "reason": reason,
        **sanitation,
        "bad_close_dates": bad_close.index.astype(str).tolist()[:10],
        "bad_open_dates": bad_open.index.astype(str).tolist()[:10],
        "max_close_gross": float(cg.max()) if len(cg) else None,
        "min_close_gross": float(cg.min()) if len(cg) else None,
        "max_open_prevclose_gross": float(og.max()) if len(og) else None,
        "min_open_prevclose_gross": float(og.min()) if len(og) else None,
    }



def quarantine_histories_with_audit(
    histories: Mapping[str, pd.DataFrame],
    *,
    integrity_start: str | None = None,
) -> tuple[Dict[str, pd.DataFrame], list[Dict[str, Any]], list[Dict[str, Any]]]:
    """
    Extended API used by v0.2.2 runner.
    Returns (good_cleaned, quarantined, sparse_sanitation_audit).
    """
    good: Dict[str, pd.DataFrame] = {}
    bad: list[Dict[str, Any]] = []
    sanitation: list[Dict[str, Any]] = []

    for symbol, df in histories.items():
        audit = history_integrity(df, start_date=integrity_start)
        if audit["ok"]:
            clean, info = sanitize_history_prices(df)
            good[str(symbol)] = clean
            if info["paired_zero_rows_sanitized"] > 0:
                sanitation.append({
                    "symbol": str(symbol),
                    **info,
                })
        else:
            bad.append({"symbol": str(symbol), **audit})
    return good, bad, sanitation


def quarantine_histories(
    histories: Mapping[str, pd.DataFrame],
    *,
    integrity_start: str | None = None,
) -> tuple[Dict[str, pd.DataFrame], list[Dict[str, Any]]]:
    """
    Backwards-compatible API retained for existing PositionX tests/callers.
    """
    good, bad, _ = quarantine_histories_with_audit(
        histories,
        integrity_start=integrity_start,
    )
    return good, bad

def attach_forward_excess(
    df: pd.DataFrame,
    benchmark: pd.DataFrame,
    calendar: Sequence[str],
    horizons: Sequence[int] = HORIZONS,
) -> pd.DataFrame:
    """
    Exact label semantics frozen from v0.1:
      feature at T close
      entry at S1/T+1 OPEN
      exit at S20/S60/S120 CLOSE

    The v0.1 arithmetic itself was simple endpoint division, but it lacked a
    catastrophic data-integrity gate. v0.2 keeps the semantics and adds
    non-clipping validity masks.
    """
    x = df.reindex(calendar).copy()
    b = benchmark.reindex(calendar)

    entry = pd.to_numeric(x["open"], errors="coerce").shift(-1)
    bentry = pd.to_numeric(b["open"], errors="coerce").shift(-1)

    for h in horizons:
        exit_ = pd.to_numeric(x["close"], errors="coerce").shift(-h)
        bexit = pd.to_numeric(b["close"], errors="coerce").shift(-h)

        stock_gross = exit_ / entry
        bench_gross = bexit / bentry

        valid = (
            entry.notna() & exit_.notna() & bentry.notna() & bexit.notna()
            & np.isfinite(entry) & np.isfinite(exit_)
            & np.isfinite(bentry) & np.isfinite(bexit)
            & (entry > 0) & (exit_ > 0) & (bentry > 0) & (bexit > 0)
            & stock_gross.between(
                FORWARD_GROSS_RATIO_MIN, FORWARD_GROSS_RATIO_MAX
            )
            & bench_gross.between(
                FORWARD_GROSS_RATIO_MIN, FORWARD_GROSS_RATIO_MAX
            )
        )

        excess = (stock_gross - 1.0) - (bench_gross - 1.0)
        x[f"excess_{h}"] = excess.where(valid)
        raw_observable = (
            entry.notna() & exit_.notna() & bentry.notna() & bexit.notna()
        )
        x[f"label_quality_reject_{h}"] = raw_observable & ~valid

    return x


def assign_quintiles(values: pd.Series) -> pd.Series:
    v = pd.to_numeric(values, errors="coerce")
    out = pd.Series(index=v.index, dtype="object")
    good = v.dropna()
    if len(good) < 50:
        return out

    # Rank first to make duplicated feature values deterministic without
    # looking at future return labels.
    r = good.rank(method="first")
    q = pd.qcut(r, 5, labels=["Q1", "Q2", "Q3", "Q4", "Q5"])
    out.loc[q.index] = q.astype(str)
    return out


def monotonicity_score(qmeans: Dict[str, float]) -> float | None:
    ys = []
    for q in ("Q1", "Q2", "Q3", "Q4", "Q5"):
        v = qmeans.get(q)
        if v is None or not math.isfinite(float(v)):
            return None
        ys.append(float(v))
    ranks = pd.Series(ys).rank(method="average").to_numpy(dtype=float)
    return float(np.corrcoef(np.arange(1, 6, dtype=float), ranks)[0, 1])


def _trimmed_mean(s: pd.Series, frac: float = 0.05) -> float | None:
    x = pd.to_numeric(s, errors="coerce").dropna().sort_values()
    if x.empty:
        return None
    k = int(len(x) * frac)
    if k and len(x) > 2 * k:
        x = x.iloc[k:-k]
    return float(x.mean())


def summarize_daily_buckets(daily: pd.DataFrame) -> Dict[str, Any]:
    qmeans = {
        q: float(daily.loc[daily["bucket"] == q, "mean_excess"].mean())
        for q in ["Q1", "Q2", "Q3", "Q4", "Q5"]
    }
    qmedians = {
        q: float(daily.loc[daily["bucket"] == q, "median_excess"].median())
        for q in ["Q1", "Q2", "Q3", "Q4", "Q5"]
    }
    spread = qmeans["Q5"] - qmeans["Q1"]
    spread_daily = (
        daily.pivot_table(
            index="signal_date",
            columns="bucket",
            values="mean_excess",
            aggfunc="first",
        )
    )
    spread_series = (
        spread_daily["Q5"] - spread_daily["Q1"]
        if {"Q1", "Q5"} <= set(spread_daily.columns)
        else pd.Series(dtype=float)
    )
    return {
        "days": int(daily["signal_date"].nunique()),
        "qmeans": qmeans,
        "qmedians": qmedians,
        "q5_minus_q1": float(spread),
        "q5_minus_q1_daily_median": (
            float(spread_series.median()) if len(spread_series) else None
        ),
        "q5_minus_q1_daily_trim5": _trimmed_mean(spread_series, 0.05),
        "monotonicity": monotonicity_score(qmeans),
    }


def empty_summary() -> Dict[str, Any]:
    return {
        "days": 0,
        "qmeans": {q: 0.0 for q in ["Q1", "Q2", "Q3", "Q4", "Q5"]},
        "qmedians": {q: 0.0 for q in ["Q1", "Q2", "Q3", "Q4", "Q5"]},
        "q5_minus_q1": 0.0,
        "q5_minus_q1_daily_median": None,
        "q5_minus_q1_daily_trim5": None,
        "monotonicity": 0.0,
    }


def fast_reject(feature_result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Frozen v0.1 Fast-Reject gate. No threshold was changed.
    """
    full = feature_result["full_sample"]
    folds = feature_result["folds"]

    full_positive = sum(
        1 for h in HORIZONS if full[str(h)]["q5_minus_q1"] > 0
    )
    mono_good = sum(
        1 for h in HORIZONS
        if (full[str(h)]["monotonicity"] or -9) >= 0.5
    )
    fold_positive = {}
    for h in HORIZONS:
        fold_positive[str(h)] = sum(
            1 for f in ("F1", "F2", "F3")
            if folds[f][str(h)]["q5_minus_q1"] > 0
        )

    candidate = (
        full_positive == 3
        and mono_good >= 2
        and all(v >= 2 for v in fold_positive.values())
    )
    return {
        "full_positive_horizons": full_positive,
        "monotonic_horizons_ge_0_5": mono_good,
        "positive_folds_by_horizon": fold_positive,
        "fast_reject_candidate": bool(candidate),
    }
