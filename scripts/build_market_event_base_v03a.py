from __future__ import annotations

"""
PositionX — Market-Derived Event Base V0.3

Adds four cheap, fully rebuildable market-derived events:

- RETURN_TOP1PCT
- RETURN_BOTTOM1PCT
- GAP_UP_TOP1PCT
- GAP_DOWN_BOTTOM1PCT

Existing V0.2 event families are rebuilt unchanged in semantics:
- PRICE_52W_HIGH / LOW
- PRICE_ALL_TIME_HIGH / LOW
- TURNOVER_TOP5
- TURNOVER_TOP1PCT

Price semantics
---------------
Price-level events:
    PIT_BACK_RATIO_COMPARABLE_V1

Daily return:
    close / pre_close - 1
This is ex-right-safe because pre_close is already adjusted to the current session basis.

Opening gap:
    open / pre_close - 1
This is also ex-right-safe for the same reason.

Cross-sectional ranks
---------------------
RETURN/GAP ranks use all non-suspended SH/SZ rows with valid positive prices that day.
Smoke mode limits only which symbols are materialized; ranks still use the full market.

Time semantics
--------------
Close-known events:
    knowable at T 15:00
    effective_date = next market session
    effective_phase = OPEN

Gap events:
    knowable at T 09:30 after the opening print is known
    effective_date = T
    effective_phase = AFTER_OPEN
They must NOT be backtested as if buyable at the same exact opening print.

Historical ST PIT remains incomplete. This is an event-fact layer, not a final strict
historical trading universe.
"""

import argparse
import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

CATALOG = Path(r"D:\positionx_data\catalog\market_data.duckdb")
OUT_ROOT = Path(r"D:\positionx_data\events\market_derived_v0_3a")
VIEW = "market.daily_bars_v1"

DATASET_VERSION = "0.3a"
UNIVERSE_ID = "A_SHARE_SHSZ_RAW_DAILY_V1"
PIT_PRICE_BASIS = "PIT_BACK_RATIO_COMPARABLE_V1"
RETURN_BASIS = "CLOSE_OVER_ADJUSTED_PRE_CLOSE_V1"
GAP_BASIS = "OPEN_OVER_ADJUSTED_PRE_CLOSE_V1"

SMOKE_SYMBOLS = [
    "603986.SH","601689.SH","002050.SZ","600699.SH","688017.SH","603667.SH","603009.SH",
    "300308.SZ","300502.SZ","601138.SH","688041.SH","300476.SZ","603019.SH","603259.SH",
    "002821.SZ","301080.SZ","688235.SH","600276.SH","688506.SH","001368.SZ",
    "000001.SZ","000333.SZ","002594.SZ","300750.SZ","600000.SH","600519.SH","601318.SH",
    "688981.SH","002415.SZ","000858.SZ","601398.SH","601857.SH","601888.SH","600036.SH",
    "000725.SZ","300274.SZ","002230.SZ","600030.SH","601012.SH","000651.SZ"
]


def safe(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        if isinstance(v, float) and not math.isfinite(v):
            return None
        return v
    if hasattr(v, "item"):
        try:
            return safe(v.item())
        except Exception:
            pass
    return str(v)


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=safe), encoding="utf-8")
    os.replace(tmp, path)


def connect():
    if not CATALOG.exists():
        raise RuntimeError(f"Catalog not found: {CATALOG}")
    import duckdb
    return duckdb.connect(str(CATALOG), read_only=True)


def catalog_meta(con):
    r = con.execute(
        f"SELECT count(*),count(DISTINCT symbol),min(trade_date),max(trade_date) FROM {VIEW}"
    ).fetchone()
    return {
        "rows": int(r[0]),
        "symbols": int(r[1]),
        "first_date": str(r[2]),
        "last_date": str(r[3]),
    }


def present_symbols(con, req: Sequence[str]):
    if not req:
        return [], []
    q = ",".join("?" for _ in req)
    got = {
        r[0]
        for r in con.execute(
            f"SELECT DISTINCT symbol FROM {VIEW} WHERE symbol IN ({q})", list(req)
        ).fetchall()
    }
    return [s for s in req if s in got], [s for s in req if s not in got]


