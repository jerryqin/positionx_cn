from __future__ import annotations

r"""
PositionX — A-Share Limit-Price Continuation Fast-Reject V0.1

Economic mechanism
------------------
A-share daily price limits can mechanically delay price discovery. If information
cannot be fully incorporated on signal day T because the stock closes at its
daily up-limit, the remaining information may continue to be incorporated after
the limit event.

This is a different economic source from:
- Deep Oversold: panic / mean reversion
- Earnings Improving: fundamental disclosure drift
- Gap Continuation: overnight opening shock

Frozen event definition
-----------------------
Signal timestamp: T close.

POS:
    signal-day close is exactly at the official/computed up-limit.

NEG control:
    signal-day close is exactly at the down-limit.

Basic market-structure hygiene only:
- signal row not suspended
- known new-listing-special day excluded
- known ST signal row excluded
- unknown ST retained and reported
- limit price must be non-null
- T+1 exact OPEN must exist
- T+1 must be buyable at open according to the existing trading-status asset

Entry:
    T+1 exact OPEN.

Exits:
    S5 / S20 / S60 exact CLOSE, where S1 is the entry session.
    No shifting when the exact exit bar is unavailable.

All horizons use the same H60-complete signal cohort.

No alpha factors
----------------
No volume, turnover, RSI, industry, size, valuation, number-of-limit-ups,
one-price/non-one-price filter, gap, or momentum filter is used for selection.
One-price and board fields are exported only as diagnostics.

Market adjustment
-----------------
For each exact entry/exit window:
    benchmark = cross-sectional median A-share OPEN(entry)->CLOSE(exit) return
    adjusted_return = event_return - benchmark

Signal clustering
-----------------
Limit events cluster on market-wide risk-on/risk-off days. Report both:
- event-weighted results
- signal-date-equal results

Predeclared H20/H60 gate
------------------------
A horizon passes only if:
1) POS adjusted mean > 0
2) POS adjusted 5% trimmed mean > 0
3) POS signal-date-equal adjusted mean > 0
4) POS-NEG adjusted mean spread > 0
5) POS-NEG signal-date-equal spread > 0
6) >= 500 observations in both groups
7) POS adjusted mean > 0 in >=2 valid years
8) POS-NEG adjusted mean spread > 0 in >=2 valid years
   valid year requires >=100 observations in each group

Verdict:
- STRONG_LIMIT_CONTINUATION_CANDIDATE: H20 and H60 pass
- PROMISING_HORIZON_CANDIDATE: exactly one of H20/H60 passes
- FAST_REJECT: neither passes and either
      (both POS trim5 <= 0) OR
      (both date-equal POS-NEG spreads <= 0)
- INCONCLUSIVE: otherwise

H5 is reaction diagnostic only.
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

CATALOG = Path(r"D:\positionx_data\catalog\market_data.duckdb")
STATUS_PARQUET = Path(
    r"D:\positionx_data\incoming\eva\20260925_foundation_assets"
    r"\data\research\px_assets\a_share_daily_trading_status.parquet"
)
OUT_ROOT = Path(
    r"D:\positionx_data\research\limit_price_continuation_a_share_v0_1"
)

START_DATE = "20240101"
HORIZONS = (5, 20, 60)
ALLOWED_BOARDS = ("SSE_MAIN", "SZSE_MAIN", "CHINEXT", "STAR")
MIN_GROUP_N = 500
MIN_YEAR_GROUP_N = 100


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


def safe_float(v: Any) -> float | None:
    if v is None or pd.isna(v):
        return None
    return float(v)


def trim_mean(s: pd.Series, pct: float) -> float | None:
    x = pd.to_numeric(s, errors="coerce").dropna().to_numpy(dtype=float)
    if not len(x):
        return None
    x.sort()
    k = int(math.floor(len(x) * pct))
    if 2 * k >= len(x):
        return None
    if k:
        x = x[k:-k]
    return float(np.mean(x))


def describe_catalog(con: duckdb.DuckDBPyConnection) -> dict:
    cols = {
        str(r[0])
        for r in con.execute("DESCRIBE market.daily_bars_v1").fetchall()
    }
    required = {"symbol", "trade_date", "open", "close"}
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
          max(CAST(trade_date AS DATE)) d1
        FROM market.daily_bars_v1
        """
    ).fetchone()
    return {
        "rows": int(row[0]),
        "symbols": int(row[1]),
        "min_date": str(row[2]),
        "max_date": str(row[3]),
    }


