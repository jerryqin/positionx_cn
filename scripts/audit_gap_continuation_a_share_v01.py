from __future__ import annotations

r"""
PositionX — A-Share Overnight Gap Continuation Fast-Reject V0.1

Purpose
-------
Test a new economic source, distinct from:
- Deep Oversold: panic / mean reversion
- Earnings Improving H60: fundamental information diffusion

Hypothesis
----------
Information arriving overnight may not be fully incorporated at the opening
auction. Large opening gaps may show post-event drift.

Frozen event definition
-----------------------
Signal date T:
    gap_T = OPEN_T / PRE_CLOSE_T - 1

Positive event:
    gap_T >= +5%

Negative control:
    gap_T <= -5%

Timing:
    signal observed at T OPEN
    enter T+1 exact OPEN
    exits S5 / S20 / S60 exact CLOSE
    S1 = entry session
    no shifting for suspension / missing exact bars

Important:
- Uses stored exchange/QMT `pre_close`, NOT lag(close). This avoids treating
  ex-right / corporate-action reference-price changes as ordinary overnight gap.
- Known ST rows and known new-listing-special rows are excluded as structural
  market-mechanism hygiene, not alpha factors.
- Unknown ST status is retained and reported; historical strict non-ST PIT is
  not claimed.
- Negative gap group is a control; no short strategy is assumed.
- This is an event study, not a portfolio backtest.
- No volume, RSI, industry, size, valuation, or other factor is added.

Research window
---------------
Signal start: 2024-01-01
Signal end: latest T for which S60 exact market session exists in
            market.daily_bars_v1.
All horizons use the SAME H60-complete signal cohort.

Market adjustment
-----------------
For each exact entry/exit window:
    benchmark = cross-sectional median A-share OPEN(entry)->CLOSE(exit) return
    adjusted_return = event_return - benchmark

This prevents market-wide overnight shocks from masquerading as stock-specific
gap continuation.

Fast-reject gate (predeclared before seeing V0.1 results)
---------------------------------------------------------
A horizon H20/H60 passes only if:
1) POS adjusted mean > 0
2) POS adjusted 5% trimmed mean > 0
3) POS signal-date-equal adjusted mean > 0
4) POS-NEG adjusted mean spread > 0
5) POS-NEG signal-date-equal spread > 0
6) >= 500 observations in both POS and NEG
7) POS adjusted mean > 0 in >=2 valid years
8) POS-NEG adjusted mean spread > 0 in >=2 valid years
   valid year requires >=100 observations in each group

Verdict:
- STRONG_GAP_CONTINUATION_CANDIDATE: H20 and H60 pass
- PROMISING_HORIZON_CANDIDATE: exactly one of H20/H60 passes
- FAST_REJECT: neither passes and either
      (both POS trim5 <= 0) OR
      (both date-equal spreads <= 0)
- INCONCLUSIVE: otherwise

H5 is reaction diagnostic only and does not decide the candidate gate.
"""

import argparse
import hashlib
import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import duckdb
import numpy as np
import pandas as pd

CATALOG = Path(r"D:\positionx_data\catalog\market_data.duckdb")
STATUS_PARQUET = Path(
    r"D:\positionx_data\incoming\eva\20260925_foundation_assets"
    r"\data\research\px_assets\a_share_daily_trading_status.parquet"
)
OUT_ROOT = Path(r"D:\positionx_data\research\gap_continuation_a_share_v0_1")

START_DATE = "20240101"
GAP_THRESHOLD = 0.05
HORIZONS = (5, 20, 60)
ALLOWED_BOARDS = ("SSE_MAIN", "SZSE_MAIN", "CHINEXT", "STAR")
MIN_GROUP_N = 500
MIN_YEAR_GROUP_N = 100


