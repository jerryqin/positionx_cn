from __future__ import annotations

r"""
PositionX — EARNINGS_IMPROVING_H60_V0.1 Standalone Portfolio Audit

Purpose
-------
Convert the already-passed event-study candidate into a real shared-equity
long-only portfolio simulation before any Strategy Portfolio / combination work.

Frozen candidate
----------------
- event_kind = ORIGINAL_FORECAST
- parser_confidence = HIGH
- change_type = IMPROVING
- A-share stock filter already applied in Earnings Drift Audit V0.1
- entry = T+1 exact OPEN
- exit = S60 exact CLOSE
- use only measurement_status = OK
- while a symbol is held, later signals for that symbol are ignored

Portfolio baseline
------------------
- initial equity = 1,000,000
- target weight per new entry = 1%
- max total gross target = 30%
- max positions = 30
- fractional shares = true (research economics, not lot execution)
- same-day tie break = SYMBOL_ASC
- same-day exits are NOT reusable at that day's OPEN
- existing positions are never rebalanced
- round-trip cost:
    main   = 20bp total = 10bp entry + 10bp exit
    stress = 50bp total = 25bp entry + 25bp exit

Capacity robustness
-------------------
The 60-session holding period can create heavy overlap. Therefore this audit also
replays the 20bp portfolio under 100 deterministic random same-day tie-break seeds.
This is NOT parameter optimization; it measures whether economics depend on the
arbitrary SYMBOL_ASC baseline selection.

Inputs
------
D:\positionx_data\research\earnings_drift_v0_1\event_returns.csv
D:\positionx_data\research\earnings_drift_v0_1\audit_summary.json
D:\positionx_data\catalog\market_data.duckdb

Safety
------
Read-only. No network, broker, order, production SQLite, research DB, catalog, or
Strategy parameter mutation.
"""

import argparse
import hashlib
import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

EVENT_RETURNS = Path(r"D:\positionx_data\research\earnings_drift_v0_1\event_returns.csv")
EVENT_AUDIT = Path(r"D:\positionx_data\research\earnings_drift_v0_1\audit_summary.json")
CATALOG = Path(r"D:\positionx_data\catalog\market_data.duckdb")
OUT_ROOT = Path(r"D:\positionx_data\research\earnings_improving_h60_portfolio_v0_1")

EXPECTED_EVENT_RETURNS_SHA256 = "8d5988d720ae1eafe313ee9512ea9d7c8d8637566afe79ffc8c0634fa97c12fb"
EXPECTED_CORPUS_SHA256 = "488c29b12ca9975c36005e9afcc2afb82516aae5bf0305a337c69a81060db0d4"
EXPECTED_PARSER_SHA256 = "7cf5dcd39d584a13a1095b482beb8b2298b26b381f900f13f18c0f8cd28e7ed4"

INITIAL_EQUITY = 1_000_000.0
TARGET_WEIGHT = 0.01
MAX_GROSS = 0.30
MAX_POSITIONS = 30
MAIN_COST = 0.002
STRESS_COST = 0.005
RANDOM_SEEDS = 100


def safe(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, bool)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if hasattr(v, "item"):
        try:
            return safe(v.item())
        except Exception:
            pass
    return str(v)


def sha256_file(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, default=safe),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def stable_random_key(seed: int, d: str, symbol: str, ann: str) -> str:
    raw = f"{seed}|{d}|{symbol}|{ann}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def verify_inputs() -> dict:
    for p in (EVENT_RETURNS, EVENT_AUDIT, CATALOG):
        if not p.exists():
            raise RuntimeError(f"FAIL_CLOSED missing input: {p}")

    actual = sha256_file(EVENT_RETURNS)
    if actual != EXPECTED_EVENT_RETURNS_SHA256:
        raise RuntimeError(
            "FAIL_CLOSED event_returns hash mismatch "
            f"expected={EXPECTED_EVENT_RETURNS_SHA256} actual={actual}"
        )

    audit = json.loads(EVENT_AUDIT.read_text(encoding="utf-8"))
    if audit.get("status") != "PASS_EARNINGS_DRIFT_STRATEGY_AUDIT_V0_1":
        raise RuntimeError(
            f"FAIL_CLOSED event audit status={audit.get('status')}"
        )

    semantic = audit.get("semantic_source") or {}
    if semantic.get("corpus_sha256") != EXPECTED_CORPUS_SHA256:
        raise RuntimeError("FAIL_CLOSED corpus hash lineage mismatch")
    if semantic.get("parser_sha256") != EXPECTED_PARSER_SHA256:
        raise RuntimeError("FAIL_CLOSED parser hash lineage mismatch")

    return audit


