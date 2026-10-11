from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any, Dict, Mapping, Sequence

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class SharedEquityConfig:
    initial_capital: float = 1_000_000.0
    max_positions: int = 40
    target_weight: float = 0.02
    max_total_weight: float = 0.80
    round_trip_cost: float = 0.002

    def validate(self) -> None:
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        if self.max_positions <= 0:
            raise ValueError("max_positions must be positive")
        if not (0 < self.target_weight <= 1):
            raise ValueError("target_weight must be in (0,1]")
        if not (0 < self.max_total_weight <= 1):
            raise ValueError("max_total_weight must be in (0,1]")
        if self.round_trip_cost < 0:
            raise ValueError("round_trip_cost must be >=0")
        if self.target_weight * self.max_positions > self.max_total_weight + 1e-12:
            raise ValueError(
                "target_weight * max_positions must not exceed max_total_weight"
            )


def _norm_date(v: Any) -> str:
    s = str(v).replace("-", "").replace("/", "")[:8]
    if len(s) != 8 or not s.isdigit():
        raise ValueError(f"invalid date: {v!r}")
    return s


def _price_map(df: pd.DataFrame) -> Dict[str, Dict[str, float]]:
    if df is None or df.empty:
        return {}
    x = df.copy()
    cols = {str(c).lower(): c for c in x.columns}
    if "open" not in cols or "close" not in cols:
        raise ValueError("history requires open and close columns")

    out = {}
    for idx, row in x.iterrows():
        try:
            d = pd.Timestamp(idx).strftime("%Y%m%d")
        except Exception:
            d = _norm_date(idx)
        op = row[cols["open"]]
        cl = row[cols["close"]]
        out[d] = {
            "open": float(op) if pd.notna(op) else math.nan,
            "close": float(cl) if pd.notna(cl) else math.nan,
        }
    return out


def _valid_price(x: Any) -> bool:
    try:
        v = float(x)
        return math.isfinite(v) and v > 0
    except Exception:
        return False


def _metrics(equity_rows: Sequence[Mapping[str, Any]], initial_capital: float) -> Dict[str, Any]:
    if not equity_rows:
        return {}
    eq = np.asarray([float(x["equity"]) for x in equity_rows], dtype=float)
    dates = [pd.Timestamp(str(x["date"])) for x in equity_rows]

    total_return = float(eq[-1] / initial_capital - 1.0)
    days = max(1, (dates[-1] - dates[0]).days)
    cagr = float((eq[-1] / initial_capital) ** (365.2425 / days) - 1.0)

    curve = np.concatenate([[initial_capital], eq])
    rets = curve[1:] / curve[:-1] - 1.0
    peak = np.maximum.accumulate(curve)
    dd = curve / peak - 1.0
    max_dd = float(np.min(dd))

    annual_sessions = (len(equity_rows) - 1) * 365.2425 / days if days > 0 else 252.0
    std = float(np.std(rets, ddof=1)) if len(rets) > 1 else 0.0
    sharpe = (
        float(np.mean(rets) / std * math.sqrt(annual_sessions))
        if std > 0 and annual_sessions > 0
        else 0.0
    )
    return {
        "total_return": total_return,
        "cagr_calendar": cagr,
        "max_drawdown": max_dd,
        "sharpe_actual_sessions": sharpe,
        "annual_sessions_observed": annual_sessions,
        "final_equity": float(eq[-1]),
    }


