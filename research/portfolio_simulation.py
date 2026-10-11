from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

import math
import pandas as pd


@dataclass(frozen=True)
class PortfolioSimulationConfig:
    initial_capital: float = 1_000_000.0
    max_positions: int = 10
    holding_days: int = 20
    round_trip_cost: float = 0.002
    annual_sessions: int = 252
    tie_break: str = "SYMBOL_ASC"


@dataclass
class _Slot:
    slot_id: int
    cash: float
    symbol: Optional[str] = None
    shares: float = 0.0
    entry_date: Optional[str] = None
    entry_price: Optional[float] = None
    exit_date: Optional[str] = None
    signal_date: Optional[str] = None
    level: Optional[float] = None
    entry_notional: float = 0.0
    slot_cash_at_entry: float = 0.0
    portfolio_equity_before_entry: Optional[float] = None
    entry_weight_vs_portfolio: Optional[float] = None
    entry_open_gap_vs_prev_close: Optional[float] = None
    entry_one_price: Optional[bool] = None
    fillability_suspect: Optional[bool] = None

    @property
    def occupied(self) -> bool:
        return self.symbol is not None


def _frame(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame):
        raise TypeError("history must be pandas.DataFrame")
    x = df.copy()
    x.columns = [str(c).lower() for c in x.columns]
    for c in x.columns:
        x[c] = pd.to_numeric(x[c], errors="coerce")
    return x


def prepare_fixed_horizon_candidates(
    history: Mapping[str, pd.DataFrame],
    events: Iterable[Dict[str, Any]],
    holding_days: int,
) -> List[Dict[str, Any]]:
    """Turn signal-day events into executable next-open / fixed-close candidates.

    Signal at bar i -> entry at bar i+1 open -> exit at bar i+holding_days close.
    With holding_days=20 this is a 20-session holding window from T+1 through T+20.
    """
    holding_days = int(holding_days)
    if holding_days < 1:
        raise ValueError("holding_days must be >= 1")

    out: List[Dict[str, Any]] = []
    for event in events:
        symbol = str(event["symbol"])
        df = history.get(symbol)
        if df is None:
            continue
        # Keep row indexing aligned with Backtest Gate event generation.
        required = ["open", "high", "low", "close", "volume"]
        x = _frame(df)
        if not set(required).issubset(x.columns):
            continue
        x = x.dropna(subset=required).copy()
        i = int(event["signal_pos"])
        entry_pos = i + 1
        exit_pos = i + holding_days
        if i < 0 or entry_pos >= len(x) or exit_pos >= len(x):
            continue

        entry = float(x["open"].iloc[entry_pos])
        exit_price = float(x["close"].iloc[exit_pos])
        if not math.isfinite(entry) or not math.isfinite(exit_price) or entry <= 0 or exit_price <= 0:
            continue

        prev_close = float(x["close"].iloc[entry_pos - 1]) if entry_pos > 0 else None
        entry_high = float(x["high"].iloc[entry_pos])
        entry_low = float(x["low"].iloc[entry_pos])
        open_gap = (entry / prev_close - 1.0) if prev_close and prev_close > 0 else None
        tol = max(1e-8, abs(entry) * 1e-8)
        one_price = bool(abs(entry - entry_high) <= tol and abs(entry - entry_low) <= tol)
        fillability_suspect = bool(one_price and open_gap is not None and open_gap >= 0.045)
        amount = None
        if "amount" in x.columns and pd.notna(x["amount"].iloc[entry_pos]):
            amount = float(x["amount"].iloc[entry_pos])

        out.append({
            "symbol": symbol,
            "strategy": str(event.get("strategy", "BREAKOUT_RETEST")),
            "signal_pos": i,
            "signal_date": str(event.get("signal_date", x.index[i])),
            "entry_date": str(x.index[entry_pos]),
            "entry_price": entry,
            "exit_date": str(x.index[exit_pos]),
            "exit_price": exit_price,
            "level": event.get("level"),
            "entry_open_gap_vs_prev_close": open_gap,
            "entry_one_price": one_price,
            "entry_amount": amount,
            "fillability_suspect": fillability_suspect,
        })

    return sorted(out, key=lambda r: (r["entry_date"], r["symbol"]))


