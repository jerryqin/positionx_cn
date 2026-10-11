from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Sequence

import math
import numpy as np
import pandas as pd


TARGET_WEIGHT = 0.01
MAX_TOTAL_WEIGHT = 0.30
PARTICIPATION_RATES = (0.0025, 0.0050, 0.0100)
ACCOUNT_SIZES = (
    500_000.0,
    1_000_000.0,
    2_000_000.0,
    5_000_000.0,
    10_000_000.0,
    20_000_000.0,
    50_000_000.0,
    100_000_000.0,
    200_000_000.0,
    500_000_000.0,
)


def norm_date(v: Any) -> str:
    s = "".join(ch for ch in str(v) if ch.isdigit())
    if len(s) < 8:
        raise ValueError(f"invalid date {v!r}")
    return s[:8]


def research_buy_lot(symbol: str) -> int:
    """
    Historical A-share buy-lot proxy for capacity research.

    STAR Market (688/689) uses 200-share minimum buy quantity.
    Other SH/SZ cash equities use 100 shares.

    Forward execution itself uses QMT MinLimitOrderVolume; this function is only
    for historical capacity estimation.
    """
    code = str(symbol).split(".")[0]
    if code.startswith(("688", "689")):
        return 200
    return 100


def adv20_traded_days(
    raw_history: pd.DataFrame,
    signal_date: str,
    required: int = 20,
) -> Dict[str, Any]:
    """
    T-observable liquidity only.

    Use the last 20 positive-amount traded days up to and including signal day T.
    No T+1 or future amount is used.
    """
    if raw_history is None or raw_history.empty or "amount" not in raw_history.columns:
        return {"ok": False, "reason": "NO_AMOUNT_HISTORY"}
    x = raw_history.loc[raw_history.index <= signal_date, "amount"]
    x = pd.to_numeric(x, errors="coerce")
    x = x[(x > 0) & np.isfinite(x)]
    if len(x) < required:
        return {
            "ok": False,
            "reason": "INSUFFICIENT_TRADED_DAYS",
            "observations": int(len(x)),
        }
    tail = x.iloc[-required:]
    return {
        "ok": True,
        "adv20_amount": float(tail.mean()),
        "amount_median20": float(tail.median()),
        "amount_min20": float(tail.min()),
        "observations": int(len(tail)),
        "first_date": str(tail.index[0]),
        "last_date": str(tail.index[-1]),
    }