def load_candidates() -> tuple[pd.DataFrame, dict]:
    df = pd.read_csv(EVENT_RETURNS)
    required = {
        "announcementId", "symbol", "secName", "event_time", "event_date",
        "event_year", "report_period", "change_type", "signal_group",
        "entry_date", "horizon", "exit_date", "entry_open", "exit_close",
        "measurement_status",
    }
    missing = sorted(required - set(df.columns))
    if missing:
        raise RuntimeError(f"FAIL_CLOSED event_returns missing columns={missing}")

    counts = {
        "event_returns_rows": int(len(df)),
        "improving_h60_all_status": int(
            ((df["change_type"] == "IMPROVING") & (df["horizon"] == 60)).sum()
        ),
    }

    c = df[
        (df["change_type"] == "IMPROVING")
        & (df["horizon"] == 60)
        & (df["measurement_status"] == "OK")
    ].copy()

    if len(c) != 2731:
        raise RuntimeError(
            f"FAIL_CLOSED expected 2731 IMPROVING H60 OK rows, got {len(c)}"
        )

    c["announcementId"] = c["announcementId"].astype(str)
    c["entry_date"] = pd.to_datetime(c["entry_date"]).dt.date
    c["exit_date"] = pd.to_datetime(c["exit_date"]).dt.date
    c["event_date_dt"] = pd.to_datetime(c["event_date"]).dt.date
    c["event_year"] = pd.to_numeric(c["event_year"]).astype(int)

    if c["announcementId"].duplicated().any():
        raise RuntimeError("FAIL_CLOSED duplicate announcementId in candidate set")

    if (c["exit_date"] < c["entry_date"]).any():
        raise RuntimeError("FAIL_CLOSED exit before entry")

    counts.update({
        "candidate_rows": int(len(c)),
        "candidate_symbols": int(c["symbol"].nunique()),
        "candidate_entry_days": int(c["entry_date"].nunique()),
        "min_entry_date": str(c["entry_date"].min()),
        "max_entry_date": str(c["entry_date"].max()),
        "min_exit_date": str(c["exit_date"].min()),
        "max_exit_date": str(c["exit_date"].max()),
    })
    return c.sort_values(
        ["entry_date", "symbol", "announcementId"]
    ).reset_index(drop=True), counts


def load_market(
    candidates: pd.DataFrame,
) -> tuple[pd.DataFrame, list, dict]:
    symbols = pd.DataFrame(
        {"symbol": sorted(candidates["symbol"].unique().tolist())}
    )
    min_d = candidates["entry_date"].min()
    max_d = candidates["exit_date"].max()

    con = duckdb.connect(str(CATALOG), read_only=True)
    con.register("_symbols", symbols)
    try:
        bars = con.execute("""
            SELECT
              b.symbol,
              b.trade_date,
              b."open",
              b."close",
              b.suspend_flag
            FROM market.daily_bars_v1 b
            INNER JOIN _symbols s USING(symbol)
            WHERE b.trade_date BETWEEN ? AND ?
            ORDER BY b.trade_date, b.symbol
        """, [min_d, max_d]).df()

        cal = [
            x[0]
            for x in con.execute("""
                SELECT DISTINCT trade_date
                FROM market.daily_bars_v1
                WHERE trade_date BETWEEN ? AND ?
                ORDER BY trade_date
            """, [min_d, max_d]).fetchall()
        ]

        stats = con.execute("""
            SELECT
              count(*) AS rows,
              count(DISTINCT symbol) AS symbols,
              min(trade_date),
              max(trade_date)
            FROM market.daily_bars_v1
        """).fetchone()
    finally:
        try:
            con.unregister("_symbols")
        except Exception:
            pass
        con.close()

    bars["trade_date"] = pd.to_datetime(bars["trade_date"]).dt.date
    if bars.duplicated(["symbol", "trade_date"]).any():
        raise RuntimeError(
            "FAIL_CLOSED duplicate symbol/trade_date in market.daily_bars_v1"
        )

    market_info = {
        "catalog_rows": int(stats[0]),
        "catalog_symbols": int(stats[1]),
        "catalog_min_date": str(stats[2]),
        "catalog_max_date": str(stats[3]),
        "candidate_market_rows": int(len(bars)),
        "calendar_sessions": int(len(cal)),
    }
    return bars, cal, market_info


