from __future__ import annotations

r"""
PositionX — Earnings Drift Strategy Audit V0.1

Fast event-study / fast-reject audit for the second independent alpha candidate.

Frozen semantic input
---------------------
D:\positionx_data\events\forecast_semantic_corpus_v0_2_3\semantic_events_all.parquet

Market input
------------
D:\positionx_data\catalog\market_data.duckdb
  market.daily_bars_v1
  market.security_master_v1

Signal definition
-----------------
Use only:
- event_kind = ORIGINAL_FORECAST
- parser_confidence = HIGH
- strategy_eligible_semantics = TRUE
- A-share stock master: SSE_MAIN / SZSE_MAIN / CHINEXT / STAR
- one earliest ORIGINAL event per symbol + report_period

Positive:
  TURNAROUND / IMPROVING / LOSS_NARROWING

Negative control / risk group:
  TURN_TO_LOSS / DETERIORATING / LOSS_WIDENING

Timing
------
- Signal date = announcement event_date.
- Entry = exact OPEN of the first market session strictly after event_date.
- No "wait until tradable": if the stock has no valid OPEN on that exact T+1
  market session, the event is not measurable/tradable for this audit.
- H5/H20/H60 = close of the 5th/20th/60th market session including the entry
  session as session #1.
- If the exact horizon close is unavailable, that event/horizon is excluded.
- No costs/slippage in V0.1. This is an event-study, not the portfolio backtest.

Market adjustment
-----------------
For every unique (entry_date, exit_date) pair, compute the cross-sectional MEDIAN
A-share OPEN->CLOSE return from market.daily_bars_v1. Event adjusted return is:

  stock_return - same-window market_median_return

This is an internal broad-market proxy, NOT an official index benchmark.

Safety
------
Read-only market/catalog/corpus. No network, broker, order, production SQLite,
research DB or catalog mutation.
"""

import argparse
import bisect
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

CORPUS_ROOT = Path(r"D:\positionx_data\events\forecast_semantic_corpus_v0_2_3")
CORPUS = CORPUS_ROOT / "semantic_events_all.parquet"
CORPUS_SUMMARY = CORPUS_ROOT / "corpus_summary.json"
CATALOG = Path(r"D:\positionx_data\catalog\market_data.duckdb")
OUT_ROOT = Path(r"D:\positionx_data\research\earnings_drift_v0_1")

EXPECTED_CORPUS_SHA256 = "488c29b12ca9975c36005e9afcc2afb82516aae5bf0305a337c69a81060db0d4"
EXPECTED_PARSER_SHA256 = "7cf5dcd39d584a13a1095b482beb8b2298b26b381f900f13f18c0f8cd28e7ed4"

POSITIVE = {"TURNAROUND", "IMPROVING", "LOSS_NARROWING"}
NEGATIVE = {"TURN_TO_LOSS", "DETERIORATING", "LOSS_WIDENING"}
ALLOWED_BOARDS = {"SSE_MAIN", "SZSE_MAIN", "CHINEXT", "STAR"}
HORIZONS = (5, 20, 60)


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


def qpath(p: Path) -> str:
    return str(p).replace("\\", "/").replace("'", "''")


def write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, default=safe),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def verify_inputs() -> dict:
    for p in (CORPUS, CORPUS_SUMMARY, CATALOG):
        if not p.exists():
            raise RuntimeError(f"FAIL_CLOSED missing input: {p}")

    summary = json.loads(CORPUS_SUMMARY.read_text(encoding="utf-8"))
    if summary.get("status") != "PASS_FORECAST_SEMANTIC_CORPUS_LOCAL_REPARSE_V0_2_3":
        raise RuntimeError(
            f"FAIL_CLOSED corpus status={summary.get('status')}"
        )
    if summary.get("parser_sha256") != EXPECTED_PARSER_SHA256:
        raise RuntimeError("FAIL_CLOSED parser hash in corpus summary mismatch")
    actual = sha256_file(CORPUS)
    if actual != EXPECTED_CORPUS_SHA256:
        raise RuntimeError(
            "FAIL_CLOSED corpus parquet hash mismatch "
            f"expected={EXPECTED_CORPUS_SHA256} actual={actual}"
        )
    if int((summary.get("aggregate") or {}).get("rows") or 0) != 12090:
        raise RuntimeError("FAIL_CLOSED corpus row count in summary is not 12090")
    return summary


