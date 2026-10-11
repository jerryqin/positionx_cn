from __future__ import annotations
from dataclasses import replace
import math
import statistics
from typing import Any, Dict, Mapping, Sequence

import numpy as np
import pandas as pd

from research.capacity_selection_audit import replay_selection
from research.shared_equity_portfolio import SharedEquityConfig


def norm_date(v: Any) -> str:
    s = str(v).replace("-", "").replace("/", "")[:8]
    if len(s) != 8 or not s.isdigit():
        raise ValueError(v)
    return s


def prepare_prices(history: Mapping[str, pd.DataFrame]) -> Dict[str, Dict[str, Dict[str, float]]]:
    out = {}
    for sym, df in history.items():
        if df is None or df.empty:
            continue
        cols = {str(c).lower(): c for c in df.columns}
        if "open" not in cols or "close" not in cols:
            raise ValueError(f"{sym}: open/close missing")
        rows = {}
        for idx, row in df.iterrows():
            try:
                d = pd.Timestamp(idx).strftime("%Y%m%d")
            except Exception:
                d = norm_date(idx)
            op = row[cols["open"]]
            cl = row[cols["close"]]
            rows[d] = {
                "open": float(op) if pd.notna(op) else math.nan,
                "close": float(cl) if pd.notna(cl) else math.nan,
            }
        out[str(sym)] = rows
    return out


def valid_price(v: Any) -> bool:
    try:
        x = float(v)
        return math.isfinite(x) and x > 0
    except Exception:
        return False


def curve_metrics(daily: Sequence[Mapping[str, Any]], initial_capital: float) -> Dict[str, Any]:
    if not daily:
        return {}
    eq = np.asarray([float(x["equity"]) for x in daily], dtype=float)
    dates = [pd.Timestamp(str(x["date"])) for x in daily]
    curve = np.concatenate([[initial_capital], eq])
    total = float(curve[-1] / initial_capital - 1)
    days = max(1, (dates[-1] - dates[0]).days)
    cagr = float((curve[-1] / initial_capital) ** (365.2425 / days) - 1)
    peak = np.maximum.accumulate(curve)
    mdd = float(np.min(curve / peak - 1))
    rets = curve[1:] / curve[:-1] - 1
    sessions = (len(daily)-1) * 365.2425 / days if days > 0 else 252
    std = float(np.std(rets, ddof=1)) if len(rets) > 1 else 0
    sharpe = float(np.mean(rets) / std * math.sqrt(sessions)) if std > 0 else 0
    return {
        "total_return": total,
        "cagr": cagr,
        "max_drawdown": mdd,
        "sharpe": sharpe,
        "final_equity": float(curve[-1]),
    }