def crosscheck_event_prices(
    candidates: pd.DataFrame,
    bars: pd.DataFrame,
) -> dict:
    px = bars.set_index(["symbol", "trade_date"])
    mismatches = []

    for _, r in candidates.iterrows():
        k0 = (r["symbol"], r["entry_date"])
        k1 = (r["symbol"], r["exit_date"])
        if k0 not in px.index or k1 not in px.index:
            mismatches.append({
                "announcementId": r["announcementId"],
                "symbol": r["symbol"],
                "reason": "CATALOG_BAR_MISSING",
            })
            continue

        b0 = px.loc[k0]
        b1 = px.loc[k1]
        open_now = float(b0["open"])
        close_now = float(b1["close"])
        open_old = float(r["entry_open"])
        close_old = float(r["exit_close"])

        def eq(a, b):
            scale = max(abs(a), abs(b), 1.0)
            return abs(a - b) <= scale * 1e-10

        if not eq(open_now, open_old) or not eq(close_now, close_old):
            mismatches.append({
                "announcementId": r["announcementId"],
                "symbol": r["symbol"],
                "reason": "EVENT_PRICE_LINEAGE_MISMATCH",
                "event_entry_open": open_old,
                "catalog_entry_open": open_now,
                "event_exit_close": close_old,
                "catalog_exit_close": close_now,
            })

    if mismatches:
        raise RuntimeError(
            "FAIL_CLOSED event/catalog price lineage mismatch "
            f"count={len(mismatches)} sample={mismatches[:5]}"
        )
    return {"checked": int(len(candidates)), "mismatches": 0}


def price_maps(bars: pd.DataFrame):
    p = {}
    for r in bars.itertuples(index=False):
        p[(r.symbol, r.trade_date)] = {
            "open": None if pd.isna(r.open) else float(r.open),
            "close": None if pd.isna(r.close) else float(r.close),
            "suspend_flag": (
                0 if pd.isna(r.suspend_flag) else int(r.suspend_flag)
            ),
        }
    return p


def entry_groups(
    candidates: pd.DataFrame,
) -> dict:
    out = {}
    for d, g in candidates.groupby("entry_date", sort=True):
        out[d] = g.to_dict("records")
    return out


