from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from xtquant import xtdata


ROOT = Path(__file__).resolve().parents[1]
EDGE_DIR = ROOT / "data" / "research" / "edge_discovery"
TEMPORAL_DIR = EDGE_DIR / "ret120_temporal_v01"
OUT_DIR = EDGE_DIR / "ret120_shared_equity_v01"

UNIVERSES = ("csi300", "csi500")
HORIZON = 120
CUTOFF = "20260924"
CALENDAR_SYMBOL = "000001.SH"
COST_SCENARIOS = {
    "0bp": 0.0,
    "20bp": 0.0020,
    "50bp": 0.0050,
}
INITIAL_EQUITY = 1.0


def finite(x: Any) -> bool:
    try:
        return bool(np.isfinite(float(x)))
    except (TypeError, ValueError):
        return False


def normalize_date(v: Any) -> str | None:
    s = "".join(ch for ch in str(v) if ch.isdigit())
    if len(s) < 8:
        return None
    d = s[:8]
    try:
        datetime.strptime(d, "%Y%m%d")
    except ValueError:
        return None
    return d


def assign_quintiles(values: pd.Series) -> pd.Series:
    valid = values.notna()
    out = pd.Series(index=values.index, dtype="object")
    if valid.sum() < 5:
        return out
    ranks = values[valid].rank(method="first")
    out.loc[valid] = pd.qcut(
        ranks,
        5,
        labels=["Q1", "Q2", "Q3", "Q4", "Q5"],
    ).astype(str).to_numpy()
    return out


def curve_metrics(curve: pd.DataFrame) -> dict[str, float]:
    if len(curve) < 2:
        return {
            "final_equity": math.nan,
            "cagr": math.nan,
            "max_drawdown": math.nan,
            "sharpe": math.nan,
            "avg_gross_exposure": math.nan,
            "max_gross_exposure": math.nan,
            "avg_unique_positions": math.nan,
            "max_unique_positions": math.nan,
            "max_symbol_weight": math.nan,
        }

    eq = curve["equity"].to_numpy(dtype=float)
    rets = pd.Series(eq).pct_change().fillna(0.0).to_numpy(dtype=float)

    running_max = np.maximum.accumulate(eq)
    dd = eq / running_max - 1.0

    years = (len(eq) - 1) / 252.0
    cagr = (
        float((eq[-1] / eq[0]) ** (1.0 / years) - 1.0)
        if years > 0 and eq[0] > 0 and eq[-1] > 0
        else math.nan
    )

    sd = rets.std(ddof=1)
    sharpe = (
        float(rets.mean() / sd * np.sqrt(252.0))
        if np.isfinite(sd) and sd > 0
        else math.nan
    )

    return {
        "final_equity": float(eq[-1]),
        "cagr": cagr,
        "max_drawdown": float(dd.min()),
        "sharpe": sharpe,
        "avg_gross_exposure": float(curve["gross_exposure"].mean()),
        "max_gross_exposure": float(curve["gross_exposure"].max()),
        "avg_unique_positions": float(curve["unique_positions"].mean()),
        "max_unique_positions": int(curve["unique_positions"].max()),
        "max_symbol_weight": float(curve["max_symbol_weight"].max()),
    }


def turnover_metrics(
    curve: pd.DataFrame,
    trades: pd.DataFrame,
) -> dict[str, float]:
    if curve.empty or trades.empty:
        return {
            "gross_traded_notional": 0.0,
            "buy_notional": 0.0,
            "sell_notional": 0.0,
            "annualized_gross_turnover": 0.0,
            "annualized_one_way_equivalent": 0.0,
        }

    buy = float(trades.loc[trades["side"] == "BUY", "notional"].sum())
    sell = float(trades.loc[trades["side"] == "SELL", "notional"].sum())
    gross = buy + sell

    avg_equity = float(curve["equity"].mean())
    years = max((len(curve) - 1) / 252.0, 1.0 / 252.0)

    annual_gross = gross / avg_equity / years if avg_equity > 0 else math.nan

    return {
        "gross_traded_notional": gross,
        "buy_notional": buy,
        "sell_notional": sell,
        "annualized_gross_turnover": annual_gross,
        "annualized_one_way_equivalent": annual_gross / 2.0,
    }