def write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def sha256_file(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def trim_mean(s: pd.Series, pct: float) -> float | None:
    x = pd.to_numeric(s, errors="coerce").dropna().to_numpy(dtype=float)
    if len(x) == 0:
        return None
    x.sort()
    k = int(math.floor(len(x) * pct))
    if k * 2 >= len(x):
        return None
    if k:
        x = x[k:-k]
    return float(np.mean(x))


def safe_float(v: Any) -> float | None:
    if v is None or pd.isna(v):
        return None
    return float(v)


def describe_catalog(con: duckdb.DuckDBPyConnection) -> dict:
    cols = {
        str(r[0])
        for r in con.execute("DESCRIBE market.daily_bars_v1").fetchall()
    }
    required = {
        "symbol", "trade_date", "open", "close", "pre_close"
    }
    missing = sorted(required - cols)
    if missing:
        raise RuntimeError(
            f"FAIL_CLOSED market.daily_bars_v1 missing={missing}"
        )

    mcols = {
        str(r[0])
        for r in con.execute("DESCRIBE market.security_master_v1").fetchall()
    }
    mreq = {"symbol", "board", "instrument_type_stock"}
    mmissing = sorted(mreq - mcols)
    if mmissing:
        raise RuntimeError(
            f"FAIL_CLOSED market.security_master_v1 missing={mmissing}"
        )

    row = con.execute(
        """
        SELECT
          count(*) n,
          count(DISTINCT symbol) symbols,
          min(CAST(trade_date AS DATE)) d0,
          max(CAST(trade_date AS DATE)) d1,
          count(*) FILTER (WHERE pre_close IS NULL OR pre_close<=0) bad_pre_close
        FROM market.daily_bars_v1
        """
    ).fetchone()
    return {
        "rows": int(row[0]),
        "symbols": int(row[1]),
        "min_date": str(row[2]),
        "max_date": str(row[3]),
        "bad_pre_close_rows": int(row[4]),
        "daily_columns": sorted(cols),
        "master_columns": sorted(mcols),
    }


def load_calendar(con: duckdb.DuckDBPyConnection) -> list[str]:
    rows = con.execute(
        """
        SELECT DISTINCT strftime(CAST(trade_date AS DATE), '%Y%m%d') d
        FROM market.daily_bars_v1
        WHERE CAST(trade_date AS DATE) >= CAST(? AS DATE)
        ORDER BY d
        """,
        [f"{START_DATE[:4]}-{START_DATE[4:6]}-{START_DATE[6:]}"],
    ).fetchall()
    cal = [str(r[0]) for r in rows if r and r[0]]
    if len(cal) < 100:
        raise RuntimeError(
            f"FAIL_CLOSED market calendar too short={len(cal)}"
        )
    return cal


def signal_cutoff_h60(calendar: list[str]) -> str:
    # T=i, entry=i+1, S60 exit=i+60. Need i+60 <= last_index.
    if len(calendar) < 61:
        raise RuntimeError("FAIL_CLOSED calendar lacks H60 runway")
    return calendar[-61]


def create_master_temp(con: duckdb.DuckDBPyConnection) -> None:
    boards = ",".join(f"'{x}'" for x in ALLOWED_BOARDS)
    con.execute(
        f"""
        CREATE OR REPLACE TEMP VIEW px_stock_master AS
        SELECT symbol, board
        FROM (
          SELECT
            CAST(symbol AS VARCHAR) symbol,
            CAST(board AS VARCHAR) board,
            row_number() OVER (
              PARTITION BY CAST(symbol AS VARCHAR)
              ORDER BY
                CASE WHEN scope_role='CURRENT_ELIGIBLE' THEN 0 ELSE 1 END,
                CAST(as_of AS VARCHAR) DESC NULLS LAST
            ) rn
          FROM market.security_master_v1
          WHERE instrument_type_stock=TRUE
            AND board IN ({boards})
        )
        WHERE rn=1
        """
    )


def status_schema() -> set[str]:
    con = duckdb.connect(":memory:")
    try:
        p = str(STATUS_PARQUET).replace("\\", "/").replace("'", "''")
        return {
            str(r[0])
            for r in con.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{p}')"
            ).fetchall()
        }
    finally:
        con.close()