def price_event_sql(filter_sql=""):
    return f"""
WITH raw AS (
  SELECT
    symbol,trade_date,"open","high","low","close",volume,amount,pre_close,suspend_flag,
    lag("close") OVER(PARTITION BY symbol ORDER BY trade_date) AS prev_raw_close
  FROM {VIEW}
  {filter_sql}
),
ratio AS (
  SELECT *,
    CASE
      WHEN prev_raw_close IS NULL THEN 1.0
      WHEN prev_raw_close <= 0 OR pre_close <= 0 THEN NULL
      ELSE prev_raw_close / pre_close
    END AS daily_dr
  FROM raw
),
basis AS (
  SELECT *,
    exp(
      sum(CASE WHEN daily_dr IS NULL THEN 0.0 ELSE ln(daily_dr) END)
      OVER(
        PARTITION BY symbol ORDER BY trade_date
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
      )
    ) AS cum_dr
  FROM ratio
),
p AS (
  SELECT *,
    "close" * cum_dr AS pit_close,
    row_number() OVER(PARTITION BY symbol ORDER BY trade_date) AS trade_idx
  FROM basis
),
x AS (
  SELECT *,
    count(*) OVER(
      PARTITION BY symbol ORDER BY trade_date
      ROWS BETWEEN 250 PRECEDING AND 1 PRECEDING
    ) AS prior250,
    max(pit_close) OVER(
      PARTITION BY symbol ORDER BY trade_date
      ROWS BETWEEN 250 PRECEDING AND 1 PRECEDING
    ) AS prev250max,
    min(pit_close) OVER(
      PARTITION BY symbol ORDER BY trade_date
      ROWS BETWEEN 250 PRECEDING AND 1 PRECEDING
    ) AS prev250min,
    max(pit_close) OVER(
      PARTITION BY symbol ORDER BY trade_date
      ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
    ) AS prevallmax,
    min(pit_close) OVER(
      PARTITION BY symbol ORDER BY trade_date
      ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
    ) AS prevallmin
  FROM p
),
cal AS (
  SELECT trade_date,lead(trade_date) OVER(ORDER BY trade_date) AS next_trade_date
  FROM (SELECT DISTINCT trade_date FROM {VIEW})
),
ev AS (
  SELECT symbol,trade_date,trade_idx,"close" raw_close,pit_close event_value,
         prev250max reference_value,daily_dr,cum_dr,'PRICE_52W_HIGH' event_type
  FROM x WHERE prior250=250 AND pit_close>prev250max
  UNION ALL
  SELECT symbol,trade_date,trade_idx,"close",pit_close,prev250min,daily_dr,cum_dr,'PRICE_52W_LOW'
  FROM x WHERE prior250=250 AND pit_close<prev250min
  UNION ALL
  SELECT symbol,trade_date,trade_idx,"close",pit_close,prevallmax,daily_dr,cum_dr,'PRICE_ALL_TIME_HIGH'
  FROM x WHERE trade_idx>1 AND pit_close>prevallmax
  UNION ALL
  SELECT symbol,trade_date,trade_idx,"close",pit_close,prevallmin,daily_dr,cum_dr,'PRICE_ALL_TIME_LOW'
  FROM x WHERE trade_idx>1 AND pit_close<prevallmin
)
SELECT
  md5(symbol||'|'||event_type||'|'||strftime(trade_date,'%Y%m%d')||'|0.3a') AS event_id,
  symbol,trade_date,event_type,
  CAST(event_value AS DOUBLE) AS event_value,
  CAST(reference_value AS DOUBLE) AS reference_value,
  CAST(raw_close AS DOUBLE) AS raw_close,
  CAST(event_value AS DOUBLE) AS pit_close,
  CAST(daily_dr AS DOUBLE) AS daily_dr,
  CAST(cum_dr AS DOUBLE) AS cum_dr,
  '{PIT_PRICE_BASIS}' AS price_basis,
  CAST(NULL AS BIGINT) AS market_rank,
  CAST(NULL AS DOUBLE) AS percentile,
  CAST(NULL AS BIGINT) AS universe_size,
  trade_idx,
  'CLOSE' AS event_phase,
  trade_date + INTERVAL 15 HOUR AS event_knowable_at,
  c.next_trade_date AS effective_date,
  'OPEN' AS effective_phase
FROM ev
JOIN cal c USING(trade_date)
"""


