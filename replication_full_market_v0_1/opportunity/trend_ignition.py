from __future__ import annotations

import numpy as np
import pandas as pd


def _safe_ret(s: pd.Series, n: int) -> pd.Series:
    return s / s.shift(n) - 1.0


def _atr(g: pd.DataFrame) -> pd.Series:
    prev_close = g["close"].shift(1)
    a = (g["high"] - g["low"]).abs()
    b = (g["high"] - prev_close).abs()
    c = (g["low"] - prev_close).abs()
    return pd.concat([a, b, c], axis=1).max(axis=1)


def synthetic_adjust_ohlc(g: pd.DataFrame) -> pd.DataFrame:
    """Convert raw/unadjusted OHLC to a corporate-action-safe synthetic price state.

    PositionX convention:
      daily_factor = close / pre_close
      adj_close = cumulative product(daily_factor)
      adj_open/high/low = adj_close * raw_open/high/low / raw_close

    The resulting level is arbitrary; only relative changes/structure matter.
    Fail closed on non-positive or missing prices in the requested research window.
    """
    required = {"open", "high", "low", "close", "pre_close"}
    missing = required - set(g.columns)
    if missing:
        raise ValueError(f"missing adjusted-state columns: {sorted(missing)}")

    x = g.sort_values("trade_date").copy()
    for c in required:
        x[c] = pd.to_numeric(x[c], errors="coerce")

    invalid = (
        x["close"].isna() | x["pre_close"].isna()
        | (x["close"] <= 0) | (x["pre_close"] <= 0)
        | x["open"].isna() | x["high"].isna() | x["low"].isna()
        | (x["open"] <= 0) | (x["high"] <= 0) | (x["low"] <= 0)
    )
    if bool(invalid.any()):
        bad = x.loc[invalid, ["trade_date", "open", "high", "low", "close", "pre_close"]].head(5)
        raise ValueError(f"invalid raw OHLC/pre_close rows: {bad.to_dict('records')}")

    daily_factor = x["close"] / x["pre_close"]
    adj_close = daily_factor.cumprod()

    raw_close = x["close"].copy()
    x["raw_open"] = x["open"]
    x["raw_high"] = x["high"]
    x["raw_low"] = x["low"]
    x["raw_close"] = x["close"]
    x["open"] = adj_close * x["open"] / raw_close
    x["high"] = adj_close * x["high"] / raw_close
    x["low"] = adj_close * x["low"] / raw_close
    x["close"] = adj_close
    x["price_state"] = "SYNTHETIC_ADJ_CLOSE_OVER_PRE_CLOSE"
    return x


def add_symbol_features(g: pd.DataFrame) -> pd.DataFrame:
    g = g.sort_values("trade_date").copy()

    g["ret20"] = _safe_ret(g["close"], 20)
    g["ret60"] = _safe_ret(g["close"], 60)

    # Prior 20-session return ending 20 sessions ago.
    g["ret20_prev"] = g["close"].shift(20) / g["close"].shift(40) - 1.0

    daily_ret = g["close"].pct_change()
    g["vol20"] = daily_ret.rolling(20, min_periods=20).std()
    g["vol60"] = daily_ret.rolling(60, min_periods=60).std()
    g["vol_compression"] = g["vol20"] / g["vol60"]

    atr = _atr(g)
    g["atr20"] = atr.rolling(20, min_periods=20).mean()
    g["atr60"] = atr.rolling(60, min_periods=60).mean()
    g["atr_compression"] = g["atr20"] / g["atr60"]

    g["volume20"] = g["volume"].rolling(20, min_periods=20).mean()
    g["volume60"] = g["volume"].rolling(60, min_periods=60).mean()
    g["volume_contraction"] = g["volume20"] / g["volume60"]

    # Breakout reference uses information available by T close.
    g["high60"] = g["high"].rolling(60, min_periods=60).max()
    g["distance_to_60d_high"] = g["close"] / g["high60"] - 1.0

    # Stabilization proxy: recent lows lifted from the 60d floor.
    g["low20"] = g["low"].rolling(20, min_periods=20).min()
    g["low60"] = g["low"].rolling(60, min_periods=60).min()
    g["higher_low_20_vs_60"] = g["low20"] / g["low60"] - 1.0

    return g


def add_benchmark_relative_features(panel: pd.DataFrame,
                                    benchmark: pd.DataFrame) -> pd.DataFrame:
    """Add RS20 acceleration relative to the benchmark.

    RS20 = stock 20d return - benchmark 20d return
    RS20_prev = stock prior-20d return - benchmark prior-20d return
    RS acceleration = RS20 - RS20_prev
    """
    b = benchmark.sort_values("trade_date").copy()
    b["bench_ret20"] = _safe_ret(b["close"], 20)
    b["bench_ret20_prev"] = b["close"].shift(20) / b["close"].shift(40) - 1.0
    b = b[["trade_date", "bench_ret20", "bench_ret20_prev"]].drop_duplicates("trade_date")

    x = panel.merge(b, on="trade_date", how="left", validate="many_to_one")
    x["rs20"] = x["ret20"] - x["bench_ret20"]
    x["rs20_prev"] = x["ret20_prev"] - x["bench_ret20_prev"]
    x["rs_accel"] = x["rs20"] - x["rs20_prev"]
    return x


def add_trend_ignition_signal(panel: pd.DataFrame) -> pd.DataFrame:
    """Predeclared V0.1 mechanism test.

    Conditions are intentionally simple and fixed:
      A: RS acceleration > 0
      B: vol20/vol60 <= 0.85
      C: volume20/volume60 <= 0.90
      D: close is within 5% of 60d high
      E: 20d low is at least 2% above the 60d low

    Candidate = at least 4 of 5 conditions.

    This is a Fast-Reject research rule, not a deployable strategy.
    """
    x = panel.copy()
    conds = {
        "c_rs_accel": x["rs_accel"] > 0.0,
        "c_vol_compress": x["vol_compression"] <= 0.85,
        "c_volume_contract": x["volume_contraction"] <= 0.90,
        "c_near_breakout": x["distance_to_60d_high"].between(-0.05, 0.0, inclusive="both"),
        "c_higher_low": x["higher_low_20_vs_60"] >= 0.02,
    }
    for name, s in conds.items():
        x[name] = s.fillna(False)
    cols = list(conds)
    x["ignition_score_0_5"] = x[cols].astype(int).sum(axis=1)
    x["trend_ignition_v01"] = x["ignition_score_0_5"] >= 4
    return x