class BreakoutRetestPortfolioSimulator:
    """Slot-based portfolio simulation for promoted BreakoutRetest signals.

    Each slot starts with initial_capital / max_positions. A signal can occupy one
    free slot at next-session open. The slot remains occupied until fixed-horizon
    close. Slots compound independently; inactive slots stay in cash.

    v1 intentionally uses a deterministic SYMBOL_ASC tie-break for simultaneous
    signals. It is an engineering rule, not an alpha ranking rule, and skipped
    signals are audited explicitly.
    """

    def __init__(self, config: Optional[PortfolioSimulationConfig] = None):
        self.cfg = config or PortfolioSimulationConfig()
        if self.cfg.initial_capital <= 0:
            raise ValueError("initial_capital must be > 0")
        if self.cfg.max_positions < 1:
            raise ValueError("max_positions must be >= 1")
        if self.cfg.holding_days < 1:
            raise ValueError("holding_days must be >= 1")
        if not (0 <= self.cfg.round_trip_cost < 1):
            raise ValueError("round_trip_cost must be in [0, 1)")
        if self.cfg.tie_break != "SYMBOL_ASC":
            raise ValueError("v1 supports only tie_break=SYMBOL_ASC")

    def simulate(
        self,
        history: Mapping[str, pd.DataFrame],
        events: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        candidates = prepare_fixed_horizon_candidates(
            history,
            events,
            self.cfg.holding_days,
        )

        per_symbol: Dict[str, pd.DataFrame] = {
            str(s): _frame(df).copy() for s, df in history.items()
        }
        close_maps: Dict[str, Dict[str, float]] = {}
        calendar = set()
        for symbol, df in per_symbol.items():
            if "close" not in df.columns:
                continue
            close_maps[symbol] = {
                str(idx): float(v)
                for idx, v in df["close"].items()
                if pd.notna(v) and math.isfinite(float(v))
            }
            calendar.update(close_maps[symbol].keys())

        if not candidates:
            return self._empty_result()

        start_date = min(c["entry_date"] for c in candidates)
        end_date = max(c["exit_date"] for c in candidates)
        dates = sorted(d for d in calendar if start_date <= d <= end_date)
        if not dates:
            return self._empty_result()

        slot_cash = self.cfg.initial_capital / self.cfg.max_positions
        slots = [_Slot(i, slot_cash) for i in range(self.cfg.max_positions)]
        entry_fee = self.cfg.round_trip_cost / 2.0
        exit_fee = self.cfg.round_trip_cost / 2.0

        entries_by_date: Dict[str, List[Dict[str, Any]]] = {}
        for c in candidates:
            entries_by_date.setdefault(c["entry_date"], []).append(c)
        for day in entries_by_date:
            entries_by_date[day].sort(key=lambda r: r["symbol"])

        last_close: Dict[str, float] = {}
        trades: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        equity_curve: List[Dict[str, Any]] = []
        daily_audit: List[Dict[str, Any]] = []
        skipped_no_slot = 0
        skipped_duplicate_symbol = 0
        max_concurrent = 0
        gross_traded_value = 0.0

        for date in dates:
            # Entry happens at the open. For audit purposes, portfolio equity at
            # entry is marked using prior-known closes (never today's close).
            portfolio_equity_before_entry = 0.0
            for slot in slots:
                if not slot.occupied:
                    portfolio_equity_before_entry += slot.cash
                else:
                    mark = last_close.get(str(slot.symbol), float(slot.entry_price or 0.0))
                    portfolio_equity_before_entry += slot.shares * mark

            day_candidates = entries_by_date.get(date, [])
            held_symbols = {s.symbol for s in slots if s.occupied}
            for allocation_rank, cand in enumerate(day_candidates, start=1):
                symbol = cand["symbol"]
                free_count = sum(1 for s in slots if not s.occupied)
                cf_return = (
                    float(cand["exit_price"]) / float(cand["entry_price"])
                    * (1.0 - exit_fee) / (1.0 + entry_fee) - 1.0
                )
                common_skip = {
                    "symbol": symbol,
                    "signal_date": cand["signal_date"],
                    "entry_date": cand["entry_date"],
                    "exit_date": cand["exit_date"],
                    "allocation_rank": allocation_rank,
                    "n_candidates_same_day": len(day_candidates),
                    "n_free_slots_at_decision": free_count,
                    "counterfactual_net_return": cf_return,
                    "entry_open_gap_vs_prev_close": cand.get("entry_open_gap_vs_prev_close"),
                    "entry_one_price": cand.get("entry_one_price"),
                    "entry_amount": cand.get("entry_amount"),
                    "fillability_suspect": cand.get("fillability_suspect", False),
                }

                if symbol in held_symbols:
                    skipped_duplicate_symbol += 1
                    skipped.append({**common_skip, "reason": "duplicate_symbol"})
                    continue

                free = next((s for s in slots if not s.occupied), None)
                if free is None:
                    skipped_no_slot += 1
                    skipped.append({**common_skip, "reason": "no_slot"})
                    continue

                entry_price = float(cand["entry_price"])
                slot_cash_before = float(free.cash)
                notional = slot_cash_before / (1.0 + entry_fee)
                shares = notional / entry_price
                gross_traded_value += notional

                free.symbol = symbol
                free.shares = shares
                free.entry_date = date
                free.entry_price = entry_price
                free.exit_date = cand["exit_date"]
                free.signal_date = cand["signal_date"]
                free.level = cand.get("level")
                free.entry_notional = notional
                free.slot_cash_at_entry = slot_cash_before
                free.portfolio_equity_before_entry = portfolio_equity_before_entry
                free.entry_weight_vs_portfolio = (
                    slot_cash_before / portfolio_equity_before_entry
                    if portfolio_equity_before_entry > 0 else None
                )
                free.entry_open_gap_vs_prev_close = cand.get("entry_open_gap_vs_prev_close")
                free.entry_one_price = cand.get("entry_one_price")
                free.fillability_suspect = cand.get("fillability_suspect", False)
                free.cash = 0.0
                held_symbols.add(symbol)

            max_concurrent = max(max_concurrent, sum(1 for s in slots if s.occupied))

            # Today's close becomes known only after entries were decided.
            for symbol, cmap in close_maps.items():
                if date in cmap:
                    last_close[symbol] = cmap[date]

            # Exit happens at the close.
            for slot in slots:
                if not slot.occupied or slot.exit_date != date:
                    continue
                symbol = str(slot.symbol)
                cmap = close_maps.get(symbol, {})
                exit_price = cmap.get(date)
                if exit_price is None:
                    raise RuntimeError(
                        f"prepared exit price missing for {symbol} on {date}; "
                        "candidate/history index alignment is inconsistent"
                    )

                gross_proceeds = slot.shares * exit_price
                exit_cost = gross_proceeds * exit_fee
                net_proceeds = gross_proceeds - exit_cost
                gross_traded_value += gross_proceeds

                initial_slot_value = slot.entry_notional * (1.0 + entry_fee)
                net_return = net_proceeds / initial_slot_value - 1.0
                trades.append({
                    "slot_id": slot.slot_id,
                    "symbol": symbol,
                    "strategy": "BREAKOUT_RETEST",
                    "signal_date": slot.signal_date,
                    "entry_date": slot.entry_date,
                    "entry_price": slot.entry_price,
                    "exit_date": date,
                    "exit_price": exit_price,
                    "level": slot.level,
                    "entry_notional": slot.entry_notional,
                    "slot_cash_at_entry": slot.slot_cash_at_entry,
                    "portfolio_equity_before_entry": slot.portfolio_equity_before_entry,
                    "entry_weight_vs_portfolio": slot.entry_weight_vs_portfolio,
                    "entry_open_gap_vs_prev_close": slot.entry_open_gap_vs_prev_close,
                    "entry_one_price": slot.entry_one_price,
                    "fillability_suspect": slot.fillability_suspect,
                    "net_return": net_return,
                })

                slot.cash = net_proceeds
                slot.symbol = None
                slot.shares = 0.0
                slot.entry_date = None
                slot.entry_price = None
                slot.exit_date = None
                slot.signal_date = None
                slot.level = None
                slot.entry_notional = 0.0
                slot.slot_cash_at_entry = 0.0
                slot.portfolio_equity_before_entry = None
                slot.entry_weight_vs_portfolio = None
                slot.entry_open_gap_vs_prev_close = None
                slot.entry_one_price = None
                slot.fillability_suspect = None

            total_equity = 0.0
            invested_value = 0.0
            active = 0
            for slot in slots:
                if not slot.occupied:
                    total_equity += slot.cash
                    continue
                active += 1
                mark = last_close.get(str(slot.symbol), float(slot.entry_price or 0.0))
                value = slot.shares * mark
                invested_value += value
                total_equity += value

            equity_curve.append({
                "date": date,
                "equity": total_equity,
                "active_positions": active,
                "exposure": invested_value / total_equity if total_equity > 0 else 0.0,
            })
            daily_audit.append({
                "date": date,
                "equity": total_equity,
                "active_positions": active,
                "free_slots": self.cfg.max_positions - active,
                "occupied_slot_ids": [s.slot_id for s in slots if s.occupied],
                "cash_total": sum(s.cash for s in slots if not s.occupied),
            })

        metrics = self._metrics(equity_curve, trades, gross_traded_value)
        return {
            "config": asdict(self.cfg),
            "summary": {
                **metrics,
                "signals_executable": len(candidates),
                "trades_executed": len(trades),
                "skipped_no_slot": skipped_no_slot,
                "skipped_duplicate_symbol": skipped_duplicate_symbol,
                "max_concurrent_positions": max_concurrent,
                "executed_fillability_suspect": sum(
                    1 for t in trades if t.get("fillability_suspect")
                ),
                "skipped_fillability_suspect": sum(
                    1 for r in skipped if r.get("fillability_suspect")
                ),
            },
            "trades": trades,
            "skipped": skipped,
            "equity_curve": equity_curve,
            "daily_audit": daily_audit,
        }

    def _empty_result(self) -> Dict[str, Any]:
        return {
            "config": asdict(self.cfg),
            "summary": {
                "start_date": None,
                "end_date": None,
                "sessions": 0,
                "total_return": 0.0,
                "cagr": None,
                "max_drawdown": 0.0,
                "sharpe": None,
                "avg_exposure": 0.0,
                "avg_active_positions": 0.0,
                "trade_win_rate": None,
                "avg_trade_return": None,
                "median_trade_return": None,
                "turnover": 0.0,
                "turnover_cumulative_double_sided": 0.0,
                "turnover_annualized_double_sided": None,
                "turnover_annualized_single_sided": None,
                "avg_trade_return_capital_weighted": None,
                "geometric_avg_trade_return": None,
                "signals_executable": 0,
                "trades_executed": 0,
                "skipped_no_slot": 0,
                "skipped_duplicate_symbol": 0,
                "max_concurrent_positions": 0,
                "executed_fillability_suspect": 0,
                "skipped_fillability_suspect": 0,
            },
            "trades": [],
            "skipped": [],
            "equity_curve": [],
            "daily_audit": [],
        }

    def _metrics(
        self,
        curve: Sequence[Dict[str, Any]],
        trades: Sequence[Dict[str, Any]],
        gross_traded_value: float,
    ) -> Dict[str, Any]:
        if not curve:
            return self._empty_result()["summary"]

        equities = pd.Series([float(r["equity"]) for r in curve], dtype="float64")
        equity_with_initial = pd.concat(
            [pd.Series([self.cfg.initial_capital], dtype="float64"), equities],
            ignore_index=True,
        )
        daily_ret = equity_with_initial.pct_change().dropna()
        total_return = float(equities.iloc[-1] / self.cfg.initial_capital - 1.0)
        sessions = len(curve)

        calendar_years = None
        try:
            start_dt = pd.to_datetime(str(curve[0]["date"]), format="%Y%m%d")
            end_dt = pd.to_datetime(str(curve[-1]["date"]), format="%Y%m%d")
            days = max(1, int((end_dt - start_dt).days))
            calendar_years = days / 365.2425
        except Exception:
            calendar_years = sessions / float(self.cfg.annual_sessions) if sessions > 0 else None

        ratio = float(equities.iloc[-1] / self.cfg.initial_capital)
        cagr_calendar = None
        if calendar_years and calendar_years > 0 and ratio > 0:
            cagr_calendar = float(ratio ** (1.0 / calendar_years) - 1.0)
        cagr_252 = None
        if sessions > 0 and ratio > 0:
            cagr_252 = float(ratio ** (self.cfg.annual_sessions / sessions) - 1.0)
        cagr = cagr_calendar

        running_max = equity_with_initial.cummax()
        drawdowns = equity_with_initial / running_max - 1.0
        max_drawdown = float(drawdowns.min())

        actual_sessions_per_year = (sessions / calendar_years) if calendar_years and calendar_years > 0 else float(self.cfg.annual_sessions)
        sharpe_actual = None
        sharpe_252 = None
        if len(daily_ret) > 1:
            sd = float(daily_ret.std(ddof=1))
            if sd > 0:
                base = float(daily_ret.mean() / sd)
                sharpe_actual = float(base * math.sqrt(actual_sessions_per_year))
                sharpe_252 = float(base * math.sqrt(self.cfg.annual_sessions))
        sharpe = sharpe_actual

        trade_returns = [float(t["net_return"]) for t in trades]
        win_rate = (sum(1 for r in trade_returns if r > 0) / len(trade_returns)) if trade_returns else None
        avg_trade = float(sum(trade_returns) / len(trade_returns)) if trade_returns else None
        median_trade = float(pd.Series(trade_returns).median()) if trade_returns else None

        capital_weighted_trade = None
        if trades:
            capital = sum(float(t.get("slot_cash_at_entry") or 0.0) for t in trades)
            pnl = sum(
                float(t.get("slot_cash_at_entry") or 0.0) * float(t["net_return"])
                for t in trades
            )
            capital_weighted_trade = float(pnl / capital) if capital > 0 else None

        geometric_trade = None
        if trade_returns:
            factor = 1.0
            valid = True
            for r in trade_returns:
                if 1.0 + r <= 0:
                    valid = False
                    break
                factor *= 1.0 + r
            if valid and factor > 0:
                geometric_trade = float(factor ** (1.0 / len(trade_returns)) - 1.0)

        avg_equity = float(equities.mean()) if len(equities) else self.cfg.initial_capital
        turnover_cumulative = gross_traded_value / avg_equity if avg_equity > 0 else 0.0
        turnover_annual_double = (turnover_cumulative / calendar_years) if calendar_years and calendar_years > 0 else None
        turnover_annual_single = (turnover_annual_double / 2.0) if turnover_annual_double is not None else None

        calmar = None
        if cagr is not None and max_drawdown < 0:
            calmar = float(cagr / abs(max_drawdown))

        return {
            "start_date": str(curve[0]["date"]),
            "end_date": str(curve[-1]["date"]),
            "sessions": sessions,
            "total_return": total_return,
            "cagr": cagr,
            "cagr_calendar_basis": cagr_calendar,
            "cagr_252_session_basis": cagr_252,
            "calendar_years": calendar_years,
            "actual_sessions_per_year": actual_sessions_per_year,
            "max_drawdown": max_drawdown,
            "sharpe": sharpe,
            "sharpe_actual_sessions_including_first_day": sharpe_actual,
            "sharpe_252_including_first_day": sharpe_252,
            "calmar": calmar,
            "avg_exposure": float(sum(float(r["exposure"]) for r in curve) / len(curve)),
            "avg_active_positions": float(sum(int(r["active_positions"]) for r in curve) / len(curve)),
            "trade_win_rate": win_rate,
            "avg_trade_return": avg_trade,
            "median_trade_return": median_trade,
            "avg_trade_return_capital_weighted": capital_weighted_trade,
            "geometric_avg_trade_return": geometric_trade,
            "turnover": float(turnover_cumulative),
            "turnover_cumulative_double_sided": float(turnover_cumulative),
            "turnover_annualized_double_sided": turnover_annual_double,
            "turnover_annualized_single_sided": turnover_annual_single,
        }