def load_raw_events(
    con: duckdb.DuckDBPyConnection,
    cutoff: str,
) -> tuple[pd.DataFrame, dict]:
    if not STATUS_PARQUET.exists():
        raise RuntimeError(
            f"FAIL_CLOSED missing status asset: {STATUS_PARQUET}"
        )
    scols = status_schema()
    sreq = {
        "trade_date", "symbol", "is_suspended",
        "st_status", "st_known", "new_listing_special"
    }
    missing = sorted(sreq - scols)
    if missing:
        raise RuntimeError(
            f"FAIL_CLOSED status parquet missing={missing}"
        )

    p = str(STATUS_PARQUET).replace("\\", "/").replace("'", "''")
    con.execute(
        f"""
        CREATE OR REPLACE TEMP VIEW px_status AS
        SELECT
          CAST(trade_date AS DATE) trade_date,
          CAST(symbol AS VARCHAR) symbol,
          CAST(is_suspended AS BOOLEAN) is_suspended,
          CAST(st_status AS VARCHAR) st_status,
          CAST(st_known AS BOOLEAN) st_known,
          CAST(new_listing_special AS BOOLEAN) new_listing_special,
          CAST(pre_close AS DOUBLE) status_pre_close,
          CAST(open AS DOUBLE) status_open
        FROM read_parquet('{p}')
        """
    )

    q = """
    WITH raw AS (
      SELECT
        b.symbol,
        CAST(b.trade_date AS DATE) signal_date,
        m.board,
        CAST(b."open" AS DOUBLE) signal_open,
        CAST(b.pre_close AS DOUBLE) pre_close,
        CAST(b."open" AS DOUBLE) / NULLIF(CAST(b.pre_close AS DOUBLE),0) - 1.0 AS gap,
        s.symbol IS NOT NULL AS status_found,
        s.is_suspended,
        s.st_status,
        s.st_known,
        s.new_listing_special,
        s.status_pre_close,
        s.status_open
      FROM market.daily_bars_v1 b
      INNER JOIN px_stock_master m USING(symbol)
      LEFT JOIN px_status s
        ON s.symbol=b.symbol
       AND s.trade_date=CAST(b.trade_date AS DATE)
      WHERE strftime(CAST(b.trade_date AS DATE), '%Y%m%d')
            BETWEEN ? AND ?
        AND CAST(b."open" AS DOUBLE) > 0
        AND CAST(b.pre_close AS DOUBLE) > 0
    )
    SELECT *
    FROM raw
    WHERE gap >= ? OR gap <= ?
    ORDER BY signal_date, symbol
    """
    df = con.execute(
        q, [START_DATE, cutoff, GAP_THRESHOLD, -GAP_THRESHOLD]
    ).df()

    if df.empty:
        raise RuntimeError("FAIL_CLOSED no +/-5% gap events")

    df["signal_date8"] = pd.to_datetime(
        df["signal_date"]
    ).dt.strftime("%Y%m%d")
    df["group"] = np.where(df["gap"] >= GAP_THRESHOLD, "POS", "NEG")
    df["known_st"] = (
        df["st_known"].fillna(False)
        & df["st_status"].fillna("").astype(str).str.upper().isin(
            ["ST", "STAR_ST"]
        )
    )
    df["status_missing"] = ~df["status_found"].fillna(False)
    df["exclude_reason"] = ""
    df.loc[df["status_missing"], "exclude_reason"] = "NO_STATUS"
    df.loc[
        (df["exclude_reason"]=="")
        & df["is_suspended"].fillna(False),
        "exclude_reason"
    ] = "SUSPENDED"
    df.loc[
        (df["exclude_reason"]=="")
        & df["new_listing_special"].fillna(False),
        "exclude_reason"
    ] = "NEW_LISTING_SPECIAL"
    df.loc[
        (df["exclude_reason"]=="")
        & df["known_st"],
        "exclude_reason"
    ] = "KNOWN_ST"

    # Cross-source event-price agreement is a diagnostic, not a filter.
    denom_pc = np.maximum(
        np.maximum(
            np.abs(pd.to_numeric(df["pre_close"], errors="coerce")),
            np.abs(pd.to_numeric(df["status_pre_close"], errors="coerce")),
        ),
        1.0,
    )
    denom_o = np.maximum(
        np.maximum(
            np.abs(pd.to_numeric(df["signal_open"], errors="coerce")),
            np.abs(pd.to_numeric(df["status_open"], errors="coerce")),
        ),
        1.0,
    )
    df["pre_close_cross_rel_diff"] = (
        pd.to_numeric(df["pre_close"], errors="coerce")
        - pd.to_numeric(df["status_pre_close"], errors="coerce")
    ).abs() / denom_pc
    df["open_cross_rel_diff"] = (
        pd.to_numeric(df["signal_open"], errors="coerce")
        - pd.to_numeric(df["status_open"], errors="coerce")
    ).abs() / denom_o

    eligible = df[df["exclude_reason"]==""].copy()

    audit = {
        "raw_gap_events": int(len(df)),
        "raw_pos": int((df["group"]=="POS").sum()),
        "raw_neg": int((df["group"]=="NEG").sum()),
        "excluded": (
            df.loc[df["exclude_reason"]!="", "exclude_reason"]
            .value_counts().to_dict()
        ),
        "eligible_gap_events": int(len(eligible)),
        "eligible_pos": int((eligible["group"]=="POS").sum()),
        "eligible_neg": int((eligible["group"]=="NEG").sum()),
        "eligible_st_unknown": int(
            (~eligible["st_known"].fillna(False)).sum()
        ),
        "cross_source_pre_close_rel_diff": {
            "n": int(df["pre_close_cross_rel_diff"].notna().sum()),
            "max": safe_float(df["pre_close_cross_rel_diff"].max()),
            "p99": safe_float(df["pre_close_cross_rel_diff"].quantile(0.99)),
            "gt_1bp": int(
                (df["pre_close_cross_rel_diff"] > 0.0001).sum()
            ),
        },
        "cross_source_open_rel_diff": {
            "n": int(df["open_cross_rel_diff"].notna().sum()),
            "max": safe_float(df["open_cross_rel_diff"].max()),
            "p99": safe_float(df["open_cross_rel_diff"].quantile(0.99)),
            "gt_1bp": int(
                (df["open_cross_rel_diff"] > 0.0001).sum()
            ),
        },
    }
    return eligible, audit


def attach_horizon_dates(
    events: pd.DataFrame,
    calendar: list[str],
) -> pd.DataFrame:
    idx = {d:i for i,d in enumerate(calendar)}
    rows = []
    for r in events.to_dict("records"):
        t = r["signal_date8"]
        if t not in idx:
            continue
        i = idx[t]
        entry_i = i + 1
        for h in HORIZONS:
            exit_i = i + h
            if exit_i >= len(calendar):
                raise RuntimeError(
                    f"FAIL_CLOSED cohort contains incomplete H{h}: {t}"
                )
            z = dict(r)
            z["horizon"] = int(h)
            z["entry_date"] = calendar[entry_i]
            z["exit_date"] = calendar[exit_i]
            rows.append(z)
    out = pd.DataFrame(rows)
    if len(out) != len(events) * len(HORIZONS):
        raise RuntimeError(
            f"FAIL_CLOSED horizon expansion rows={len(out)} "
            f"expected={len(events)*len(HORIZONS)}"
        )
    return out


