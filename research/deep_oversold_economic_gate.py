from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Sequence

import math
import numpy as np
import pandas as pd


FOLDS = (
    ("F1", "20200803", "20221017"),
    ("F2", "20221018", "20240919"),
    ("F3", "20240920", "20260924"),
)


@dataclass(frozen=True)
class EconomicProfile:
    name: str
    target_weight: float
    max_total_weight: float

    def validate(self) -> None:
        if not 0 < self.target_weight <= 0.10:
            raise ValueError("invalid target_weight")
        if not 0 < self.max_total_weight <= 1.0:
            raise ValueError("invalid max_total_weight")
        if self.target_weight > self.max_total_weight:
            raise ValueError("target_weight > max_total_weight")


PROFILES = (
    EconomicProfile("LOW_0_5x", 0.005, 0.15),
    EconomicProfile("BASE_1_0x", 0.010, 0.30),
    EconomicProfile("HIGH_1_5x", 0.015, 0.45),
    EconomicProfile("STRESS_2_0x", 0.020, 0.60),
)


def norm_date(v: Any) -> str:
    s = "".join(ch for ch in str(v).replace("-", "").replace("/", "") if ch.isdigit())
    if len(s) < 8:
        raise ValueError(f"invalid date {v!r}")
    return s[:8]