def metric_block(curve: pd.DataFrame, trades: pd.DataFrame) -> dict:
    if curve.empty:
        raise RuntimeError("empty equity curve")

    eq = pd.to_numeric(curve["equity"], errors="raise")
    total_return = float(eq.iloc[-1] / eq.iloc[0] - 1.0)

    d0 = pd.Timestamp(curve["date"].iloc[0])
    d1 = pd.Timestamp(curve["date"].iloc[-1])
    cal_days = max((d1 - d0).days, 1)
    years = cal_days / 365.25
    cagr = float((eq.iloc[-1] / eq.iloc[0]) ** (1.0 / years) - 1.0)

    roll = eq.cummax()
    dd = eq / roll - 1.0
    mdd = float(dd.min())

    ret = pd.to_numeric(curve["daily_return"], errors="coerce").dropna()
    annual_sessions = (
        max(len(curve) - 1, 1) / years
        if years > 0 else 252.0
    )
    sharpe = None
    if len(ret) > 1 and float(ret.std(ddof=1)) > 0:
        sharpe = float(
            ret.mean() / ret.std(ddof=1) * math.sqrt(annual_sessions)
        )

    avg_gross = float(curve["gross_weight"].mean())
    max_gross = float(curve["gross_weight"].max())
    avg_active = float(curve["active_positions"].mean())
    max_active = int(curve["active_positions"].max())

    t = pd.to_numeric(
        trades["net_return_on_trade_capital"], errors="coerce"
    ).dropna() if len(trades) else pd.Series(dtype=float)

    trade_stats = {
        "mean": float(t.mean()) if len(t) else None,
        "median": float(t.median()) if len(t) else None,
        "trim5": (
            float(t.sort_values().iloc[
                int(len(t)*0.05): len(t)-int(len(t)*0.05)
            ].mean())
            if len(t) >= 40 else None
        ),
        "win_rate": float((t > 0).mean()) if len(t) else None,
    }

    return {
        "total_return": total_return,
        "cagr_calendar": cagr,
        "max_drawdown": mdd,
        "sharpe_actual_sessions": sharpe,
        "annual_sessions_observed": float(annual_sessions),
        "final_equity": float(eq.iloc[-1]),
        "avg_gross_weight": avg_gross,
        "max_gross_weight": max_gross,
        "avg_active_positions": avg_active,
        "max_active_positions": max_active,
        "cagr_per_avg_gross": (
            float(cagr / avg_gross) if avg_gross > 0 else None
        ),
        "trade_return_stats": trade_stats,
    }


def yearly_returns(curve: pd.DataFrame) -> list[dict]:
    x = curve.copy()
    x["year"] = pd.to_datetime(x["date"]).dt.year
    out = []
    for y, g in x.groupby("year", sort=True):
        if len(g) < 2:
            continue
        start = float(g["equity"].iloc[0])
        end = float(g["equity"].iloc[-1])
        out.append({
            "year": int(y),
            "sessions": int(len(g)),
            "return": end / start - 1.0,
            "min_equity": float(g["equity"].min()),
            "max_equity": float(g["equity"].max()),
            "avg_gross_weight": float(g["gross_weight"].mean()),
            "avg_active_positions": float(g["active_positions"].mean()),
        })
    return out