def connect_catalog() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(CATALOG), read_only=True)


def load_candidate_events(con: duckdb.DuckDBPyConnection) -> tuple[pd.DataFrame, dict]:
    c = qpath(CORPUS)

    # The stock-master join is the mandatory Strategy input filter that blocks
    # bond / non-stock documents even if their six-digit code was normalized.
    df = con.execute(f"""
        WITH e AS (
          SELECT *
          FROM read_parquet('{c}')
          WHERE strategy_eligible_semantics
            AND parser_confidence = 'HIGH'
            AND event_kind = 'ORIGINAL_FORECAST'
            AND change_type IN (
              'TURNAROUND','IMPROVING','LOSS_NARROWING',
              'TURN_TO_LOSS','DETERIORATING','LOSS_WIDENING'
            )
        ),
        s AS (
          SELECT symbol, board, instrument_type_stock
          FROM market.security_master_v1
          WHERE instrument_type_stock
            AND board IN ('SSE_MAIN','SZSE_MAIN','CHINEXT','STAR')
        ),
        stock_filtered AS (
          SELECT e.*, s.board
          FROM e
          INNER JOIN s USING(symbol)
          WHERE EXISTS (
            SELECT 1
            FROM market.daily_bars_v1 b
            WHERE b.symbol=e.symbol
            LIMIT 1
          )
        ),
        dedup AS (
          SELECT *,
            row_number() OVER (
              PARTITION BY symbol, report_period
              ORDER BY try_cast(event_time AS TIMESTAMP), announcementId
            ) AS rn
          FROM stock_filtered
        )
        SELECT * EXCLUDE(rn)
        FROM dedup
        WHERE rn=1
        ORDER BY event_time, announcementId
    """).df()

    raw_counts = con.execute(f"""
        SELECT
          count(*) AS corpus_rows,
          sum(CASE WHEN strategy_eligible_semantics THEN 1 ELSE 0 END) AS eligible,
          sum(CASE WHEN strategy_eligible_semantics
                    AND parser_confidence='HIGH'
                   THEN 1 ELSE 0 END) AS eligible_high,
          sum(CASE WHEN strategy_eligible_semantics
                    AND parser_confidence='HIGH'
                    AND event_kind='ORIGINAL_FORECAST'
                    AND change_type IN (
                      'TURNAROUND','IMPROVING','LOSS_NARROWING',
                      'TURN_TO_LOSS','DETERIORATING','LOSS_WIDENING'
                    )
                   THEN 1 ELSE 0 END) AS semantic_signal_rows
        FROM read_parquet('{c}')
    """).fetchone()

    if len(df):
        df["signal_group"] = np.where(
            df["change_type"].isin(POSITIVE), "POSITIVE", "NEGATIVE"
        )
        df["event_date_dt"] = pd.to_datetime(
            df["event_date"], errors="coerce"
        ).dt.date
        df["event_year"] = pd.to_datetime(
            df["event_date"], errors="coerce"
        ).dt.year.astype("Int64")

    info = {
        "corpus_rows": int(raw_counts[0]),
        "strategy_eligible_rows": int(raw_counts[1]),
        "eligible_high_rows": int(raw_counts[2]),
        "semantic_signal_rows_before_stock_filter_and_dedup": int(raw_counts[3]),
        "candidate_rows_after_stock_filter_and_dedup": int(len(df)),
        "positive_rows": int((df["signal_group"] == "POSITIVE").sum()) if len(df) else 0,
        "negative_rows": int((df["signal_group"] == "NEGATIVE").sum()) if len(df) else 0,
    }
    return df, info


