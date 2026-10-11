from __future__ import annotations

r"""
PositionX — EARNINGS_IMPROVING_H60 Residual Capacity Audit V0.1

Question
--------
Deep Oversold has priority. Can EARNINGS_IMPROVING_H60 use only Deep's unused
target gross budget and improve the common-period account?

This is the final cheap role test after:
- Earnings standalone portfolio PASS
- 50/50 diversification vs Deep did NOT improve Deep's CAGR/MDD/Sharpe mix

Primary policy
--------------
- Deep Oversold book is fixed / priority and is never resized by Earnings.
- Earnings candidates: frozen IMPROVING + H60 + OK event set.
- Earnings target = 1% of prior-close combined equity per new entry.
- Earnings max positions = 30.
- Combined TARGET gross budget at entry = 30%.
- Same-day Deep entries consume budget first.
- Same-day Deep exits are not reusable at that day's open.
- Existing Earnings positions are not rebalanced.
- Earnings signals while that Earnings symbol is already held are ignored.
- New Earnings entry is blocked if Deep already holds that symbol at the open.
- If Deep enters a symbol already held by Earnings later, the Earnings position
  is NOT forcibly closed; Deep remains priority and the scheduled Earnings S60
  exit is preserved.
- Mark-to-market can make realized gross exceed 30% after entry. This mirrors the
  existing PositionX shared-equity semantics; 30% is a TARGET-GROSS entry budget,
  not a forced daily deleveraging rule.
- 20bp round-trip main cost for Earnings overlay.
- 50bp Earnings-only stress cost diagnostic (Deep remains its 20bp frozen replay).
- 100 deterministic random same-day tie-break replays at 20bp.

Deep source
-----------
Uses the V0.1.3 economic-equivalent frozen-event replay artifacts:
- exact 1366-row trades_df export
- exact replay equity curve
- current cached QMT front-adjusted marks, no download/refresh

Earnings source
---------------
Uses event_returns.csv (2731 IMPROVING/H60/OK candidates) and
market.daily_bars_v1, preserving the Earnings standalone research lineage.

Verdict (predeclared)
---------------------
STRONG_PASS:
  dCAGR > 0 AND dSharpe > 0 AND dMDD >= 0

USEFUL_INCREMENTAL:
  dCAGR > 0 AND (dSharpe > 0 OR dMDD >= 0)

CAPITAL_FILLER_ONLY:
  dCAGR > 0 AND dSharpe <= 0 AND dMDD < 0

REJECT:
  dCAGR <= 0

No factor tuning and no portfolio-weight optimization.
"""

import argparse
import hashlib
import importlib.util
import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEEP_EXPORT_ROOT = Path(
    r"D:\positionx_data\research\alpha2_incremental_value_v0_1\deep_oversold_export"
)
DEEP_SUMMARY = DEEP_EXPORT_ROOT / "deep_oversold_export_summary_v013.json"
DEEP_CURVE = DEEP_EXPORT_ROOT / "deep_oversold_20bps_curve.csv"
DEEP_TRADES = DEEP_EXPORT_ROOT / "deep_oversold_20bps_trades.csv"

DEEP_RUNNER = PROJECT_ROOT / "scripts" / "run_deep_oversold_csi500_universe.py"
EXPECTED_DEEP_RUNNER_SHA256 = (
    "e18792cb9f3676bc515d937fe555cab7d28e4f2e94f918385bace4f54e51e73c"
)

EARN_ROOT = Path(r"D:\positionx_data\research\earnings_drift_v0_1")
EVENT_RETURNS = EARN_ROOT / "event_returns.csv"
EXPECTED_EVENT_RETURNS_SHA256 = (
    "8d5988d720ae1eafe313ee9512ea9d7c8d8637566afe79ffc8c0634fa97c12fb"
)

EARN_PORTFOLIO_SUMMARY = Path(
    r"D:\positionx_data\research\earnings_improving_h60_portfolio_v0_1\portfolio_summary.json"
)
EXPECTED_EARN_PORTFOLIO_SUMMARY_SHA256 = (
    "ae24ebf363cb74aca40f0fecf00f3f83afc36bc377a60017f7725787d1d208f8"
)

CATALOG = Path(r"D:\positionx_data\catalog\market_data.duckdb")
OUT_ROOT = Path(r"D:\positionx_data\research\earnings_residual_capacity_v0_1")

INITIAL_EQUITY = 1_000_000.0
EARN_TARGET_WEIGHT = 0.01
COMBINED_TARGET_GROSS = 0.30
EARN_MAX_POSITIONS = 30
MAIN_COST = 0.002
EARN_STRESS_COST = 0.005
RANDOM_SEEDS = 100
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