def simulate(
    candidates: pd.DataFrame,
    cal: list,
    prices: dict,
    round_trip_cost: float,
    tie_seed: int | None,
) -> dict:
    half_cost = round_trip_cost / 2.0
    groups = entry_groups(candidates)

    active: dict[str, dict] = {}
    cash = INITIAL_EQUITY
    last_mark: dict[str, float] = {}

    curve_rows = [{
        "date": cal[0],
        "equity": INITIAL_EQUITY,
        "cash": INITIAL_EQUITY,
        "gross_weight": 0.0,
        "active_positions": 0,
        "daily_return": None,
    }]
    trades = []
    skipped = []
    scaled_entry_days = 0
    risk_budget_blocked = 0

    prev_equity = INITIAL_EQUITY

    for d in cal:
        # Mark existing positions at today's OPEN. If a position is suspended or
        # today's OPEN is unavailable, carry the last known close/mark.
        open_value = 0.0
        for sym, pos in active.items():
            bar = prices.get((sym, d))
            mark = None
            if bar and bar["open"] is not None and bar["open"] > 0:
                mark = bar["open"]
            elif sym in last_mark:
                mark = last_mark[sym]
            else:
                mark = pos["entry_price"]
            pos["open_mark"] = mark
            open_value += pos["shares"] * mark

        pre_entry_equity = cash + open_value
        if pre_entry_equity <= 0:
            raise RuntimeError(f"FAIL_CLOSED nonpositive equity at open {d}")

        todays = list(groups.get(d, []))

        # A symbol that is still held at the OPEN is ineligible, even if its
        # existing position exits at today's CLOSE.
        eligible = []
        for e in todays:
            sym = e["symbol"]
            if sym in active:
                skipped.append({
                    "announcementId": e["announcementId"],
                    "symbol": sym,
                    "entry_date": str(d),
                    "reason": "DUPLICATE_SYMBOL_WHILE_HELD",
                })
            else:
                eligible.append(e)

        if tie_seed is None:
            eligible.sort(key=lambda e: (e["symbol"], e["announcementId"]))
        else:
            eligible.sort(
                key=lambda e: stable_random_key(
                    tie_seed, str(d), e["symbol"], e["announcementId"]
                )
            )

        slots = max(0, MAX_POSITIONS - len(active))
        selected = eligible[:slots]
        for e in eligible[slots:]:
            skipped.append({
                "announcementId": e["announcementId"],
                "symbol": e["symbol"],
                "entry_date": str(d),
                "reason": "NO_SLOT",
            })

        if selected:
            existing_gross = open_value
            gross_budget = max(
                0.0, MAX_GROSS * pre_entry_equity - existing_gross
            )
            desired_each = TARGET_WEIGHT * pre_entry_equity
            desired_total = desired_each * len(selected)

            # Entry cost is paid immediately, so available cash must cover
            # notional * (1 + half_cost).
            cash_notional_budget = max(0.0, cash / (1.0 + half_cost))
            budget = min(gross_budget, cash_notional_budget)
            scale = min(1.0, budget / desired_total) if desired_total > 0 else 0.0

            if scale < 1.0 - 1e-12:
                scaled_entry_days += 1

            if scale <= 1e-12:
                risk_budget_blocked += len(selected)
                for e in selected:
                    skipped.append({
                        "announcementId": e["announcementId"],
                        "symbol": e["symbol"],
                        "entry_date": str(d),
                        "reason": "RISK_BUDGET_BLOCKED",
                    })
            else:
                entry_notional = desired_each * scale
                for e in selected:
                    sym = e["symbol"]
                    bar = prices.get((sym, d))
                    if not bar or bar["open"] is None or bar["open"] <= 0:
                        raise RuntimeError(
                            f"FAIL_CLOSED missing exact entry open {sym} {d}"
                        )
                    px = float(bar["open"])
                    if abs(px - float(e["entry_open"])) > max(abs(px), 1.0)*1e-10:
                        raise RuntimeError(
                            f"FAIL_CLOSED entry lineage changed {sym} {d}"
                        )

                    shares = entry_notional / px
                    entry_fee = entry_notional * half_cost
                    cash -= entry_notional + entry_fee

                    active[sym] = {
                        "announcementId": e["announcementId"],
                        "symbol": sym,
                        "secName": e.get("secName"),
                        "event_time": e.get("event_time"),
                        "event_date": e.get("event_date"),
                        "event_year": int(e["event_year"]),
                        "report_period": e.get("report_period"),
                        "entry_date": d,
                        "exit_date": e["exit_date"],
                        "entry_price": px,
                        "entry_notional": entry_notional,
                        "entry_fee": entry_fee,
                        "shares": shares,
                    }
                    last_mark[sym] = px

        # CLOSE: execute exact scheduled exits, then mark remaining holdings.
        for sym in list(active.keys()):
            pos = active[sym]
            if pos["exit_date"] != d:
                continue
            bar = prices.get((sym, d))
            if not bar or bar["close"] is None or bar["close"] <= 0:
                raise RuntimeError(
                    f"FAIL_CLOSED missing exact exit close {sym} {d}"
                )
            exit_px = float(bar["close"])
            gross_proceeds = pos["shares"] * exit_px
            exit_fee = gross_proceeds * half_cost
            net_proceeds = gross_proceeds - exit_fee
            cash += net_proceeds

            buy_cash = pos["entry_notional"] + pos["entry_fee"]
            net_pnl = net_proceeds - buy_cash
            net_ret = net_pnl / pos["entry_notional"]

            trades.append({
                **{k: pos[k] for k in (
                    "announcementId", "symbol", "secName", "event_time",
                    "event_date", "event_year", "report_period"
                )},
                "entry_date": str(pos["entry_date"]),
                "exit_date": str(pos["exit_date"]),
                "entry_price": pos["entry_price"],
                "exit_price": exit_px,
                "entry_notional": pos["entry_notional"],
                "entry_fee": pos["entry_fee"],
                "exit_fee": exit_fee,
                "net_pnl": net_pnl,
                "net_return_on_trade_capital": net_ret,
            })
            last_mark[sym] = exit_px
            del active[sym]

        close_value = 0.0
        for sym, pos in active.items():
            bar = prices.get((sym, d))
            mark = None
            if bar and bar["close"] is not None and bar["close"] > 0:
                mark = bar["close"]
            elif sym in last_mark:
                mark = last_mark[sym]
            else:
                mark = pos["entry_price"]
            last_mark[sym] = mark
            close_value += pos["shares"] * mark

        equity = cash + close_value
        gross_weight = close_value / equity if equity > 0 else float("nan")
        daily_ret = equity / prev_equity - 1.0 if prev_equity > 0 else None

        curve_rows.append({
            "date": d,
            "equity": equity,
            "cash": cash,
            "gross_weight": gross_weight,
            "active_positions": len(active),
            "daily_return": daily_ret,
        })
        prev_equity = equity

    if active:
        sample = [(s, str(p["exit_date"])) for s, p in list(active.items())[:10]]
        raise RuntimeError(
            f"FAIL_CLOSED active positions remain after last calendar date: {sample}"
        )

    curve = pd.DataFrame(curve_rows).drop_duplicates(
        subset=["date"], keep="last"
    ).reset_index(drop=True)
    tdf = pd.DataFrame(trades)
    sdf = pd.DataFrame(skipped)

    metrics = metric_block(curve, tdf)
    metrics.update({
        "round_trip_cost": round_trip_cost,
        "trades_executed": int(len(tdf)),
        "skipped_no_slot": int(
            (sdf["reason"] == "NO_SLOT").sum()
        ) if len(sdf) else 0,
        "skipped_duplicate_symbol": int(
            (sdf["reason"] == "DUPLICATE_SYMBOL_WHILE_HELD").sum()
        ) if len(sdf) else 0,
        "risk_budget_blocked": int(risk_budget_blocked),
        "scaled_entry_days": int(scaled_entry_days),
        "candidate_events": int(len(candidates)),
        "coverage_vs_candidate_events": (
            float(len(tdf) / len(candidates)) if len(candidates) else None
        ),
        "yearly": yearly_returns(curve),
    })

    return {
        "metrics": metrics,
        "curve": curve,
        "trades": tdf,
        "skipped": sdf,
    }