def measure_event_returns(
    con: duckdb.DuckDBPyConnection,
    long_events: pd.DataFrame,
) -> pd.DataFrame:
    ev = long_events[
        [
            "symbol", "signal_date8", "board", "group", "gap",
            "horizon", "entry_date", "exit_date"
        ]
    ].copy()
    con.register("px_events_long", ev)
    try:
        out = con.execute(
            """
            SELECT
              e.*,
              CAST(be."open" AS DOUBLE) entry_open,
              CAST(bx."close" AS DOUBLE) exit_close
            FROM px_events_long e
            LEFT JOIN market.daily_bars_v1 be
              ON be.symbol=e.symbol
             AND strftime(CAST(be.trade_date AS DATE),'%Y%m%d')=e.entry_date
            LEFT JOIN market.daily_bars_v1 bx
              ON bx.symbol=e.symbol
             AND strftime(CAST(bx.trade_date AS DATE),'%Y%m%d')=e.exit_date
            ORDER BY e.signal_date8, e.symbol, e.horizon
            """
        ).df()
    finally:
        con.unregister("px_events_long")

    out["measurement_status"] = "OK"
    out.loc[out["entry_open"].isna(), "measurement_status"] = "NO_T1_BAR"
    out.loc[
        (out["measurement_status"]=="OK")
        & out["exit_close"].isna(),
        "measurement_status"
    ] = "NO_EXACT_EXIT_BAR"
    out.loc[
        (out["measurement_status"]=="OK")
        & (
            (pd.to_numeric(out["entry_open"], errors="coerce") <= 0)
            | (pd.to_numeric(out["exit_close"], errors="coerce") <= 0)
        ),
        "measurement_status"
    ] = "INVALID_PRICE"

    out["raw_return"] = np.nan
    ok = out["measurement_status"]=="OK"
    out.loc[ok, "raw_return"] = (
        pd.to_numeric(out.loc[ok, "exit_close"], errors="raise")
        / pd.to_numeric(out.loc[ok, "entry_open"], errors="raise")
        - 1.0
    )
    return out


def load_window_benchmarks(
    con: duckdb.DuckDBPyConnection,
    measured: pd.DataFrame,
) -> pd.DataFrame:
    windows = (
        measured[["horizon", "entry_date", "exit_date"]]
        .drop_duplicates()
        .sort_values(["horizon", "entry_date"])
        .reset_index(drop=True)
    )
    con.register("px_windows", windows)
    try:
        b = con.execute(
            """
            SELECT
              w.horizon,
              w.entry_date,
              w.exit_date,
              median(
                CAST(bx."close" AS DOUBLE)
                / NULLIF(CAST(be."open" AS DOUBLE),0) - 1.0
              ) AS market_median_return,
              count(*) FILTER (
                WHERE CAST(be."open" AS DOUBLE)>0
                  AND CAST(bx."close" AS DOUBLE)>0
              ) AS market_n
            FROM px_windows w
            INNER JOIN market.daily_bars_v1 be
              ON strftime(CAST(be.trade_date AS DATE),'%Y%m%d')=w.entry_date
            INNER JOIN px_stock_master m
              ON m.symbol=be.symbol
            INNER JOIN market.daily_bars_v1 bx
              ON bx.symbol=be.symbol
             AND strftime(CAST(bx.trade_date AS DATE),'%Y%m%d')=w.exit_date
            WHERE CAST(be."open" AS DOUBLE)>0
              AND CAST(bx."close" AS DOUBLE)>0
            GROUP BY 1,2,3
            ORDER BY 1,2
            """
        ).df()
    finally:
        con.unregister("px_windows")

    if len(b) != len(windows):
        missing = windows.merge(
            b, on=["horizon","entry_date","exit_date"],
            how="left", indicator=True
        )
        bad = missing[missing["_merge"]!="both"].head(10).to_dict("records")
        raise RuntimeError(
            f"FAIL_CLOSED missing market benchmarks sample={bad}"
        )
    if (pd.to_numeric(b["market_n"], errors="coerce") < 1000).any():
        bad = b[pd.to_numeric(b["market_n"]) < 1000].head(10).to_dict("records")
        raise RuntimeError(
            f"FAIL_CLOSED benchmark cross-section too small sample={bad}"
        )
    return b