def turnover_sql(output_filter=""):
    return f"""
WITH ranked AS (
  SELECT symbol,trade_date,amount,
    row_number() OVER(PARTITION BY trade_date ORDER BY amount DESC,symbol) AS amount_rank,
    count(*) OVER(PARTITION BY trade_date) AS universe_size
  FROM {VIEW}
  WHERE amount>0
),
idx AS (
  SELECT symbol,trade_date,
         row_number() OVER(PARTITION BY symbol ORDER BY trade_date) AS trade_idx
  FROM {VIEW}
),
cal AS (
  SELECT trade_date,lead(trade_date) OVER(ORDER BY trade_date) AS next_trade_date
  FROM (SELECT DISTINCT trade_date FROM {VIEW})
),
ev AS (
  SELECT r.*,'TURNOVER_TOP5' AS event_type
  FROM ranked r WHERE amount_rank<=5
  UNION ALL
  SELECT r.*,'TURNOVER_TOP1PCT'
  FROM ranked r
  WHERE amount_rank<=greatest(1,ceil(universe_size*0.01))
)
SELECT
  md5(e.symbol||'|'||e.event_type||'|'||strftime(e.trade_date,'%Y%m%d')||'|0.3a') AS event_id,
  e.symbol,e.trade_date,e.event_type,
  CAST(e.amount AS DOUBLE) AS event_value,
  CAST(NULL AS DOUBLE) AS reference_value,
  CAST(NULL AS DOUBLE) AS raw_close,
  CAST(NULL AS DOUBLE) AS pit_close,
  CAST(NULL AS DOUBLE) AS daily_dr,
  CAST(NULL AS DOUBLE) AS cum_dr,
  CAST(NULL AS VARCHAR) AS price_basis,
  CAST(e.amount_rank AS BIGINT) AS market_rank,
  1.0-((e.amount_rank-1.0)/e.universe_size) AS percentile,
  CAST(e.universe_size AS BIGINT) AS universe_size,
  i.trade_idx,
  'CLOSE' AS event_phase,
  e.trade_date + INTERVAL 15 HOUR AS event_knowable_at,
  c.next_trade_date AS effective_date,
  'OPEN' AS effective_phase
FROM ev e
JOIN idx i USING(symbol,trade_date)
JOIN cal c USING(trade_date)
{output_filter}
"""


def extreme_return_sql(output_filter=""):
    return f"""
WITH base AS (
  SELECT
    symbol,trade_date,
    "close"/pre_close-1.0 AS metric,
    row_number() OVER(PARTITION BY symbol ORDER BY trade_date) AS trade_idx
  FROM {VIEW}
  WHERE coalesce(suspend_flag,0)=0
    AND "close">0
    AND pre_close>0
),
r1 AS (
  SELECT *,
    row_number() OVER(PARTITION BY trade_date ORDER BY metric DESC,symbol) AS rn_desc,
    row_number() OVER(PARTITION BY trade_date ORDER BY metric ASC,symbol) AS rn_asc,
    rank() OVER(PARTITION BY trade_date ORDER BY metric DESC) AS rank_desc,
    rank() OVER(PARTITION BY trade_date ORDER BY metric ASC) AS rank_asc,
    count(*) OVER(PARTITION BY trade_date) AS universe_size
  FROM base
),
r2 AS (
  SELECT *,
    greatest(1,ceil(universe_size*0.01)) AS k,
    max(CASE WHEN rn_desc=greatest(1,ceil(universe_size*0.01)) THEN metric END)
      OVER(PARTITION BY trade_date) AS top_cutoff,
    max(CASE WHEN rn_asc=greatest(1,ceil(universe_size*0.01)) THEN metric END)
      OVER(PARTITION BY trade_date) AS bottom_cutoff
  FROM r1
),
cal AS (
  SELECT trade_date,lead(trade_date) OVER(ORDER BY trade_date) AS next_trade_date
  FROM (SELECT DISTINCT trade_date FROM {VIEW})
),
ev AS (
  SELECT symbol,trade_date,trade_idx,metric event_value,top_cutoff reference_value,
         rank_desc market_rank,universe_size,'RETURN_TOP1PCT' event_type
  FROM r2 WHERE metric>=top_cutoff
  UNION ALL
  SELECT symbol,trade_date,trade_idx,metric,bottom_cutoff,
         rank_asc,universe_size,'RETURN_BOTTOM1PCT'
  FROM r2 WHERE metric<=bottom_cutoff
)
SELECT
  md5(e.symbol||'|'||e.event_type||'|'||strftime(e.trade_date,'%Y%m%d')||'|0.3a') AS event_id,
  e.symbol,e.trade_date,e.event_type,
  CAST(e.event_value AS DOUBLE) AS event_value,
  CAST(e.reference_value AS DOUBLE) AS reference_value,
  CAST(NULL AS DOUBLE) AS raw_close,
  CAST(NULL AS DOUBLE) AS pit_close,
  CAST(NULL AS DOUBLE) AS daily_dr,
  CAST(NULL AS DOUBLE) AS cum_dr,
  '{RETURN_BASIS}' AS price_basis,
  CAST(e.market_rank AS BIGINT) AS market_rank,
  CASE
    WHEN e.universe_size<=1 THEN NULL
    WHEN e.event_type='RETURN_TOP1PCT'
      THEN 1.0-((e.market_rank-1.0)/(e.universe_size-1.0))
    ELSE ((e.market_rank-1.0)/(e.universe_size-1.0))
  END AS percentile,
  CAST(e.universe_size AS BIGINT) AS universe_size,
  e.trade_idx,
  'CLOSE' AS event_phase,
  e.trade_date + INTERVAL 15 HOUR AS event_knowable_at,
  c.next_trade_date AS effective_date,
  'OPEN' AS effective_phase
FROM ev e
JOIN cal c USING(trade_date)
{output_filter}
"""


