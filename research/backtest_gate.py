from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

import math
import statistics
import pandas as pd

from research.strategy_matching import (
    BreakoutRetestConfig,
    MA60SupportConfig,
)


@dataclass(frozen=True)
class BacktestGateConfig:
    horizons: tuple[int, ...] = (5, 10, 20)
    gate_horizon: int = 20
    min_events: int = 100
    assumed_round_trip_cost: float = 0.002
    min_profit_factor: float = 1.05
    folds: int = 3
    min_positive_folds: int = 1
    robustness_cost_scenarios: tuple[float, ...] = (0.002, 0.005, 0.010)
    # Backward-compatible v1 constructor fields; no longer hard gates in v1.1.
    min_win_rate: float = 0.50
    min_avg_return: float = 0.0
    ma60_event_gap: int = 10


def _frame(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    x.columns = [str(c).lower() for c in x.columns]
    needed = ["open", "high", "low", "close", "volume"]
    for c in needed:
        if c in x.columns:
            x[c] = pd.to_numeric(x[c], errors="coerce")
    return x


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, float("nan"))
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.where(avg_loss != 0.0, 100.0)
    rsi = rsi.where(~((avg_gain == 0.0) & (avg_loss == 0.0)), 50.0)
    return rsi


def _macd(close: pd.Series, fast: int, slow: int, signal: int):
    dif = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    dea = dif.ewm(span=signal, adjust=False).mean()
    hist = dif - dea
    return dif, dea, hist


def breakout_retest_events(
    symbol: str,
    history: pd.DataFrame,
    config: Optional[BreakoutRetestConfig] = None,
) -> List[Dict[str, Any]]:
    cfg = config or BreakoutRetestConfig()
    x = _frame(history).dropna(subset=["open", "high", "low", "close", "volume"]).copy()
    if len(x) < max(cfg.lookback, cfg.volume_lookback) + cfg.max_retest_bars + 2:
        return []

    level = x["high"].shift(1).rolling(cfg.lookback).max()
    prior_vol = x["volume"].shift(1).rolling(cfg.volume_lookback).mean()
    vol_ratio = x["volume"] / prior_vol
    breakout = (
        (x["close"] >= level * (1.0 + cfg.breakout_buffer))
        & (vol_ratio >= cfg.min_volume_ratio)
    )

    events: List[Dict[str, Any]] = []
    active = None

    for i in range(len(x)):
        if active is not None:
            bars_since = i - active["pos"]
            active_level = active["level"]

            if i > active["pos"]:
                if float(x["close"].iloc[i]) < active_level * (1.0 - cfg.invalidation_tolerance):
                    active = None
                else:
                    touched = float(x["low"].iloc[i]) <= active_level * (1.0 + cfg.retest_tolerance)
                    held = float(x["close"].iloc[i]) >= active_level
                    near = float(x["close"].iloc[i]) <= active_level * (1.0 + cfg.retest_upper_buffer)
                    if touched and held and near:
                        events.append({
                            "symbol": symbol,
                            "strategy": "BREAKOUT_RETEST",
                            "signal_pos": i,
                            "signal_date": str(x.index[i]),
                            "breakout_date": str(x.index[active["pos"]]),
                            "level": round(active_level, 6),
                        })
                        active = None
                    elif bars_since >= cfg.max_retest_bars:
                        active = None

        if active is None and bool(breakout.iloc[i]) and pd.notna(level.iloc[i]):
            active = {
                "pos": i,
                "level": float(level.iloc[i]),
            }

    return events