def add_adjustment(
    measured: pd.DataFrame,
    bench: pd.DataFrame,
) -> pd.DataFrame:
    x = measured.merge(
        bench,
        on=["horizon","entry_date","exit_date"],
        how="left",
        validate="many_to_one",
    )
    x["adjusted_return"] = np.where(
        x["measurement_status"]=="OK",
        pd.to_numeric(x["raw_return"], errors="coerce")
        - pd.to_numeric(x["market_median_return"], errors="coerce"),
        np.nan,
    )
    return x


def group_stats(g: pd.DataFrame) -> dict:
    x = g[g["measurement_status"]=="OK"].copy()
    raw = pd.to_numeric(x["raw_return"], errors="coerce").dropna()
    adj = pd.to_numeric(x["adjusted_return"], errors="coerce").dropna()
    return {
        "n": int(len(adj)),
        "raw_mean": safe_float(raw.mean()),
        "raw_median": safe_float(raw.median()),
        "raw_trim1": trim_mean(raw, 0.01),
        "raw_trim5": trim_mean(raw, 0.05),
        "raw_win_rate": safe_float((raw>0).mean()),
        "adjusted_mean": safe_float(adj.mean()),
        "adjusted_median": safe_float(adj.median()),
        "adjusted_trim1": trim_mean(adj, 0.01),
        "adjusted_trim5": trim_mean(adj, 0.05),
        "adjusted_win_rate": safe_float((adj>0).mean()),
    }