def quantiles(vals: list[float]) -> dict:
    s = pd.Series(vals, dtype=float)
    return {
        "min": float(s.min()),
        "p10": float(s.quantile(0.10)),
        "p25": float(s.quantile(0.25)),
        "median": float(s.median()),
        "p75": float(s.quantile(0.75)),
        "p90": float(s.quantile(0.90)),
        "max": float(s.max()),
        "mean": float(s.mean()),
    }


def unconstrained_demand(candidates: pd.DataFrame, cal: list) -> dict:
    # Count overlapping candidate holding windows ignoring duplicate-symbol and
    # portfolio caps. Entry date through exit date are both considered active.
    delta = {d: 0 for d in cal}
    idx = {d: i for i, d in enumerate(cal)}
    arr = np.zeros(len(cal) + 1, dtype=int)
    entry_counts = {}

    for r in candidates.itertuples(index=False):
        i0 = idx[r.entry_date]
        i1 = idx[r.exit_date]
        arr[i0] += 1
        if i1 + 1 < len(arr):
            arr[i1 + 1] -= 1
        entry_counts[r.entry_date] = entry_counts.get(r.entry_date, 0) + 1

    active = np.cumsum(arr[:-1])
    entries = pd.Series(list(entry_counts.values()), dtype=float)

    return {
        "days": int(len(cal)),
        "active_mean": float(active.mean()),
        "active_median": float(np.median(active)),
        "active_p90": float(np.quantile(active, 0.90)),
        "active_p95": float(np.quantile(active, 0.95)),
        "active_p99": float(np.quantile(active, 0.99)),
        "active_max": int(active.max()),
        "entry_days": int(len(entry_counts)),
        "entries_per_entry_day_median": float(entries.median()),
        "entries_per_entry_day_p90": float(entries.quantile(0.90)),
        "entries_per_entry_day_max": int(entries.max()),
        "nominal_weight_active_mean": float(active.mean() * TARGET_WEIGHT),
        "nominal_weight_active_p95": float(np.quantile(active, 0.95) * TARGET_WEIGHT),
        "nominal_weight_active_max": float(active.max() * TARGET_WEIGHT),
    }