def simulate_prepared(
    prices: Mapping[str, Mapping[str, Mapping[str, float]]],
    selected: Sequence[Mapping[str, Any]],
    market_dates: Sequence[str],
    config: SharedEquityConfig,
) -> Dict[str, Any]:
    config.validate()
    by_entry = {}
    for raw in selected:
        c = dict(raw)
        c["symbol"] = str(c["symbol"])
        c["entry_date"] = norm_date(c["entry_date"])
        c["exit_date"] = norm_date(c["exit_date"])
        by_entry.setdefault(c["entry_date"], []).append(c)
    for d in by_entry:
        by_entry[d].sort(key=lambda x: x["symbol"])

    if not selected:
        return {"summary": {}, "daily": []}

    first = min(norm_date(c["entry_date"]) for c in selected)
    last = max(norm_date(c["exit_date"]) for c in selected)
    cal = [d for d in sorted({norm_date(x) for x in market_dates}) if first <= d <= last]

    cash = float(config.initial_capital)
    buy_fee = config.round_trip_cost / 2
    sell_fee = config.round_trip_cost / 2
    positions = {}
    daily = []
    trades = 0
    scaled_days = 0

    for day in cal:
        open_values = {}
        for sym, pos in positions.items():
            px = prices.get(sym, {}).get(day, {}).get("open")
            if not valid_price(px):
                px = pos["last_close"]
            open_values[sym] = float(px) * pos["shares"]

        equity_open = cash + sum(open_values.values())
        held = set(positions)

        todays = by_entry.get(day, [])
        eligible = []
        free = max(0, config.max_positions - len(positions))
        for c in todays:
            if c["symbol"] in held:
                raise RuntimeError("preselected set contains duplicate overlap")
            if len(eligible) >= free:
                raise RuntimeError("preselected set exceeds capacity")
            eligible.append(c)
            held.add(c["symbol"])

        if eligible:
            existing_gross = sum(open_values.values())
            gross_budget = max(0.0, config.max_total_weight * equity_open - existing_gross)
            cash_budget = max(0.0, cash) / (1 + buy_fee)
            desired_each = config.target_weight * equity_open
            allowed_each = min(desired_each, gross_budget / len(eligible), cash_budget / len(eligible))
            if allowed_each <= 1e-12:
                raise RuntimeError("risk budget unexpectedly blocks preselected entries")
            if allowed_each < desired_each - 1e-9:
                scaled_days += 1
            for c in eligible:
                sym = c["symbol"]
                px = prices.get(sym, {}).get(day, {}).get("open")
                if not valid_price(px):
                    raise RuntimeError(f"bad entry price {sym} {day}")
                notional = allowed_each
                fee = notional * buy_fee
                shares = notional / float(px)
                cash -= notional + fee
                positions[sym] = {
                    "shares": shares,
                    "exit_date": c["exit_date"],
                    "last_close": float(px),
                }

        exits = []
        for sym, pos in positions.items():
            cl = prices.get(sym, {}).get(day, {}).get("close")
            if valid_price(cl):
                pos["last_close"] = float(cl)
            if pos["exit_date"] == day:
                if not valid_price(cl):
                    raise RuntimeError(f"bad exit price {sym} {day}")
                exits.append((sym, float(cl)))

        for sym, px in exits:
            pos = positions.pop(sym)
            proceeds = pos["shares"] * px
            cash += proceeds * (1 - sell_fee)
            trades += 1

        gross = 0.0
        for sym, pos in positions.items():
            cl = prices.get(sym, {}).get(day, {}).get("close")
            if valid_price(cl):
                pos["last_close"] = float(cl)
            gross += pos["shares"] * pos["last_close"]
        equity = cash + gross
        daily.append({
            "date": day,
            "equity": equity,
            "cash": cash,
            "gross": gross,
            "gross_weight": gross / equity if equity > 0 else 0.0,
            "active": len(positions),
        })

    m = curve_metrics(daily, config.initial_capital)
    m.update({
        "trades": trades,
        "scaled_entry_days": scaled_days,
        "avg_gross_weight": float(np.mean([x["gross_weight"] for x in daily])),
        "max_gross_weight": float(np.max([x["gross_weight"] for x in daily])),
        "avg_active": float(np.mean([x["active"] for x in daily])),
        "max_active": max((x["active"] for x in daily), default=0),
    })
    return {"summary": m, "daily": daily}


def period_metrics(
    daily: Sequence[Mapping[str, Any]],
    start: str,
    end: str,
) -> Dict[str, Any]:
    rows = [x for x in daily if start <= norm_date(x["date"]) <= end]
    if not rows:
        return {"start": start, "end": end, "sessions": 0}
    eq = [float(x["equity"]) for x in rows]
    base = float(rows[0]["equity"])
    # Period return from first observed equity to last observed equity.
    total = eq[-1] / base - 1 if base else None
    peak = np.maximum.accumulate(np.asarray(eq))
    mdd = float(np.min(np.asarray(eq) / peak - 1))
    return {
        "start": start,
        "end": end,
        "sessions": len(rows),
        "total_return": total,
        "max_drawdown": mdd,
    }


def distribution(xs: Sequence[float]) -> Dict[str, Any]:
    vals = sorted(float(x) for x in xs if x is not None and math.isfinite(float(x)))
    if not vals:
        return {"n": 0}
    def q(p):
        if len(vals) == 1:
            return vals[0]
        pos = (len(vals)-1)*p
        lo, hi = math.floor(pos), math.ceil(pos)
        if lo == hi:
            return vals[lo]
        w = pos-lo
        return vals[lo]*(1-w)+vals[hi]*w
    return {
        "n": len(vals),
        "min": vals[0],
        "p05": q(0.05),
        "median": q(0.5),
        "p95": q(0.95),
        "max": vals[-1],
        "mean": statistics.fmean(vals),
    }