def gap_sql(output_filter=""):
    return f"""
WITH base AS (
  SELECT
    symbol,trade_date,
    "open"/pre_close-1.0 AS metric,
    row_number() OVER(PARTITION BY symbol ORDER BY trade_date) AS trade_idx
  FROM {VIEW}
  WHERE coalesce(suspend_flag,0)=0
    AND "open">0
    AND pre_close>0
),
r1 AS (
  SELECT *,
    row_number() OVER(PARTITION BY trade_date ORDER BY metric DESC,symbol) AS rn_desc,
    row_number() OVER(PARTITION BY trade_date ORDER BY metric ASC,symbol) AS rn_asc,
    rank() OVER(PARTITION BY trade_date ORDER BY metric DESC) AS rank_desc,
    rank() OVER(PARTITION BY trade_date ORDER BY metric ASC) AS rank_asc,
    count(*) OVER(PARTITION BY trade_date) AS universe_size
  FROM base
),
r2 AS (
  SELECT *,
    greatest(1,ceil(universe_size*0.01)) AS k,
    max(CASE WHEN rn_desc=greatest(1,ceil(universe_size*0.01)) THEN metric END)
      OVER(PARTITION BY trade_date) AS top_cutoff,
    max(CASE WHEN rn_asc=greatest(1,ceil(universe_size*0.01)) THEN metric END)
      OVER(PARTITION BY trade_date) AS bottom_cutoff
  FROM r1
),
ev AS (
  SELECT symbol,trade_date,trade_idx,metric event_value,top_cutoff reference_value,
         rank_desc market_rank,universe_size,'GAP_UP_TOP1PCT' event_type
  FROM r2 WHERE metric>=top_cutoff AND metric>0
  UNION ALL
  SELECT symbol,trade_date,trade_idx,metric,bottom_cutoff,
         rank_asc,universe_size,'GAP_DOWN_BOTTOM1PCT'
  FROM r2 WHERE metric<=bottom_cutoff AND metric<0
)
SELECT
  md5(e.symbol||'|'||e.event_type||'|'||strftime(e.trade_date,'%Y%m%d')||'|0.3a') AS event_id,
  e.symbol,e.trade_date,e.event_type,
  CAST(e.event_value AS DOUBLE) AS event_value,
  CAST(e.reference_value AS DOUBLE) AS reference_value,
  CAST(NULL AS DOUBLE) AS raw_close,
  CAST(NULL AS DOUBLE) AS pit_close,
  CAST(NULL AS DOUBLE) AS daily_dr,
  CAST(NULL AS DOUBLE) AS cum_dr,
  '{GAP_BASIS}' AS price_basis,
  CAST(e.market_rank AS BIGINT) AS market_rank,
  CASE
    WHEN e.universe_size<=1 THEN NULL
    WHEN e.event_type='GAP_UP_TOP1PCT'
      THEN 1.0-((e.market_rank-1.0)/(e.universe_size-1.0))
    ELSE ((e.market_rank-1.0)/(e.universe_size-1.0))
  END AS percentile,
  CAST(e.universe_size AS BIGINT) AS universe_size,
  e.trade_idx,
  'OPEN' AS event_phase,
  e.trade_date + INTERVAL 9 HOUR + INTERVAL 30 MINUTE AS event_knowable_at,
  e.trade_date AS effective_date,
  'AFTER_OPEN' AS effective_phase
FROM ev e
{output_filter}
"""