def standalone_gate(main: dict, stress: dict, random_audit: dict) -> dict:
    positive_years = sum(
        1 for x in main["yearly"] if x["return"] > 0
    )
    checks = {
        "cagr20_positive": main["cagr_calendar"] > 0,
        "cagr50_positive": stress["cagr_calendar"] > 0,
        "sharpe20_ge_0_5": (
            main["sharpe_actual_sessions"] is not None
            and main["sharpe_actual_sessions"] >= 0.5
        ),
        "mdd20_within_20pct": main["max_drawdown"] >= -0.20,
        "at_least_2_positive_calendar_years": positive_years >= 2,
        "random_seed_median_cagr_positive": (
            random_audit["cagr"]["median"] > 0
        ),
        "random_seed_p10_cagr_positive": (
            random_audit["cagr"]["p10"] > 0
        ),
    }
    return {
        "status": (
            "PASS_STANDALONE_PORTFOLIO_GATE"
            if all(checks.values())
            else "FAIL_OR_HOLD_STANDALONE_PORTFOLIO_GATE"
        ),
        "checks": checks,
        "note": (
            "This gate is pre-Portfolio-combination. PASS means Alpha #2 can "
            "advance to incremental-value/correlation testing with Deep Oversold; "
            "it is not yet a Forward/live Strategy approval."
        ),
    }