def parse_date_series(s: pd.Series) -> pd.Series:
    raw = s.astype(str).str.replace(".0", "", regex=False)
    eight = raw.str.fullmatch(r"\d{8}")
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    if eight.any():
        out.loc[eight] = pd.to_datetime(
            raw.loc[eight], format="%Y%m%d", errors="coerce"
        )
    if (~eight).any():
        out.loc[~eight] = pd.to_datetime(raw.loc[~eight], errors="coerce")
    return out


def date8(v: Any) -> str:
    x = str(v).replace("-", "").replace(".0", "")
    return x[:8]


def stable_key(seed: int, d: str, symbol: str, ann: str) -> str:
    return hashlib.sha256(
        f"{seed}|{d}|{symbol}|{ann}".encode("utf-8")
    ).hexdigest()


def load_deep_runner():
    if not DEEP_RUNNER.exists():
        raise RuntimeError(f"FAIL_CLOSED missing Deep runner: {DEEP_RUNNER}")
    actual = sha256_file(DEEP_RUNNER)
    if actual != EXPECTED_DEEP_RUNNER_SHA256:
        raise RuntimeError(
            "FAIL_CLOSED Deep runner hash drift "
            f"expected={EXPECTED_DEEP_RUNNER_SHA256} actual={actual}"
        )
    spec = importlib.util.spec_from_file_location(
        "px_residual_deep_runner", str(DEEP_RUNNER)
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot import Deep runner")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def verify_inputs() -> dict:
    for p in (
        DEEP_SUMMARY,
        DEEP_CURVE,
        DEEP_TRADES,
        EVENT_RETURNS,
        EARN_PORTFOLIO_SUMMARY,
        CATALOG,
    ):
        if not p.exists():
            raise RuntimeError(f"FAIL_CLOSED missing input: {p}")

    ds = json.loads(DEEP_SUMMARY.read_text(encoding="utf-8"))
    if ds.get("status") not in {
        "PASS_DEEP_OVERSOLD_EXACT_REPLAY_V0_1_3",
        "PASS_DEEP_OVERSOLD_ECONOMIC_EQUIVALENCE_V0_1_3",
    }:
        raise RuntimeError(
            f"FAIL_CLOSED Deep V013 status={ds.get('status')}"
        )
    if ds.get("sim_output_shape", {}).get("exact_trade_rows_available") is not True:
        raise RuntimeError("FAIL_CLOSED Deep exact trades_df unavailable")
    if int(ds.get("sim_output_shape", {}).get("trades_df_rows") or 0) < 1300:
        raise RuntimeError("FAIL_CLOSED Deep trade rows unexpectedly small")

    if sha256_file(EVENT_RETURNS) != EXPECTED_EVENT_RETURNS_SHA256:
        raise RuntimeError("FAIL_CLOSED event_returns hash drift")
    if sha256_file(EARN_PORTFOLIO_SUMMARY) != EXPECTED_EARN_PORTFOLIO_SUMMARY_SHA256:
        raise RuntimeError("FAIL_CLOSED Earnings portfolio summary hash drift")

    es = json.loads(EARN_PORTFOLIO_SUMMARY.read_text(encoding="utf-8"))
    if es.get("standalone_gate", {}).get("status") != "PASS_STANDALONE_PORTFOLIO_GATE":
        raise RuntimeError("FAIL_CLOSED Earnings standalone gate not PASS")

    return {"deep": ds, "earnings": es}


def load_deep_curve() -> pd.DataFrame:
    x = pd.read_csv(DEEP_CURVE)
    date_col = next(
        (c for c in ("date", "trade_date", "session") if c in x.columns),
        None,
    )
    if date_col is None or "equity" not in x.columns or "gross_weight" not in x.columns:
        raise RuntimeError(
            f"FAIL_CLOSED Deep curve columns={list(x.columns)}"
        )
    out = pd.DataFrame(
        {
            "date": parse_date_series(x[date_col]),
            "equity": pd.to_numeric(x["equity"], errors="coerce"),
            "gross_weight": pd.to_numeric(
                x["gross_weight"], errors="coerce"
            ),
        }
    ).dropna()
    return (
        out.sort_values("date")
        .drop_duplicates("date", keep="last")
        .reset_index(drop=True)
    )


def load_deep_trades() -> pd.DataFrame:
    x = pd.read_csv(DEEP_TRADES)
    req = {
        "symbol",
        "entry_date",
        "exit_date",
        "entry_price",
        "entry_notional",
    }
    missing = sorted(req - set(x.columns))
    if missing:
        raise RuntimeError(
            f"FAIL_CLOSED Deep trades missing columns={missing}; "
            f"available={list(x.columns)}"
        )
    x = x.copy()
    x["symbol"] = x["symbol"].astype(str)
    x["entry_date"] = x["entry_date"].map(date8)
    x["exit_date"] = x["exit_date"].map(date8)
    x["entry_price"] = pd.to_numeric(x["entry_price"], errors="raise")
    x["entry_notional"] = pd.to_numeric(
        x["entry_notional"], errors="raise"
    )
    if "exit_price" in x.columns:
        x["exit_price"] = pd.to_numeric(x["exit_price"], errors="coerce")
    return x


def load_candidates() -> pd.DataFrame:
    df = pd.read_csv(EVENT_RETURNS)
    req = {
        "announcementId",
        "symbol",
        "change_type",
        "horizon",
        "measurement_status",
        "entry_date",
        "exit_date",
        "entry_open",
        "exit_close",
    }
    missing = sorted(req - set(df.columns))
    if missing:
        raise RuntimeError(
            f"FAIL_CLOSED event_returns missing={missing}"
        )
    c = df[
        (df["change_type"] == "IMPROVING")
        & (df["horizon"] == 60)
        & (df["measurement_status"] == "OK")
    ].copy()
    if len(c) != 2731:
        raise RuntimeError(
            f"FAIL_CLOSED expected 2731 candidates got={len(c)}"
        )
    c["announcementId"] = c["announcementId"].astype(str)
    c["symbol"] = c["symbol"].astype(str)
    c["entry_date"] = c["entry_date"].map(date8)
    c["exit_date"] = c["exit_date"].map(date8)
    c["entry_open"] = pd.to_numeric(c["entry_open"], errors="raise")
    c["exit_close"] = pd.to_numeric(c["exit_close"], errors="raise")
    return c.sort_values(
        ["entry_date", "symbol", "announcementId"]
    ).reset_index(drop=True)


def load_earn_bars(candidates: pd.DataFrame) -> tuple[dict, dict]:
    symbols = pd.DataFrame(
        {"symbol": sorted(candidates["symbol"].unique())}
    )
    start = candidates["entry_date"].min()
    end = candidates["exit_date"].max()

    con = duckdb.connect(str(CATALOG), read_only=True)
    con.register("_symbols", symbols)
    try:
        bars = con.execute(
            """
            SELECT b.symbol, b.trade_date, b."open", b."close"
            FROM market.daily_bars_v1 b
            INNER JOIN _symbols s USING(symbol)
            WHERE strftime(CAST(b.trade_date AS DATE), '%Y%m%d')
                  BETWEEN ? AND ?
            ORDER BY b.trade_date, b.symbol
            """,
            [start, end],
        ).df()
    finally:
        try:
            con.unregister("_symbols")
        except Exception:
            pass
        con.close()

    bars["date8"] = (
        pd.to_datetime(bars["trade_date"]).dt.strftime("%Y%m%d")
    )
    bars["open"] = pd.to_numeric(bars["open"], errors="coerce")
    bars["close"] = pd.to_numeric(bars["close"], errors="coerce")

    direct = {}
    by_symbol = {}
    for sym, g in bars.groupby("symbol", sort=False):
        z = g.set_index("date8")[["open", "close"]].copy()
        by_symbol[str(sym)] = z
        for d, r in z.iterrows():
            direct[(str(sym), str(d))] = {
                "open": None if pd.isna(r["open"]) else float(r["open"]),
                "close": None if pd.isna(r["close"]) else float(r["close"]),
            }

    # Exact entry/exit lineage check.
    bad = []
    for r in candidates.itertuples(index=False):
        e = direct.get((r.symbol, r.entry_date))
        x = direct.get((r.symbol, r.exit_date))
        if not e or not x:
            bad.append((r.announcementId, r.symbol, "MISSING_BAR"))
            continue

        def close_enough(a, b):
            return abs(a-b) <= max(abs(a), abs(b), 1.0) * 1e-10

        if (
            e["open"] is None
            or x["close"] is None
            or not close_enough(float(r.entry_open), float(e["open"]))
            or not close_enough(float(r.exit_close), float(x["close"]))
        ):
            bad.append((r.announcementId, r.symbol, "PRICE_MISMATCH"))
    if bad:
        raise RuntimeError(
            f"FAIL_CLOSED Earnings market lineage mismatch count={len(bad)} "
            f"sample={bad[:10]}"
        )

    return direct, by_symbol


def load_deep_histories(
    mod,
    deep_trades: pd.DataFrame,
    calendar8: list[str],
    common_start: str,
    common_end: str,
):
    overlap = deep_trades[
        (deep_trades["entry_date"] <= common_end)
        & (deep_trades["exit_date"] >= common_start)
    ]
    symbols = sorted(overlap["symbol"].unique().tolist())

    adapter = mod.XtDataWatchlistAdapter(
        mod.xtdata,
        mod.AdapterConfig(
            sector="", bars=2000, dividend_type="front"
        ),
    )
    loaded = adapter.history(symbols)
    loaded = {
        s: mod.normalize_history(df)
        for s, df in loaded.items()
        if df is not None and not df.empty
    }
    missing = [s for s in symbols if s not in loaded]
    if missing:
        raise RuntimeError(
            f"BLOCKED_INCOMPLETE_LOCAL_DEEP_HISTORY "
            f"missing={len(missing)} sample={missing[:20]}"
        )

    # Reindex and carry marks through suspensions/missing daily rows.
    marks = {}
    for sym in symbols:
        h = loaded[sym].copy()
        if "open" not in h.columns or "close" not in h.columns:
            raise RuntimeError(
                f"FAIL_CLOSED Deep history lacks OHLC: {sym}"
            )
        z = h[["open", "close"]].copy()
        z.index = z.index.astype(str)
        z = z.reindex(calendar8)
        z["close"] = pd.to_numeric(
            z["close"], errors="coerce"
        ).ffill()
        z["open"] = pd.to_numeric(
            z["open"], errors="coerce"
        )
        z["open"] = z["open"].where(
            z["open"].notna(), z["close"].shift(1)
        )
        z["open"] = z["open"].where(
            z["open"].notna(), z["close"]
        )
        marks[sym] = z

    return marks


def build_earn_mark_tables(
    by_symbol: dict, calendar8: list[str]
) -> dict:
    marks = {}
    for sym, z0 in by_symbol.items():
        z = z0.copy().reindex(calendar8)
        z["close"] = pd.to_numeric(
            z["close"], errors="coerce"
        ).ffill()
        z["open"] = pd.to_numeric(z["open"], errors="coerce")
        z["open"] = z["open"].where(
            z["open"].notna(), z["close"].shift(1)
        )
        z["open"] = z["open"].where(
            z["open"].notna(), z["close"]
        )
        marks[sym] = z
    return marks


def mark_price(marks: dict, sym: str, d: str, field: str) -> float:
    if sym not in marks or d not in marks[sym].index:
        raise RuntimeError(f"FAIL_CLOSED missing mark {sym} {d}")
    v = marks[sym].at[d, field]
    if pd.isna(v) or float(v) <= 0:
        raise RuntimeError(
            f"FAIL_CLOSED invalid mark {sym} {d} {field}={v}"
        )
    return float(v)


def deep_book_tables(
    deep_curve: pd.DataFrame,
    deep_trades: pd.DataFrame,
    deep_marks: dict,
    common_start: str,
    common_end: str,
):
    dc = deep_curve.copy()
    dc["date8"] = dc["date"].dt.strftime("%Y%m%d")
    dates = dc["date8"].tolist()

    if common_start not in dates:
        raise RuntimeError(
            f"FAIL_CLOSED common start not in Deep calendar: {common_start}"
        )
    i0 = dates.index(common_start)
    if i0 == 0:
        raise RuntimeError("FAIL_CLOSED no prior Deep session")
    prior = dates[i0-1]

    prior_eq = float(
        dc.loc[dc["date8"] == prior, "equity"].iloc[0]
    )
    scale = INITIAL_EQUITY / prior_eq

    t = deep_trades.copy()
    t["shares_scaled"] = (
        t["entry_notional"] / t["entry_price"] * scale
    )

    rows = []
    for d in dates[i0:]:
        if d > common_end:
            break
        open_active = t[
            (t["entry_date"] <= d) & (t["exit_date"] >= d)
        ]
        close_active = t[
            (t["entry_date"] <= d) & (t["exit_date"] > d)
        ]

        open_mv = 0.0
        for r in open_active.itertuples(index=False):
            open_mv += r.shares_scaled * mark_price(
                deep_marks, r.symbol, d, "open"
            )

        close_mv = 0.0
        for r in close_active.itertuples(index=False):
            close_mv += r.shares_scaled * mark_price(
                deep_marks, r.symbol, d, "close"
            )

        cr = dc.loc[dc["date8"] == d].iloc[0]
        eq = float(cr["equity"]) * scale
        expected_close_mv = (
            float(cr["gross_weight"]) * float(cr["equity"]) * scale
        )
        rows.append(
            {
                "date8": d,
                "deep_equity": eq,
                "deep_open_mv": open_mv,
                "deep_close_mv": close_mv,
                "deep_expected_close_mv": expected_close_mv,
                "deep_open_symbols": set(
                    open_active["symbol"].astype(str)
                ),
                "deep_active_open": int(len(open_active)),
                "deep_active_close": int(len(close_active)),
            }
        )

    out = pd.DataFrame(rows)
    denom = np.maximum(
        np.abs(out["deep_expected_close_mv"].to_numpy()), 1.0
    )
    rel = np.abs(
        out["deep_close_mv"].to_numpy()
        - out["deep_expected_close_mv"].to_numpy()
    ) / denom
    max_rel = float(np.max(rel))
    p99_rel = float(np.quantile(rel, 0.99))

    # The same current QMT history and exact replay trades should reproduce the
    # Deep gross curve nearly exactly. A 25bp relative MV mismatch is already
    # suspicious and fails closed.
    if max_rel > 0.0025:
        bad_i = int(np.argmax(rel))
        bad = out.iloc[bad_i]
        raise RuntimeError(
            "FAIL_CLOSED Deep reconstructed gross mismatch "
            f"max_rel={max_rel:.6f} date={bad['date8']} "
            f"reconstructed={bad['deep_close_mv']:.2f} "
            f"curve={bad['deep_expected_close_mv']:.2f}"
        )

    return out, {
        "prior_date": prior,
        "common_start": common_start,
        "common_end": common_end,
        "deep_equity_rebase_scale": scale,
        "deep_close_gross_reconstruction_max_rel_error": max_rel,
        "deep_close_gross_reconstruction_p99_rel_error": p99_rel,
    }


def metrics(curve: pd.DataFrame, equity_col: str) -> dict:
    z = curve[["date8", equity_col]].copy()
    z["date"] = pd.to_datetime(z["date8"], format="%Y%m%d")
    eq = pd.to_numeric(z[equity_col], errors="raise")
    ret = eq.pct_change()

    d0, d1 = z["date"].iloc[0], z["date"].iloc[-1]
    years = max((d1-d0).days, 1) / 365.25
    total = float(eq.iloc[-1] / eq.iloc[0] - 1.0)
    cagr = float(
        (eq.iloc[-1] / eq.iloc[0]) ** (1.0 / years) - 1.0
    )
    dd = eq / eq.cummax() - 1.0
    r = ret.dropna()
    ann = len(r) / years
    sd = float(r.std(ddof=1))
    sharpe = float(r.mean() / sd * math.sqrt(ann)) if sd > 0 else None
    return {
        "sessions": int(len(z)),
        "start": str(d0.date()),
        "end": str(d1.date()),
        "total_return": total,
        "cagr": cagr,
        "max_drawdown": float(dd.min()),
        "sharpe": sharpe,
        "annual_sessions_observed": float(ann),
    }


def classify(delta: dict) -> str:
    dc = delta["cagr"]
    ds = delta["sharpe"]
    dd = delta["max_drawdown"]
    if dc <= 0:
        return "REJECT"
    if ds > 0 and dd >= 0:
        return "STRONG_PASS"
    if ds > 0 or dd >= 0:
        return "USEFUL_INCREMENTAL"
    return "CAPITAL_FILLER_ONLY"


def simulate(
    deep_daily: pd.DataFrame,
    candidates: pd.DataFrame,
    earn_marks: dict,
    round_trip_cost: float,
    seed: int | None,
):
    half = round_trip_cost / 2.0
    groups = {
        d: g.to_dict("records")
        for d, g in candidates.groupby("entry_date", sort=True)
    }

    active = {}
    realized_pnl = 0.0
    skipped = []
    trades = []
    rows = []

    prev_combined_equity = INITIAL_EQUITY
    scaled_entry_days = 0
    no_residual_days = 0

    for dr in deep_daily.itertuples(index=False):
        d = dr.date8
        deep_open_symbols = dr.deep_open_symbols

        # Existing Earnings positions marked at today's OPEN.
        earn_open_mv = 0.0
        for sym, pos in active.items():
            px = mark_price(earn_marks, sym, d, "open")
            pos["open_mark"] = px
            earn_open_mv += pos["shares"] * px

        todays = list(groups.get(d, []))
        eligible = []
        for e in todays:
            sym = e["symbol"]
            if sym in active:
                skipped.append(
                    {
                        "announcementId": e["announcementId"],
                        "symbol": sym,
                        "entry_date": d,
                        "reason": "EARNINGS_DUPLICATE_WHILE_HELD",
                    }
                )
            elif sym in deep_open_symbols:
                skipped.append(
                    {
                        "announcementId": e["announcementId"],
                        "symbol": sym,
                        "entry_date": d,
                        "reason": "DEEP_PRIORITY_SAME_SYMBOL",
                    }
                )
            else:
                eligible.append(e)

        if seed is None:
            eligible.sort(
                key=lambda e: (e["symbol"], e["announcementId"])
            )
        else:
            eligible.sort(
                key=lambda e: stable_key(
                    seed, d, e["symbol"], e["announcementId"]
                )
            )

        slots = max(0, EARN_MAX_POSITIONS - len(active))
        selected = eligible[:slots]
        for e in eligible[slots:]:
            skipped.append(
                {
                    "announcementId": e["announcementId"],
                    "symbol": e["symbol"],
                    "entry_date": d,
                    "reason": "EARNINGS_NO_SLOT",
                }
            )

        residual_budget = max(
            0.0,
            COMBINED_TARGET_GROSS * prev_combined_equity
            - float(dr.deep_open_mv)
            - earn_open_mv,
        )
        desired_each = EARN_TARGET_WEIGHT * prev_combined_equity
        desired_total = desired_each * len(selected)

        scale = (
            min(1.0, residual_budget / desired_total)
            if desired_total > 0 else 0.0
        )
        if selected and scale < 1.0 - 1e-12:
            scaled_entry_days += 1
        if selected and scale <= EPS:
            no_residual_days += 1

        if scale <= EPS:
            for e in selected:
                skipped.append(
                    {
                        "announcementId": e["announcementId"],
                        "symbol": e["symbol"],
                        "entry_date": d,
                        "reason": "NO_RESIDUAL_GROSS",
                    }
                )
        else:
            notional = desired_each * scale
            for e in selected:
                px = mark_price(
                    earn_marks, e["symbol"], d, "open"
                )
                # Exact original event-study entry price check.
                if abs(px - float(e["entry_open"])) > max(
                    abs(px), abs(float(e["entry_open"])), 1.0
                ) * 1e-10:
                    raise RuntimeError(
                        f"FAIL_CLOSED Earnings entry drift "
                        f"{e['symbol']} {d} event={e['entry_open']} mark={px}"
                    )
                shares = notional / px
                entry_fee = notional * half
                active[e["symbol"]] = {
                    "announcementId": e["announcementId"],
                    "symbol": e["symbol"],
                    "entry_date": d,
                    "exit_date": e["exit_date"],
                    "entry_price": px,
                    "entry_notional": notional,
                    "entry_fee": entry_fee,
                    "shares": shares,
                }

        # Close exits first; same-day exits were not reusable at OPEN.
        for sym in list(active.keys()):
            pos = active[sym]
            if pos["exit_date"] != d:
                continue
            px = mark_price(earn_marks, sym, d, "close")
            proceeds = pos["shares"] * px
            exit_fee = proceeds * half
            pnl = (
                proceeds
                - pos["entry_notional"]
                - pos["entry_fee"]
                - exit_fee
            )
            realized_pnl += pnl
            trades.append(
                {
                    **pos,
                    "exit_price": px,
                    "exit_fee": exit_fee,
                    "net_pnl": pnl,
                    "net_return_on_trade_capital": (
                        pnl / pos["entry_notional"]
                    ),
                }
            )
            del active[sym]

        # Mark remaining Earnings positions at CLOSE.
        earn_close_mv = 0.0
        active_unreal = 0.0
        for sym, pos in active.items():
            px = mark_price(earn_marks, sym, d, "close")
            mv = pos["shares"] * px
            earn_close_mv += mv
            active_unreal += (
                mv - pos["entry_notional"] - pos["entry_fee"]
            )

        overlay_pnl = realized_pnl + active_unreal
        combined_equity = float(dr.deep_equity) + overlay_pnl
        combined_close_gross = (
            float(dr.deep_close_mv) + earn_close_mv
        ) / combined_equity

        rows.append(
            {
                "date8": d,
                "deep_equity": float(dr.deep_equity),
                "deep_open_gross": (
                    float(dr.deep_open_mv) / prev_combined_equity
                ),
                "deep_close_gross": (
                    float(dr.deep_close_mv) / float(dr.deep_equity)
                ),
                "earn_close_mv": earn_close_mv,
                "earn_active_positions": len(active),
                "overlay_pnl": overlay_pnl,
                "combined_equity": combined_equity,
                "combined_close_gross": combined_close_gross,
                "residual_budget_at_open": residual_budget,
            }
        )
        prev_combined_equity = combined_equity

    curve = pd.DataFrame(rows)
    tdf = pd.DataFrame(trades)
    sdf = pd.DataFrame(skipped)

    deep_m = metrics(curve, "deep_equity")
    combo_m = metrics(curve, "combined_equity")
    delta = {
        "cagr": combo_m["cagr"] - deep_m["cagr"],
        "max_drawdown": (
            combo_m["max_drawdown"] - deep_m["max_drawdown"]
        ),
        "sharpe": combo_m["sharpe"] - deep_m["sharpe"],
    }

    skip_counts = (
        sdf["reason"].value_counts().to_dict()
        if len(sdf) else {}
    )

    return {
        "curve": curve,
        "trades": tdf,
        "skipped": sdf,
        "deep_metrics": deep_m,
        "combined_metrics": combo_m,
        "delta": delta,
        "classification": classify(delta),
        "diagnostics": {
            "earnings_trades_executed": int(len(tdf)),
            "earnings_candidate_events": int(len(candidates)),
            "earnings_event_coverage": (
                float(len(tdf)/len(candidates))
                if len(candidates) else None
            ),
            "scaled_entry_days": int(scaled_entry_days),
            "no_residual_days": int(no_residual_days),
            "skip_counts": skip_counts,
            "avg_combined_gross": float(
                curve["combined_close_gross"].mean()
            ),
            "p95_combined_gross": float(
                curve["combined_close_gross"].quantile(0.95)
            ),
            "max_combined_gross": float(
                curve["combined_close_gross"].max()
            ),
            "avg_earn_active_positions": float(
                curve["earn_active_positions"].mean()
            ),
            "max_earn_active_positions": int(
                curve["earn_active_positions"].max()
            ),
        },
    }


def quantiles(s: pd.Series) -> dict:
    s = pd.to_numeric(s, errors="coerce").dropna()
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


def prepare():
    lineage = verify_inputs()
    mod = load_deep_runner()
    deep_curve = load_deep_curve()
    deep_trades = load_deep_trades()
    candidates = load_candidates()

    common_start = candidates["entry_date"].min()
    common_end = candidates["exit_date"].max()

    dc = deep_curve.copy()
    dc["date8"] = dc["date"].dt.strftime("%Y%m%d")
    calendar8 = [
        d for d in dc["date8"].tolist()
        if common_start <= d <= common_end
    ]
    if len(calendar8) < 600:
        raise RuntimeError(
            f"FAIL_CLOSED common calendar too short={len(calendar8)}"
        )

    earn_direct, earn_by_symbol = load_earn_bars(candidates)
    earn_marks = build_earn_mark_tables(
        earn_by_symbol, calendar8
    )

    deep_marks = load_deep_histories(
        mod, deep_trades, calendar8, common_start, common_end
    )
    deep_daily, deep_validation = deep_book_tables(
        deep_curve,
        deep_trades,
        deep_marks,
        common_start,
        common_end,
    )

    if len(deep_daily) != len(calendar8):
        raise RuntimeError("FAIL_CLOSED Deep daily/common calendar mismatch")

    return {
        "lineage": lineage,
        "deep_daily": deep_daily,
        "candidates": candidates,
        "earn_marks": earn_marks,
        "deep_validation": deep_validation,
    }


def check() -> int:
    x = prepare()
    result = {
        "status": "PASS_EARNINGS_RESIDUAL_CAPACITY_V0_1_CHECK",
        "deep_replay_status": x["lineage"]["deep"].get("status"),
        "candidates": int(len(x["candidates"])),
        "common_sessions": int(len(x["deep_daily"])),
        "common_start": x["deep_validation"]["common_start"],
        "common_end": x["deep_validation"]["common_end"],
        "deep_gross_reconstruction": x["deep_validation"],
        "policy": {
            "deep_priority": True,
            "earn_target_weight": EARN_TARGET_WEIGHT,
            "combined_target_gross": COMBINED_TARGET_GROSS,
            "earn_max_positions": EARN_MAX_POSITIONS,
            "main_round_trip_cost": MAIN_COST,
            "earn_stress_round_trip_cost": EARN_STRESS_COST,
            "random_tie_break_seeds": RANDOM_SEEDS,
        },
        "no_download": True,
        "no_database_mutation": True,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def run() -> int:
    x = prepare()
    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    main = simulate(
        x["deep_daily"],
        x["candidates"],
        x["earn_marks"],
        MAIN_COST,
        seed=None,
    )
    stress = simulate(
        x["deep_daily"],
        x["candidates"],
        x["earn_marks"],
        EARN_STRESS_COST,
        seed=None,
    )

    main["curve"].to_csv(
        OUT_ROOT / "combined_curve_20bps.csv",
        index=False,
        encoding="utf-8-sig",
    )
    main["trades"].to_csv(
        OUT_ROOT / "earnings_filler_trades_20bps.csv",
        index=False,
        encoding="utf-8-sig",
    )
    main["skipped"].to_csv(
        OUT_ROOT / "earnings_filler_skipped_20bps.csv",
        index=False,
        encoding="utf-8-sig",
    )
    stress["curve"].to_csv(
        OUT_ROOT / "combined_curve_earnings50bps_stress.csv",
        index=False,
        encoding="utf-8-sig",
    )

    random_rows = []
    for seed in range(RANDOM_SEEDS):
        sim = simulate(
            x["deep_daily"],
            x["candidates"],
            x["earn_marks"],
            MAIN_COST,
            seed=seed,
        )
        random_rows.append(
            {
                "seed": seed,
                "classification": sim["classification"],
                "delta_cagr": sim["delta"]["cagr"],
                "delta_mdd": sim["delta"]["max_drawdown"],
                "delta_sharpe": sim["delta"]["sharpe"],
                "combined_cagr": sim["combined_metrics"]["cagr"],
                "combined_mdd": sim["combined_metrics"]["max_drawdown"],
                "combined_sharpe": sim["combined_metrics"]["sharpe"],
                "earnings_trades": sim["diagnostics"][
                    "earnings_trades_executed"
                ],
                "avg_combined_gross": sim["diagnostics"][
                    "avg_combined_gross"
                ],
                "max_combined_gross": sim["diagnostics"][
                    "max_combined_gross"
                ],
            }
        )

    rdf = pd.DataFrame(random_rows)
    rdf.to_csv(
        OUT_ROOT / "random_tie_break_100.csv",
        index=False,
        encoding="utf-8-sig",
    )

    classification_counts = (
        rdf["classification"].value_counts().to_dict()
    )
    robustness = {
        "seeds": RANDOM_SEEDS,
        "classification_counts": classification_counts,
        "delta_cagr": quantiles(rdf["delta_cagr"]),
        "delta_mdd": quantiles(rdf["delta_mdd"]),
        "delta_sharpe": quantiles(rdf["delta_sharpe"]),
        "earnings_trades": quantiles(rdf["earnings_trades"]),
        "avg_combined_gross": quantiles(
            rdf["avg_combined_gross"]
        ),
        "max_combined_gross": quantiles(
            rdf["max_combined_gross"]
        ),
    }

    result = {
        "status": "PASS_EARNINGS_RESIDUAL_CAPACITY_AUDIT_V0_1",
        "generated_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "strategy_role_test": "EARNINGS_IMPROVING_H60_AS_RESIDUAL_CAPACITY",
        "deep_lineage_status": x["lineage"]["deep"].get("status"),
        "methodology": {
            "deep_priority": True,
            "deep_resized_by_earnings": False,
            "earnings_candidate_source": (
                "IMPROVING + H60 + measurement_status=OK"
            ),
            "earnings_target_weight": EARN_TARGET_WEIGHT,
            "earnings_max_positions": EARN_MAX_POSITIONS,
            "combined_target_gross_at_entry": COMBINED_TARGET_GROSS,
            "same_day_deep_entry_priority": True,
            "same_day_deep_exit_reusable_at_open": False,
            "existing_positions_rebalanced": False,
            "target_gross_note": (
                "30% is an entry-time target gross budget. Natural mark-to-market "
                "and later Deep priority entries can make observed close gross exceed "
                "30%; no forced daily deleveraging is introduced."
            ),
            "new_earnings_entry_blocked_if_deep_holds_same_symbol": True,
            "deep_later_entry_does_not_force_earnings_exit": True,
            "main_cost": "20bp round-trip Earnings overlay; Deep fixed at its 20bp replay",
            "stress_cost": "50bp round-trip Earnings overlay; Deep fixed at its 20bp replay",
            "same_day_tie_break_baseline": "SYMBOL_ASC",
            "random_same_day_tie_break_seeds": RANDOM_SEEDS,
        },
        "common_period": {
            "start": x["deep_validation"]["common_start"],
            "end": x["deep_validation"]["common_end"],
            "sessions": int(len(x["deep_daily"])),
        },
        "deep_reconstruction_validation": x["deep_validation"],
        "main_20bps": {
            "deep": main["deep_metrics"],
            "combined": main["combined_metrics"],
            "delta_combined_minus_deep": main["delta"],
            "classification": main["classification"],
            "diagnostics": main["diagnostics"],
        },
        "earnings_50bps_stress": {
            "deep": stress["deep_metrics"],
            "combined": stress["combined_metrics"],
            "delta_combined_minus_deep": stress["delta"],
            "classification": stress["classification"],
            "diagnostics": stress["diagnostics"],
        },
        "random_tie_break_20bps": robustness,
        "predeclared_verdict_rule": {
            "STRONG_PASS": "dCAGR>0 and dSharpe>0 and dMDD>=0",
            "USEFUL_INCREMENTAL": (
                "dCAGR>0 and (dSharpe>0 or dMDD>=0)"
            ),
            "CAPITAL_FILLER_ONLY": (
                "dCAGR>0 and dSharpe<=0 and dMDD<0"
            ),
            "REJECT": "dCAGR<=0",
        },
        "role_interpretation": {
            "if_STRONG_PASS": (
                "Promote toward Strategy #2 / Strategy Portfolio integration."
            ),
            "if_USEFUL_INCREMENTAL": (
                "Retain as incremental portfolio component; do not call it "
                "fully independent until further portfolio validation."
            ),
            "if_CAPITAL_FILLER_ONLY": (
                "Freeze as idle-capital filler, not Strategy #2; move on to "
                "search for a different economic source."
            ),
            "if_REJECT": (
                "Stop Earnings line for portfolio use and move on."
            ),
        },
        "outputs": [
            "residual_capacity_summary.json",
            "combined_curve_20bps.csv",
            "earnings_filler_trades_20bps.csv",
            "earnings_filler_skipped_20bps.csv",
            "combined_curve_earnings50bps_stress.csv",
            "random_tie_break_100.csv",
        ],
        "safety": {
            "network_download_called": False,
            "qmt_refresh_called": False,
            "broker_used": False,
            "production_db_opened": False,
            "database_mutation": False,
            "strategy_parameters_modified": False,
        },
    }

    write_json_atomic(
        OUT_ROOT / "residual_capacity_summary.json", result
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["check", "run"])
    args = p.parse_args()
    return check() if args.command == "check" else run()


if __name__ == "__main__":
    raise SystemExit(main())