def load_folds() -> dict[str, tuple[str, str]]:
    path = TEMPORAL_DIR / "audit.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = payload["folds"]
    return {
        k: (str(raw[k]["start"]), str(raw[k]["end"]))
        for k in ("F1", "F2", "F3")
    }


def assign_fold(d: str, folds: dict[str, tuple[str, str]]) -> str | None:
    for name in ("F1", "F2", "F3"):
        a, b = folds[name]
        if a <= d <= b:
            return name
    return None


def panel_path(universe: str) -> Path:
    return EDGE_DIR / f"trend_persistence_scan_{universe}_panel_v0221.parquet"


def bucket_path(universe: str) -> Path:
    return EDGE_DIR / f"trend_persistence_scan_{universe}_daily_buckets_v0221.parquet"


def load_q5(
    con: duckdb.DuckDBPyConnection,
    universe: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    p = panel_path(universe)
    b = bucket_path(universe)

    qp = p.as_posix().replace("'", "''")
    qb = b.as_posix().replace("'", "''")

    panel = con.execute(
        f"""
        SELECT signal_date, symbol, ret120, excess_120
        FROM read_parquet('{qp}')
        WHERE signal_date <= '{CUTOFF}'
          AND ret120 IS NOT NULL
          AND excess_120 IS NOT NULL
          AND COALESCE(label_quality_reject_120, FALSE) = FALSE
        ORDER BY signal_date, symbol
        """
    ).fetchdf()

    frozen = con.execute(
        f"""
        SELECT signal_date, bucket, n, mean_excess
        FROM read_parquet('{qb}')
        WHERE feature = 'ret120'
          AND horizon = 120
          AND signal_date <= '{CUTOFF}'
        ORDER BY signal_date, bucket
        """
    ).fetchdf()

    panel["signal_date"] = panel["signal_date"].astype(str)
    panel["symbol"] = panel["symbol"].astype(str)
    frozen["signal_date"] = frozen["signal_date"].astype(str)

    parts = []
    for _, x in panel.groupby("signal_date", sort=True):
        y = x.copy()
        y["bucket"] = assign_quintiles(y["ret120"])
        parts.append(y)

    rebuilt = pd.concat(parts, ignore_index=True)

    calc = (
        rebuilt.groupby(["signal_date", "bucket"], observed=True)
        .agg(
            n_calc=("symbol", "size"),
            mean_excess_calc=("excess_120", "mean"),
        )
        .reset_index()
    )

    chk = frozen.merge(
        calc,
        on=["signal_date", "bucket"],
        how="outer",
        validate="one_to_one",
    )
    chk["n_diff"] = chk["n_calc"] - chk["n"]
    chk["mean_diff"] = chk["mean_excess_calc"] - chk["mean_excess"]

    count_mismatch = int((chk["n_diff"].fillna(999999) != 0).sum())
    max_mean_diff = float(chk["mean_diff"].abs().max())

    if count_mismatch != 0 or max_mean_diff > 1e-12:
        raise RuntimeError(
            f"Frozen bucket reconciliation failed for {universe}: "
            f"count_mismatch={count_mismatch}, max_mean_diff={max_mean_diff}"
        )

    q5 = rebuilt[rebuilt["bucket"] == "Q5"].copy()

    return q5, {
        "count_mismatch_rows": count_mismatch,
        "max_mean_abs_diff": max_mean_diff,
        "signal_dates": int(q5["signal_date"].nunique()),
        "q5_rows": int(len(q5)),
    }


def parse_market_ex(
    raw: Any,
    symbols: list[str],
    fields: tuple[str, ...],
) -> pd.DataFrame:
    if not isinstance(raw, dict):
        raise TypeError(type(raw))

    rows = []
    symbol_set = set(symbols)

    symbol_keys = [k for k in raw.keys() if str(k) in symbol_set]
    if symbol_keys:
        for symbol in symbol_keys:
            obj = raw[symbol]
            if not isinstance(obj, pd.DataFrame):
                continue
            cmap = {str(c).lower(): c for c in obj.columns}
            if not all(f in cmap for f in fields):
                continue

            data = {
                "date": [normalize_date(x) for x in obj.index],
                "symbol": str(symbol),
            }
            for f in fields:
                data[f] = pd.to_numeric(
                    obj[cmap[f]], errors="coerce"
                ).to_numpy()
            rows.append(pd.DataFrame(data))

    if not rows:
        key_map = {str(k).lower(): k for k in raw.keys()}
        if all(f in key_map for f in fields):
            first = raw[key_map[fields[0]]]
            if isinstance(first, pd.DataFrame):
                for symbol in first.columns:
                    if str(symbol) not in symbol_set:
                        continue
                    data = {
                        "date": [normalize_date(x) for x in first.index],
                        "symbol": str(symbol),
                    }
                    good = True
                    for f in fields:
                        obj = raw[key_map[f]]
                        if not isinstance(obj, pd.DataFrame) or symbol not in obj.columns:
                            good = False
                            break
                        data[f] = pd.to_numeric(
                            obj[symbol], errors="coerce"
                        ).to_numpy()
                    if good:
                        rows.append(pd.DataFrame(data))

    if not rows:
        raise RuntimeError("Could not normalize QMT market-data result.")

    out = pd.concat(rows, ignore_index=True)
    out = out.dropna(subset=["date"])
    out = out.drop_duplicates(["date", "symbol"], keep="last")
    return out


def get_calendar(start: str, end: str) -> list[str]:
    raw = xtdata.get_market_data_ex(
        field_list=["close"],
        stock_list=[CALENDAR_SYMBOL],
        period="1d",
        start_time=start,
        end_time=end,
        count=-1,
        dividend_type="none",
        fill_data=False,
    )
    df = parse_market_ex(raw, [CALENDAR_SYMBOL], ("close",))
    return sorted(
        d for d, px in zip(df["date"], df["close"])
        if d is not None and finite(px) and float(px) > 0
    )


def build_cohorts(
    q5: pd.DataFrame,
    calendar: list[str],
) -> pd.DataFrame:
    pos = {d: i for i, d in enumerate(calendar)}
    rows = []

    signal_dates = sorted(q5["signal_date"].unique().tolist())
    first_pos = pos.get(signal_dates[0])

    if first_pos is None:
        raise RuntimeError("First signal date missing from market calendar.")

    for seq, d in enumerate(signal_dates):
        i = pos.get(d)
        if i is None:
            raise RuntimeError(f"Signal date missing from market calendar: {d}")

        if i + 1 >= len(calendar) or i + HORIZON >= len(calendar):
            raise RuntimeError(f"Insufficient calendar after signal date: {d}")

        rows.append({
            "signal_date": d,
            "entry_date": calendar[i + 1],
            "exit_date": calendar[i + HORIZON],
            "slot_id": seq % HORIZON,
        })

    cohorts = pd.DataFrame(rows)

    # The frozen daily panels should provide one signal date per market session.
    entry_idx = [pos[d] for d in cohorts["entry_date"]]
    gaps = np.diff(entry_idx)
    if len(gaps) and not np.all(gaps == 1):
        raise RuntimeError(
            "Signal dates are not contiguous market sessions; "
            "120-sleeve cycling would no longer be exact."
        )

    return cohorts


def build_price_maps(
    market: pd.DataFrame,
    calendar: list[str],
    symbols: list[str],
):
    opens = {}
    closes = {}

    for _, r in market.iterrows():
        d = r["date"]
        s = r["symbol"]

        o = r.get("open")
        c = r.get("close")

        if finite(o) and float(o) > 0:
            opens[(d, s)] = float(o)
        if finite(c) and float(c) > 0:
            closes[(d, s)] = float(c)

    # Mark-to-market may carry the latest valid close through suspension days.
    last = {}
    mark = {}

    market_symbol_set = set(symbols)

    by_date = {}
    for (d, s), c in closes.items():
        by_date.setdefault(d, {})[s] = c

    for d in calendar:
        for s, c in by_date.get(d, {}).items():
            if s in market_symbol_set:
                last[s] = c
        # Store only a shallow snapshot; 1179 x ~1500 is acceptable.
        mark[d] = dict(last)

    return opens, closes, mark


@dataclass
class Lot:
    signal_date: str
    entry_date: str
    nominal_exit_date: str
    positions: dict[str, float] = field(default_factory=dict)


@dataclass
class Sleeve:
    cash: float
    lots: list[Lot] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return any(lot.positions for lot in self.lots)


def simulate(
    q5: pd.DataFrame,
    cohorts: pd.DataFrame,
    calendar: list[str],
    opens: dict[tuple[str, str], float],
    exact_closes: dict[tuple[str, str], float],
    mark_closes: dict[str, dict[str, float]],
    round_trip_cost: float,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """
    STANDARD_POSITION_DEFERRED_EXIT_V1

    Capital is shared through 120 sleeves, but deferred exits are handled at
    POSITION level:

    - At nominal S120 close, every constituent with an exact valid close is
      sold immediately.
    - Only constituents lacking an exact close remain invested.
    - Released cash stays in the same sleeve and is available at the next
      scheduled cohort entry.
    - A new cohort invests only the cash actually available at that OPEN.
      No fresh capital is injected.
    - A blocked position is retried at every later close and exits at its first
      later exact valid close.
    - Carried marks are valuation-only and are never treated as executable.

    This preserves the frozen signal/horizon while avoiding the overly
    conservative v0.1a rule where one blocked stock locked an entire sleeve.
    """
    side_cost = round_trip_cost / 2.0
    sleeves = [
        Sleeve(cash=INITIAL_EQUITY / HORIZON)
        for _ in range(HORIZON)
    ]

    q5_by_date = {
        d: g["symbol"].astype(str).tolist()
        for d, g in q5.groupby("signal_date", sort=False)
    }

    cohort_by_entry = {
        r.entry_date: r
        for r in cohorts.itertuples(index=False)
    }

    cal_pos = {d: i for i, d in enumerate(calendar)}

    first_entry = cohorts["entry_date"].min()
    last_nominal_exit = cohorts["exit_date"].max()
    sim_dates = [d for d in calendar if first_entry <= d <= last_nominal_exit]

    curve_rows = []
    trade_rows = []

    missing_entry_rows = 0
    deferred_position_attempts = 0
    positions_deferred_at_nominal_exit = 0
    completed_deferred_positions = 0
    cohorts_with_deferred_positions = set()
    max_exit_delay_sessions = 0
    mark_carry_count = 0
    skipped_cohorts_no_cash = 0
    partial_cash_entries = 0

    initial_sleeve_cash = INITIAL_EQUITY / HORIZON

    for d in sim_dates:
        # 1) Scheduled cohort enters at OPEN using only currently available
        # cash in its assigned sleeve.
        c = cohort_by_entry.get(d)
        if c is not None:
            sleeve = sleeves[int(c.slot_id)]
            available_cash = float(sleeve.cash)

            if available_cash <= 1e-15:
                skipped_cohorts_no_cash += 1
                trade_rows.append({
                    "date": d,
                    "signal_date": c.signal_date,
                    "slot_id": int(c.slot_id),
                    "side": "SKIP_NO_CASH",
                    "n_symbols": 0,
                    "notional": 0.0,
                    "fee": 0.0,
                })
            else:
                symbols = q5_by_date[c.signal_date]
                valid = [
                    s for s in symbols
                    if finite(opens.get((d, s))) and opens[(d, s)] > 0
                ]
                missing_entry_rows += len(symbols) - len(valid)

                if not valid:
                    trade_rows.append({
                        "date": d,
                        "signal_date": c.signal_date,
                        "slot_id": int(c.slot_id),
                        "side": "SKIP_NO_ENTRY_PRICE",
                        "n_symbols": 0,
                        "notional": 0.0,
                        "fee": 0.0,
                    })
                else:
                    if available_cash < initial_sleeve_cash * 0.999:
                        partial_cash_entries += 1

                    total_notional = available_cash / (1.0 + side_cost)
                    per_symbol = total_notional / len(valid)
                    fee = total_notional * side_cost

                    sleeve.cash = available_cash - total_notional - fee
                    if abs(sleeve.cash) < 1e-12:
                        sleeve.cash = 0.0

                    lot = Lot(
                        signal_date=c.signal_date,
                        entry_date=d,
                        nominal_exit_date=c.exit_date,
                        positions={
                            s: per_symbol / opens[(d, s)]
                            for s in valid
                        },
                    )
                    sleeve.lots.append(lot)

                    trade_rows.append({
                        "date": d,
                        "signal_date": c.signal_date,
                        "slot_id": int(c.slot_id),
                        "side": "BUY",
                        "n_symbols": len(valid),
                        "notional": total_notional,
                        "fee": fee,
                        "cash_fraction_of_initial_sleeve":
                            available_cash / initial_sleeve_cash,
                    })

        # 2) At CLOSE, sell every individually executable position whose
        # nominal exit has arrived. Only blocked positions remain.
        for slot_id, sleeve in enumerate(sleeves):
            new_lots = []

            for lot in sleeve.lots:
                if not lot.positions:
                    continue

                if d < lot.nominal_exit_date:
                    new_lots.append(lot)
                    continue

                sold_symbols = []
                sold_notional = 0.0
                blocked_symbols = []

                nominal_pos = cal_pos.get(lot.nominal_exit_date)
                actual_pos = cal_pos.get(d)
                delay = (
                    actual_pos - nominal_pos
                    if nominal_pos is not None and actual_pos is not None
                    else 0
                )

                for s, shares in list(lot.positions.items()):
                    px = exact_closes.get((d, s))
                    if finite(px) and px > 0:
                        sold_symbols.append(s)
                        sold_notional += shares * float(px)
                    else:
                        blocked_symbols.append(s)
                        deferred_position_attempts += 1
                        if delay == 0:
                            positions_deferred_at_nominal_exit += 1
                            cohorts_with_deferred_positions.add(lot.signal_date)

                if sold_symbols:
                    fee = sold_notional * side_cost
                    sleeve.cash += sold_notional - fee

                    if delay > 0:
                        completed_deferred_positions += len(sold_symbols)
                        max_exit_delay_sessions = max(
                            max_exit_delay_sessions,
                            int(delay),
                        )

                    for s in sold_symbols:
                        del lot.positions[s]

                    trade_rows.append({
                        "date": d,
                        "signal_date": lot.signal_date,
                        "slot_id": int(slot_id),
                        "side": (
                            "SELL"
                            if delay == 0
                            else "SELL_DEFERRED_POSITION"
                        ),
                        "n_symbols": len(sold_symbols),
                        "notional": sold_notional,
                        "fee": fee,
                        "nominal_exit_date": lot.nominal_exit_date,
                        "exit_delay_sessions": int(delay),
                        "blocked_symbols_remaining":
                            len(blocked_symbols),
                    })

                if lot.positions:
                    new_lots.append(lot)

            sleeve.lots = new_lots

        # 3) End-of-day mark-to-market after exit processing.
        total_cash = sum(s.cash for s in sleeves)
        post_symbol_values = {}
        post_gross = 0.0
        deferred_positions_eod = 0

        for sleeve in sleeves:
            for lot in sleeve.lots:
                for s, shares in lot.positions.items():
                    px = exact_closes.get((d, s))
                    if not finite(px) or px <= 0:
                        px = mark_closes.get(d, {}).get(s)
                        if finite(px) and px > 0:
                            mark_carry_count += 1

                    if not finite(px) or px <= 0:
                        raise RuntimeError(
                            f"No close/mark price for active {s} on {d}."
                        )

                    value = shares * float(px)
                    post_gross += value
                    post_symbol_values[s] = (
                        post_symbol_values.get(s, 0.0) + value
                    )

                    if d >= lot.nominal_exit_date:
                        deferred_positions_eod += 1

        equity = total_cash + post_gross
        max_symbol_weight = (
            max(post_symbol_values.values()) / equity
            if post_symbol_values and equity > 0
            else 0.0
        )

        curve_rows.append({
            "date": d,
            "equity": equity,
            "cash": total_cash,
            "gross_market_value": post_gross,
            "gross_exposure":
                post_gross / equity if equity > 0 else math.nan,
            "unique_positions": len(post_symbol_values),
            "active_sleeves":
                sum(1 for s in sleeves if s.active),
            "deferred_positions": int(deferred_positions_eod),
            "max_symbol_weight": max_symbol_weight,
        })

    curve = pd.DataFrame(curve_rows)
    trades = pd.DataFrame(trade_rows)

    unresolved = []
    for slot_id, sleeve in enumerate(sleeves):
        for lot in sleeve.lots:
            if lot.positions and last_nominal_exit >= lot.nominal_exit_date:
                unresolved.append({
                    "slot_id": int(slot_id),
                    "signal_date": lot.signal_date,
                    "nominal_exit_date": lot.nominal_exit_date,
                    "symbols": sorted(lot.positions.keys()),
                })

    receipt = {
        "missing_entry_candidate_rows": int(missing_entry_rows),
        "positions_deferred_at_nominal_exit":
            int(positions_deferred_at_nominal_exit),
        "deferred_position_attempts":
            int(deferred_position_attempts),
        "completed_deferred_positions":
            int(completed_deferred_positions),
        "cohorts_with_deferred_positions":
            int(len(cohorts_with_deferred_positions)),
        "max_exit_delay_sessions":
            int(max_exit_delay_sessions),
        "partial_cash_entries":
            int(partial_cash_entries),
        "skipped_cohorts_no_cash":
            int(skipped_cohorts_no_cash),
        "unresolved_positions_at_cutoff":
            int(sum(len(x["symbols"]) for x in unresolved)),
        "unresolved_lot_sample": unresolved[:20],
        "mark_carry_count": int(mark_carry_count),
    }
    return curve, trades, receipt

def period_metrics(
    curve: pd.DataFrame,
    folds: dict[str, tuple[str, str]],
) -> list[dict[str, Any]]:
    rows = []
    segments = {
        "ALL": (curve["date"].min(), curve["date"].max()),
        "F1": folds["F1"],
        "F2": folds["F2"],
        "F3": folds["F3"],
        "ERA_SW2021": ("20210730", curve["date"].max()),
    }

    for name, (start, end) in segments.items():
        x = curve[(curve["date"] >= start) & (curve["date"] <= end)].copy()
        if len(x) < 2:
            continue

        # Rebase for subperiod metrics.
        x["equity"] = x["equity"] / float(x["equity"].iloc[0])
        m = curve_metrics(x)
        m["segment"] = name
        m["start"] = str(x["date"].iloc[0])
        m["end"] = str(x["date"].iloc[-1])
        rows.append(m)

    return rows


def pct(x: Any) -> str:
    return "NA" if not finite(x) else f"{float(x) * 100:+.2f}%"


def num(x: Any) -> str:
    return "NA" if not finite(x) else f"{float(x):.3f}"


def main():
    folds = load_folds()
    con = duckdb.connect()

    try:
        q5s = {}
        reconciliations = {}
        all_symbols = set()
        all_signal_dates = set()

        for universe in UNIVERSES:
            q5, rec = load_q5(con, universe)
            q5s[universe] = q5
            reconciliations[universe] = rec
            all_symbols.update(q5["symbol"].unique().tolist())
            all_signal_dates.update(q5["signal_date"].unique().tolist())
    finally:
        con.close()

    min_signal = min(all_signal_dates)
    calendar = get_calendar(min_signal, CUTOFF)

    cohorts = {
        universe: build_cohorts(q5s[universe], calendar)
        for universe in UNIVERSES
    }

    first_entry = min(c["entry_date"].min() for c in cohorts.values())
    last_exit = max(c["exit_date"].max() for c in cohorts.values())
    symbols = sorted(all_symbols)

    print("=" * 110)
    print("RET120 SHARED-EQUITY ECONOMIC AUDIT V0.1B")
    print("=" * 110)
    print("Horizon:", HORIZON, "sessions")
    print("Capital sleeves:", HORIZON)
    print("Entry: S1/T+1 OPEN | Exit: S120 CLOSE")
    print("Price representation: QMT back_ratio")
    print("Market range:", first_entry, "->", last_exit)
    print("Symbols:", len(symbols))
    print()

    raw = xtdata.get_market_data_ex(
        field_list=["open", "close"],
        stock_list=symbols,
        period="1d",
        start_time=first_entry,
        end_time=last_exit,
        count=-1,
        dividend_type="back_ratio",
        fill_data=False,
    )
    market = parse_market_ex(raw, symbols, ("open", "close"))
    opens, exact_closes, marks = build_price_maps(
        market,
        calendar,
        symbols,
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    audit = {
        "audit_id": "RET120_SHARED_EQUITY_V01B",
        "horizon_sessions": HORIZON,
        "capital_sleeves": HORIZON,
        "initial_equity": INITIAL_EQUITY,
        "construction": (
            "One equal-capital sleeve per horizon session. Each signal date's "
            "Q5 cohort enters next OPEN and is equal-weight using only cash "
            "actually available in that sleeve. At nominal S120 CLOSE, every "
            "individually executable constituent is sold. Only blocked "
            "positions are deferred to their first later exact CLOSE. Released "
            "cash is reusable; no fresh capital is injected."
        ),
        "cost_scenarios": COST_SCENARIOS,
        "universes": {},
        "notes": [
            "No regime filter.",
            "No ret120 threshold tuning.",
            "No max-position parameter.",
            "No board-lot constraint; fractional research shares are used.",
            "Missing exact S120 close defers only that blocked position; executable peers are sold normally.",
            "Suspension-day marking may carry the most recent valid close for valuation only, never for execution.",
            "A scheduled new cohort invests only cash actually available in its assigned sleeve.",
        ],
    }

    summary_rows = []

    for universe in UNIVERSES:
        print(universe.upper())
        print("-" * 110)

        uni = {
            "bucket_reconciliation": reconciliations[universe],
            "scenarios": {},
        }

        for label, cost in COST_SCENARIOS.items():
            curve, trades, receipt = simulate(
                q5=q5s[universe],
                cohorts=cohorts[universe],
                calendar=calendar,
                opens=opens,
                exact_closes=exact_closes,
                mark_closes=marks,
                round_trip_cost=cost,
            )

            m = curve_metrics(curve)
            t = turnover_metrics(curve, trades)
            periods = period_metrics(curve, folds)

            curve.to_csv(
                OUT_DIR / f"{universe}_{label}_equity_curve.csv",
                index=False,
                encoding="utf-8-sig",
            )
            trades.to_csv(
                OUT_DIR / f"{universe}_{label}_trades.csv",
                index=False,
                encoding="utf-8-sig",
            )

            uni["scenarios"][label] = {
                "metrics": m,
                "turnover": t,
                "periods": periods,
                "execution_receipt": receipt,
            }

            print(
                f"{label}: "
                f"CAGR={pct(m['cagr'])} "
                f"MDD={pct(m['max_drawdown'])} "
                f"Sharpe={num(m['sharpe'])} "
                f"final={num(m['final_equity'])} "
                f"avgGross={pct(m['avg_gross_exposure'])} "
                f"annTurnover(one-way-eq)={num(t['annualized_one_way_equivalent'])}x "
                f"avgPos={m['avg_unique_positions']:.1f} "
                f"maxSymW={pct(m['max_symbol_weight'])} "
                f"missingEntry={receipt['missing_entry_candidate_rows']} "
                f"deferredPos={receipt['positions_deferred_at_nominal_exit']} "
                f"maxDelay={receipt['max_exit_delay_sessions']} "
                f"partialCash={receipt['partial_cash_entries']} "
                f"skipNoCash={receipt['skipped_cohorts_no_cash']} "
                f"unresolved={receipt['unresolved_positions_at_cutoff']}"
            )
            for period in periods:
                print(
                    f"    {period['segment']}: "
                    f"CAGR={pct(period['cagr'])} "
                    f"MDD={pct(period['max_drawdown'])} "
                    f"Sharpe={num(period['sharpe'])}"
                )

            for p in periods:
                summary_rows.append({
                    "universe": universe,
                    "cost_scenario": label,
                    **p,
                    "annualized_one_way_equivalent":
                        t["annualized_one_way_equivalent"],
                    "avg_gross_exposure": m["avg_gross_exposure"],
                    "avg_unique_positions": m["avg_unique_positions"],
                    "max_symbol_weight": m["max_symbol_weight"],
                    "missing_entry_candidate_rows":
                        receipt["missing_entry_candidate_rows"],
                    "mark_carry_count": receipt["mark_carry_count"],
                    "positions_deferred_at_nominal_exit":
                        receipt["positions_deferred_at_nominal_exit"],
                    "completed_deferred_positions":
                        receipt["completed_deferred_positions"],
                    "max_exit_delay_sessions":
                        receipt["max_exit_delay_sessions"],
                    "partial_cash_entries":
                        receipt["partial_cash_entries"],
                    "skipped_cohorts_no_cash":
                        receipt["skipped_cohorts_no_cash"],
                    "unresolved_positions_at_cutoff":
                        receipt["unresolved_positions_at_cutoff"],
                })

        audit["universes"][universe] = uni
        print()

    pd.DataFrame(summary_rows).to_csv(
        OUT_DIR / "summary.csv",
        index=False,
        encoding="utf-8-sig",
    )

    (OUT_DIR / "audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("Output:", OUT_DIR)
    print("This is the shared-equity economic audit.")
    print("Capacity/liquidity sizing is intentionally left for the next gate if economics survive.")
    print("No production database was modified.")


if __name__ == "__main__":
    main()