def ma60_support_events(
    symbol: str,
    history: pd.DataFrame,
    config: Optional[MA60SupportConfig] = None,
    event_gap: int = 10,
) -> List[Dict[str, Any]]:
    cfg = config or MA60SupportConfig()
    x = _frame(history).dropna(subset=["open", "high", "low", "close", "volume"]).copy()
    need = max(
        cfg.ma200_period,
        cfg.volume_lookback + 1,
        cfg.rsi_period + 2,
        cfg.macd_slow + cfg.macd_signal + 2,
    )
    if len(x) < need:
        return []

    close = x["close"]
    ma60 = close.rolling(cfg.ma60_period).mean()
    ma200 = close.rolling(cfg.ma200_period).mean()
    rsi = _rsi(close, cfg.rsi_period)
    dif, dea, hist = _macd(close, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal)
    prior_volume = x["volume"].shift(1).rolling(cfg.volume_lookback).mean()
    volume_ratio = x["volume"] / prior_volume

    events: List[Dict[str, Any]] = []
    last_event_pos = -10**9

    for i in range(need - 1, len(x)):
        c = float(close.iloc[i])
        m60 = float(ma60.iloc[i])
        m200 = float(ma200.iloc[i])
        low = float(x["low"].iloc[i])
        latest_rsi = float(rsi.iloc[i])

        trend_ok = c > m60 and c > m200
        support_zone = c >= m60 and c <= m60 * (1.0 + cfg.support_band)
        wick_reclaim = low <= m60 and c >= m60
        rsi_confirm = latest_rsi < cfg.rsi_oversold
        stop_confirmed = wick_reclaim or rsi_confirm

        volume_filter = float(volume_ratio.iloc[i]) > cfg.volume_ratio
        macd_filter = (
            float(hist.iloc[i]) > float(hist.iloc[i - 1])
            or float(dif.iloc[i]) > float(dea.iloc[i])
        )

        streak = 0
        j = i - 1
        while j >= 1 and float(close.iloc[j]) < float(close.iloc[j - 1]):
            streak += 1
            j -= 1
        decline_filter = streak >= cfg.consecutive_down_days

        filter_count = sum([volume_filter, macd_filter, decline_filter])
        triggered = trend_ok and support_zone and stop_confirmed and filter_count >= cfg.min_filters

        if triggered and (i - last_event_pos) >= int(event_gap):
            events.append({
                "symbol": symbol,
                "strategy": "MA60_SUPPORT",
                "signal_pos": i,
                "signal_date": str(x.index[i]),
                "level": round(m60, 6),
                "filter_count": int(filter_count),
            })
            last_event_pos = i

    return events


def attach_forward_outcomes(
    history: pd.DataFrame,
    events: Iterable[Dict[str, Any]],
    horizons: Sequence[int] = (5, 10, 20),
) -> List[Dict[str, Any]]:
    x = _frame(history).dropna(subset=["open", "high", "low", "close"]).copy()
    rows: List[Dict[str, Any]] = []
    max_h = max(horizons)

    for event in events:
        i = int(event["signal_pos"])
        entry_pos = i + 1
        if entry_pos >= len(x):
            continue

        entry = float(x["open"].iloc[entry_pos])
        if not math.isfinite(entry) or entry <= 0:
            continue

        row = dict(event)
        row["entry_date"] = str(x.index[entry_pos])
        row["entry_price"] = round(entry, 6)

        for h in horizons:
            exit_pos = i + int(h)
            key = f"return_{h}d"
            if exit_pos < len(x):
                row[key] = float(x["close"].iloc[exit_pos] / entry - 1.0)
            else:
                row[key] = None

        end_pos = min(len(x) - 1, i + max_h)
        if end_pos >= entry_pos:
            window = x.iloc[entry_pos : end_pos + 1]
            row[f"mfe_{max_h}d"] = float(window["high"].max() / entry - 1.0)
            row[f"mae_{max_h}d"] = float(window["low"].min() / entry - 1.0)
        else:
            row[f"mfe_{max_h}d"] = None
            row[f"mae_{max_h}d"] = None

        rows.append(row)

    return rows


def _safe_mean(vals: List[float]) -> Optional[float]:
    return float(sum(vals) / len(vals)) if vals else None