def check() -> int:
    audit = verify_inputs()
    candidates, cinfo = load_candidates()
    bars, cal, minfo = load_market(candidates)
    lineage = crosscheck_event_prices(candidates, bars)

    result = {
        "status": "PASS_EARNINGS_IMPROVING_H60_PORTFOLIO_V0_1_CHECK",
        "event_audit_status": audit.get("status"),
        "event_returns_sha256": EXPECTED_EVENT_RETURNS_SHA256,
        "candidate": cinfo,
        "market": minfo,
        "price_lineage": lineage,
        "portfolio": {
            "initial_equity": INITIAL_EQUITY,
            "target_weight": TARGET_WEIGHT,
            "max_gross": MAX_GROSS,
            "max_positions": MAX_POSITIONS,
            "main_round_trip_cost": MAIN_COST,
            "stress_round_trip_cost": STRESS_COST,
            "random_tie_break_seeds": RANDOM_SEEDS,
        },
        "network": False,
        "database_mutation": False,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def run() -> int:
    audit = verify_inputs()
    candidates, cinfo = load_candidates()
    bars, cal, minfo = load_market(candidates)
    lineage = crosscheck_event_prices(candidates, bars)
    prices = price_maps(bars)

    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    demand = unconstrained_demand(candidates, cal)

    base = simulate(
        candidates, cal, prices,
        round_trip_cost=MAIN_COST,
        tie_seed=None,
    )
    stress = simulate(
        candidates, cal, prices,
        round_trip_cost=STRESS_COST,
        tie_seed=None,
    )

    base["curve"].to_csv(
        OUT_ROOT / "equity_curve_20bps.csv",
        index=False, encoding="utf-8-sig",
    )
    base["trades"].to_csv(
        OUT_ROOT / "trades_20bps.csv",
        index=False, encoding="utf-8-sig",
    )
    base["skipped"].to_csv(
        OUT_ROOT / "skipped_20bps.csv",
        index=False, encoding="utf-8-sig",
    )
    stress["curve"].to_csv(
        OUT_ROOT / "equity_curve_50bps.csv",
        index=False, encoding="utf-8-sig",
    )

    random_rows = []
    for seed in range(RANDOM_SEEDS):
        sim = simulate(
            candidates, cal, prices,
            round_trip_cost=MAIN_COST,
            tie_seed=seed,
        )
        m = sim["metrics"]
        random_rows.append({
            "seed": seed,
            "trades": m["trades_executed"],
            "coverage": m["coverage_vs_candidate_events"],
            "cagr": m["cagr_calendar"],
            "max_drawdown": m["max_drawdown"],
            "sharpe": m["sharpe_actual_sessions"],
            "avg_gross": m["avg_gross_weight"],
            "capital_efficiency": m["cagr_per_avg_gross"],
        })
        if seed == 0 or (seed + 1) % 10 == 0:
            print(
                f"RANDOM_TIE_BREAK [{seed+1}/{RANDOM_SEEDS}] "
                f"CAGR={m['cagr_calendar']:+.3%} "
                f"MDD={m['max_drawdown']:+.3%} "
                f"Sharpe={m['sharpe_actual_sessions']:.3f}"
            )

    rdf = pd.DataFrame(random_rows)
    rdf.to_csv(
        OUT_ROOT / "random_tie_break_100.csv",
        index=False, encoding="utf-8-sig",
    )

    random_audit = {
        "seeds": RANDOM_SEEDS,
        "cagr": quantiles(rdf["cagr"].tolist()),
        "max_drawdown": quantiles(rdf["max_drawdown"].tolist()),
        "sharpe": quantiles(rdf["sharpe"].tolist()),
        "coverage": quantiles(rdf["coverage"].tolist()),
        "avg_gross": quantiles(rdf["avg_gross"].tolist()),
        "capital_efficiency": quantiles(
            rdf["capital_efficiency"].tolist()
        ),
    }

    gate = standalone_gate(
        base["metrics"], stress["metrics"], random_audit
    )

    result = {
        "status": "PASS_EARNINGS_IMPROVING_H60_PORTFOLIO_AUDIT_V0_1",
        "generated_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "strategy_id": "EARNINGS_IMPROVING_H60_V0_1",
        "source": {
            "event_returns": str(EVENT_RETURNS),
            "event_returns_sha256": EXPECTED_EVENT_RETURNS_SHA256,
            "event_audit": str(EVENT_AUDIT),
            "event_audit_status": audit.get("status"),
            "corpus_sha256": EXPECTED_CORPUS_SHA256,
            "parser_sha256": EXPECTED_PARSER_SHA256,
        },
        "definition": {
            "event_kind": "ORIGINAL_FORECAST",
            "parser_confidence": "HIGH",
            "change_type": "IMPROVING",
            "entry": "T+1 exact OPEN",
            "exit": "S60 exact CLOSE",
            "duplicate": "ignore later signal while symbol actually held",
            "target_weight": TARGET_WEIGHT,
            "max_gross": MAX_GROSS,
            "max_positions": MAX_POSITIONS,
            "fractional_shares": True,
            "same_day_tie_break": "SYMBOL_ASC",
            "same_day_exit_not_reusable_at_open": True,
            "no_rebalance_existing_positions": True,
        },
        "candidate": cinfo,
        "market": minfo,
        "price_lineage": lineage,
        "unconstrained_capital_demand": demand,
        "base_20bps": base["metrics"],
        "stress_50bps": stress["metrics"],
        "random_tie_break_20bps": random_audit,
        "standalone_gate": gate,
        "caveats": {
            "historical_strict_non_st_pit_applied": False,
            "event_universe_note": (
                "Semantic parser excludes explicit ST-at-event, but complete "
                "historical ST PIT is unavailable."
            ),
            "official_benchmark_not_used_in_portfolio_pnl": True,
            "capacity_liquidity_not_yet_audited": True,
            "lot_size_not_modeled": True,
            "fractional_shares_research_only": True,
            "forward_not_approved": True,
        },
        "next_if_gate_pass": (
            "ALPHA2_INCREMENTAL_VALUE_V0_1: compare correlation, trade/capital "
            "overlap, delta CAGR/MDD/Sharpe versus frozen Deep Oversold."
        ),
        "outputs": [
            "portfolio_summary.json",
            "equity_curve_20bps.csv",
            "equity_curve_50bps.csv",
            "trades_20bps.csv",
            "skipped_20bps.csv",
            "random_tie_break_100.csv",
        ],
        "no_network": True,
        "no_database_mutation": True,
        "output_dir": str(OUT_ROOT),
    }

    write_json_atomic(OUT_ROOT / "portfolio_summary.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["check", "run"])
    args = ap.parse_args()
    return check() if args.command == "check" else run()


if __name__ == "__main__":
    raise SystemExit(main())