def make_group_summary(x: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (grp,h), g in x.groupby(["group","horizon"], sort=True):
        rows.append({"group":grp, "horizon":int(h), **group_stats(g)})
    return pd.DataFrame(rows)


def make_date_equal_summary(x: pd.DataFrame) -> tuple[pd.DataFrame,pd.DataFrame]:
    ok = x[x["measurement_status"]=="OK"].copy()
    daily = (
        ok.groupby(["group","horizon","signal_date8"], as_index=False)
        .agg(
            n=("adjusted_return","size"),
            adjusted_mean=("adjusted_return","mean"),
            raw_mean=("raw_return","mean"),
        )
    )
    rows = []
    for (grp,h), g in daily.groupby(["group","horizon"], sort=True):
        rows.append(
            {
                "group":grp,
                "horizon":int(h),
                "signal_dates":int(len(g)),
                "date_equal_adjusted_mean":safe_float(g["adjusted_mean"].mean()),
                "date_equal_adjusted_median":safe_float(g["adjusted_mean"].median()),
                "positive_date_fraction":safe_float((g["adjusted_mean"]>0).mean()),
                "events_per_signal_date_mean":safe_float(g["n"].mean()),
                "events_per_signal_date_p95":safe_float(g["n"].quantile(0.95)),
                "events_per_signal_date_max":int(g["n"].max()),
            }
        )
    return pd.DataFrame(rows), daily


def make_yearly_summary(x: pd.DataFrame) -> pd.DataFrame:
    z = x[x["measurement_status"]=="OK"].copy()
    z["year"] = z["signal_date8"].str[:4].astype(int)
    rows = []
    for (year,grp,h), g in z.groupby(
        ["year","group","horizon"], sort=True
    ):
        rows.append(
            {
                "year":int(year),
                "group":grp,
                "horizon":int(h),
                **group_stats(g),
            }
        )
    return pd.DataFrame(rows)


def make_spreads(
    group_summary: pd.DataFrame,
    date_equal_summary: pd.DataFrame,
) -> pd.DataFrame:
    rows=[]
    for h in HORIZONS:
        p=group_summary[
            (group_summary["group"]=="POS")
            & (group_summary["horizon"]==h)
        ]
        n=group_summary[
            (group_summary["group"]=="NEG")
            & (group_summary["horizon"]==h)
        ]
        pdq=date_equal_summary[
            (date_equal_summary["group"]=="POS")
            & (date_equal_summary["horizon"]==h)
        ]
        ndq=date_equal_summary[
            (date_equal_summary["group"]=="NEG")
            & (date_equal_summary["horizon"]==h)
        ]
        if len(p)!=1 or len(n)!=1 or len(pdq)!=1 or len(ndq)!=1:
            raise RuntimeError(f"FAIL_CLOSED missing summary H{h}")
        p,n,pdq,ndq=p.iloc[0],n.iloc[0],pdq.iloc[0],ndq.iloc[0]
        rows.append(
            {
                "horizon":h,
                "pos_n":int(p["n"]),
                "neg_n":int(n["n"]),
                "adjusted_mean_spread":float(
                    p["adjusted_mean"]-n["adjusted_mean"]
                ),
                "adjusted_trim5_spread":float(
                    p["adjusted_trim5"]-n["adjusted_trim5"]
                ),
                "date_equal_adjusted_spread":float(
                    pdq["date_equal_adjusted_mean"]
                    - ndq["date_equal_adjusted_mean"]
                ),
            }
        )
    return pd.DataFrame(rows)


def make_yearly_spreads(yearly: pd.DataFrame) -> pd.DataFrame:
    rows=[]
    for (year,h), g in yearly.groupby(["year","horizon"],sort=True):
        p=g[g["group"]=="POS"]
        n=g[g["group"]=="NEG"]
        if len(p)!=1 or len(n)!=1:
            continue
        p,n=p.iloc[0],n.iloc[0]
        rows.append(
            {
                "year":int(year),
                "horizon":int(h),
                "pos_n":int(p["n"]),
                "neg_n":int(n["n"]),
                "pos_adjusted_mean":float(p["adjusted_mean"]),
                "neg_adjusted_mean":float(n["adjusted_mean"]),
                "adjusted_mean_spread":float(
                    p["adjusted_mean"]-n["adjusted_mean"]
                ),
                "valid_year":bool(
                    int(p["n"])>=MIN_YEAR_GROUP_N
                    and int(n["n"])>=MIN_YEAR_GROUP_N
                ),
            }
        )
    return pd.DataFrame(rows)


def clustering_summary(events: pd.DataFrame) -> tuple[dict,pd.DataFrame]:
    d=(
        events.groupby(["signal_date8","group"],as_index=False)
        .size().rename(columns={"size":"events"})
    )
    total=len(events)
    top = (
        events.groupby("signal_date8").size()
        .sort_values(ascending=False)
    )
    return {
        "signal_dates":int(events["signal_date8"].nunique()),
        "events_per_date_mean":safe_float(
            events.groupby("signal_date8").size().mean()
        ),
        "events_per_date_p95":safe_float(
            events.groupby("signal_date8").size().quantile(0.95)
        ),
        "events_per_date_max":int(top.iloc[0]) if len(top) else 0,
        "top_5_signal_dates_event_share":(
            float(top.head(5).sum()/total) if total else None
        ),
        "top_10_signal_dates":[
            {"signal_date":str(k),"events":int(v)}
            for k,v in top.head(10).items()
        ],
    }, d


def board_summary(x: pd.DataFrame) -> pd.DataFrame:
    z=x[x["measurement_status"]=="OK"].copy()
    return (
        z.groupby(["board","group","horizon"],as_index=False)
        .agg(
            n=("adjusted_return","size"),
            adjusted_mean=("adjusted_return","mean"),
            adjusted_median=("adjusted_return","median"),
        )
        .sort_values(["horizon","group","board"])
    )


def decision(
    group_summary: pd.DataFrame,
    date_equal_summary: pd.DataFrame,
    spreads: pd.DataFrame,
    yearly: pd.DataFrame,
    yearly_spreads: pd.DataFrame,
) -> dict:
    out={}
    for h in (20,60):
        p=group_summary[
            (group_summary["group"]=="POS")
            & (group_summary["horizon"]==h)
        ].iloc[0]
        n=group_summary[
            (group_summary["group"]=="NEG")
            & (group_summary["horizon"]==h)
        ].iloc[0]
        pdq=date_equal_summary[
            (date_equal_summary["group"]=="POS")
            & (date_equal_summary["horizon"]==h)
        ].iloc[0]
        sp=spreads[spreads["horizon"]==h].iloc[0]

        py=yearly[
            (yearly["group"]=="POS")
            & (yearly["horizon"]==h)
            & (yearly["n"]>=MIN_YEAR_GROUP_N)
        ]
        ys=yearly_spreads[
            (yearly_spreads["horizon"]==h)
            & (yearly_spreads["valid_year"]==True)
        ]
        pos_years=int((py["adjusted_mean"]>0).sum())
        valid_pos_years=int(len(py))
        spread_years=int((ys["adjusted_mean_spread"]>0).sum())
        valid_spread_years=int(len(ys))

        gates={
            "pos_adjusted_mean_positive":bool(p["adjusted_mean"]>0),
            "pos_adjusted_trim5_positive":bool(p["adjusted_trim5"]>0),
            "pos_date_equal_adjusted_mean_positive":bool(
                pdq["date_equal_adjusted_mean"]>0
            ),
            "pos_neg_adjusted_mean_spread_positive":bool(
                sp["adjusted_mean_spread"]>0
            ),
            "pos_neg_date_equal_spread_positive":bool(
                sp["date_equal_adjusted_spread"]>0
            ),
            "min_group_n":int(min(p["n"],n["n"])),
            "min_group_n_pass":bool(
                min(p["n"],n["n"])>=MIN_GROUP_N
            ),
            "pos_positive_years":pos_years,
            "valid_pos_years":valid_pos_years,
            "pos_year_gate":bool(pos_years>=2),
            "spread_positive_years":spread_years,
            "valid_spread_years":valid_spread_years,
            "spread_year_gate":bool(spread_years>=2),
        }
        gates["horizon_pass"]=all(
            [
                gates["pos_adjusted_mean_positive"],
                gates["pos_adjusted_trim5_positive"],
                gates["pos_date_equal_adjusted_mean_positive"],
                gates["pos_neg_adjusted_mean_spread_positive"],
                gates["pos_neg_date_equal_spread_positive"],
                gates["min_group_n_pass"],
                gates["pos_year_gate"],
                gates["spread_year_gate"],
            ]
        )
        out[f"h{h}"]=gates

    passes=sum(int(out[f"h{h}"]["horizon_pass"]) for h in (20,60))
    if passes==2:
        verdict="STRONG_GAP_CONTINUATION_CANDIDATE"
    elif passes==1:
        verdict="PROMISING_HORIZON_CANDIDATE"
    else:
        trim_nonpos=all(
            not out[f"h{h}"]["pos_adjusted_trim5_positive"]
            for h in (20,60)
        )
        date_spread_nonpos=all(
            not out[f"h{h}"]["pos_neg_date_equal_spread_positive"]
            for h in (20,60)
        )
        verdict=(
            "FAST_REJECT"
            if (trim_nonpos or date_spread_nonpos)
            else "INCONCLUSIVE"
        )
    return {
        "verdict":verdict,
        **out,
        "rule":{
            "STRONG_GAP_CONTINUATION_CANDIDATE":"H20 and H60 pass all predeclared gates.",
            "PROMISING_HORIZON_CANDIDATE":"Exactly one of H20/H60 passes all predeclared gates.",
            "FAST_REJECT":"Neither passes and both H20/H60 have non-positive POS trim5 OR both have non-positive date-equal POS-NEG spread.",
            "INCONCLUSIVE":"Everything else; do not add factors before inspecting basic evidence.",
        },
    }


def check() -> int:
    if not CATALOG.exists():
        raise RuntimeError(f"missing catalog: {CATALOG}")
    if not STATUS_PARQUET.exists():
        raise RuntimeError(f"missing status asset: {STATUS_PARQUET}")
    con=duckdb.connect(str(CATALOG),read_only=True)
    try:
        meta=describe_catalog(con)
        create_master_temp(con)
        cal=load_calendar(con)
        cutoff=signal_cutoff_h60(cal)
        master_n=int(con.execute(
            "SELECT count(*) FROM px_stock_master"
        ).fetchone()[0])
    finally:
        con.close()

    result={
        "status":"PASS_GAP_CONTINUATION_A_SHARE_V0_1_CHECK",
        "catalog":str(CATALOG),
        "catalog_sha256":sha256_file(CATALOG),
        "catalog_meta":meta,
        "status_asset":str(STATUS_PARQUET),
        "status_asset_sha256":sha256_file(STATUS_PARQUET),
        "eligible_stock_master_rows":master_n,
        "signal_start":START_DATE,
        "signal_cutoff_h60":cutoff,
        "latest_market_session":cal[-1],
        "horizons":list(HORIZONS),
        "gap_threshold":GAP_THRESHOLD,
        "no_network":True,
        "no_database_mutation":True,
    }
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0


def run() -> int:
    OUT_ROOT.mkdir(parents=True,exist_ok=True)
    con=duckdb.connect(str(CATALOG),read_only=True)
    try:
        meta=describe_catalog(con)
        create_master_temp(con)
        cal=load_calendar(con)
        cutoff=signal_cutoff_h60(cal)

        events,event_audit=load_raw_events(con,cutoff)
        long_events=attach_horizon_dates(events,cal)
        measured=measure_event_returns(con,long_events)
        bench=load_window_benchmarks(con,measured)
        x=add_adjustment(measured,bench)
    finally:
        con.close()

    group=make_group_summary(x)
    date_eq,date_daily=make_date_equal_summary(x)
    yearly=make_yearly_summary(x)
    spreads=make_spreads(group,date_eq)
    yearly_spreads=make_yearly_spreads(yearly)
    cluster,cluster_daily=clustering_summary(events)
    boards=board_summary(x)
    dec=decision(group,date_eq,spreads,yearly,yearly_spreads)

    status_counts=x["measurement_status"].value_counts().to_dict()

    # Signal composition.
    composition=(
        events.groupby(["signal_date8","group","board"],as_index=False)
        .agg(
            events=("symbol","size"),
            mean_gap=("gap","mean"),
            median_gap=("gap","median"),
        )
    )

    outputs={
        "candidate_events":"candidate_events.csv",
        "event_returns":"event_returns.csv",
        "measurement_status":"measurement_status.csv",
        "group_summary":"group_summary.csv",
        "date_equal_summary":"date_equal_summary.csv",
        "signal_date_group_returns":"signal_date_group_returns.csv",
        "yearly_summary":"yearly_summary.csv",
        "spread_summary":"spread_summary.csv",
        "yearly_spread_summary":"yearly_spread_summary.csv",
        "board_summary":"board_summary.csv",
        "signal_composition":"signal_composition.csv",
        "signal_clustering":"signal_clustering.csv",
        "market_benchmarks":"market_benchmarks.csv",
        "audit_summary":"audit_summary.json",
    }

    events.to_csv(
        OUT_ROOT/outputs["candidate_events"],
        index=False,encoding="utf-8-sig"
    )
    x.to_csv(
        OUT_ROOT/outputs["event_returns"],
        index=False,encoding="utf-8-sig"
    )
    (
        x.groupby(["group","horizon","measurement_status"],as_index=False)
        .size()
        .rename(columns={"size":"rows"})
        .to_csv(
            OUT_ROOT/outputs["measurement_status"],
            index=False,encoding="utf-8-sig"
        )
    )
    group.to_csv(OUT_ROOT/outputs["group_summary"],index=False,encoding="utf-8-sig")
    date_eq.to_csv(OUT_ROOT/outputs["date_equal_summary"],index=False,encoding="utf-8-sig")
    date_daily.to_csv(OUT_ROOT/outputs["signal_date_group_returns"],index=False,encoding="utf-8-sig")
    yearly.to_csv(OUT_ROOT/outputs["yearly_summary"],index=False,encoding="utf-8-sig")
    spreads.to_csv(OUT_ROOT/outputs["spread_summary"],index=False,encoding="utf-8-sig")
    yearly_spreads.to_csv(OUT_ROOT/outputs["yearly_spread_summary"],index=False,encoding="utf-8-sig")
    boards.to_csv(OUT_ROOT/outputs["board_summary"],index=False,encoding="utf-8-sig")
    composition.to_csv(OUT_ROOT/outputs["signal_composition"],index=False,encoding="utf-8-sig")
    cluster_daily.to_csv(OUT_ROOT/outputs["signal_clustering"],index=False,encoding="utf-8-sig")
    bench.to_csv(OUT_ROOT/outputs["market_benchmarks"],index=False,encoding="utf-8-sig")

    result={
        "status":"PASS_GAP_CONTINUATION_A_SHARE_V0_1",
        "generated_at":datetime.now().astimezone().isoformat(timespec="seconds"),
        "research_id":"GAP_CONTINUATION_A_SHARE_V0_1",
        "market_source":{
            "catalog":str(CATALOG),
            "view":"market.daily_bars_v1",
            "security_master":"market.security_master_v1",
            "catalog_meta":meta,
        },
        "status_source":{
            "path":str(STATUS_PARQUET),
            "sha256":sha256_file(STATUS_PARQUET),
            "purpose":"known ST / suspension / new-listing-special hygiene only",
            "historical_strict_non_st_pit_applied":False,
            "unknown_st_retained":True,
        },
        "definition":{
            "signal":"gap_T = OPEN_T / PRE_CLOSE_T - 1",
            "positive":"gap >= +5%",
            "negative_control":"gap <= -5%",
            "pre_close_semantics":"stored pre_close, not lag(close)",
            "signal_timestamp":"T OPEN",
            "entry":"T+1 exact OPEN",
            "h5":"5th market session including entry as #1, exact CLOSE",
            "h20":"20th market session including entry as #1, exact CLOSE",
            "h60":"60th market session including entry as #1, exact CLOSE",
            "same_h60_complete_cohort_for_all_horizons":True,
            "no_shift_for_suspension":True,
            "known_st_excluded":True,
            "known_new_listing_special_excluded":True,
            "unknown_st_retained":True,
            "no_alpha_factors_added":True,
        },
        "window":{
            "signal_start":START_DATE,
            "signal_cutoff_h60":cutoff,
            "latest_market_session":cal[-1],
            "calendar_sessions_from_start":len(cal),
        },
        "candidate_audit":event_audit,
        "measurement_status_counts":{
            str(k):int(v) for k,v in status_counts.items()
        },
        "market_adjustment":{
            "method":"same exact entry/exit window cross-sectional median A-share OPEN->CLOSE return",
            "benchmark_windows":int(len(bench)),
            "benchmark_market_n_min":int(bench["market_n"].min()),
            "benchmark_market_n_median":float(bench["market_n"].median()),
        },
        "clustering":cluster,
        "decision":dec,
        "caveats":{
            "event_study_not_portfolio_backtest":True,
            "transaction_costs_included":False,
            "negative_group_short_assumed":False,
            "negative_group_role":"control / discrimination evidence",
            "historical_strict_non_st_pit_applied":False,
            "known_st_only_excluded":True,
            "entry_fillability_not_yet_audited":True,
            "corporate_action_note":"gap uses stored exchange/QMT pre_close rather than previous raw close",
        },
        "next":{
            "if_candidate":"Run standalone shared-equity portfolio audit first; only then test incremental value vs Deep Oversold.",
            "if_fast_reject":"Freeze/reject Gap Continuation V0.1 and move to a different economic source. Do not add factors.",
            "if_inconclusive":"Inspect robustness/tail/date clustering before any additional complexity.",
        },
        "outputs":outputs,
        "no_network":True,
        "no_database_mutation":True,
    }
    write_json_atomic(OUT_ROOT/"audit_summary.json",result)

    print(json.dumps(result,ensure_ascii=False,indent=2))
    print(f"OUTPUT: {OUT_ROOT}")
    return 0


def main() -> int:
    p=argparse.ArgumentParser()
    p.add_argument("command",choices=["check","run"])
    args=p.parse_args()
    return check() if args.command=="check" else run()


if __name__=="__main__":
    raise SystemExit(main())
