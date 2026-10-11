from __future__ import annotations

r"""
PositionX — CSI500 52-Week-High Standalone Portfolio Audit V0.1

This audit starts only because Phase-1 event study returned:
    STRONG_52WH_CANDIDATE

It tests BOTH predeclared candidate holding horizons:
    H60 and H120

No new alpha filter is introduced.

Frozen portfolio policy
-----------------------
Signal source:
    Phase-1 POS 52WH candidate_events.csv only.

One position per symbol:
    while held, later 52WH signals for the same symbol are ignored.

Timing:
    signal T close
    enter T+1 exact OPEN
    exit exact S60 or S120 CLOSE

Capital:
    initial equity = 1,000,000
    target weight per entry = 1%
    max target gross = 30%
    max active positions = 30
    fractional shares (research only)
    no rebalance of existing positions

Same-day order:
    existing positions marked at OPEN
    new entries allocated at OPEN
    exits happen at CLOSE
    same-day CLOSE exits are NOT reusable at that OPEN

Capacity:
    baseline same-day tie-break = SYMBOL_ASC
    100 deterministic random tie-break seeds at 20bp
    if same-day candidates exceed free slots, only selected candidates enter
    if target gross/cash is tighter, selected entries are pro-rata scaled

Costs:
    20bp round-trip main
    50bp round-trip stress

Prices:
    use the same corporate-action-safe synthetic adjusted price state as Phase 1:
        daily_factor = close / pre_close
        adj_close = cumulative product(daily_factor)
        adj_open = adj_close * open / close

Standalone gate (same gate used for Earnings standalone portfolio)
------------------------------------------------------------------
For each H60 / H120:
- CAGR @20bp > 0
- CAGR @50bp > 0
- Sharpe @20bp >= 0.5
- MDD @20bp >= -20%
- at least 2 positive calendar years
- random-seed median CAGR > 0
- random-seed p10 CAGR > 0

PASS here means the 52WH horizon may advance to same-family comparison with
ret120. It is NOT Forward approval and NOT yet Strategy #2.
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

ROOT = Path(__file__).resolve().parents[1]
CATALOG = Path(r"D:\positionx_data\catalog\market_data.duckdb")
PHASE1_ROOT = Path(r"D:\positionx_data\research\csi500_52wh_v0_1")
PHASE1_SUMMARY = PHASE1_ROOT / "audit_summary.json"
PHASE1_EVENTS = PHASE1_ROOT / "candidate_events.csv"
PHASE1_RETURNS = PHASE1_ROOT / "event_returns.csv"

EXPECTED_PHASE1_SUMMARY_SHA256 = (
    "81905965522e3de13aa17bdfe3b76014ee974165ffdf3d8608713cf25cce74ab"
)
EXPECTED_MEMBERSHIP_SHA256 = (
    "8ab943bcb14a95a9c06a24809f96eb2b61a3fa9244cffe2a861413cd203ad542"
)
EXPECTED_STATUS_SHA256 = (
    "bfe788d755fabb03986635d975cab22f1311af3a82ec533eb244ab85560c5bf2"
)

OUT_ROOT = Path(r"D:\positionx_data\research\csi500_52wh_portfolio_v0_1")

INITIAL_EQUITY = 1_000_000.0
TARGET_WEIGHT = 0.01
MAX_GROSS = 0.30
MAX_POSITIONS = 30
MAIN_COST = 0.002
STRESS_COST = 0.005
RANDOM_SEEDS = 100
HORIZONS = (60, 120)
EPS = 1e-12


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
        json.dumps(obj, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def stable_key(seed: int, d: str, symbol: str) -> str:
    return hashlib.sha256(
        f"{seed}|{d}|{symbol}".encode("utf-8")
    ).hexdigest()


def safe_float(v: Any) -> float | None:
    if v is None or pd.isna(v):
        return None
    return float(v)


def qtile(s: pd.Series) -> dict:
    x = pd.to_numeric(s, errors="coerce").dropna()
    return {
        "min": safe_float(x.min()),
        "p10": safe_float(x.quantile(0.10)),
        "p25": safe_float(x.quantile(0.25)),
        "median": safe_float(x.median()),
        "p75": safe_float(x.quantile(0.75)),
        "p90": safe_float(x.quantile(0.90)),
        "max": safe_float(x.max()),
        "mean": safe_float(x.mean()),
    }


def verify_phase1() -> dict:
    for p in (CATALOG, PHASE1_SUMMARY, PHASE1_EVENTS, PHASE1_RETURNS):
        if not p.exists():
            raise RuntimeError(f"FAIL_CLOSED missing input: {p}")

    actual = sha256_file(PHASE1_SUMMARY)
    if actual != EXPECTED_PHASE1_SUMMARY_SHA256:
        raise RuntimeError(
            "FAIL_CLOSED Phase1 summary hash drift "
            f"expected={EXPECTED_PHASE1_SUMMARY_SHA256} actual={actual}"
        )
    s = json.loads(PHASE1_SUMMARY.read_text(encoding="utf-8"))
    if s.get("status") != "PASS_CSI500_52WH_V0_1":
        raise RuntimeError(
            f"FAIL_CLOSED Phase1 status={s.get('status')}"
        )
    if s.get("decision", {}).get("verdict") != "STRONG_52WH_CANDIDATE":
        raise RuntimeError(
            "FAIL_CLOSED Phase1 verdict is not STRONG_52WH_CANDIDATE"
        )
    fp = s.get("fingerprints", {})
    if fp.get("membership_sha256") != EXPECTED_MEMBERSHIP_SHA256:
        raise RuntimeError("FAIL_CLOSED Phase1 membership fingerprint drift")
    if fp.get("status_sha256") != EXPECTED_STATUS_SHA256:
        raise RuntimeError("FAIL_CLOSED Phase1 status fingerprint drift")

    return s


def load_events_by_horizon(phase1: dict) -> dict[int, pd.DataFrame]:
    """
    Standalone portfolio uses the exact Phase-1 complete-outcome rows for each
    horizon. This matches the accepted PositionX research convention used by
    earlier standalone audits: missing exact future exit bars are excluded from
    the economic sample rather than silently shifted to a different exit rule.

    This is a research completeness convention, not a live execution rule.
    Forward approval would still require an explicit suspension/exit policy.
    """
    x = pd.read_csv(PHASE1_RETURNS, dtype={"symbol": str})
    required = {
        "symbol", "signal_date", "group", "horizon",
        "entry_date", "exit_date", "measurement_status",
        "entry_adj_open", "exit_adj_close"
    }
    missing = sorted(required - set(x.columns))
    if missing:
        raise RuntimeError(
            f"FAIL_CLOSED event_returns missing={missing}"
        )

    x["symbol"] = x["symbol"].astype(str)
    for c in ("signal_date", "entry_date", "exit_date"):
        x[c] = (
            x[c].astype(str)
            .str.replace("-", "", regex=False)
            .str.replace(".0", "", regex=False)
            .str[:8]
        )
    x["horizon"] = pd.to_numeric(x["horizon"], errors="raise").astype(int)

    out = {}
    expected_n = {60: 7387, 120: 7374}
    for h in HORIZONS:
        z = x[
            (x["group"] == "POS")
            & (x["horizon"] == h)
            & (x["measurement_status"] == "OK")
        ].copy()
        if len(z) != expected_n[h]:
            raise RuntimeError(
                f"FAIL_CLOSED Phase1 complete POS H{h} count drift "
                f"got={len(z)} expected={expected_n[h]}"
            )
        if z.duplicated(["signal_date", "symbol"]).any():
            raise RuntimeError(
                f"FAIL_CLOSED duplicate Phase1 H{h} signal_date/symbol"
            )
        out[h] = z.sort_values(
            ["signal_date", "symbol"]
        ).reset_index(drop=True)
    return out


def load_calendar(con: duckdb.DuckDBPyConnection,
                  start: str, end: str) -> list[str]:
    rows = con.execute(
        """
        SELECT DISTINCT strftime(CAST(trade_date AS DATE), '%Y%m%d') d
        FROM market.daily_bars_v1
        WHERE strftime(CAST(trade_date AS DATE), '%Y%m%d')
              BETWEEN ? AND ?
        ORDER BY d
        """,
        [start, end],
    ).fetchall()
    cal = [str(r[0]) for r in rows if r and r[0]]
    if not cal or cal[0] > start or cal[-1] < end:
        raise RuntimeError(
            f"FAIL_CLOSED incomplete calendar {start}..{end}"
        )
    return cal


def load_adjusted_panel(
    con: duckdb.DuckDBPyConnection,
    events: pd.DataFrame,
) -> pd.DataFrame:
    symbols = pd.DataFrame(
        {"symbol": sorted(events["symbol"].unique())}
    )
    con.register("px_symbols", symbols)
    # Need enough history before Phase1 signal window to make cumulative
    # close/pre_close state comparable with Phase1. Same start as Phase1.
    start = "20220101"
    end = str(events["exit_date"].max())
    try:
        x = con.execute(
            """
            SELECT
              CAST(b.symbol AS VARCHAR) symbol,
              strftime(CAST(b.trade_date AS DATE),'%Y%m%d') trade_date,
              CAST(b."open" AS DOUBLE) "open",
              CAST(b."close" AS DOUBLE) "close",
              CAST(b.pre_close AS DOUBLE) pre_close
            FROM market.daily_bars_v1 b
            INNER JOIN px_symbols s USING(symbol)
            WHERE strftime(CAST(b.trade_date AS DATE),'%Y%m%d')
                  BETWEEN ? AND ?
            ORDER BY b.symbol, b.trade_date
            """,
            [start, end],
        ).df()
    finally:
        con.unregister("px_symbols")

    for c in ("open", "close", "pre_close"):
        x[c] = pd.to_numeric(x[c], errors="coerce")
    x["daily_factor"] = np.where(
        (x["close"] > 0) & (x["pre_close"] > 0),
        x["close"] / x["pre_close"],
        np.nan,
    )
    if x["daily_factor"].isna().any():
        # Missing/invalid rows are not silently bridged because this portfolio
        # requires exact adjusted MTM.
        bad = x[x["daily_factor"].isna()][
            ["symbol", "trade_date", "close", "pre_close"]
        ].head(10).to_dict("records")
        raise RuntimeError(
            f"FAIL_CLOSED invalid adjusted factor rows sample={bad}"
        )

    x["log_factor"] = np.log(x["daily_factor"])
    x["cum_log"] = (
        x.groupby("symbol", sort=False)["log_factor"].cumsum()
    )
    x["adj_close"] = np.exp(x["cum_log"])
    x["adj_open"] = np.where(
        (x["open"] > 0) & (x["close"] > 0),
        x["adj_close"] * x["open"] / x["close"],
        np.nan,
    )
    return x[
        ["symbol", "trade_date", "adj_open", "adj_close"]
    ].copy()


def price_maps(panel: pd.DataFrame) -> tuple[dict, dict]:
    op = {}
    cl = {}
    for r in panel.itertuples(index=False):
        if pd.notna(r.adj_open) and float(r.adj_open) > 0:
            op[(str(r.symbol), str(r.trade_date))] = float(r.adj_open)
        if pd.notna(r.adj_close) and float(r.adj_close) > 0:
            cl[(str(r.symbol), str(r.trade_date))] = float(r.adj_close)
    return op, cl


def metrics(curve: pd.DataFrame) -> dict:
    if curve.empty:
        raise RuntimeError("empty curve")
    eq = curve["equity"].astype(float)
    dates = pd.to_datetime(curve["date"], format="%Y%m%d")
    years = max(
        (dates.iloc[-1] - dates.iloc[0]).days / 365.2425,
        1 / 365.2425,
    )
    total = float(eq.iloc[-1] / eq.iloc[0] - 1.0)
    cagr = float(
        (eq.iloc[-1] / eq.iloc[0]) ** (1.0 / years) - 1.0
    )
    ret = eq.pct_change(fill_method=None).fillna(0.0)
    sd = float(ret.std(ddof=1))
    sharpe = (
        float(ret.mean() / sd * math.sqrt(252))
        if sd > 0 else None
    )
    dd = eq / eq.cummax() - 1.0

    tmp = curve[["date", "equity"]].copy()
    tmp["year"] = tmp["date"].astype(str).str[:4]
    tmp["daily_return"] = tmp["equity"].pct_change(fill_method=None)
    if len(tmp):
        tmp.loc[tmp.index[0], "daily_return"] = (
            float(tmp.loc[tmp.index[0], "equity"]) / INITIAL_EQUITY - 1.0
        )
    yearly = {
        str(y): float((1.0 + g["daily_return"]).prod() - 1.0)
        for y, g in tmp.groupby("year", sort=True)
    }

    return {
        "sessions": int(len(curve)),
        "start": str(curve["date"].iloc[0]),
        "end": str(curve["date"].iloc[-1]),
        "total_return": total,
        "cagr": cagr,
        "max_drawdown": float(dd.min()),
        "sharpe": sharpe,
        "avg_gross": float(curve["gross_weight"].mean()),
        "max_gross": float(curve["gross_weight"].max()),
        "avg_active_positions": float(
            curve["active_positions"].mean()
        ),
        "max_active_positions": int(
            curve["active_positions"].max()
        ),
        "invested_day_fraction": float(
            (curve["active_positions"] > 0).mean()
        ),
        "capital_efficiency": (
            cagr / float(curve["gross_weight"].mean())
            if float(curve["gross_weight"].mean()) > 0 else None
        ),
        "calendar_year_returns": yearly,
    }


def simulate(
    events: pd.DataFrame,
    calendar: list[str],
    open_map: dict,
    close_map: dict,
    horizon: int,
    round_trip_cost: float,
    seed: int | None,
) -> dict:
    e = events.copy()
    if int(e["horizon"].nunique()) != 1 or int(e["horizon"].iloc[0]) != horizon:
        raise RuntimeError(f"FAIL_CLOSED mixed/wrong horizon input H{horizon}")

    by_signal_entry = {
        d: g.to_dict("records")
        for d, g in e.groupby("entry_date", sort=True)
    }

    half = round_trip_cost / 2.0
    cash = INITIAL_EQUITY
    active: dict[str, dict] = {}
    trades = []
    skipped = []
    curve = []

    start = str(e["entry_date"].min())
    end = str(e["exit_date"].max())
    dates = [d for d in calendar if start <= d <= end]
    if not dates or dates[0] != start or dates[-1] != end:
        raise RuntimeError(
            f"FAIL_CLOSED simulation calendar {start}..{end}"
        )

    last_close = {}
    scaled_entry_days = 0
    capacity_bind_days = 0
    no_slot_days = 0
    entry_candidate_total = 0

    for d in dates:
        # OPEN mark existing positions.
        open_mv = 0.0
        for sym, pos in active.items():
            px = open_map.get((sym, d))
            if px is None:
                px = last_close.get(sym)
            if px is None:
                raise RuntimeError(
                    f"FAIL_CLOSED no OPEN mark {sym} {d}"
                )
            pos["open_mark"] = px
            open_mv += pos["shares"] * px

        equity_open = cash + open_mv
        gross_cap = MAX_GROSS * equity_open
        remaining_gross = max(0.0, gross_cap - open_mv)
        remaining_cash = max(0.0, cash / (1.0 + half))
        available = min(remaining_gross, remaining_cash)

        todays = list(by_signal_entry.get(d, []))
        entry_candidate_total += len(todays)

        eligible = []
        for r in todays:
            sym = str(r["symbol"])
            if sym in active:
                skipped.append(
                    {
                        "symbol": sym,
                        "signal_date": r["signal_date"],
                        "entry_date": d,
                        "reason": "DUPLICATE_WHILE_HELD",
                    }
                )
            else:
                eligible.append(r)

        if seed is None:
            eligible.sort(
                key=lambda r: (str(r["symbol"]), str(r["signal_date"]))
            )
        else:
            eligible.sort(
                key=lambda r: stable_key(
                    seed, d, str(r["symbol"])
                )
            )

        slots = max(0, MAX_POSITIONS - len(active))
        selected = eligible[:slots]
        if eligible and slots == 0:
            no_slot_days += 1
        for r in eligible[slots:]:
            skipped.append(
                {
                    "symbol": str(r["symbol"]),
                    "signal_date": r["signal_date"],
                    "entry_date": d,
                    "reason": "NO_SLOT",
                }
            )

        desired_each = TARGET_WEIGHT * equity_open
        desired_total = desired_each * len(selected)
        alloc_total = min(desired_total, available)
        scale = (
            alloc_total / desired_total
            if desired_total > 0 else 0.0
        )
        if selected and scale < 1.0 - 1e-12:
            scaled_entry_days += 1
            capacity_bind_days += 1

        if scale <= EPS:
            for r in selected:
                skipped.append(
                    {
                        "symbol": str(r["symbol"]),
                        "signal_date": r["signal_date"],
                        "entry_date": d,
                        "reason": "NO_GROSS_OR_CASH_CAPACITY",
                    }
                )
        else:
            notional = desired_each * scale
            for r in selected:
                sym = str(r["symbol"])
                px = open_map.get((sym, d))
                if px is None or px <= 0:
                    skipped.append(
                        {
                            "symbol": sym,
                            "signal_date": r["signal_date"],
                            "entry_date": d,
                            "reason": "NO_EXACT_ENTRY_PRICE",
                        }
                    )
                    continue
                entry_fee = notional * half
                if notional + entry_fee > cash + 1e-8:
                    raise RuntimeError(
                        "FAIL_CLOSED cash invariant at entry"
                    )
                shares = notional / px
                cash -= notional + entry_fee
                active[sym] = {
                    "symbol": sym,
                    "signal_date": str(r["signal_date"]),
                    "entry_date": d,
                    "exit_date": str(r["exit_date"]),
                    "entry_price": px,
                    "entry_notional": notional,
                    "entry_fee": entry_fee,
                    "shares": shares,
                }

        # CLOSE mark all positions and then exact exits.
        for sym, pos in list(active.items()):
            px = close_map.get((sym, d))
            if px is None:
                px = last_close.get(sym)
            if px is None:
                raise RuntimeError(
                    f"FAIL_CLOSED no CLOSE mark {sym} {d}"
                )
            pos["close_mark"] = px
            last_close[sym] = px

        for sym in list(active.keys()):
            pos = active[sym]
            if pos["exit_date"] != d:
                continue
            px = close_map.get((sym, d))
            if px is None or px <= 0:
                raise RuntimeError(
                    f"FAIL_CLOSED missing exact exit {sym} {d}"
                )
            gross = pos["shares"] * px
            exit_fee = gross * half
            cash += gross - exit_fee
            pnl = (
                gross - exit_fee
                - pos["entry_notional"] - pos["entry_fee"]
            )
            trades.append(
                {
                    **pos,
                    "exit_price": px,
                    "exit_notional": gross,
                    "exit_fee": exit_fee,
                    "net_pnl": pnl,
                    "net_return_on_entry_notional": (
                        pnl / pos["entry_notional"]
                    ),
                }
            )
            del active[sym]

        close_mv = sum(
            p["shares"] * p["close_mark"]
            for p in active.values()
        )
        equity = cash + close_mv
        curve.append(
            {
                "date": d,
                "equity": equity,
                "cash": cash,
                "gross_market_value": close_mv,
                "gross_weight": (
                    close_mv / equity if equity > 0 else 0.0
                ),
                "active_positions": len(active),
            }
        )

    if active:
        sample = [
            (sym, p["exit_date"])
            for sym, p in list(active.items())[:10]
        ]
        raise RuntimeError(
            f"FAIL_CLOSED positions remain={len(active)} sample={sample}"
        )

    curve_df = pd.DataFrame(curve)
    trades_df = pd.DataFrame(trades)
    skipped_df = pd.DataFrame(skipped)
    m = metrics(curve_df)

    skip_counts = (
        skipped_df["reason"].value_counts().to_dict()
        if len(skipped_df) else {}
    )

    return {
        "metrics": m,
        "curve": curve_df,
        "trades": trades_df,
        "skipped": skipped_df,
        "diagnostics": {
            "horizon": horizon,
            "round_trip_cost": round_trip_cost,
            "candidate_signals": int(len(e)),
            "entry_candidates_total": int(entry_candidate_total),
            "executed_trades": int(len(trades_df)),
            "coverage": float(
                len(trades_df) / len(e)
            ) if len(e) else None,
            "scaled_entry_days": int(scaled_entry_days),
            "capacity_bind_days": int(capacity_bind_days),
            "no_slot_days": int(no_slot_days),
            "skip_counts": {
                str(k): int(v) for k, v in skip_counts.items()
            },
            "trade_avg_net_return": (
                safe_float(
                    trades_df["net_return_on_entry_notional"].mean()
                ) if len(trades_df) else None
            ),
            "trade_median_net_return": (
                safe_float(
                    trades_df["net_return_on_entry_notional"].median()
                ) if len(trades_df) else None
            ),
            "trade_win_rate": (
                safe_float(
                    (trades_df["net_return_on_entry_notional"] > 0).mean()
                ) if len(trades_df) else None
            ),
        },
    }


def standalone_gate(
    main: dict,
    stress: dict,
    random_df: pd.DataFrame,
) -> dict:
    yearly = main["metrics"]["calendar_year_returns"]
    positive_years = sum(1 for v in yearly.values() if v > 0)
    checks = {
        "cagr20_positive": bool(main["metrics"]["cagr"] > 0),
        "cagr50_positive": bool(stress["metrics"]["cagr"] > 0),
        "sharpe20_ge_0_5": bool(
            main["metrics"]["sharpe"] is not None
            and main["metrics"]["sharpe"] >= 0.5
        ),
        "mdd20_within_20pct": bool(
            main["metrics"]["max_drawdown"] >= -0.20
        ),
        "at_least_2_positive_calendar_years": bool(
            positive_years >= 2
        ),
        "random_seed_median_cagr_positive": bool(
            random_df["cagr"].median() > 0
        ),
        "random_seed_p10_cagr_positive": bool(
            random_df["cagr"].quantile(0.10) > 0
        ),
    }
    return {
        "status": (
            "PASS_STANDALONE_PORTFOLIO_GATE"
            if all(checks.values())
            else "FAIL_STANDALONE_PORTFOLIO_GATE"
        ),
        "checks": checks,
        "positive_calendar_years": positive_years,
        "note": (
            "PASS only advances this horizon to same-family comparison "
            "with ret120; it is not Strategy #2 or Forward approval."
        ),
    }


def run_horizon(
    events: pd.DataFrame,
    calendar: list[str],
    op: dict,
    cl: dict,
    horizon: int,
) -> dict:
    main = simulate(
        events, calendar, op, cl,
        horizon, MAIN_COST, seed=None
    )
    stress = simulate(
        events, calendar, op, cl,
        horizon, STRESS_COST, seed=None
    )

    random_rows = []
    for seed in range(RANDOM_SEEDS):
        z = simulate(
            events, calendar, op, cl,
            horizon, MAIN_COST, seed=seed
        )
        random_rows.append(
            {
                "seed": seed,
                "cagr": z["metrics"]["cagr"],
                "max_drawdown": z["metrics"]["max_drawdown"],
                "sharpe": z["metrics"]["sharpe"],
                "avg_gross": z["metrics"]["avg_gross"],
                "capital_efficiency": z["metrics"]["capital_efficiency"],
                "trades": z["diagnostics"]["executed_trades"],
                "coverage": z["diagnostics"]["coverage"],
            }
        )
    rdf = pd.DataFrame(random_rows)
    gate = standalone_gate(main, stress, rdf)

    return {
        "main": main,
        "stress": stress,
        "random_df": rdf,
        "random_summary": {
            "seeds": RANDOM_SEEDS,
            "cagr": qtile(rdf["cagr"]),
            "max_drawdown": qtile(rdf["max_drawdown"]),
            "sharpe": qtile(rdf["sharpe"]),
            "avg_gross": qtile(rdf["avg_gross"]),
            "capital_efficiency": qtile(
                rdf["capital_efficiency"]
            ),
            "trades": qtile(rdf["trades"]),
            "coverage": qtile(rdf["coverage"]),
        },
        "gate": gate,
    }


def family_state(results: dict[int, dict]) -> str:
    passed = [
        h for h in HORIZONS
        if results[h]["gate"]["status"]
        == "PASS_STANDALONE_PORTFOLIO_GATE"
    ]
    if len(passed) == 2:
        return "BOTH_H60_H120_PASS_NEEDS_RET120_FAMILY_COMPARISON"
    if len(passed) == 1:
        return f"H{passed[0]}_PASS_NEEDS_RET120_FAMILY_COMPARISON"
    return "NO_52WH_HORIZON_PASSES_STANDALONE"


def check() -> int:
    phase1 = verify_phase1()
    events_by_h = load_events_by_horizon(phase1)
    result = {
        "status": "PASS_CSI500_52WH_PORTFOLIO_V0_1_CHECK",
        "phase1_summary_sha256": sha256_file(PHASE1_SUMMARY),
        "phase1_events_sha256": sha256_file(PHASE1_EVENTS),
        "phase1_returns_sha256": sha256_file(PHASE1_RETURNS),
        "complete_pos_events": {
            str(h): int(len(events_by_h[h])) for h in HORIZONS
        },
        "horizons": list(HORIZONS),
        "target_weight": TARGET_WEIGHT,
        "max_gross": MAX_GROSS,
        "max_positions": MAX_POSITIONS,
        "main_cost": MAIN_COST,
        "stress_cost": STRESS_COST,
        "random_seeds": RANDOM_SEEDS,
        "no_network": True,
        "no_qmt": True,
        "no_database_mutation": True,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def run() -> int:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    phase1 = verify_phase1()
    events_by_h = load_events_by_horizon(phase1)
    all_events = pd.concat(
        [events_by_h[h] for h in HORIZONS],
        ignore_index=True,
    )

    con = duckdb.connect(str(CATALOG), read_only=True)
    try:
        panel = load_adjusted_panel(con, all_events)
        calendar = load_calendar(
            con,
            min(events_by_h[h]["entry_date"].min() for h in HORIZONS),
            max(events_by_h[h]["exit_date"].max() for h in HORIZONS),
        )
    finally:
        con.close()

    op, cl = price_maps(panel)

    # Cross-check Phase-1 exact entry/exit adjusted prices before portfolio use.
    for h in HORIZONS:
        z = events_by_h[h]
        bad = []
        for r in z.itertuples(index=False):
            ep = op.get((str(r.symbol), str(r.entry_date)))
            xp = cl.get((str(r.symbol), str(r.exit_date)))
            if ep is None or xp is None:
                bad.append((r.symbol, r.entry_date, r.exit_date, "MISSING"))
                continue
            if abs(ep - float(r.entry_adj_open)) > max(abs(ep), abs(float(r.entry_adj_open)), 1.0) * 1e-10:
                bad.append((r.symbol, r.entry_date, r.exit_date, "ENTRY_DRIFT"))
            elif abs(xp - float(r.exit_adj_close)) > max(abs(xp), abs(float(r.exit_adj_close)), 1.0) * 1e-10:
                bad.append((r.symbol, r.entry_date, r.exit_date, "EXIT_DRIFT"))
            if len(bad) >= 10:
                break
        if bad:
            raise RuntimeError(
                f"FAIL_CLOSED Phase1 adjusted-price lineage mismatch H{h} sample={bad}"
            )

    results = {}
    for h in HORIZONS:
        results[h] = run_horizon(
            events_by_h[h], calendar, op, cl, h
        )

        results[h]["main"]["curve"].to_csv(
            OUT_ROOT / f"h{h}_equity_curve_20bps.csv",
            index=False, encoding="utf-8-sig",
        )
        results[h]["main"]["trades"].to_csv(
            OUT_ROOT / f"h{h}_trades_20bps.csv",
            index=False, encoding="utf-8-sig",
        )
        results[h]["main"]["skipped"].to_csv(
            OUT_ROOT / f"h{h}_skipped_20bps.csv",
            index=False, encoding="utf-8-sig",
        )
        results[h]["stress"]["curve"].to_csv(
            OUT_ROOT / f"h{h}_equity_curve_50bps.csv",
            index=False, encoding="utf-8-sig",
        )
        results[h]["random_df"].to_csv(
            OUT_ROOT / f"h{h}_random_tie_break_100.csv",
            index=False, encoding="utf-8-sig",
        )

    compact = {}
    for h in HORIZONS:
        compact[f"h{h}"] = {
            "main_20bps": {
                "metrics": results[h]["main"]["metrics"],
                "diagnostics": results[h]["main"]["diagnostics"],
            },
            "stress_50bps": {
                "metrics": results[h]["stress"]["metrics"],
                "diagnostics": results[h]["stress"]["diagnostics"],
            },
            "random_tie_break_20bps": results[h]["random_summary"],
            "standalone_gate": results[h]["gate"],
        }

    summary = {
        "status": "PASS_CSI500_52WH_STANDALONE_PORTFOLIO_AUDIT_V0_1",
        "generated_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "research_id": "CSI500_52WH_STANDALONE_PORTFOLIO_V0_1",
        "phase1": {
            "status": phase1["status"],
            "verdict": phase1["decision"]["verdict"],
            "summary_sha256": sha256_file(PHASE1_SUMMARY),
            "events_sha256": sha256_file(PHASE1_EVENTS),
            "event_returns_sha256": sha256_file(PHASE1_RETURNS),
            "complete_pos_events": {
                str(h): int(len(events_by_h[h])) for h in HORIZONS
            },
        },
        "policy": {
            "candidate_group": "POS 52WH only",
            "horizons": list(HORIZONS),
            "target_weight": TARGET_WEIGHT,
            "max_target_gross": MAX_GROSS,
            "max_positions": MAX_POSITIONS,
            "fractional_shares": True,
            "no_duplicate_symbol_while_held": True,
            "same_day_exit_not_reusable_at_open": True,
            "same_day_tie_break_baseline": "SYMBOL_ASC",
            "random_same_day_tie_break_seeds": RANDOM_SEEDS,
            "entry_capacity_scaling": "pro-rata selected entries if gross/cash tighter",
            "existing_positions_rebalanced": False,
            "main_round_trip_cost": MAIN_COST,
            "stress_round_trip_cost": STRESS_COST,
            "price_state": "synthetic adjusted close/pre_close cumulative state",
            "no_alpha_filters_added": True,
        },
        "results": compact,
        "family_state": family_state(results),
        "family_note": {
            "ret120_same_broad_family": True,
            "rule": (
                "A standalone PASS does not make 52WH Strategy #2. "
                "Next compare surviving 52WH horizon(s) against ret120 "
                "under comparable shared-equity economics, then only the "
                "family survivor advances to Deep incremental-value testing."
            ),
        },
        "standalone_gate_definition": {
            "cagr20_positive": True,
            "cagr50_positive": True,
            "sharpe20_ge_0_5": 0.5,
            "mdd20_floor": -0.20,
            "at_least_2_positive_calendar_years": 2,
            "random_seed_median_cagr_positive": True,
            "random_seed_p10_cagr_positive": True,
        },
        "caveats": {
            "historical_strict_non_st_pit_applied": False,
            "unknown_st_retained": True,
            "complete_outcome_research_cohort": True,
            "missing_exact_future_exit_rows_excluded_from_economic_sample": True,
            "forward_exit_suspension_policy_not_defined": True,
            "capacity_liquidity_not_yet_audited": True,
            "lot_size_not_modeled": True,
            "fractional_shares_research_only": True,
            "forward_not_approved": True,
        },
        "outputs": [
            "portfolio_summary.json",
            "h60_equity_curve_20bps.csv",
            "h60_trades_20bps.csv",
            "h60_random_tie_break_100.csv",
            "h120_equity_curve_20bps.csv",
            "h120_trades_20bps.csv",
            "h120_random_tie_break_100.csv",
        ],
        "no_network": True,
        "no_qmt": True,
        "no_database_mutation": True,
    }

    write_json_atomic(
        OUT_ROOT / "portfolio_summary.json", summary
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"OUTPUT: {OUT_ROOT}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["check", "run"])
    args = p.parse_args()
    return check() if args.command == "check" else run()


if __name__ == "__main__":
    raise SystemExit(main())