def enrich(df, observed_at):
    if df.empty:
        return df
    d=df.copy()
    d["trade_date"]=pd.to_datetime(d["trade_date"])
    d["effective_date"]=pd.to_datetime(d["effective_date"],errors="coerce")
    d["event_knowable_at"]=pd.to_datetime(d["event_knowable_at"])
    d=d.sort_values(["symbol","event_type","trade_date","event_id"]).reset_index(drop=True)

    g=d.groupby(["symbol","event_type"],sort=False)
    d["event_seq"]=g.cumcount()+1
    d["previous_event_date"]=g["trade_date"].shift(1)
    prev_idx=g["trade_idx"].shift(1)
    d["trading_days_since_previous"]=d["trade_idx"]-prev_idx
    d["calendar_days_since_previous"]=(d["trade_date"]-d["previous_event_date"]).dt.days
    gap=d["trading_days_since_previous"]

    d["first_in_20d"]=gap.isna()|(gap>20)
    d["first_in_60d"]=gap.isna()|(gap>60)
    d["first_in_250d"]=gap.isna()|(gap>250)

    groups=(gap.isna()|(gap!=1)).groupby([d["symbol"],d["event_type"]]).cumsum()
    d["consecutive_count"]=d.groupby(["symbol","event_type",groups]).cumcount()+1

    d["universe_id"]=UNIVERSE_ID
    d["dataset_version"]=DATASET_VERSION
    d["source"]="DERIVED_FROM_MARKET.DAILY_BARS_V1"
    d["observation_mode"]="HISTORICAL_RECONSTRUCTION_FROM_ACCEPTED_MARKET_DATA"
    d["observed_at"]=pd.Timestamp(observed_at)
    d["strict_non_st_pit_applied"]=False
    return d


def recent_cross_section_sanity(con):
    return con.execute(f"""
WITH dates AS (
  SELECT trade_date
  FROM (SELECT DISTINCT trade_date FROM {VIEW} ORDER BY trade_date DESC LIMIT 10)
),
base AS (
  SELECT m.trade_date,m.symbol,
         CASE WHEN coalesce(m.suspend_flag,0)=0 AND m."close">0 AND m.pre_close>0
              THEN m."close"/m.pre_close-1.0 END AS ret_metric,
         CASE WHEN coalesce(m.suspend_flag,0)=0 AND m."open">0 AND m.pre_close>0
              THEN m."open"/m.pre_close-1.0 END AS gap_metric
  FROM {VIEW} m
  JOIN dates d USING(trade_date)
),
ret_ranked AS (
  SELECT *,
         row_number() OVER(PARTITION BY trade_date ORDER BY ret_metric DESC NULLS LAST,symbol) rn_ret_desc,
         row_number() OVER(PARTITION BY trade_date ORDER BY ret_metric ASC NULLS LAST,symbol) rn_ret_asc,
         count(ret_metric) OVER(PARTITION BY trade_date) return_universe
  FROM base
),
ret_cut AS (
  SELECT *,
         greatest(1,ceil(return_universe*0.01)) k_ret,
         max(CASE WHEN rn_ret_desc=greatest(1,ceil(return_universe*0.01)) THEN ret_metric END)
           OVER(PARTITION BY trade_date) ret_top_cutoff,
         max(CASE WHEN rn_ret_asc=greatest(1,ceil(return_universe*0.01)) THEN ret_metric END)
           OVER(PARTITION BY trade_date) ret_bottom_cutoff
  FROM ret_ranked
),
ret_summary AS (
  SELECT trade_date,
         max(return_universe) return_universe,
         max(k_ret) expected_return_tail_count,
         max(ret_top_cutoff) return_top_cutoff,
         max(ret_bottom_cutoff) return_bottom_cutoff,
         count(*) FILTER(WHERE ret_metric IS NOT NULL AND ret_metric>=ret_top_cutoff) actual_return_top_count,
         count(*) FILTER(WHERE ret_metric IS NOT NULL AND ret_metric<=ret_bottom_cutoff) actual_return_bottom_count
  FROM ret_cut
  GROUP BY trade_date
),
gap_ranked AS (
  SELECT *,
         row_number() OVER(PARTITION BY trade_date ORDER BY gap_metric DESC NULLS LAST,symbol) rn_gap_desc,
         row_number() OVER(PARTITION BY trade_date ORDER BY gap_metric ASC NULLS LAST,symbol) rn_gap_asc,
         count(gap_metric) OVER(PARTITION BY trade_date) gap_universe
  FROM base
),
gap_cut AS (
  SELECT *,
         greatest(1,ceil(gap_universe*0.01)) k_gap,
         max(CASE WHEN rn_gap_desc=greatest(1,ceil(gap_universe*0.01)) THEN gap_metric END)
           OVER(PARTITION BY trade_date) gap_top_cutoff,
         max(CASE WHEN rn_gap_asc=greatest(1,ceil(gap_universe*0.01)) THEN gap_metric END)
           OVER(PARTITION BY trade_date) gap_bottom_cutoff
  FROM gap_ranked
),
gap_summary AS (
  SELECT trade_date,
         max(gap_universe) gap_universe,
         max(k_gap) gap_tail_cap,
         count(*) FILTER(WHERE gap_metric>0) positive_gap_rows,
         count(*) FILTER(WHERE gap_metric<0) negative_gap_rows,
         max(gap_top_cutoff) gap_top_cutoff,
         max(gap_bottom_cutoff) gap_bottom_cutoff,
         count(*) FILTER(WHERE gap_metric>0 AND gap_metric>=gap_top_cutoff) actual_gap_up_count,
         count(*) FILTER(WHERE gap_metric<0 AND gap_metric<=gap_bottom_cutoff) actual_gap_down_count
  FROM gap_cut
  GROUP BY trade_date
)
SELECT r.trade_date,
       r.return_universe,
       g.gap_universe,
       g.positive_gap_rows,
       g.negative_gap_rows,
       r.expected_return_tail_count,
       r.actual_return_top_count,
       r.actual_return_bottom_count,
       r.return_top_cutoff,
       r.return_bottom_cutoff,
       g.gap_tail_cap,
       g.actual_gap_up_count,
       g.actual_gap_down_count,
       g.gap_top_cutoff,
       g.gap_bottom_cutoff
FROM ret_summary r
JOIN gap_summary g USING(trade_date)
ORDER BY r.trade_date
""").fetchdf()