def _distribution_stats(vals: Sequence[float], assumed_round_trip_cost: float = 0.0) -> Dict[str, Any]:
    xs = [float(v) for v in vals if v is not None and math.isfinite(float(v))]
    if not xs:
        return {
            "valid_events": 0, "win_rate": None, "avg_return": None,
            "median_return": None, "avg_win": None, "avg_loss": None,
            "payoff_ratio": None, "profit_factor": None,
            "breakeven_win_rate": None, "std_return": None,
            "stderr_return": None, "naive_iid_ci95_low": None,
            "naive_iid_ci95_high": None,
            "assumed_round_trip_cost": float(assumed_round_trip_cost),
            "net_avg_return": None, "net_median_return": None,
        }

    wins = [v for v in xs if v > 0]
    losses = [v for v in xs if v <= 0]
    avg_return = float(sum(xs) / len(xs))
    median_return = float(statistics.median(xs))
    avg_win = _safe_mean(wins)
    avg_loss = _safe_mean(losses)

    payoff_ratio = None
    breakeven_win_rate = None
    if avg_win is not None and avg_loss is not None and avg_loss < 0:
        payoff_ratio = float(avg_win / abs(avg_loss))
        breakeven_win_rate = float(abs(avg_loss) / (avg_win + abs(avg_loss)))

    gross_profit = float(sum(wins))
    gross_loss = float(abs(sum(losses)))
    if gross_loss > 0:
        profit_factor = float(gross_profit / gross_loss)
    elif gross_profit > 0:
        profit_factor = float("inf")
    else:
        profit_factor = None

    std_return = float(statistics.stdev(xs)) if len(xs) >= 2 else 0.0
    stderr = float(std_return / math.sqrt(len(xs)))
    ci_low = float(avg_return - 1.96 * stderr)
    ci_high = float(avg_return + 1.96 * stderr)
    cost = float(assumed_round_trip_cost)

    return {
        "valid_events": len(xs),
        "win_rate": float(len(wins) / len(xs)),
        "avg_return": avg_return,
        "median_return": median_return,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff_ratio": payoff_ratio,
        "profit_factor": profit_factor,
        "breakeven_win_rate": breakeven_win_rate,
        "std_return": std_return,
        "stderr_return": stderr,
        "naive_iid_ci95_low": ci_low,
        "naive_iid_ci95_high": ci_high,
        "assumed_round_trip_cost": cost,
        "net_avg_return": float(avg_return - cost),
        "net_median_return": float(median_return - cost),
    }


def _chronological_folds(rows: Sequence[Dict[str, Any]], return_key: str, folds: int, assumed_round_trip_cost: float) -> List[Dict[str, Any]]:
    valid = [r for r in rows if r.get(return_key) is not None]
    if not valid:
        return []

    parsed = []
    for r in valid:
        raw = str(r.get("signal_date") or r.get("entry_date") or "")
        dt = pd.to_datetime(raw, errors="coerce")
        parsed.append((dt, r))
    good = [(dt, r) for dt, r in parsed if not pd.isna(dt)]

    if good:
        unique_dates = sorted({dt.normalize() for dt, _ in good})
        nfolds = max(1, min(int(folds), len(unique_dates)))
        out = []
        for i in range(nfolds):
            start = (len(unique_dates) * i) // nfolds
            end = (len(unique_dates) * (i + 1)) // nfolds
            date_slice = unique_dates[start:end]
            date_set = set(date_slice)
            chunk = [r for dt, r in good if dt.normalize() in date_set]
            stats = _distribution_stats([float(r[return_key]) for r in chunk], assumed_round_trip_cost)
            stats.update({
                "fold": i + 1,
                "start_date": str(date_slice[0].date()) if date_slice else None,
                "end_date": str(date_slice[-1].date()) if date_slice else None,
            })
            out.append(stats)
        return out

    ordered = list(valid)
    nfolds = max(1, min(int(folds), len(ordered)))
    out = []
    for i in range(nfolds):
        start = (len(ordered) * i) // nfolds
        end = (len(ordered) * (i + 1)) // nfolds
        chunk = ordered[start:end]
        stats = _distribution_stats([float(r[return_key]) for r in chunk], assumed_round_trip_cost)
        stats.update({
            "fold": i + 1,
            "start_date": str(chunk[0].get("signal_date")) if chunk else None,
            "end_date": str(chunk[-1].get("signal_date")) if chunk else None,
        })
        out.append(stats)
    return out