def market_calendar(con: duckdb.DuckDBPyConnection) -> list:
    vals = con.execute("""
        SELECT DISTINCT trade_date
        FROM market.daily_bars_v1
        ORDER BY trade_date
    """).fetchall()
    return [x[0] for x in vals]


def map_targets(events: pd.DataFrame, cal: list) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows = []
    excluded = []

    for _, r in events.iterrows():
        d = r["event_date_dt"]
        if pd.isna(d) or d is None:
            excluded.append({
                "announcementId": r["announcementId"],
                "symbol": r["symbol"],
                "reason": "INVALID_EVENT_DATE",
            })
            continue

        # Strictly next market session after event date.
        entry_idx = bisect.bisect_right(cal, d)
        if entry_idx >= len(cal):
            excluded.append({
                "announcementId": r["announcementId"],
                "symbol": r["symbol"],
                "reason": "NO_NEXT_MARKET_SESSION",
            })
            continue

        entry_date = cal[entry_idx]
        for h in HORIZONS:
            exit_idx = entry_idx + h - 1
            exit_date = cal[exit_idx] if exit_idx < len(cal) else None
            rows.append({
                "announcementId": str(r["announcementId"]),
                "symbol": r["symbol"],
                "secName": r["secName"],
                "board": r["board"],
                "event_time": r["event_time"],
                "event_date": str(r["event_date"]),
                "event_year": int(r["event_year"]),
                "report_period": r["report_period"],
                "change_type": r["change_type"],
                "signal_group": r["signal_group"],
                "entry_date": entry_date,
                "horizon": h,
                "exit_date": exit_date,
            })

    return pd.DataFrame(rows), pd.DataFrame(excluded)


def attach_stock_returns(
    con: duckdb.DuckDBPyConnection,
    targets: pd.DataFrame,
) -> pd.DataFrame:
    con.register("_event_targets", targets)
    try:
        out = con.execute("""
            SELECT
              t.*,
              b0."open" AS entry_open,
              b0.suspend_flag AS entry_suspend_flag,
              bx."close" AS exit_close,
              bx.suspend_flag AS exit_suspend_flag,
              CASE
                WHEN t.exit_date IS NULL THEN 'NO_HORIZON_SESSION'
                WHEN b0.symbol IS NULL THEN 'NO_T1_BAR'
                WHEN b0."open" IS NULL OR b0."open" <= 0 THEN 'INVALID_T1_OPEN'
                WHEN coalesce(b0.suspend_flag,0) <> 0 THEN 'T1_SUSPENDED'
                WHEN bx.symbol IS NULL THEN 'NO_EXACT_EXIT_BAR'
                WHEN bx."close" IS NULL OR bx."close" <= 0 THEN 'INVALID_EXIT_CLOSE'
                ELSE 'OK'
              END AS measurement_status,
              CASE
                WHEN t.exit_date IS NOT NULL
                 AND b0."open" > 0
                 AND coalesce(b0.suspend_flag,0)=0
                 AND bx."close" > 0
                THEN bx."close" / b0."open" - 1.0
                ELSE NULL
              END AS stock_return
            FROM _event_targets t
            LEFT JOIN market.daily_bars_v1 b0
              ON b0.symbol=t.symbol
             AND b0.trade_date=t.entry_date
            LEFT JOIN market.daily_bars_v1 bx
              ON bx.symbol=t.symbol
             AND bx.trade_date=t.exit_date
            ORDER BY t.event_time, t.announcementId, t.horizon
        """).df()
    finally:
        con.unregister("_event_targets")
    return out


