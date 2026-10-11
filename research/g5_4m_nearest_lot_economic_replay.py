from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Any, Mapping, Sequence
import math
import pandas as pd
import numpy as np

@dataclass(frozen=True)
class G5ReplayPolicy:
    policy_id: str = "G5_4M_NEAREST_LOT_ECONOMIC_REPLAY_V0_2"
    total_pilot_capital: float = 4_000_000.0
    deep_sleeve_weight: float = 0.5
    ret120_sleeve_weight: float = 0.5
    strategy_target_weight: float = 0.01
    max_target_gross: float = 0.30
    ret120_max_positions: int = 30
    max_relative_target_error: float = 0.25
    fidelity_min: float = 0.90
    max_abs_cagr_drift: float = 0.005
    max_abs_mdd_drift: float = 0.005
    max_abs_sharpe_drift: float = 0.10
    main_cost: float = 0.002
    stress_cost: float = 0.005

    @property
    def deep_initial(self) -> float:
        return self.total_pilot_capital * self.deep_sleeve_weight
    @property
    def ret_initial(self) -> float:
        return self.total_pilot_capital * self.ret120_sleeve_weight

POLICY = G5ReplayPolicy()

def norm_date(x: Any) -> str:
    s = str(x)
    s = s.replace("-", "").replace("/", "")[:8]
    return s

def board_lot_candidates(symbol: str, target_shares: float) -> list[int]:
    """Buy-side executable candidates for Pilot sizing.
    STAR (688*) uses minimum 200 shares, then 1-share increments.
    Other A-share boards use 100-share lots.
    """
    symbol = str(symbol).upper()
    if symbol.startswith("688"):
        if target_shares < 200:
            return [200]
        lo = max(200, int(math.floor(target_shares)))
        hi = max(200, int(math.ceil(target_shares)))
        return sorted(set([lo, hi]))
    lot = 100
    lo = int(math.floor(target_shares / lot) * lot)
    hi = int(math.ceil(target_shares / lot) * lot)
    vals = [v for v in (lo, hi) if v >= lot]
    if not vals:
        vals = [lot]
    return sorted(set(vals))

def nearest_executable_shares(
    symbol: str,
    price: float,
    target_notional: float,
    max_relative_error: float,
    max_notional: float | None = None,
) -> tuple[int, float, float] | None:
    if not (price > 0 and target_notional > 0):
        return None
    raw = target_notional / price
    candidates = board_lot_candidates(symbol, raw)
    feasible = []
    for sh in candidates:
        notional = sh * price
        err = abs(notional - target_notional) / target_notional
        if err <= max_relative_error + 1e-12:
            if max_notional is None or notional <= max_notional + 1e-8:
                feasible.append((sh, notional, err))
    if not feasible:
        return None
    return min(feasible, key=lambda x: (x[2], x[1], x[0]))

def map_get(m: Any, symbol: str, date: str, default=None):
    """Tolerate dict/Series maps keyed either (date,symbol) or (symbol,date)."""
    date = norm_date(date)
    keys = [(date, symbol), (symbol, date), f"{date}|{symbol}", f"{symbol}|{date}"]
    if hasattr(m, "get"):
        for k in keys:
            try:
                v = m.get(k, None)
                if v is not None and not pd.isna(v):
                    return float(v)
            except Exception:
                pass
    try:
        if isinstance(m, pd.Series):
            for k in keys[:2]:
                try:
                    v = m.loc[k]
                    if not pd.isna(v): return float(v)
                except Exception:
                    pass
    except Exception:
        pass
    return default

def curve_metrics(curve: pd.DataFrame, initial_equity: float | None = None) -> dict:
    if curve is None or curve.empty:
        raise ValueError("empty curve")
    x = curve.copy()
    date_col = "date" if "date" in x.columns else "trade_date"
    eq_col = "equity"
    x[date_col] = x[date_col].map(norm_date)
    x = x.sort_values(date_col)
    eq = x[eq_col].astype(float)
    if initial_equity is None:
        # Metrics are intended on a full curve. Use first equity only if no explicit initial is supplied.
        initial_equity = float(eq.iloc[0])
    total = float(eq.iloc[-1] / initial_equity - 1.0)
    d0, d1 = pd.Timestamp(x[date_col].iloc[0]), pd.Timestamp(x[date_col].iloc[-1])
    years = max((d1-d0).days/365.2425, 1/365.2425)
    cagr = float((eq.iloc[-1]/initial_equity)**(1/years)-1)
    ret = eq.pct_change(fill_method=None).fillna(0.0)
    std = float(ret.std(ddof=1))
    sharpe = float(ret.mean()/std*math.sqrt(252)) if std > 0 else None
    dd = eq/eq.cummax()-1
    return {
        "sessions": int(len(x)),
        "start": x[date_col].iloc[0],
        "end": x[date_col].iloc[-1],
        "total_return": total,
        "cagr": cagr,
        "max_drawdown": float(dd.min()),
        "sharpe": sharpe,
    }