def _concentration(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {
            "unique_symbols": 0, "unique_signal_dates": 0,
            "max_symbol_share": None, "top5_symbol_share": None,
            "max_signal_date_share": None, "top5_signal_date_share": None,
        }

    symbol_counts = Counter(str(r.get("symbol", "")) for r in rows)
    date_counts = Counter(str(r.get("signal_date", "")) for r in rows)

    def shares(counter: Counter):
        vals = sorted(counter.values(), reverse=True)
        return (
            float(vals[0] / n) if vals else None,
            float(sum(vals[:5]) / n) if vals else None,
        )

    max_symbol, top5_symbol = shares(symbol_counts)
    max_date, top5_date = shares(date_counts)
    return {
        "unique_symbols": len(symbol_counts),
        "unique_signal_dates": len(date_counts),
        "max_symbol_share": max_symbol,
        "top5_symbol_share": top5_symbol,
        "max_signal_date_share": max_date,
        "top5_signal_date_share": top5_date,
    }



def _robustness_stats(
    vals: Sequence[float],
    cost_scenarios: Sequence[float] = (0.002, 0.005, 0.010),
) -> Dict[str, Any]:
    xs = sorted(float(v) for v in vals if v is not None and math.isfinite(float(v)))
    if not xs:
        return {
            "status": "NO_DATA",
            "quantiles": {},
            "trimmed_mean_1pct": None,
            "trimmed_mean_5pct": None,
            "winsorized_mean_1pct": None,
            "winsorized_mean_5pct": None,
            "top1pct_share_of_gross_profit": None,
            "top5pct_share_of_gross_profit": None,
            "bottom1pct_share_of_gross_loss": None,
            "bottom5pct_share_of_gross_loss": None,
            "cost_sensitivity": [],
        }

    n = len(xs)
    ser = pd.Series(xs, dtype=float)
    quantiles = {
        "p05": float(ser.quantile(0.05)),
        "p10": float(ser.quantile(0.10)),
        "p25": float(ser.quantile(0.25)),
        "p50": float(ser.quantile(0.50)),
        "p75": float(ser.quantile(0.75)),
        "p90": float(ser.quantile(0.90)),
        "p95": float(ser.quantile(0.95)),
    }

    def trimmed_mean(frac: float) -> Optional[float]:
        k = int(math.floor(n * frac))
        if k <= 0:
            return float(sum(xs) / n)
        if 2 * k >= n:
            return None
        core = xs[k:n-k]
        return float(sum(core) / len(core))

    def winsorized_mean(frac: float) -> Optional[float]:
        k = int(math.floor(n * frac))
        if k <= 0:
            return float(sum(xs) / n)
        if 2 * k >= n:
            return None
        lo = xs[k]
        hi = xs[n-k-1]
        w = [min(max(v, lo), hi) for v in xs]
        return float(sum(w) / len(w))

    wins = sorted((v for v in xs if v > 0), reverse=True)
    losses = sorted((v for v in xs if v < 0))
    gross_profit = float(sum(wins))
    gross_loss = float(abs(sum(losses)))

    def top_share(frac: float) -> Optional[float]:
        if gross_profit <= 0 or not wins:
            return None
        k = max(1, int(math.ceil(n * frac)))
        return float(sum(wins[:k]) / gross_profit)

    def bottom_share(frac: float) -> Optional[float]:
        if gross_loss <= 0 or not losses:
            return None
        k = max(1, int(math.ceil(n * frac)))
        return float(abs(sum(losses[:k])) / gross_loss)

    avg_return = float(sum(xs) / n)
    cost_sensitivity = [
        {
            "round_trip_cost": float(cost),
            "net_avg_return": float(avg_return - float(cost)),
            "positive": bool(avg_return - float(cost) > 0),
        }
        for cost in cost_scenarios
    ]

    trim5 = trimmed_mean(0.05)
    stress_05 = next(
        (r["net_avg_return"] for r in cost_sensitivity if abs(r["round_trip_cost"] - 0.005) < 1e-12),
        None,
    )
    if trim5 is not None and trim5 <= 0:
        status = "TAIL_DEPENDENT"
    elif stress_05 is not None and stress_05 <= 0:
        status = "COST_SENSITIVE"
    else:
        status = "ROBUST_CANDIDATE"

    return {
        "status": status,
        "quantiles": quantiles,
        "trimmed_mean_1pct": trimmed_mean(0.01),
        "trimmed_mean_5pct": trim5,
        "winsorized_mean_1pct": winsorized_mean(0.01),
        "winsorized_mean_5pct": winsorized_mean(0.05),
        "top1pct_share_of_gross_profit": top_share(0.01),
        "top5pct_share_of_gross_profit": top_share(0.05),
        "bottom1pct_share_of_gross_loss": bottom_share(0.01),
        "bottom5pct_share_of_gross_loss": bottom_share(0.05),
        "cost_sensitivity": cost_sensitivity,
        "note": (
            "Diagnostic only. ROBUST_CANDIDATE requires positive 5% trimmed mean "
            "and positive average return after a 0.5% round-trip stress cost."
        ),
    }

def summarize_strategy(strategy: str, events: Sequence[Dict[str, Any]], config: Optional[BacktestGateConfig] = None) -> Dict[str, Any]:
    cfg = config or BacktestGateConfig()
    rows = [e for e in events if e.get("strategy") == strategy]
    out: Dict[str, Any] = {"strategy": strategy, "events_total": len(rows), "horizons": {}}

    for h in cfg.horizons:
        vals = [float(r[f"return_{h}d"]) for r in rows if r.get(f"return_{h}d") is not None]
        out["horizons"][str(h)] = _distribution_stats(vals, cfg.assumed_round_trip_cost)

    max_h = max(cfg.horizons)
    mfes = [float(r[f"mfe_{max_h}d"]) for r in rows if r.get(f"mfe_{max_h}d") is not None]
    maes = [float(r[f"mae_{max_h}d"]) for r in rows if r.get(f"mae_{max_h}d") is not None]
    out["path"] = {
        f"avg_mfe_{max_h}d": _safe_mean(mfes),
        f"avg_mae_{max_h}d": _safe_mean(maes),
    }

    return_key = f"return_{cfg.gate_horizon}d"
    fold_rows = _chronological_folds(rows, return_key, cfg.folds, cfg.assumed_round_trip_cost)
    positive_folds = sum(
        1 for f in fold_rows
        if f.get("net_avg_return") is not None and float(f["net_avg_return"]) > 0
    )
    out["stability"] = {
        "folds_requested": cfg.folds,
        "folds": fold_rows,
        "positive_folds": positive_folds,
    }

    gate_rows = [r for r in rows if r.get(return_key) is not None]
    out["concentration"] = _concentration(gate_rows)
    out["robustness"] = _robustness_stats(
        [float(r[return_key]) for r in gate_rows],
        cfg.robustness_cost_scenarios,
    )

    gate = out["horizons"].get(str(cfg.gate_horizon), {})
    n = int(gate.get("valid_events") or 0)
    net_avg_return = gate.get("net_avg_return")
    profit_factor = gate.get("profit_factor")

    reasons: List[str] = []
    if n < cfg.min_events:
        status = "INSUFFICIENT_SAMPLE"
        reasons.append(f"valid_events={n} < min_events={cfg.min_events}")
    else:
        if net_avg_return is None or float(net_avg_return) <= 0:
            reasons.append(
                f"net_avg_return={net_avg_return} <= 0 after_cost={cfg.assumed_round_trip_cost}"
            )
        if profit_factor is None or float(profit_factor) < cfg.min_profit_factor:
            reasons.append(
                f"profit_factor={profit_factor} < min_profit_factor={cfg.min_profit_factor}"
            )
        if positive_folds < cfg.min_positive_folds:
            reasons.append(
                f"positive_folds={positive_folds} < min_positive_folds={cfg.min_positive_folds}"
            )
        status = "PASS_CANDIDATE" if not reasons else "FAIL"

    out["gate"] = {
        "status": status,
        "gate_horizon": cfg.gate_horizon,
        "min_events": cfg.min_events,
        "assumed_round_trip_cost": cfg.assumed_round_trip_cost,
        "min_profit_factor": cfg.min_profit_factor,
        "folds": cfg.folds,
        "min_positive_folds": cfg.min_positive_folds,
        "reasons": reasons,
        "note": (
            "Win rate and median return are reported but are not hard gates; "
            "positively skewed strategies may have win_rate < 50%."
        ),
    }
    return out