def market_proxy(
    con: duckdb.DuckDBPyConnection,
    measured: pd.DataFrame,
) -> pd.DataFrame:
    pairs = (
        measured.loc[
            measured["exit_date"].notna(),
            ["entry_date", "exit_date", "horizon"],
        ]
        .drop_duplicates()
        .reset_index(drop=True)
    )
    con.register("_pairs", pairs)
    try:
        px = con.execute("""
            SELECT
              p.entry_date,
              p.exit_date,
              p.horizon,
              count(*) AS market_n,
              median(bx."close" / b0."open" - 1.0) AS market_median_return,
              avg(bx."close" / b0."open" - 1.0) AS market_mean_return
            FROM _pairs p
            INNER JOIN market.daily_bars_v1 b0
              ON b0.trade_date=p.entry_date
             AND b0."open" > 0
             AND coalesce(b0.suspend_flag,0)=0
            INNER JOIN market.daily_bars_v1 bx
              ON bx.symbol=b0.symbol
             AND bx.trade_date=p.exit_date
             AND bx."close" > 0
            GROUP BY p.entry_date, p.exit_date, p.horizon
            ORDER BY p.entry_date, p.horizon
        """).df()
    finally:
        con.unregister("_pairs")
    return px


def describe_group(g: pd.DataFrame, ret_col: str) -> dict:
    x = pd.to_numeric(g[ret_col], errors="coerce").dropna()
    if len(x) == 0:
        return {
            "n": 0, "mean": None, "median": None, "win_rate": None,
            "p10": None, "p90": None, "se": None, "t_stat": None,
        }
    mean = float(x.mean())
    sd = float(x.std(ddof=1)) if len(x) > 1 else None
    se = (sd / math.sqrt(len(x))) if sd is not None else None
    t = (mean / se) if se not in (None, 0) else None
    return {
        "n": int(len(x)),
        "mean": mean,
        "median": float(x.median()),
        "win_rate": float((x > 0).mean()),
        "p10": float(x.quantile(0.10)),
        "p90": float(x.quantile(0.90)),
        "se": se,
        "t_stat": t,
    }