def enrich_capacity_events(
    fillable_events: pd.DataFrame,
    raw_histories: Mapping[str, pd.DataFrame],
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    rows = []
    failures = []
    for r in fillable_events.to_dict("records"):
        symbol = str(r["symbol"])
        signal_date = norm_date(r["signal_date"])
        entry_date = norm_date(r["entry_date"])
        raw = raw_histories.get(symbol)
        liq = adv20_traded_days(raw, signal_date, 20)
        if not liq["ok"]:
            failures.append({
                "symbol": symbol,
                "signal_date": signal_date,
                "reason": liq["reason"],
            })
            continue
        if raw is None or entry_date not in raw.index or "open" not in raw.columns:
            failures.append({
                "symbol": symbol,
                "signal_date": signal_date,
                "reason": "RAW_ENTRY_OPEN_MISSING",
            })
            continue
        raw_open = raw.at[entry_date, "open"]
        if pd.isna(raw_open) or not math.isfinite(float(raw_open)) or float(raw_open) <= 0:
            failures.append({
                "symbol": symbol,
                "signal_date": signal_date,
                "reason": "RAW_ENTRY_OPEN_INVALID",
            })
            continue

        lot = research_buy_lot(symbol)
        lot_notional = float(raw_open) * lot
        out = dict(r)
        out.update({
            "raw_entry_open": float(raw_open),
            "buy_lot": int(lot),
            "one_lot_notional": lot_notional,
            "min_account_for_one_lot_at_1pct": lot_notional / TARGET_WEIGHT,
            "adv20_amount": liq["adv20_amount"],
            "adv20_amount_median": liq["amount_median20"],
            "adv20_amount_min": liq["amount_min20"],
            "adv20_first_date": liq["first_date"],
            "adv20_last_date": liq["last_date"],
        })
        for p in PARTICIPATION_RATES:
            bp = int(round(p * 10000))
            out[f"liq_cap_notional_{bp}bp"] = liq["adv20_amount"] * p
            out[f"account_capacity_full_1pct_{bp}bp"] = (
                liq["adv20_amount"] * p / TARGET_WEIGHT
            )
        rows.append(out)

    df = pd.DataFrame(rows)
    return df, {
        "input_fillable_events": int(len(fillable_events)),
        "capacity_ready_events": int(len(df)),
        "failed_events": int(len(failures)),
        "failure_sample": failures[:30],
    }


def q(values: pd.Series, probs=(0.05,0.10,0.25,0.50,0.75,0.90,0.95,0.99)) -> Dict[str, float]:
    x = pd.to_numeric(values, errors="coerce").dropna()
    if len(x) == 0:
        return {}
    out = {}
    for p in probs:
        out[f"p{int(round(p*100)):02d}"] = float(x.quantile(p))
    out["min"] = float(x.min())
    out["max"] = float(x.max())
    out["mean"] = float(x.mean())
    return out


def capacity_distribution(events: pd.DataFrame) -> Dict[str, Any]:
    out = {
        "events": int(len(events)),
        "adv20_amount": q(events["adv20_amount"]),
        "one_lot_notional": q(events["one_lot_notional"]),
        "min_account_for_one_lot_at_1pct": q(events["min_account_for_one_lot_at_1pct"]),
        "participation": {},
    }
    for p in PARTICIPATION_RATES:
        bp = int(round(p * 10000))
        out["participation"][f"{bp}bp_adv"] = {
            "rate": p,
            "liq_cap_notional": q(events[f"liq_cap_notional_{bp}bp"]),
            "account_capacity_full_1pct": q(
                events[f"account_capacity_full_1pct_{bp}bp"]
            ),
        }
    for account in ACCOUNT_SIZES:
        ideal = account * TARGET_WEIGHT
        eligible = (events["one_lot_notional"] <= ideal)
        out.setdefault("lot_eligibility_by_account", []).append({
            "account_size": account,
            "ideal_1pct_notional": ideal,
            "event_fraction_can_buy_one_lot": float(eligible.mean()),
        })
    return out


@dataclass
class Position:
    symbol: str
    signal_date: str
    exit_date: str
    economic_units: float
    entry_notional: float
    entry_fee: float
    last_value: float


def _adj_price(
    histories: Mapping[str, pd.DataFrame],
    symbol: str,
    date: str,
    field: str,
) -> float | None:
    df = histories.get(symbol)
    if df is None or date not in df.index or field not in df.columns:
        return None
    v = df.at[date, field]
    if pd.isna(v) or not math.isfinite(float(v)) or float(v) <= 0:
        return None
    return float(v)


def simulate_capacity(
    events: pd.DataFrame,
    adjusted_histories: Mapping[str, pd.DataFrame],
    market_dates: Sequence[str],
    initial_equity: float,
    participation_rate: float | None,
    round_trip_cost: float,
) -> Dict[str, Any]:
    """
    Executable-lot shared-account replay.

    Signal set = all historically STANDARD-fillable stock-day events.
    Duplicate rule is applied dynamically to actual holdings.  If an entry is
    blocked by lot/liquidity/capital, the symbol remains flat and a later
    oversold signal can be considered.

    Sizing order:
      1. 1% ideal target from account equity;
      2. pro-rata scale if same-day aggregate gross/cash budget binds;
      3. per-symbol ADV20 participation cap;
      4. round down to historical minimum buy lot;
      5. unused budget is NOT redistributed.
    """
    if initial_equity <= 0:
        raise ValueError("initial_equity must be positive")
    if participation_rate is not None and not (0 < participation_rate <= 0.10):
        raise ValueError("invalid participation rate")
    half = round_trip_cost / 2.0
    dates = list(market_dates)
    date_set = set(dates)
    e = events[
        events["entry_date"].isin(date_set) & events["exit_date"].isin(date_set)
    ].copy()
    entries = {
        d: g.sort_values(["signal_date","symbol"]).to_dict("records")
        for d,g in e.groupby("entry_date")
    }

    cash = float(initial_equity)
    positions: Dict[str, Position] = {}
    curve = []

    attempts = 0
    executed = 0
    duplicate_while_held = 0
    skipped_no_gross = 0
    skipped_lot = 0
    skipped_liquidity_below_lot = 0
    gross_scaled_days = 0
    ideal_notional_sum = 0.0
    actual_notional_sum = 0.0

    for d in dates:
        # Existing positions marked at today's open for sizing.
        open_mv = 0.0
        for symbol, pos in positions.items():
            px = _adj_price(adjusted_histories, symbol, d, "open")
            open_mv += pos.last_value if px is None else pos.economic_units * px

        equity_open = cash + open_mv
        remaining_gross = max(0.0, MAX_TOTAL_WEIGHT * equity_open - open_mv)
        remaining_cash_notional = max(0.0, cash / (1.0 + half))

        candidates = []
        for r in entries.get(d, []):
            symbol = str(r["symbol"])
            if symbol in positions:
                duplicate_while_held += 1
                continue
            attempts += 1
            candidates.append(r)

        if candidates:
            desired_each = TARGET_WEIGHT * equity_open
            desired_total = desired_each * len(candidates)
            alloc_total = min(desired_total, remaining_gross, remaining_cash_notional)
            scale = alloc_total / desired_total if desired_total > 0 else 0.0
            if scale < 0.999999:
                gross_scaled_days += 1

            for r in candidates:
                symbol = str(r["symbol"])
                ideal_notional_sum += desired_each
                planned = desired_each * scale
                if planned <= 0:
                    skipped_no_gross += 1
                    continue

                liq_cap = math.inf
                if participation_rate is not None:
                    liq_cap = float(r["adv20_amount"]) * participation_rate
                capped = min(planned, liq_cap)

                raw_open = float(r["raw_entry_open"])
                lot = int(r["buy_lot"])
                lot_notional = raw_open * lot
                if capped < lot_notional:
                    if participation_rate is not None and liq_cap < lot_notional:
                        skipped_liquidity_below_lot += 1
                    else:
                        skipped_lot += 1
                    continue

                lots = int(capped // lot_notional)
                shares = lots * lot
                if shares <= 0:
                    skipped_lot += 1
                    continue
                actual_notional = shares * raw_open

                adj_entry = float(r["entry_open"])
                economic_units = actual_notional / adj_entry
                fee = actual_notional * half
                if actual_notional + fee > cash + 1e-7:
                    # A final cash-safe round-down, still by one whole lot.
                    max_lots = int((cash / (1.0 + half)) // lot_notional)
                    shares = max_lots * lot
                    if shares <= 0:
                        skipped_no_gross += 1
                        continue
                    actual_notional = shares * raw_open
                    economic_units = actual_notional / adj_entry
                    fee = actual_notional * half

                cash -= actual_notional + fee
                positions[symbol] = Position(
                    symbol=symbol,
                    signal_date=str(r["signal_date"]),
                    exit_date=str(r["exit_date"]),
                    economic_units=economic_units,
                    entry_notional=actual_notional,
                    entry_fee=fee,
                    last_value=actual_notional,
                )
                executed += 1
                actual_notional_sum += actual_notional

        # Close exits then mark remaining positions.
        for symbol in list(positions.keys()):
            pos = positions[symbol]
            cp = _adj_price(adjusted_histories, symbol, d, "close")
            if cp is None:
                continue
            value = pos.economic_units * cp
            pos.last_value = value
            if d >= pos.exit_date:
                exit_fee = value * half
                cash += value - exit_fee
                del positions[symbol]

        mv = sum(p.last_value for p in positions.values())
        equity = cash + mv
        curve.append({
            "date": d,
            "equity": equity,
            "cash": cash,
            "gross_market_value": mv,
            "gross_weight": mv / equity if equity > 0 else 0.0,
            "active_positions": len(positions),
        })

    c = pd.DataFrame(curve)
    eq = c["equity"].astype(float)
    ret = eq.pct_change(fill_method=None).fillna(0.0)
    dd = eq / eq.cummax() - 1.0
    start = pd.Timestamp(c["date"].iloc[0])
    end = pd.Timestamp(c["date"].iloc[-1])
    years = max((end-start).days / 365.2425, 1/365.2425)
    total_return = float(eq.iloc[-1] / initial_equity - 1.0)
    cagr = float((eq.iloc[-1] / initial_equity) ** (1.0 / years) - 1.0)
    std = float(ret.std(ddof=1))
    sharpe = float(ret.mean() / std * math.sqrt(252)) if std > 0 else None
    avg_gross = float(c["gross_weight"].mean())

    return {
        "initial_equity": initial_equity,
        "participation_rate": participation_rate,
        "round_trip_cost": round_trip_cost,
        "attempts": int(attempts),
        "executed_trades": int(executed),
        "execution_rate": float(executed/attempts) if attempts else None,
        "duplicate_while_held": int(duplicate_while_held),
        "skipped_no_gross": int(skipped_no_gross),
        "skipped_lot": int(skipped_lot),
        "skipped_liquidity_below_lot": int(skipped_liquidity_below_lot),
        "gross_scaled_days": int(gross_scaled_days),
        "ideal_target_notional_sum": float(ideal_notional_sum),
        "actual_entry_notional_sum": float(actual_notional_sum),
        "target_notional_fill_ratio": (
            float(actual_notional_sum/ideal_notional_sum)
            if ideal_notional_sum > 0 else None
        ),
        "total_return": total_return,
        "cagr": cagr,
        "max_drawdown": float(dd.min()),
        "sharpe": sharpe,
        "avg_gross_weight": avg_gross,
        "max_gross_weight": float(c["gross_weight"].max()),
        "capital_efficiency": cagr / avg_gross if avg_gross > 0 else None,
        "final_equity": float(eq.iloc[-1]),
    }