def normalize_history(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    x = df.copy()
    x.columns = [str(c).lower() for c in x.columns]
    idx = []
    for v in x.index:
        try:
            idx.append(pd.Timestamp(v).strftime("%Y%m%d"))
        except Exception:
            idx.append(norm_date(v))
    x.index = idx
    x = x[~x.index.duplicated(keep="last")].sort_index()
    for c in ("open", "high", "low", "close", "volume", "amount"):
        if c in x.columns:
            x[c] = pd.to_numeric(x[c], errors="coerce")
    return x


def load_fillable_events(path: str) -> pd.DataFrame:
    x = pd.read_csv(
        path,
        dtype={"symbol": str, "signal_date": str, "entry_date": str, "exit_date": str},
    )
    need = {
        "symbol", "signal_date", "entry_date", "exit_date",
        "standard_fillable",
    }
    missing = need - set(x.columns)
    if missing:
        raise ValueError(f"events missing columns: {sorted(missing)}")

    for c in ("signal_date", "entry_date", "exit_date"):
        x[c] = x[c].map(norm_date)

    fill = x["standard_fillable"]
    if fill.dtype == object:
        mask = fill.astype(str).str.lower().isin({"true", "1", "yes"})
    else:
        mask = fill.astype(bool)

    x = x.loc[mask].copy()
    # v1 replication event CSV intentionally stores returns/audit fields but
    # not raw entry_open/exit_close. Economic simulation therefore treats those
    # two columns as optional and reads exact prices from local history when absent.
    if "entry_open" in x.columns:
        x["entry_open"] = pd.to_numeric(x["entry_open"], errors="coerce")
    if "exit_close" in x.columns:
        x["exit_close"] = pd.to_numeric(x["exit_close"], errors="coerce")
    return x.sort_values(["signal_date", "symbol"]).reset_index(drop=True)


def dedup_while_held(events: pd.DataFrame) -> pd.DataFrame:
    kept = []
    last_exit: Dict[str, str] = {}
    for row in events.sort_values(["signal_date", "symbol"]).to_dict("records"):
        symbol = str(row["symbol"])
        signal_date = str(row["signal_date"])
        prior_exit = last_exit.get(symbol)
        if prior_exit is not None and signal_date < prior_exit:
            continue
        kept.append(row)
        last_exit[symbol] = str(row["exit_date"])
    return pd.DataFrame(kept, columns=events.columns)


def concurrency_demand(events: pd.DataFrame, market_dates: Sequence[str]) -> Dict[str, Any]:
    dates = [norm_date(d) for d in market_dates]
    rows = []
    for d in dates:
        active = int(((events["entry_date"] <= d) & (events["exit_date"] >= d)).sum())
        entries = int((events["entry_date"] == d).sum())
        exits = int((events["exit_date"] == d).sum())
        rows.append((d, active, entries, exits))
    x = pd.DataFrame(rows, columns=["date", "active", "entries", "exits"])
    active = x["active"]
    entries = x["entries"]
    return {
        "days": int(len(x)),
        "active_mean": float(active.mean()),
        "active_median": float(active.median()),
        "active_p90": float(active.quantile(0.90)),
        "active_p95": float(active.quantile(0.95)),
        "active_p99": float(active.quantile(0.99)),
        "active_max": int(active.max()),
        "entry_days": int((entries > 0).sum()),
        "entries_per_entry_day_median": float(entries[entries > 0].median()) if (entries > 0).any() else 0.0,
        "entries_per_entry_day_p90": float(entries[entries > 0].quantile(0.90)) if (entries > 0).any() else 0.0,
        "entries_per_entry_day_max": int(entries.max()),
    }


def _price(
    histories: Mapping[str, pd.DataFrame],
    symbol: str,
    date: str,
    field: str,
    fallback: float | None = None,
) -> float:
    df = histories[symbol]
    if date in df.index and field in df.columns:
        v = df.at[date, field]
        if pd.notna(v) and float(v) > 0:
            return float(v)
    if fallback is not None and fallback > 0:
        return float(fallback)
    raise ValueError(f"missing {field}: {symbol} {date}")


def simulate_shared_equity(
    events: pd.DataFrame,
    histories: Mapping[str, pd.DataFrame],
    market_dates: Sequence[str],
    profile: EconomicProfile,
    round_trip_cost: float,
    initial_equity: float = 1_000_000.0,
) -> Dict[str, Any]:
    """
    Shared-account simulator for the frozen Deep Oversold replication event set.

    - one position per symbol (events are already first-signal-while-flat)
    - fractional shares, research only
    - fixed target_weight of current equity at entry open
    - same-day entries share any remaining gross budget pro-rata
    - no symbol ranking / no tie-break selection
    - existing positions are not rebalanced
    - exit at exact predeclared close
    - half the round-trip cost applied at entry and half at exit
    """
    profile.validate()
    if not 0 <= round_trip_cost < 0.10:
        raise ValueError("invalid round_trip_cost")

    dates = [norm_date(d) for d in market_dates]
    date_set = set(dates)
    e = events[
        events["entry_date"].isin(date_set) & events["exit_date"].isin(date_set)
    ].copy()

    entries_by_date = {
        d: g.sort_values("symbol").to_dict("records")
        for d, g in e.groupby("entry_date")
    }
    exits_by_date = {
        d: g.sort_values("symbol").to_dict("records")
        for d, g in e.groupby("exit_date")
    }

    half = round_trip_cost / 2.0
    cash = float(initial_equity)
    positions: Dict[str, Dict[str, Any]] = {}
    curve = []
    trades = []
    scaled_entry_days = 0
    zero_capacity_entry_days = 0
    gross_limit_bind_days = 0
    entry_notional_total = 0.0
    exit_notional_total = 0.0

    last_close: Dict[str, float] = {}

    for date in dates:
        # Open mark for positions already held.
        open_mv = 0.0
        for symbol, pos in positions.items():
            px = _price(histories, symbol, date, "open", last_close.get(symbol, pos["entry_price"]))
            open_mv += pos["shares"] * px

        equity_open = cash + open_mv
        gross_cap = max(0.0, profile.max_total_weight * equity_open)
        remaining_gross = max(0.0, gross_cap - open_mv)
        remaining_cash_notional = max(0.0, cash / (1.0 + half))
        available_new_notional = min(remaining_gross, remaining_cash_notional)

        new_rows = [
            r for r in entries_by_date.get(date, [])
            if r["symbol"] not in positions
        ]

        if new_rows:
            desired_each = profile.target_weight * equity_open
            desired_total = desired_each * len(new_rows)
            alloc_total = min(desired_total, available_new_notional)
            scale = alloc_total / desired_total if desired_total > 0 else 0.0

            if scale < 0.999999:
                scaled_entry_days += 1
                gross_limit_bind_days += 1
            if alloc_total <= 0:
                zero_capacity_entry_days += 1

            for row in new_rows:
                if alloc_total <= 0:
                    break
                symbol = str(row["symbol"])
                entry_price = float(row.get("entry_open")) if row.get("entry_open") is not None and pd.notna(row.get("entry_open")) else _price(histories, symbol, date, "open")
                notional = desired_each * scale
                if notional <= 0:
                    continue
                shares = notional / entry_price
                entry_fee = notional * half
                if notional + entry_fee > cash + 1e-8:
                    raise AssertionError("cash invariant violated at entry")

                cash -= notional + entry_fee
                positions[symbol] = {
                    "shares": shares,
                    "entry_price": entry_price,
                    "entry_date": date,
                    "exit_date": str(row["exit_date"]),
                    "signal_date": str(row["signal_date"]),
                    "entry_notional": notional,
                    "entry_fee": entry_fee,
                    "last_value_price": entry_price,
                }
                entry_notional_total += notional

        # Close mark and exact exits.
        close_mv_before_exit = 0.0
        for symbol, pos in positions.items():
            px = _price(
                histories, symbol, date, "close",
                last_close.get(symbol, pos["last_value_price"]),
            )
            pos["last_value_price"] = px
            last_close[symbol] = px
            close_mv_before_exit += pos["shares"] * px

        for row in exits_by_date.get(date, []):
            symbol = str(row["symbol"])
            pos = positions.get(symbol)
            if pos is None:
                continue
            exit_price = float(row.get("exit_close")) if row.get("exit_close") is not None and pd.notna(row.get("exit_close")) else _price(histories, symbol, date, "close", pos["last_value_price"])
            gross_proceeds = pos["shares"] * exit_price
            exit_fee = gross_proceeds * half
            cash += gross_proceeds - exit_fee
            exit_notional_total += gross_proceeds

            pnl = (
                gross_proceeds - exit_fee
                - pos["entry_notional"] - pos["entry_fee"]
            )
            trades.append({
                "symbol": symbol,
                "signal_date": pos["signal_date"],
                "entry_date": pos["entry_date"],
                "exit_date": date,
                "entry_notional": pos["entry_notional"],
                "exit_notional": gross_proceeds,
                "net_pnl": pnl,
                "net_return_on_entry_notional": pnl / pos["entry_notional"],
            })
            del positions[symbol]

        close_mv = 0.0
        for symbol, pos in positions.items():
            close_mv += pos["shares"] * pos["last_value_price"]

        equity_close = cash + close_mv
        gross_weight = close_mv / equity_close if equity_close > 0 else 0.0
        curve.append({
            "date": date,
            "equity": equity_close,
            "cash": cash,
            "gross_market_value": close_mv,
            "gross_weight": gross_weight,
            "active_positions": len(positions),
        })

    curve_df = pd.DataFrame(curve)
    trades_df = pd.DataFrame(trades)

    if positions:
        raise AssertionError(f"positions remain after final calendar date: {len(positions)}")

    eq = curve_df["equity"].astype(float)
    daily_ret = eq.pct_change(fill_method=None).fillna(0.0)
    running_max = eq.cummax()
    dd = eq / running_max - 1.0

    first_date = pd.Timestamp(curve_df["date"].iloc[0])
    last_date = pd.Timestamp(curve_df["date"].iloc[-1])
    years = max((last_date - first_date).days / 365.2425, 1 / 365.2425)
    final_equity = float(eq.iloc[-1])
    total_return = final_equity / initial_equity - 1.0
    cagr = (final_equity / initial_equity) ** (1.0 / years) - 1.0

    std = float(daily_ret.std(ddof=1))
    sharpe = float(daily_ret.mean() / std * math.sqrt(252)) if std > 0 else None

    turnover_single = (entry_notional_total + exit_notional_total) / (2.0 * initial_equity)
    turnover_annualized = turnover_single / years

    return {
        "profile": {
            "name": profile.name,
            "target_weight": profile.target_weight,
            "max_total_weight": profile.max_total_weight,
            "nominal_slots": profile.max_total_weight / profile.target_weight,
        },
        "round_trip_cost": round_trip_cost,
        "trades": int(len(trades_df)),
        "total_return": total_return,
        "cagr": cagr,
        "max_drawdown": float(dd.min()),
        "sharpe": sharpe,
        "final_equity": final_equity,
        "avg_gross_weight": float(curve_df["gross_weight"].mean()),
        "max_gross_weight": float(curve_df["gross_weight"].max()),
        "avg_active_positions": float(curve_df["active_positions"].mean()),
        "max_active_positions": int(curve_df["active_positions"].max()),
        "invested_day_fraction": float((curve_df["active_positions"] > 0).mean()),
        "scaled_entry_days": int(scaled_entry_days),
        "zero_capacity_entry_days": int(zero_capacity_entry_days),
        "gross_limit_bind_days": int(gross_limit_bind_days),
        "annualized_single_turnover": float(turnover_annualized),
        "cagr_per_avg_gross": (
            cagr / float(curve_df["gross_weight"].mean())
            if float(curve_df["gross_weight"].mean()) > 0 else None
        ),
        "trade_stats": {
            "avg_net_return": float(trades_df["net_return_on_entry_notional"].mean()) if len(trades_df) else None,
            "median_net_return": float(trades_df["net_return_on_entry_notional"].median()) if len(trades_df) else None,
            "win_rate": float((trades_df["net_return_on_entry_notional"] > 0).mean()) if len(trades_df) else None,
        },
        "curve": curve_df,
        "trades_df": trades_df,
    }


def period_return(curve: pd.DataFrame, start: str, end: str) -> Dict[str, Any]:
    x = curve[(curve["date"] >= start) & (curve["date"] <= end)].copy()
    if len(x) < 2:
        return {"start": start, "end": end, "days": int(len(x)), "return": None, "max_drawdown": None}
    eq = x["equity"].astype(float)
    ret = float(eq.iloc[-1] / eq.iloc[0] - 1.0)
    dd = eq / eq.cummax() - 1.0
    return {
        "start": start,
        "end": end,
        "days": int(len(x)),
        "return": ret,
        "max_drawdown": float(dd.min()),
    }


def compact_result(sim: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        k: v for k, v in sim.items()
        if k not in {"curve", "trades_df"}
    }


def economic_gate(base_20: Mapping[str, Any], base_50: Mapping[str, Any]) -> Dict[str, Any]:
    """
    Predeclared v1 research gate for the BASE 1% / 30% profile.

    The thresholds are intentionally modest and economic, not optimized:
    - account CAGR >= 3% at 20bp
    - account MDD <= 15%
    - still positive CAGR at 50bp
    - at least 2/3 calendar folds positive at 20bp
    - CAGR / average gross exposure >= 10%
    """
    folds = base_20["fold_metrics"]
    positive_folds = sum(1 for x in folds if x["return"] is not None and x["return"] > 0)
    checks = {
        "cagr_20bps_ge_3pct": base_20["cagr"] >= 0.03,
        "maxdd_20bps_within_15pct": base_20["max_drawdown"] >= -0.15,
        "cagr_50bps_positive": base_50["cagr"] > 0,
        "positive_folds_20bps": positive_folds,
        "positive_folds_required": 2,
        "capital_efficiency_ge_10pct": (
            base_20["cagr_per_avg_gross"] is not None
            and base_20["cagr_per_avg_gross"] >= 0.10
        ),
    }
    checks["economic_candidate"] = (
        checks["cagr_20bps_ge_3pct"]
        and checks["maxdd_20bps_within_15pct"]
        and checks["cagr_50bps_positive"]
        and positive_folds >= 2
        and checks["capital_efficiency_ge_10pct"]
    )
    return checks