def prepare_curve(curve: pd.DataFrame) -> pd.DataFrame:
    x = curve.copy()
    date_col = "date" if "date" in x.columns else "trade_date"
    x[date_col] = x[date_col].map(norm_date)
    x = x.sort_values(date_col).copy()
    return x[[date_col, "equity"]].rename(columns={date_col: "date"})

def combine_fixed_initial_sleeves(
    deep_curve: pd.DataFrame,
    ret_curve: pd.DataFrame,
    total_initial: float,
    deep_original_initial: float,
    ret_original_initial: float,
) -> pd.DataFrame:
    """
    Fixed 50/50 initial-capital sleeves, no rebalance.

    Common period starts at the first RET120 curve date and ends at the earlier
    of the two curve ends.  For a sleeve with history before common_start, use
    the immediately preceding close as the rebasing denominator.  If a sleeve
    itself starts on common_start, use its declared original initial equity so
    the common_start day's return is retained rather than erased.
    """
    d = prepare_curve(deep_curve)
    r = prepare_curve(ret_curve)
    common_start = max(d["date"].min(), r["date"].min())
    common_end = min(d["date"].max(), r["date"].max())
    common_dates = sorted(
        set(d.loc[(d["date"] >= common_start) & (d["date"] <= common_end), "date"])
        & set(r.loc[(r["date"] >= common_start) & (r["date"] <= common_end), "date"])
    )
    if not common_dates:
        raise ValueError("no common dates")
    common_start = common_dates[0]
    common_end = common_dates[-1]

    def sleeve_scaled(x: pd.DataFrame, original_initial: float, sleeve_initial: float) -> pd.DataFrame:
        prior = x[x["date"] < common_start]
        if len(prior):
            denominator = float(prior.iloc[-1]["equity"])
        elif x["date"].min() == common_start:
            denominator = float(original_initial)
        else:
            raise ValueError("curve has no prior equity and does not start at common_start")
        y = x[(x["date"] >= common_start) & (x["date"] <= common_end)].copy()
        y["equity"] = y["equity"].astype(float) / denominator * sleeve_initial
        return y

    d2 = sleeve_scaled(d, deep_original_initial, total_initial * 0.5)
    r2 = sleeve_scaled(r, ret_original_initial, total_initial * 0.5)
    z = d2.merge(r2, on="date", suffixes=("_deep", "_ret"))
    z = z[z["date"].isin(common_dates)].copy()
    z["equity"] = z["equity_deep"] + z["equity_ret"]
    return z[["date", "equity"]]

def lot_fidelity(feasible_attempts: int, sizing_attempts: int) -> float:
    if sizing_attempts <= 0:
        return 0.0
    return float(feasible_attempts / sizing_attempts)

def metric_drift(base: dict, lot: dict) -> dict:
    return {
      "cagr": lot["cagr"]-base["cagr"],
      "max_drawdown": lot["max_drawdown"]-base["max_drawdown"],
      "sharpe": (lot["sharpe"]-base["sharpe"]) if lot["sharpe"] is not None and base["sharpe"] is not None else None,
    }

def drift_pass(base: dict, lot: dict, policy: G5ReplayPolicy=POLICY) -> dict:
    d=metric_drift(base,lot)
    checks={
      "abs_cagr_le_50bp": abs(d["cagr"]) <= policy.max_abs_cagr_drift+1e-12,
      "abs_mdd_le_50bp": abs(d["max_drawdown"]) <= policy.max_abs_mdd_drift+1e-12,
      "abs_sharpe_le_0_10": d["sharpe"] is not None and abs(d["sharpe"]) <= policy.max_abs_sharpe_drift+1e-12,
    }
    return {"drift":d,"checks":checks,"pass":all(checks.values())}
