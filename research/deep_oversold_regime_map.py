from __future__ import annotations

from typing import Any, Dict, Mapping, Sequence

import math
import numpy as np
import pandas as pd


FOLDS = (
    ("F1", "20200803", "20221017"),
    ("F2", "20221018", "20240919"),
    ("F3", "20240920", "20260924"),
)


def norm_date(v: Any) -> str:
    s = "".join(ch for ch in str(v) if ch.isdigit())
    if len(s) < 8:
        raise ValueError(f"invalid date {v!r}")
    return s[:8]


def _trimmed_mean(x: pd.Series, frac: float = 0.05) -> float | None:
    y = pd.to_numeric(x, errors="coerce").dropna().sort_values()
    if len(y) == 0:
        return None
    k = int(len(y) * frac)
    if k > 0 and len(y) > 2*k:
        y = y.iloc[k:-k]
    return float(y.mean())


def classify_panic_breadth(v: float) -> str:
    if v < 0.01:
        return "ISOLATED_LT_1PCT"
    if v < 0.05:
        return "BROAD_1_TO_5PCT"
    return "SYSTEMIC_GE_5PCT"


def classify_trend(close: float, ma60: float | None) -> str:
    if ma60 is None or not math.isfinite(float(ma60)):
        return "UNKNOWN"
    return "ABOVE_MA60" if float(close) >= float(ma60) else "BELOW_MA60"


def classify_breadth(v: float | None) -> str:
    if v is None or not math.isfinite(float(v)):
        return "UNKNOWN"
    return "BREADTH_GE_50PCT" if float(v) >= 0.50 else "BREADTH_LT_50PCT"


def classify_vol(rv20: float | None, trailing_median: float | None) -> str:
    if (
        rv20 is None or trailing_median is None
        or not math.isfinite(float(rv20))
        or not math.isfinite(float(trailing_median))
    ):
        return "UNKNOWN"
    return "HIGH_VOL" if float(rv20) > float(trailing_median) else "LOW_VOL"


def compute_daily_state(
    histories: Mapping[str, pd.DataFrame],
    membership: pd.DataFrame,
    benchmark: pd.DataFrame,
    market_dates: Sequence[str],
) -> pd.DataFrame:
    """
    All state features are observable by T close.

    No future return, future membership, or full-sample threshold is used.
    Volatility state compares RV20 with its own trailing 252-session median.
    """
    member_map = {
        d: set(g["symbol"].astype(str))
        for d,g in membership.groupby("signal_date", sort=True)
    }

    # Precompute stock-level T-observable features.
    ret20 = {}
    above60 = {}
    for symbol, df in histories.items():
        if df is None or df.empty or "close" not in df.columns:
            continue
        c = pd.to_numeric(df["close"], errors="coerce")
        ret20[symbol] = c / c.shift(20) - 1.0
        ma60 = c.rolling(60, min_periods=60).mean()
        above60[symbol] = c >= ma60

    bc = pd.to_numeric(benchmark["close"], errors="coerce")
    bma60 = bc.rolling(60, min_periods=60).mean()
    bret = bc.pct_change(fill_method=None)
    brv20 = bret.rolling(20, min_periods=20).std(ddof=1) * math.sqrt(252)
    # T-observable: today's RV20 can be compared with trailing history including T.
    brv_median252 = brv20.rolling(252, min_periods=126).median()

    rows = []
    for d in market_dates:
        members = member_map.get(d, set())
        vals = []
        ma_flags = []
        for s in members:
            rs = ret20.get(s)
            if rs is not None and d in rs.index and pd.notna(rs.at[d]):
                vals.append(float(rs.at[d]))
            ms = above60.get(s)
            if ms is not None and d in ms.index and pd.notna(ms.at[d]):
                ma_flags.append(bool(ms.at[d]))

        panic = float(np.mean(np.array(vals) <= -0.25)) if vals else np.nan
        breadth60 = float(np.mean(ma_flags)) if ma_flags else np.nan
        dispersion = float(np.std(vals, ddof=1)) if len(vals) >= 2 else np.nan
        bclose = float(bc.at[d]) if d in bc.index and pd.notna(bc.at[d]) else np.nan
        ma = float(bma60.at[d]) if d in bma60.index and pd.notna(bma60.at[d]) else np.nan
        rv = float(brv20.at[d]) if d in brv20.index and pd.notna(brv20.at[d]) else np.nan
        rvmed = (
            float(brv_median252.at[d])
            if d in brv_median252.index and pd.notna(brv_median252.at[d])
            else np.nan
        )
        rows.append({
            "trade_date": d,
            "members": len(members),
            "ret20_observations": len(vals),
            "panic_breadth": panic,
            "breadth_above_ma60": breadth60,
            "ret20_dispersion": dispersion,
            "benchmark_close": bclose,
            "benchmark_ma60": ma,
            "benchmark_rv20": rv,
            "benchmark_rv20_trailing252_median": rvmed,
            "panic_regime": classify_panic_breadth(panic) if math.isfinite(panic) else "UNKNOWN",
            "trend_regime": classify_trend(bclose, ma),
            "breadth_regime": classify_breadth(breadth60),
            "vol_regime": classify_vol(rv, rvmed),
        })
    return pd.DataFrame(rows)


def event_stats(x: pd.DataFrame, value_col: str) -> Dict[str, Any]:
    y = pd.to_numeric(x[value_col], errors="coerce")
    valid = x.loc[y.notna()].copy()
    y = y.dropna()
    if len(y) == 0:
        return {
            "events":0, "signal_dates":0, "avg":None, "median":None,
            "trim5":None, "win_rate":None, "date_equal_avg":None,
        }
    by_date = valid.groupby("signal_date")[value_col].mean()
    return {
        "events": int(len(y)),
        "signal_dates": int(valid["signal_date"].nunique()),
        "avg": float(y.mean()),
        "median": float(y.median()),
        "trim5": _trimmed_mean(y, 0.05),
        "win_rate": float((y > 0).mean()),
        "date_equal_avg": float(by_date.mean()),
    }


def summarize_dimension(
    events: pd.DataFrame,
    dimension: str,
    value_col: str,
) -> list[Dict[str, Any]]:
    out = []
    for regime, g in events.groupby(dimension, dropna=False, sort=True):
        row = {
            "dimension": dimension,
            "regime": str(regime),
            **event_stats(g, value_col),
            "folds": [],
        }
        for name,start,end in FOLDS:
            z = g[(g["signal_date"] >= start) & (g["signal_date"] <= end)]
            st = event_stats(z, value_col)
            row["folds"].append({
                "fold":name,
                "events":st["events"],
                "date_equal_avg":st["date_equal_avg"],
                "avg":st["avg"],
            })
        out.append(row)
    return out


def summarize_joint(
    events: pd.DataFrame,
    dims: Sequence[str],
    value_col: str,
    min_events: int = 30,
) -> list[Dict[str, Any]]:
    out = []
    for key,g in events.groupby(list(dims), dropna=False, sort=True):
        if not isinstance(key, tuple):
            key = (key,)
        if len(g) < min_events:
            continue
        row = {d:str(v) for d,v in zip(dims,key)}
        row.update(event_stats(g, value_col))
        out.append(row)
    return out