def status_schema() -> set[str]:
    if not STATUS_PARQUET.exists():
        raise RuntimeError(
            f"FAIL_CLOSED missing status asset: {STATUS_PARQUET}"
        )
    p = str(STATUS_PARQUET).replace("\\", "/").replace("'", "''")
    con = duckdb.connect(":memory:")
    try:
        return {
            str(r[0])
            for r in con.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{p}')"
            ).fetchall()
        }
    finally:
        con.close()


def create_temp_views(con: duckdb.DuckDBPyConnection) -> None:
    boards = ",".join(f"'{x}'" for x in ALLOWED_BOARDS)
    con.execute(
        f"""
        CREATE OR REPLACE TEMP VIEW px_stock_master AS
        SELECT symbol, board
        FROM (
          SELECT
            CAST(symbol AS VARCHAR) AS symbol,
            CAST(board AS VARCHAR) AS board,
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

    scols = status_schema()
    required = {
        "trade_date", "symbol", "is_suspended",
        "is_close_at_up_limit", "is_close_at_down_limit",
        "is_one_price_up_limit", "is_one_price_down_limit",
        "can_buy_at_open", "st_status", "st_known",
        "new_listing_special", "up_limit", "down_limit",
        "open", "close", "pre_close"
    }
    missing = sorted(required - scols)
    if missing:
        raise RuntimeError(
            f"FAIL_CLOSED status asset missing={missing}"
        )
    p = str(STATUS_PARQUET).replace("\\", "/").replace("'", "''")
    con.execute(
        f"""
        CREATE OR REPLACE TEMP VIEW px_status AS
        SELECT
          CAST(trade_date AS DATE) trade_date,
          CAST(symbol AS VARCHAR) symbol,
          CAST(is_suspended AS BOOLEAN) is_suspended,
          CAST(is_close_at_up_limit AS BOOLEAN) is_close_at_up_limit,
          CAST(is_close_at_down_limit AS BOOLEAN) is_close_at_down_limit,
          CAST(is_one_price_up_limit AS BOOLEAN) is_one_price_up_limit,
          CAST(is_one_price_down_limit AS BOOLEAN) is_one_price_down_limit,
          CAST(can_buy_at_open AS BOOLEAN) can_buy_at_open,
          CAST(st_status AS VARCHAR) st_status,
          CAST(st_known AS BOOLEAN) st_known,
          CAST(new_listing_special AS BOOLEAN) new_listing_special,
          CAST(up_limit AS DOUBLE) up_limit,
          CAST(down_limit AS DOUBLE) down_limit,
          CAST("open" AS DOUBLE) status_open,
          CAST("close" AS DOUBLE) status_close,
          CAST(pre_close AS DOUBLE) status_pre_close
        FROM read_parquet('{p}')
        """
    )


def load_calendar(con: duckdb.DuckDBPyConnection) -> list[str]:
    rows = con.execute(
        """
        SELECT DISTINCT strftime(CAST(trade_date AS DATE), '%Y%m%d') d
        FROM market.daily_bars_v1
        WHERE strftime(CAST(trade_date AS DATE), '%Y%m%d') >= ?
        ORDER BY d
        """,
        [START_DATE],
    ).fetchall()
    cal = [str(r[0]) for r in rows if r and r[0]]
    if len(cal) < 100:
        raise RuntimeError("FAIL_CLOSED market calendar too short")
    return cal


def signal_cutoff_h60(calendar: list[str]) -> str:
    # T=i, entry=i+1, S60=i+60
    if len(calendar) < 61:
        raise RuntimeError("FAIL_CLOSED insufficient H60 runway")
    return calendar[-61]


def load_signal_events(
    con: duckdb.DuckDBPyConnection,
    cutoff: str,
) -> tuple[pd.DataFrame, dict]:
    q = """
    WITH sig AS (
      SELECT
        s.symbol,
        s.trade_date signal_date,
        m.board,
        CASE
          WHEN s.is_close_at_up_limit THEN 'POS'
          WHEN s.is_close_at_down_limit THEN 'NEG'
          ELSE NULL
        END grp,
        s.is_one_price_up_limit,
        s.is_one_price_down_limit,
        s.st_status,
        s.st_known,
        s.new_listing_special,
        s.status_open,
        s.status_close,
        s.status_pre_close,
        s.up_limit,
        s.down_limit
      FROM px_status s
      INNER JOIN px_stock_master m USING(symbol)
      WHERE strftime(s.trade_date, '%Y%m%d') BETWEEN ? AND ?
        AND NOT s.is_suspended
        AND (s.is_close_at_up_limit OR s.is_close_at_down_limit)
    )
    SELECT *
    FROM sig
    WHERE grp IS NOT NULL
    ORDER BY signal_date, symbol
    """
    raw = con.execute(q, [START_DATE, cutoff]).df()
    if raw.empty:
        raise RuntimeError("FAIL_CLOSED no limit-price signals")

    raw["signal_date8"] = pd.to_datetime(
        raw["signal_date"]
    ).dt.strftime("%Y%m%d")
    raw["known_st"] = (
        raw["st_known"].fillna(False)
        & raw["st_status"].fillna("").astype(str).str.upper().isin(
            ["ST", "STAR_ST"]
        )
    )
    raw["exclude_reason"] = ""
    raw.loc[
        raw["new_listing_special"].fillna(False),
        "exclude_reason"
    ] = "NEW_LISTING_SPECIAL"
    raw.loc[
        (raw["exclude_reason"]=="") & raw["known_st"],
        "exclude_reason"
    ] = "KNOWN_ST"
    raw.loc[
        (raw["exclude_reason"]=="")
        & (
            ((raw["grp"]=="POS") & raw["up_limit"].isna())
            | ((raw["grp"]=="NEG") & raw["down_limit"].isna())
        ),
        "exclude_reason"
    ] = "MISSING_LIMIT_PRICE"

    eligible = raw[raw["exclude_reason"]==""].copy()

    audit = {
        "raw_signal_events": int(len(raw)),
        "raw_pos": int((raw["grp"]=="POS").sum()),
        "raw_neg": int((raw["grp"]=="NEG").sum()),
        "excluded": (
            raw.loc[raw["exclude_reason"]!="", "exclude_reason"]
            .value_counts().to_dict()
        ),
        "eligible_signal_events_pre_entry_fillability": int(len(eligible)),
        "eligible_pos_pre_entry_fillability": int(
            (eligible["grp"]=="POS").sum()
        ),
        "eligible_neg_pre_entry_fillability": int(
            (eligible["grp"]=="NEG").sum()
        ),
        "eligible_st_unknown": int(
            (~eligible["st_known"].fillna(False)).sum()
        ),
        "one_price_pos": int(
            eligible["is_one_price_up_limit"].fillna(False).sum()
        ),
        "one_price_neg": int(
            eligible["is_one_price_down_limit"].fillna(False).sum()
        ),
    }
    return eligible, audit


def attach_entry_and_horizons(
    con: duckdb.DuckDBPyConnection,
    events: pd.DataFrame,
    calendar: list[str],
) -> tuple[pd.DataFrame, dict]:
    idx = {d:i for i,d in enumerate(calendar)}
    base_rows=[]
    for r in events.to_dict("records"):
        t=r["signal_date8"]
        i=idx.get(t)
        if i is None or i+60>=len(calendar):
            continue
        z=dict(r)
        z["entry_date"]=calendar[i+1]
        z["exit_date_5"]=calendar[i+5]
        z["exit_date_20"]=calendar[i+20]
        z["exit_date_60"]=calendar[i+60]
        base_rows.append(z)
    base=pd.DataFrame(base_rows)
    if len(base)!=len(events):
        raise RuntimeError(
            f"FAIL_CLOSED H60 cohort mismatch events={len(events)} base={len(base)}"
        )

    con.register(
        "px_signal_base",
        base[["symbol","signal_date8","entry_date"]]
    )
    try:
        entry_status=con.execute(
            """
            SELECT
              b.symbol,
              b.signal_date8,
              b.entry_date,
              s.can_buy_at_open,
              s.is_suspended entry_is_suspended,
              s.st_known entry_st_known,
              s.st_status entry_st_status,
              s.new_listing_special entry_new_listing_special,
              s.status_open entry_status_open
            FROM px_signal_base b
            LEFT JOIN px_status s
              ON s.symbol=b.symbol
             AND strftime(s.trade_date,'%Y%m%d')=b.entry_date
            """
        ).df()
    finally:
        con.unregister("px_signal_base")

    x=base.merge(
        entry_status,
        on=["symbol","signal_date8","entry_date"],
        how="left",
        validate="one_to_one",
    )
    x["entry_status_found"]=x["entry_is_suspended"].notna()
    x["entry_exclude_reason"]=""
    x.loc[
        ~x["entry_status_found"],"entry_exclude_reason"
    ]="NO_ENTRY_STATUS"
    x.loc[
        (x["entry_exclude_reason"]=="")
        & x["entry_is_suspended"].fillna(False),
        "entry_exclude_reason"
    ]="ENTRY_SUSPENDED"
    x.loc[
        (x["entry_exclude_reason"]=="")
        & ~x["can_buy_at_open"].fillna(False),
        "entry_exclude_reason"
    ]="ENTRY_NOT_BUYABLE_AT_OPEN"
    x.loc[
        (x["entry_exclude_reason"]=="")
        & x["entry_new_listing_special"].fillna(False),
        "entry_exclude_reason"
    ]="ENTRY_NEW_LISTING_SPECIAL"
    entry_known_st=(
        x["entry_st_known"].fillna(False)
        & x["entry_st_status"].fillna("").astype(str).str.upper().isin(
            ["ST","STAR_ST"]
        )
    )
    x.loc[
        (x["entry_exclude_reason"]=="") & entry_known_st,
        "entry_exclude_reason"
    ]="ENTRY_KNOWN_ST"

    counts=(
        x.loc[x["entry_exclude_reason"]!="","entry_exclude_reason"]
        .value_counts().to_dict()
    )
    ok=x[x["entry_exclude_reason"]==""].copy()
    audit={
        "entry_fillability_excluded":counts,
        "entry_fillable_events":int(len(ok)),
        "entry_fillable_pos":int((ok["grp"]=="POS").sum()),
        "entry_fillable_neg":int((ok["grp"]=="NEG").sum()),
        "entry_st_unknown":int(
            (~ok["entry_st_known"].fillna(False)).sum()
        ),
    }
    return ok,audit


def expand_horizons(base: pd.DataFrame) -> pd.DataFrame:
    rows=[]
    for r in base.to_dict("records"):
        for h in HORIZONS:
            z=dict(r)
            z["horizon"]=h
            z["exit_date"]=r[f"exit_date_{h}"]
            rows.append(z)
    out=pd.DataFrame(rows)
    if len(out)!=len(base)*len(HORIZONS):
        raise RuntimeError("FAIL_CLOSED horizon expansion failed")
    return out


def measure(
    con: duckdb.DuckDBPyConnection,
    long_events: pd.DataFrame,
) -> pd.DataFrame:
    ev=long_events[
        [
            "symbol","signal_date8","board","grp","entry_date",
            "exit_date","horizon","is_one_price_up_limit",
            "is_one_price_down_limit"
        ]
    ].copy()
    con.register("px_long",ev)
    try:
        x=con.execute(
            """
            SELECT
              e.*,
              CAST(be."open" AS DOUBLE) entry_open,
              CAST(bx."close" AS DOUBLE) exit_close
            FROM px_long e
            LEFT JOIN market.daily_bars_v1 be
              ON be.symbol=e.symbol
             AND strftime(CAST(be.trade_date AS DATE),'%Y%m%d')=e.entry_date
            LEFT JOIN market.daily_bars_v1 bx
              ON bx.symbol=e.symbol
             AND strftime(CAST(bx.trade_date AS DATE),'%Y%m%d')=e.exit_date
            ORDER BY e.signal_date8,e.symbol,e.horizon
            """
        ).df()
    finally:
        con.unregister("px_long")

    x["measurement_status"]="OK"
    x.loc[x["entry_open"].isna(),"measurement_status"]="NO_T1_BAR"
    x.loc[
        (x["measurement_status"]=="OK") & x["exit_close"].isna(),
        "measurement_status"
    ]="NO_EXACT_EXIT_BAR"
    x.loc[
        (x["measurement_status"]=="OK")
        & (
            (pd.to_numeric(x["entry_open"],errors="coerce")<=0)
            | (pd.to_numeric(x["exit_close"],errors="coerce")<=0)
        ),
        "measurement_status"
    ]="INVALID_PRICE"
    x["raw_return"]=np.nan
    ok=x["measurement_status"]=="OK"
    x.loc[ok,"raw_return"]=(
        pd.to_numeric(x.loc[ok,"exit_close"],errors="raise")
        / pd.to_numeric(x.loc[ok,"entry_open"],errors="raise")
        - 1.0
    )
    return x


def load_benchmarks(
    con: duckdb.DuckDBPyConnection,
    measured: pd.DataFrame,
) -> pd.DataFrame:
    w=(
        measured[["horizon","entry_date","exit_date"]]
        .drop_duplicates()
        .sort_values(["horizon","entry_date"])
    )
    con.register("px_windows",w)
    try:
        b=con.execute(
            """
            SELECT
              w.horizon,w.entry_date,w.exit_date,
              median(
                CAST(bx."close" AS DOUBLE)
                / NULLIF(CAST(be."open" AS DOUBLE),0)-1.0
              ) market_median_return,
              count(*) FILTER (
                WHERE CAST(be."open" AS DOUBLE)>0
                  AND CAST(bx."close" AS DOUBLE)>0
              ) market_n
            FROM px_windows w
            INNER JOIN market.daily_bars_v1 be
              ON strftime(CAST(be.trade_date AS DATE),'%Y%m%d')=w.entry_date
            INNER JOIN px_stock_master m ON m.symbol=be.symbol
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
    if len(b)!=len(w):
        raise RuntimeError("FAIL_CLOSED missing benchmark windows")
    if (pd.to_numeric(b["market_n"])<1000).any():
        raise RuntimeError("FAIL_CLOSED benchmark cross-section too small")
    return b


def adjust(measured: pd.DataFrame, bench: pd.DataFrame) -> pd.DataFrame:
    x=measured.merge(
        bench,on=["horizon","entry_date","exit_date"],
        how="left",validate="many_to_one"
    )
    x["adjusted_return"]=np.where(
        x["measurement_status"]=="OK",
        pd.to_numeric(x["raw_return"],errors="coerce")
        - pd.to_numeric(x["market_median_return"],errors="coerce"),
        np.nan
    )
    return x


def stats(g: pd.DataFrame) -> dict:
    z=g[g["measurement_status"]=="OK"]
    raw=pd.to_numeric(z["raw_return"],errors="coerce").dropna()
    adj=pd.to_numeric(z["adjusted_return"],errors="coerce").dropna()
    return {
        "n":int(len(adj)),
        "raw_mean":safe_float(raw.mean()),
        "raw_median":safe_float(raw.median()),
        "raw_trim5":trim_mean(raw,0.05),
        "raw_win_rate":safe_float((raw>0).mean()),
        "adjusted_mean":safe_float(adj.mean()),
        "adjusted_median":safe_float(adj.median()),
        "adjusted_trim1":trim_mean(adj,0.01),
        "adjusted_trim5":trim_mean(adj,0.05),
        "adjusted_win_rate":safe_float((adj>0).mean()),
    }


def group_summary(x: pd.DataFrame) -> pd.DataFrame:
    rows=[]
    for (grp,h),g in x.groupby(["grp","horizon"],sort=True):
        rows.append({"group":grp,"horizon":int(h),**stats(g)})
    return pd.DataFrame(rows)


def date_equal_summary(
    x: pd.DataFrame,
) -> tuple[pd.DataFrame,pd.DataFrame]:
    ok=x[x["measurement_status"]=="OK"]
    daily=(
        ok.groupby(["grp","horizon","signal_date8"],as_index=False)
        .agg(
            n=("adjusted_return","size"),
            adjusted_mean=("adjusted_return","mean"),
            raw_mean=("raw_return","mean"),
        )
    )
    rows=[]
    for (grp,h),g in daily.groupby(["grp","horizon"],sort=True):
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
    return pd.DataFrame(rows),daily


def yearly_summary(x: pd.DataFrame) -> pd.DataFrame:
    z=x[x["measurement_status"]=="OK"].copy()
    z["year"]=z["signal_date8"].str[:4].astype(int)
    rows=[]
    for (year,grp,h),g in z.groupby(
        ["year","grp","horizon"],sort=True
    ):
        rows.append(
            {
                "year":int(year),
                "group":grp,
                "horizon":int(h),
                **stats(g)
            }
        )
    return pd.DataFrame(rows)


def spread_summary(
    gs: pd.DataFrame,
    de: pd.DataFrame,
) -> pd.DataFrame:
    rows=[]
    for h in HORIZONS:
        p=gs[(gs["group"]=="POS")&(gs["horizon"]==h)].iloc[0]
        n=gs[(gs["group"]=="NEG")&(gs["horizon"]==h)].iloc[0]
        pdq=de[(de["group"]=="POS")&(de["horizon"]==h)].iloc[0]
        ndq=de[(de["group"]=="NEG")&(de["horizon"]==h)].iloc[0]
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


def yearly_spreads(y: pd.DataFrame) -> pd.DataFrame:
    rows=[]
    for (year,h),g in y.groupby(["year","horizon"],sort=True):
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


def clustering(base: pd.DataFrame) -> tuple[dict,pd.DataFrame]:
    d=(
        base.groupby(["signal_date8","grp"],as_index=False)
        .size().rename(columns={"size":"events"})
    )
    top=base.groupby("signal_date8").size().sort_values(ascending=False)
    total=len(base)
    return {
        "signal_dates":int(base["signal_date8"].nunique()),
        "events_per_date_mean":safe_float(
            base.groupby("signal_date8").size().mean()
        ),
        "events_per_date_p95":safe_float(
            base.groupby("signal_date8").size().quantile(0.95)
        ),
        "events_per_date_max":int(top.iloc[0]) if len(top) else 0,
        "top_5_signal_dates_event_share":(
            float(top.head(5).sum()/total) if total else None
        ),
        "top_10_signal_dates":[
            {"signal_date":str(k),"events":int(v)}
            for k,v in top.head(10).items()
        ],
    },d


def diagnostic_one_price(x: pd.DataFrame) -> pd.DataFrame:
    z=x[x["measurement_status"]=="OK"].copy()
    z["signal_form"]=np.where(
        (z["grp"]=="POS") & z["is_one_price_up_limit"].fillna(False),
        "ONE_PRICE",
        np.where(
            (z["grp"]=="NEG") & z["is_one_price_down_limit"].fillna(False),
            "ONE_PRICE",
            "NOT_ONE_PRICE"
        )
    )
    return (
        z.groupby(["grp","signal_form","horizon"],as_index=False)
        .agg(
            n=("adjusted_return","size"),
            adjusted_mean=("adjusted_return","mean"),
            adjusted_median=("adjusted_return","median"),
        )
    )


def decision(
    gs: pd.DataFrame,
    de: pd.DataFrame,
    sp: pd.DataFrame,
    y: pd.DataFrame,
    ys: pd.DataFrame,
) -> dict:
    out={}
    for h in (20,60):
        p=gs[(gs["group"]=="POS")&(gs["horizon"]==h)].iloc[0]
        n=gs[(gs["group"]=="NEG")&(gs["horizon"]==h)].iloc[0]
        pdq=de[(de["group"]=="POS")&(de["horizon"]==h)].iloc[0]
        sr=sp[sp["horizon"]==h].iloc[0]

        py=y[
            (y["group"]=="POS")
            & (y["horizon"]==h)
            & (y["n"]>=MIN_YEAR_GROUP_N)
        ]
        ysp=ys[
            (ys["horizon"]==h)&(ys["valid_year"]==True)
        ]
        pos_years=int((py["adjusted_mean"]>0).sum())
        spread_years=int((ysp["adjusted_mean_spread"]>0).sum())

        gates={
            "pos_adjusted_mean_positive":bool(p["adjusted_mean"]>0),
            "pos_adjusted_trim5_positive":bool(p["adjusted_trim5"]>0),
            "pos_date_equal_adjusted_mean_positive":bool(
                pdq["date_equal_adjusted_mean"]>0
            ),
            "pos_neg_adjusted_mean_spread_positive":bool(
                sr["adjusted_mean_spread"]>0
            ),
            "pos_neg_date_equal_spread_positive":bool(
                sr["date_equal_adjusted_spread"]>0
            ),
            "min_group_n":int(min(p["n"],n["n"])),
            "min_group_n_pass":bool(
                min(p["n"],n["n"])>=MIN_GROUP_N
            ),
            "pos_positive_years":pos_years,
            "valid_pos_years":int(len(py)),
            "pos_year_gate":bool(pos_years>=2),
            "spread_positive_years":spread_years,
            "valid_spread_years":int(len(ysp)),
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
        verdict="STRONG_LIMIT_CONTINUATION_CANDIDATE"
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
            "STRONG_LIMIT_CONTINUATION_CANDIDATE":"H20 and H60 pass all predeclared gates.",
            "PROMISING_HORIZON_CANDIDATE":"Exactly one of H20/H60 passes all predeclared gates.",
            "FAST_REJECT":"Neither passes and both H20/H60 have non-positive POS trim5 OR both have non-positive date-equal POS-NEG spread.",
            "INCONCLUSIVE":"Everything else; inspect evidence before adding complexity.",
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
        create_temp_views(con)
        cal=load_calendar(con)
        cutoff=signal_cutoff_h60(cal)
        master_n=int(
            con.execute("SELECT count(*) FROM px_stock_master").fetchone()[0]
        )
        status_max=str(
            con.execute(
                "SELECT max(strftime(trade_date,'%Y%m%d')) FROM px_status"
            ).fetchone()[0]
        )
        if status_max < cutoff:
            raise RuntimeError(
                f"FAIL_CLOSED status asset ends before signal cutoff "
                f"status_max={status_max} cutoff={cutoff}"
            )
    finally:
        con.close()

    result={
        "status":"PASS_LIMIT_PRICE_CONTINUATION_A_SHARE_V0_1_CHECK",
        "catalog":str(CATALOG),
        "catalog_sha256":sha256_file(CATALOG),
        "catalog_meta":meta,
        "status_asset":str(STATUS_PARQUET),
        "status_asset_sha256":sha256_file(STATUS_PARQUET),
        "status_max_date":status_max,
        "eligible_stock_master_rows":master_n,
        "signal_start":START_DATE,
        "signal_cutoff_h60":cutoff,
        "latest_market_session":cal[-1],
        "horizons":list(HORIZONS),
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
        create_temp_views(con)
        cal=load_calendar(con)
        cutoff=signal_cutoff_h60(cal)

        events,event_audit=load_signal_events(con,cutoff)
        base,entry_audit=attach_entry_and_horizons(
            con,events,cal
        )
        long_events=expand_horizons(base)
        measured=measure(con,long_events)
        bench=load_benchmarks(con,measured)
        x=adjust(measured,bench)
    finally:
        con.close()

    gs=group_summary(x)
    de,date_daily=date_equal_summary(x)
    y=yearly_summary(x)
    sp=spread_summary(gs,de)
    ys=yearly_spreads(y)
    cluster,cluster_daily=clustering(base)
    onep=diagnostic_one_price(x)
    dec=decision(gs,de,sp,y,ys)

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
        "signal_clustering":"signal_clustering.csv",
        "one_price_diagnostic":"one_price_diagnostic.csv",
        "market_benchmarks":"market_benchmarks.csv",
        "audit_summary":"audit_summary.json",
    }

    base.to_csv(
        OUT_ROOT/outputs["candidate_events"],
        index=False,encoding="utf-8-sig"
    )
    x.to_csv(
        OUT_ROOT/outputs["event_returns"],
        index=False,encoding="utf-8-sig"
    )
    (
        x.groupby(["grp","horizon","measurement_status"],as_index=False)
        .size().rename(columns={"size":"rows"})
        .to_csv(
            OUT_ROOT/outputs["measurement_status"],
            index=False,encoding="utf-8-sig"
        )
    )
    gs.to_csv(OUT_ROOT/outputs["group_summary"],index=False,encoding="utf-8-sig")
    de.to_csv(OUT_ROOT/outputs["date_equal_summary"],index=False,encoding="utf-8-sig")
    date_daily.to_csv(OUT_ROOT/outputs["signal_date_group_returns"],index=False,encoding="utf-8-sig")
    y.to_csv(OUT_ROOT/outputs["yearly_summary"],index=False,encoding="utf-8-sig")
    sp.to_csv(OUT_ROOT/outputs["spread_summary"],index=False,encoding="utf-8-sig")
    ys.to_csv(OUT_ROOT/outputs["yearly_spread_summary"],index=False,encoding="utf-8-sig")
    cluster_daily.to_csv(OUT_ROOT/outputs["signal_clustering"],index=False,encoding="utf-8-sig")
    onep.to_csv(OUT_ROOT/outputs["one_price_diagnostic"],index=False,encoding="utf-8-sig")
    bench.to_csv(OUT_ROOT/outputs["market_benchmarks"],index=False,encoding="utf-8-sig")

    result={
        "status":"PASS_LIMIT_PRICE_CONTINUATION_A_SHARE_V0_1",
        "generated_at":datetime.now().astimezone().isoformat(timespec="seconds"),
        "research_id":"LIMIT_PRICE_CONTINUATION_A_SHARE_V0_1",
        "market_source":{
            "catalog":str(CATALOG),
            "view":"market.daily_bars_v1",
            "catalog_meta":meta,
        },
        "status_source":{
            "path":str(STATUS_PARQUET),
            "sha256":sha256_file(STATUS_PARQUET),
            "role":"signal-day limit state + T+1 entry fillability",
            "historical_strict_non_st_pit_applied":False,
            "unknown_st_retained":True,
        },
        "definition":{
            "positive":"T close exactly at up-limit",
            "negative_control":"T close exactly at down-limit",
            "signal_timestamp":"T close",
            "entry":"T+1 exact OPEN and can_buy_at_open=true",
            "h5":"S5 exact CLOSE",
            "h20":"S20 exact CLOSE",
            "h60":"S60 exact CLOSE",
            "same_h60_complete_cohort_for_all_horizons":True,
            "no_shift_for_missing_exact_exit":True,
            "known_st_excluded":True,
            "known_new_listing_special_excluded":True,
            "unknown_st_retained":True,
            "one_price_not_used_as_alpha_filter":True,
            "no_alpha_factors_added":True,
        },
        "window":{
            "signal_start":START_DATE,
            "signal_cutoff_h60":cutoff,
            "latest_market_session":cal[-1],
        },
        "signal_audit":event_audit,
        "entry_fillability_audit":entry_audit,
        "measurement_status_counts":{
            str(k):int(v)
            for k,v in x["measurement_status"].value_counts().items()
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
            "historical_strict_non_st_pit_applied":False,
            "unknown_st_retained":True,
            "consecutive_same_symbol_limit_events_not_deduped":True,
            "portfolio_dedup_only_if_event_study_candidate":True,
        },
        "next":{
            "if_candidate":"Run standalone shared-equity portfolio audit with one-position-per-symbol dedup, 20bp/50bp costs, then incremental value vs Deep.",
            "if_fast_reject":"Freeze/reject Limit-Price Continuation V0.1 and move to another economic source; do not tune one-price/volume/streak filters.",
            "if_inconclusive":"Inspect tail/date clustering and one-price diagnostic before adding any strategy complexity.",
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