def simulate_shared_equity(
    history: Mapping[str, pd.DataFrame],
    candidates: Sequence[Mapping[str, Any]],
    market_dates: Sequence[str],
    config: SharedEquityConfig,
) -> Dict[str, Any]:
    config.validate()
    prices = {str(s): _price_map(df) for s, df in history.items()}

    by_entry = defaultdict(list)
    for raw in candidates:
        c = dict(raw)
        c["symbol"] = str(c["symbol"])
        c["entry_date"] = _norm_date(c["entry_date"])
        c["exit_date"] = _norm_date(c["exit_date"])
        by_entry[c["entry_date"]].append(c)
    for day in by_entry:
        by_entry[day].sort(key=lambda x: x["symbol"])

    if not candidates:
        return {"summary": {}, "trades": [], "daily": []}

    first_day = min(_norm_date(c["entry_date"]) for c in candidates)
    last_day = max(_norm_date(c["exit_date"]) for c in candidates)
    cal = [d for d in sorted({_norm_date(x) for x in market_dates}) if first_day <= d <= last_day]

    cash = float(config.initial_capital)
    buy_fee = config.round_trip_cost / 2.0
    sell_fee = config.round_trip_cost / 2.0
    positions: Dict[str, Dict[str, Any]] = {}
    daily = []
    trades = []
    skips = []
    gross_turnover = 0.0
    scaled_entry_days = 0

    for day in cal:
        # Existing positions are valued at today's open if available; otherwise carry last close.
        open_values = {}
        for sym, pos in positions.items():
            px = prices.get(sym, {}).get(day, {}).get("open")
            if not _valid_price(px):
                px = pos["last_close"]
            open_values[sym] = float(px) * pos["shares"]

        pre_entry_equity = cash + sum(open_values.values())
        held = set(positions)

        todays = by_entry.get(day, [])
        capacity_free = max(0, config.max_positions - len(positions))
        eligible = []
        for c in todays:
            sym = c["symbol"]
            if sym in held:
                skips.append({**c, "reason": "DUPLICATE_SYMBOL"})
                continue
            if len(eligible) >= capacity_free:
                skips.append({**c, "reason": "NO_SLOT"})
                continue
            px = prices.get(sym, {}).get(day, {}).get("open")
            if not _valid_price(px):
                raise RuntimeError(f"missing/invalid entry open: {sym} {day}")
            eligible.append(c)
            held.add(sym)

        if eligible:
            existing_gross = sum(open_values.values())
            gross_budget = max(0.0, config.max_total_weight * pre_entry_equity - existing_gross)
            cash_budget = max(0.0, cash) / (1.0 + buy_fee)
            desired_each = config.target_weight * pre_entry_equity
            allowed_each = min(
                desired_each,
                gross_budget / len(eligible),
                cash_budget / len(eligible),
            )

            if allowed_each <= 1e-12:
                for c in eligible:
                    skips.append({**c, "reason": "RISK_BUDGET_BLOCK"})
                eligible = []
            else:
                if allowed_each < desired_each - 1e-9:
                    scaled_entry_days += 1
                for c in eligible:
                    sym = c["symbol"]
                    px = float(prices[sym][day]["open"])
                    notional = allowed_each
                    fee = notional * buy_fee
                    shares = notional / px
                    cash -= notional + fee
                    gross_turnover += notional
                    positions[sym] = {
                        "symbol": sym,
                        "signal_date": c.get("signal_date"),
                        "entry_date": day,
                        "exit_date": c["exit_date"],
                        "entry_price": px,
                        "entry_notional": notional,
                        "entry_fee": fee,
                        "shares": shares,
                        "last_close": px,
                    }

        # Close and exit at end of day. Same-day exits were not reusable at the open.
        to_exit = []
        for sym, pos in positions.items():
            px = prices.get(sym, {}).get(day, {}).get("close")
            if _valid_price(px):
                pos["last_close"] = float(px)
            if pos["exit_date"] == day:
                if not _valid_price(px):
                    raise RuntimeError(f"missing/invalid exit close: {sym} {day}")
                to_exit.append((sym, float(px)))

        for sym, px in to_exit:
            pos = positions.pop(sym)
            proceeds = pos["shares"] * px
            fee = proceeds * sell_fee
            cash += proceeds - fee
            gross_turnover += proceeds
            cost = pos["entry_notional"] + pos["entry_fee"]
            pnl = (proceeds - fee) - cost
            trades.append({
                "symbol": sym,
                "signal_date": pos.get("signal_date"),
                "entry_date": pos["entry_date"],
                "exit_date": day,
                "entry_price": pos["entry_price"],
                "exit_price": px,
                "entry_notional": pos["entry_notional"],
                "entry_weight_at_open": (
                    pos["entry_notional"] / pre_entry_equity if pre_entry_equity > 0 else None
                ),
                "net_pnl": pnl,
                "net_return_on_trade_capital": pnl / cost if cost > 0 else None,
            })

        close_values = {}
        for sym, pos in positions.items():
            px = prices.get(sym, {}).get(day, {}).get("close")
            if not _valid_price(px):
                px = pos["last_close"]
            else:
                pos["last_close"] = float(px)
            close_values[sym] = float(px) * pos["shares"]

        gross = sum(close_values.values())
        equity = cash + gross
        daily.append({
            "date": day,
            "equity": equity,
            "cash": cash,
            "gross_market_value": gross,
            "gross_weight": gross / equity if equity > 0 else None,
            "active_positions": len(positions),
        })

    m = _metrics(daily, config.initial_capital)
    m.update({
        "trades_executed": len(trades),
        "skipped_no_slot": sum(1 for x in skips if x["reason"] == "NO_SLOT"),
        "skipped_duplicate_symbol": sum(1 for x in skips if x["reason"] == "DUPLICATE_SYMBOL"),
        "risk_budget_blocked": sum(1 for x in skips if x["reason"] == "RISK_BUDGET_BLOCK"),
        "scaled_entry_days": scaled_entry_days,
        "avg_gross_weight": (
            float(np.mean([x["gross_weight"] for x in daily if x["gross_weight"] is not None]))
            if daily else 0.0
        ),
        "max_gross_weight": (
            float(np.max([x["gross_weight"] for x in daily if x["gross_weight"] is not None]))
            if daily else 0.0
        ),
        "avg_active_positions": (
            float(np.mean([x["active_positions"] for x in daily])) if daily else 0.0
        ),
        "max_active_positions": max((x["active_positions"] for x in daily), default=0),
        "turnover_cumulative_double_sided": (
            gross_turnover / config.initial_capital
        ),
    })
    return {
        "summary": m,
        "trades": trades,
        "skipped": skips,
        "daily": daily,
    }