def run(mode, symbols_arg):
    observed_at=datetime.now().astimezone().isoformat(timespec="seconds")
    con=connect()
    try:
        meta=catalog_meta(con)

        if mode=="smoke":
            req=(
                [s.strip().upper() for s in symbols_arg.split(",") if s.strip()]
                if symbols_arg else list(SMOKE_SYMBOLS)
            )
            present,missing=present_symbols(con,req)
            if not present:
                raise RuntimeError("No smoke symbols present.")
            ph=",".join("?" for _ in present)

            p=con.execute(price_event_sql(f"WHERE symbol IN ({ph})"),present).fetchdf()
            t=con.execute(turnover_sql(f"WHERE e.symbol IN ({ph})"),present).fetchdf()
            r=con.execute(extreme_return_sql(f"WHERE e.symbol IN ({ph})"),present).fetchdf()
            g=con.execute(gap_sql(f"WHERE e.symbol IN ({ph})"),present).fetchdf()
        else:
            req=[];missing=[]
            p=con.execute(price_event_sql()).fetchdf()
            t=con.execute(turnover_sql()).fetchdf()
            r=con.execute(extreme_return_sql()).fetchdf()
            g=con.execute(gap_sql()).fetchdf()

        sanity=recent_cross_section_sanity(con)
        ev=enrich(pd.concat([p,t,r,g],ignore_index=True),observed_at)
    finally:
        con.close()

    # Hard invariants.
    if ev["event_id"].duplicated().any():
        raise RuntimeError("Duplicate event_id.")
    close_mask=ev["event_phase"].eq("CLOSE") & ev["effective_date"].notna()
    if (ev.loc[close_mask,"effective_date"]<=ev.loc[close_mask,"trade_date"]).any():
        raise RuntimeError("Close-known event must not be effective on/before T.")
    gap_mask=ev["event_phase"].eq("OPEN")
    if (ev.loc[gap_mask,"effective_date"]!=ev.loc[gap_mask,"trade_date"]).any():
        raise RuntimeError("Gap event effective_date must equal T.")
    if (ev.loc[ev["event_type"].eq("GAP_UP_TOP1PCT"),"event_value"]<=0).any():
        raise RuntimeError("GAP_UP_TOP1PCT contains non-positive gap.")
    if (ev.loc[ev["event_type"].eq("GAP_DOWN_BOTTOM1PCT"),"event_value"]>=0).any():
        raise RuntimeError("GAP_DOWN_BOTTOM1PCT contains non-negative gap.")

    stamp=datetime.now().strftime("%Y%m%d_%H%M%S")
    out=OUT_ROOT/f"{mode}_{stamp}"
    out.mkdir(parents=True,exist_ok=False)

    ev.to_csv(out/"events.csv",index=False,encoding="utf-8-sig")
    ev.to_parquet(out/"events.parquet",index=False)
    sanity.to_csv(out/"recent_cross_section_sanity.csv",index=False,encoding="utf-8-sig")

    summary=(ev.groupby("event_type").agg(
        events=("event_id","count"),
        symbols=("symbol","nunique"),
        first_date=("trade_date","min"),
        last_date=("trade_date","max")
    ).reset_index() if not ev.empty else pd.DataFrame())
    summary.to_csv(out/"event_summary.csv",index=False,encoding="utf-8-sig")

    probe=None
    if mode=="smoke":
        new_types={"RETURN_TOP1PCT","RETURN_BOTTOM1PCT","GAP_UP_TOP1PCT","GAP_DOWN_BOTTOM1PCT"}
        m=(
            ev["event_type"].isin(new_types)
            & ev["trade_date"].between(pd.Timestamp("2026-09-15"),pd.Timestamp("2026-09-28"))
        )
        probe=out/"truth_probe_new_events_20260915_20260928.csv"
        ev.loc[m].sort_values(["trade_date","event_type","symbol"]).to_csv(
            probe,index=False,encoding="utf-8-sig"
        )

    counts=ev.groupby("event_type").size().sort_values(ascending=False).to_dict() if not ev.empty else {}
    manifest={
      "status":"PASS_EVENT_BASE_V0_3A",
      "generated_at":observed_at,
      "mode":mode,
      "catalog":str(CATALOG),
      "source_view":VIEW,
      "catalog_meta":meta,
      "dataset_version":DATASET_VERSION,
      "universe_id":UNIVERSE_ID,
      "strict_non_st_pit_applied":False,
      "historical_research_ready":False,
      "requested_symbols":req,
      "missing_requested_symbols":missing,
      "event_rows":int(len(ev)),
      "event_counts":counts,
      "output_dir":str(out),
      "new_event_definitions":{
        "RETURN_TOP1PCT":"metric >= empirical kth cutoff where k=ceil(1%); all ties at cutoff included",
        "RETURN_BOTTOM1PCT":"metric <= empirical kth-from-bottom cutoff; all ties at cutoff included",
        "GAP_UP_TOP1PCT":"gap >= empirical kth cutoff; all ties included; additionally gap>0",
        "GAP_DOWN_BOTTOM1PCT":"gap <= empirical kth-from-bottom cutoff; all ties included; additionally gap<0"
      },
      "time_semantics":{
        "close_events":"knowable T 15:00; effective next session OPEN",
        "gap_events":"knowable T 09:30 after opening print; effective T AFTER_OPEN; not buyable at same exact open"
      }
    }
    write_json(out/"run_manifest.json",manifest)

    print(json.dumps({
      "status":"PASS_EVENT_BASE_V0_3A",
      "mode":mode,
      "catalog_meta":meta,
      "requested_symbols":len(req),
      "missing_requested_symbols":missing,
      "event_rows":len(ev),
      "event_counts":counts,
      "output_dir":str(out),
      "truth_probe":str(probe) if probe else None,
      "sanity_file":str(out/"recent_cross_section_sanity.csv")
    },ensure_ascii=False,indent=2))
    return 0


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--mode",choices=["smoke","full"],default="smoke")
    ap.add_argument("--symbols")
    ap.add_argument("--self-test",action="store_true")
    a=ap.parse_args()

    if a.self_test:
        assert len(set(SMOKE_SYMBOLS))==len(SMOKE_SYMBOLS)
        assert "001368.SZ" in SMOKE_SYMBOLS
        assert DATASET_VERSION=="0.3a"
        print("SELF_TEST_OK")
        return 0

    return run(a.mode,a.symbols)


if __name__=="__main__":
    raise SystemExit(main())