def make_summaries(df: pd.DataFrame, out_root: Path) -> dict:
    ok = df[df["measurement_status"] == "OK"].copy()

    group_rows = []
    for (h, grp), g in ok.groupby(["horizon", "signal_group"]):
        for col in ("stock_return", "adjusted_return"):
            d = describe_group(g, col)
            group_rows.append({
                "horizon": int(h),
                "signal_group": grp,
                "return_type": col,
                **d,
            })
    group_df = pd.DataFrame(group_rows)
    group_df.to_csv(
        out_root / "group_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )

    change_rows = []
    for (h, ct), g in ok.groupby(["horizon", "change_type"]):
        for col in ("stock_return", "adjusted_return"):
            change_rows.append({
                "horizon": int(h),
                "change_type": ct,
                "return_type": col,
                **describe_group(g, col),
            })
    pd.DataFrame(change_rows).to_csv(
        out_root / "change_type_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )

    yearly_rows = []
    for (yr, h, grp), g in ok.groupby(
        ["event_year", "horizon", "signal_group"]
    ):
        for col in ("stock_return", "adjusted_return"):
            yearly_rows.append({
                "event_year": int(yr),
                "horizon": int(h),
                "signal_group": grp,
                "return_type": col,
                **describe_group(g, col),
            })
    yearly_df = pd.DataFrame(yearly_rows)
    yearly_df.to_csv(
        out_root / "yearly_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )

    spread_rows = []
    for h in HORIZONS:
        hh = ok[ok["horizon"] == h]
        for col in ("stock_return", "adjusted_return"):
            pos = pd.to_numeric(
                hh.loc[hh["signal_group"] == "POSITIVE", col],
                errors="coerce",
            ).dropna()
            neg = pd.to_numeric(
                hh.loc[hh["signal_group"] == "NEGATIVE", col],
                errors="coerce",
            ).dropna()
            spread_rows.append({
                "horizon": h,
                "return_type": col,
                "positive_n": int(len(pos)),
                "negative_n": int(len(neg)),
                "positive_mean": float(pos.mean()) if len(pos) else None,
                "negative_mean": float(neg.mean()) if len(neg) else None,
                "positive_minus_negative_mean": (
                    float(pos.mean() - neg.mean())
                    if len(pos) and len(neg) else None
                ),
                "positive_median": float(pos.median()) if len(pos) else None,
                "negative_median": float(neg.median()) if len(neg) else None,
            })
    spread_df = pd.DataFrame(spread_rows)
    spread_df.to_csv(
        out_root / "spread_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )

    ys_rows = []
    for (yr, h), hh in ok.groupby(["event_year", "horizon"]):
        for col in ("stock_return", "adjusted_return"):
            pos = pd.to_numeric(
                hh.loc[hh["signal_group"] == "POSITIVE", col],
                errors="coerce",
            ).dropna()
            neg = pd.to_numeric(
                hh.loc[hh["signal_group"] == "NEGATIVE", col],
                errors="coerce",
            ).dropna()
            ys_rows.append({
                "event_year": int(yr),
                "horizon": int(h),
                "return_type": col,
                "positive_n": int(len(pos)),
                "negative_n": int(len(neg)),
                "positive_mean": float(pos.mean()) if len(pos) else None,
                "negative_mean": float(neg.mean()) if len(neg) else None,
                "positive_minus_negative_mean": (
                    float(pos.mean() - neg.mean())
                    if len(pos) and len(neg) else None
                ),
            })
    yearly_spread_df = pd.DataFrame(ys_rows)
    yearly_spread_df.to_csv(
        out_root / "yearly_spread_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )

    return {
        "group_summary": group_df,
        "spread_summary": spread_df,
        "yearly_summary": yearly_df,
        "yearly_spread_summary": yearly_spread_df,
    }


def decision(summary_tables: dict) -> dict:
    spread = summary_tables["spread_summary"]
    yearly = summary_tables["yearly_summary"]
    yspread = summary_tables["yearly_spread_summary"]

    def srow(h):
        x = spread[
            (spread["horizon"] == h)
            & (spread["return_type"] == "adjusted_return")
        ]
        return x.iloc[0] if len(x) else None

    def positive_years(h):
        x = yearly[
            (yearly["horizon"] == h)
            & (yearly["signal_group"] == "POSITIVE")
            & (yearly["return_type"] == "adjusted_return")
            & (yearly["n"] >= 30)
        ]
        return int((x["mean"] > 0).sum()), int(len(x))

    def spread_years(h):
        x = yspread[
            (yspread["horizon"] == h)
            & (yspread["return_type"] == "adjusted_return")
            & (yspread["positive_n"] >= 30)
            & (yspread["negative_n"] >= 30)
        ]
        return (
            int((x["positive_minus_negative_mean"] > 0).sum()),
            int(len(x)),
        )

    h20, h60 = srow(20), srow(60)
    p20, py20 = positive_years(20)
    p60, py60 = positive_years(60)
    s20, sy20 = spread_years(20)
    s60, sy60 = spread_years(60)

    def val(row, field):
        if row is None:
            return None
        v = row[field]
        return None if pd.isna(v) else float(v)

    spread20 = val(h20, "positive_minus_negative_mean")
    spread60 = val(h60, "positive_minus_negative_mean")
    pos20 = val(h20, "positive_mean")
    pos60 = val(h60, "positive_mean")
    n20 = min(
        int(h20["positive_n"]), int(h20["negative_n"])
    ) if h20 is not None else 0
    n60 = min(
        int(h60["positive_n"]), int(h60["negative_n"])
    ) if h60 is not None else 0

    if (
        spread20 is not None and spread60 is not None
        and spread20 <= 0 and spread60 <= 0
    ):
        verdict = "FAST_REJECT"
    elif (
        None not in (spread20, spread60, pos20, pos60)
        and spread20 > 0 and spread60 > 0
        and pos20 > 0 and pos60 > 0
        and n20 >= 200 and n60 >= 200
        and p20 >= 2 and p60 >= 2
        and s20 >= 2 and s60 >= 2
    ):
        verdict = "PROMISING_LONG_ALPHA_CANDIDATE"
    elif (
        None not in (spread20, spread60)
        and spread20 > 0 and spread60 > 0
        and (pos20 is None or pos60 is None or pos20 <= 0 or pos60 <= 0)
    ):
        verdict = "POSSIBLE_RISK_FILTER_NOT_LONG_ALPHA"
    else:
        verdict = "INCONCLUSIVE"

    return {
        "verdict": verdict,
        "focus": "H20/H60 market-adjusted drift; H5 is reaction diagnostic",
        "h20": {
            "positive_adjusted_mean": pos20,
            "positive_minus_negative_adjusted_spread": spread20,
            "min_group_n": n20,
            "positive_years": p20,
            "valid_positive_years": py20,
            "spread_positive_years": s20,
            "valid_spread_years": sy20,
        },
        "h60": {
            "positive_adjusted_mean": pos60,
            "positive_minus_negative_adjusted_spread": spread60,
            "min_group_n": n60,
            "positive_years": p60,
            "valid_positive_years": py60,
            "spread_positive_years": s60,
            "valid_spread_years": sy60,
        },
        "rule": {
            "FAST_REJECT": "H20 and H60 adjusted POS-NEG spreads are both <= 0.",
            "PROMISING_LONG_ALPHA_CANDIDATE": (
                "H20/H60 adjusted POS means >0 and POS-NEG spreads >0; "
                ">=200 observations in each group; POS mean and spread are "
                "positive in >=2 valid calendar years at both horizons."
            ),
            "POSSIBLE_RISK_FILTER_NOT_LONG_ALPHA": (
                "H20/H60 adjusted spreads >0 but positive group does not itself "
                "show positive adjusted drift at both horizons."
            ),
            "INCONCLUSIVE": "Everything else; inspect outputs before adding complexity.",
        },
    }


def check() -> int:
    corpus_summary = verify_inputs()
    con = connect_catalog()
    try:
        market = con.execute("""
            SELECT
              count(*) AS rows,
              count(DISTINCT symbol) AS symbols,
              min(trade_date) AS min_date,
              max(trade_date) AS max_date
            FROM market.daily_bars_v1
        """).fetchone()
        stock_master = con.execute("""
            SELECT count(*)
            FROM market.security_master_v1
            WHERE instrument_type_stock
              AND board IN ('SSE_MAIN','SZSE_MAIN','CHINEXT','STAR')
        """).fetchone()[0]
        events, info = load_candidate_events(con)
    finally:
        con.close()

    result = {
        "status": "PASS_EARNINGS_DRIFT_V0_1_CHECK",
        "corpus_status": corpus_summary.get("status"),
        "corpus_sha256": EXPECTED_CORPUS_SHA256,
        "market": {
            "rows": int(market[0]),
            "symbols": int(market[1]),
            "min_date": safe(market[2]),
            "max_date": safe(market[3]),
            "a_share_stock_master_rows": int(stock_master),
        },
        "candidate_filter": info,
        "timing": "T+1 exact OPEN; H5/H20/H60 exact market-session CLOSE",
        "network": False,
        "database_mutation": False,
        "historical_strict_non_st_pit_applied": False,
        "st_note": (
            "Corpus explicit_st_at_event exclusion is used, but full historical "
            "ST PIT is not available. This caveat must remain attached to V0.1."
        ),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def run() -> int:
    corpus_summary = verify_inputs()
    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    con = connect_catalog()
    try:
        events, filter_info = load_candidate_events(con)
        if events.empty:
            raise RuntimeError("FAIL_CLOSED no candidate events after filters")

        events.to_csv(
            OUT_ROOT / "candidate_events.csv",
            index=False,
            encoding="utf-8-sig",
        )

        cal = market_calendar(con)
        targets, pre_excluded = map_targets(events, cal)
        measured = attach_stock_returns(con, targets)
        proxy = market_proxy(con, measured)

        measured = measured.merge(
            proxy,
            on=["entry_date", "exit_date", "horizon"],
            how="left",
        )
        measured["adjusted_return"] = (
            measured["stock_return"] - measured["market_median_return"]
        )

        measured.to_csv(
            OUT_ROOT / "event_returns.csv",
            index=False,
            encoding="utf-8-sig",
        )

        if len(pre_excluded):
            pre_excluded.to_csv(
                OUT_ROOT / "pre_measurement_exclusions.csv",
                index=False,
                encoding="utf-8-sig",
            )

        status_counts = {
            str(k): int(v)
            for k, v in measured["measurement_status"].value_counts().to_dict().items()
        }

        tables = make_summaries(measured, OUT_ROOT)
        gate = decision(tables)

        market_stats = con.execute("""
            SELECT
              count(*) AS rows,
              count(DISTINCT symbol) AS symbols,
              min(trade_date) AS min_date,
              max(trade_date) AS max_date
            FROM market.daily_bars_v1
        """).fetchone()

    finally:
        con.close()

    # Additional concise audit tables for event composition.
    events.groupby(
        ["signal_group", "change_type"]
    ).size().reset_index(name="events").to_csv(
        OUT_ROOT / "signal_composition.csv",
        index=False,
        encoding="utf-8-sig",
    )

    measured.groupby(
        ["horizon", "measurement_status"]
    ).size().reset_index(name="rows").to_csv(
        OUT_ROOT / "measurement_status.csv",
        index=False,
        encoding="utf-8-sig",
    )

    result = {
        "status": "PASS_EARNINGS_DRIFT_STRATEGY_AUDIT_V0_1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "semantic_source": {
            "corpus": str(CORPUS),
            "corpus_sha256": EXPECTED_CORPUS_SHA256,
            "parser_sha256": EXPECTED_PARSER_SHA256,
            "corpus_status": corpus_summary.get("status"),
        },
        "market_source": {
            "catalog": str(CATALOG),
            "view": "market.daily_bars_v1",
            "security_master": "market.security_master_v1",
            "rows": int(market_stats[0]),
            "symbols": int(market_stats[1]),
            "min_date": safe(market_stats[2]),
            "max_date": safe(market_stats[3]),
        },
        "signal_definition": {
            "event_kind": "ORIGINAL_FORECAST",
            "parser_confidence": "HIGH",
            "strategy_eligible_semantics": True,
            "positive": sorted(POSITIVE),
            "negative_control": sorted(NEGATIVE),
            "dedup": "earliest event per symbol + report_period",
            "stock_filter": (
                "instrument_type_stock=true AND board in "
                "SSE_MAIN/SZSE_MAIN/CHINEXT/STAR AND daily_bars exists"
            ),
        },
        "candidate_filter": filter_info,
        "timing": {
            "entry": "first market session strictly after event_date, exact OPEN",
            "h5": "5th market session including entry as #1, exact CLOSE",
            "h20": "20th market session including entry as #1, exact CLOSE",
            "h60": "60th market session including entry as #1, exact CLOSE",
            "no_shift_for_suspension": True,
        },
        "market_adjustment": (
            "same-window cross-sectional median A-share OPEN->CLOSE return"
        ),
        "measurement_status_counts": status_counts,
        "decision": gate,
        "caveats": {
            "event_study_not_portfolio_backtest": True,
            "transaction_costs_included": False,
            "short_negative_group_assumed": False,
            "negative_group_role": "control / discrimination / risk-filter evidence",
            "historical_strict_non_st_pit_applied": False,
            "historical_st_note": (
                "Explicit ST-at-event is excluded by semantic parser, but complete "
                "historical ST PIT is not available."
            ),
            "official_index_benchmark_used": False,
            "benchmark_note": (
                "Market adjustment uses internal cross-sectional median proxy."
            ),
        },
        "outputs": [
            "candidate_events.csv",
            "event_returns.csv",
            "signal_composition.csv",
            "measurement_status.csv",
            "group_summary.csv",
            "change_type_summary.csv",
            "yearly_summary.csv",
            "spread_summary.csv",
            "yearly_spread_summary.csv",
            "audit_summary.json",
        ],
        "no_network": True,
        "no_database_mutation": True,
        "output_dir": str(OUT_ROOT),
    }

    write_json_atomic(OUT_ROOT / "audit_summary.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["check", "run"])
    a = ap.parse_args()
    return check() if a.command == "check" else run()


if __name__ == "__main__":
    raise SystemExit(main())
