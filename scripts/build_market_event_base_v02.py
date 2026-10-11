from __future__ import annotations

"""
PositionX — Market-Derived Event Base V0.2

Changes from V0.1
-----------------
Price-level events now use PIT-safe comparable prices derived from accepted RAW daily bars:

    daily_dr(T) = close(T-1) / pre_close(T)
    cum_dr(T)   = product(daily_dr through T)
    pit_close   = raw_close * cum_dr

Future corporate actions never rewrite past dates.

Price event types:
- PRICE_52W_HIGH / PRICE_52W_LOW
- PRICE_ALL_TIME_HIGH / PRICE_ALL_TIME_LOW

Cross-sectional turnover event types remain based on RAW amount:
- TURNOVER_TOP5
- TURNOVER_TOP1PCT

Historical ST PIT remains incomplete, so this is an event-fact layer, not a final
historical strategy-eligibility layer.
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
OUT_ROOT = Path(r"D:\positionx_data\events\market_derived_v0_2")
VIEW = "market.daily_bars_v1"
UNIVERSE_ID = "A_SHARE_SHSZ_RAW_DAILY_V1"
EVENT_VERSION = "0.2"
PRICE_BASIS = "PIT_BACK_RATIO_COMPARABLE_V1"

SMOKE_SYMBOLS = [
"603986.SH","601689.SH","002050.SZ","600699.SH","688017.SH","603667.SH","603009.SH",
"300308.SZ","300502.SZ","601138.SH","688041.SH","300476.SZ","603019.SH","603259.SH",
"002821.SZ","301080.SZ","688235.SH","600276.SH","688506.SH","001368.SZ",
"000001.SZ","000333.SZ","002594.SZ","300750.SZ","600000.SH","600519.SH","601318.SH",
"688981.SH","002415.SZ","000858.SZ","601398.SH","601857.SH","601888.SH","600036.SH",
"000725.SZ","300274.SZ","002230.SZ","600030.SH","601012.SH","000651.SZ"
]


def safe(v: Any) -> Any:
    if v is None or isinstance(v,(str,int,float,bool)):
        if isinstance(v,float) and not math.isfinite(v): return None
        return v
    if hasattr(v,"item"):
        try:return safe(v.item())
        except Exception:pass
    return str(v)


def write_json(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp=p.with_suffix(p.suffix+".tmp")
    tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=safe),encoding="utf-8")
    os.replace(tmp,p)


def connect():
    if not CATALOG.exists():
        raise RuntimeError(f"Catalog not found: {CATALOG}")
    import duckdb
    return duckdb.connect(str(CATALOG),read_only=True)


def catalog_meta(con):
    r=con.execute(f"select count(*),count(distinct symbol),min(trade_date),max(trade_date) from {VIEW}").fetchone()
    return {"rows":int(r[0]),"symbols":int(r[1]),"first_date":str(r[2]),"last_date":str(r[3])}


def present_symbols(con, req: Sequence[str]):
    if not req:return [],[]
    q=",".join("?" for _ in req)
    got={r[0] for r in con.execute(f"select distinct symbol from {VIEW} where symbol in ({q})",list(req)).fetchall()}
    return [s for s in req if s in got],[s for s in req if s not in got]


def price_sql(filter_sql=""):
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
    pre_close * cum_dr AS pit_pre_close,
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
  SELECT trade_date,lead(trade_date) OVER(ORDER BY trade_date) next_trade_date
  FROM (SELECT DISTINCT trade_date FROM {VIEW})
),
ev AS (
  SELECT symbol,trade_date,trade_idx,"close" raw_close,pit_close event_value,prev250max reference_value,
         daily_dr,cum_dr,'PRICE_52W_HIGH' event_type
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
  md5(symbol||'|'||event_type||'|'||strftime(trade_date,'%Y%m%d')||'|0.2') event_id,
  symbol,trade_date,event_type,
  cast(event_value AS DOUBLE) event_value,
  cast(reference_value AS DOUBLE) reference_value,
  cast(raw_close AS DOUBLE) raw_close,
  cast(event_value AS DOUBLE) pit_close,
  cast(daily_dr AS DOUBLE) daily_dr,
  cast(cum_dr AS DOUBLE) cum_dr,
  '{PRICE_BASIS}' AS price_basis,
  cast(NULL AS BIGINT) market_rank,
  cast(NULL AS DOUBLE) percentile,
  cast(NULL AS BIGINT) universe_size,
  trade_idx,
  trade_date+interval 15 hour event_knowable_at,
  c.next_trade_date effective_date
FROM ev JOIN cal c USING(trade_date)
"""


def turnover_sql(output_filter=""):
    return f"""
WITH ranked AS (
  SELECT symbol,trade_date,amount,
    row_number() OVER(PARTITION BY trade_date ORDER BY amount DESC,symbol) amount_rank,
    count(*) OVER(PARTITION BY trade_date) universe_size
  FROM {VIEW}
  WHERE amount>0
),
idx AS (
  SELECT symbol,trade_date,row_number() OVER(PARTITION BY symbol ORDER BY trade_date) trade_idx
  FROM {VIEW}
),
cal AS (
  SELECT trade_date,lead(trade_date) OVER(ORDER BY trade_date) next_trade_date
  FROM (SELECT DISTINCT trade_date FROM {VIEW})
),
ev AS (
  SELECT r.*,'TURNOVER_TOP5' event_type FROM ranked r WHERE amount_rank<=5
  UNION ALL
  SELECT r.*,'TURNOVER_TOP1PCT' FROM ranked r
  WHERE amount_rank<=greatest(1,ceil(universe_size*0.01))
)
SELECT
  md5(e.symbol||'|'||e.event_type||'|'||strftime(e.trade_date,'%Y%m%d')||'|0.2') event_id,
  e.symbol,e.trade_date,e.event_type,
  cast(e.amount AS DOUBLE) event_value,
  cast(NULL AS DOUBLE) reference_value,
  cast(NULL AS DOUBLE) raw_close,
  cast(NULL AS DOUBLE) pit_close,
  cast(NULL AS DOUBLE) daily_dr,
  cast(NULL AS DOUBLE) cum_dr,
  cast(NULL AS VARCHAR) price_basis,
  cast(e.amount_rank AS BIGINT) market_rank,
  1.0-((e.amount_rank-1.0)/e.universe_size) percentile,
  cast(e.universe_size AS BIGINT) universe_size,
  i.trade_idx,
  e.trade_date+interval 15 hour event_knowable_at,
  c.next_trade_date effective_date
FROM ev e
JOIN idx i USING(symbol,trade_date)
JOIN cal c USING(trade_date)
{output_filter}
"""


def enrich(df, observed_at):
    if df.empty:return df
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
    d["event_version"]=EVENT_VERSION
    d["source"]="DERIVED_FROM_MARKET.DAILY_BARS_V1"
    d["observation_mode"]="HISTORICAL_RECONSTRUCTION_FROM_ACCEPTED_MARKET_DATA"
    d["observed_at"]=pd.Timestamp(observed_at)
    d["strict_non_st_pit_applied"]=False
    return d


def run(mode, symbols_arg):
    obs=datetime.now().astimezone().isoformat(timespec="seconds")
    con=connect()
    try:
        meta=catalog_meta(con)
        if mode=="smoke":
            req=[s.strip().upper() for s in symbols_arg.split(",") if s.strip()] if symbols_arg else list(SMOKE_SYMBOLS)
            present,missing=present_symbols(con,req)
            if not present:raise RuntimeError("No smoke symbols present.")
            ph=",".join("?" for _ in present)
            p=con.execute(price_sql(f"WHERE symbol IN ({ph})"),present).fetchdf()
            t=con.execute(turnover_sql(f"WHERE e.symbol IN ({ph})"),present).fetchdf()
        else:
            req=[];missing=[]
            p=con.execute(price_sql()).fetchdf()
            t=con.execute(turnover_sql()).fetchdf()
        ev=enrich(pd.concat([p,t],ignore_index=True),obs)
    finally:
        con.close()

    if ev["event_id"].duplicated().any():raise RuntimeError("Duplicate event_id.")
    if (ev["effective_date"].notna()&(ev["effective_date"]<=ev["trade_date"])).any():
        raise RuntimeError("effective_date invariant failed.")

    stamp=datetime.now().strftime("%Y%m%d_%H%M%S")
    out=OUT_ROOT/f"{mode}_{stamp}"
    out.mkdir(parents=True,exist_ok=False)
    ev.to_csv(out/"events.csv",index=False,encoding="utf-8-sig")
    ev.to_parquet(out/"events.parquet",index=False)

    summary=(ev.groupby("event_type").agg(
        events=("event_id","count"),symbols=("symbol","nunique"),
        first_date=("trade_date","min"),last_date=("trade_date","max")
    ).reset_index() if not ev.empty else pd.DataFrame())
    summary.to_csv(out/"event_summary.csv",index=False,encoding="utf-8-sig")

    probe_file=None
    if mode=="smoke" and "001368.SZ" in req:
        m=(ev["symbol"].eq("001368.SZ")&
           ev["trade_date"].between(pd.Timestamp("2026-09-15"),pd.Timestamp("2026-09-25")))
        probe_file=out/"truth_probe_001368_SZ_20260915_20260925.csv"
        ev.loc[m].to_csv(probe_file,index=False,encoding="utf-8-sig")

    counts=ev.groupby("event_type").size().sort_values(ascending=False).to_dict() if not ev.empty else {}
    manifest={
      "status":"PASS_EVENT_BASE_V0_2","generated_at":obs,"mode":mode,
      "catalog":str(CATALOG),"source_view":VIEW,"catalog_meta":meta,
      "universe_id":UNIVERSE_ID,"event_version":EVENT_VERSION,
      "price_basis_for_price_events":PRICE_BASIS,
      "future_action_rewrites_past":False,
      "turnover_basis":"RAW_AMOUNT",
      "strict_non_st_pit_applied":False,"historical_research_ready":False,
      "requested_symbols":req,"missing_requested_symbols":missing,
      "event_rows":int(len(ev)),"event_counts":counts,"output_dir":str(out),
      "definitions":{
        "PRICE_52W_HIGH":"pit_close(T) > max(previous 250 pit_close), requires 250 prior observations",
        "PRICE_52W_LOW":"pit_close(T) < min(previous 250 pit_close), requires 250 prior observations",
        "PRICE_ALL_TIME_HIGH":"pit_close(T) > all prior pit_close; first observation excluded",
        "PRICE_ALL_TIME_LOW":"pit_close(T) < all prior pit_close; first observation excluded",
        "TURNOVER_TOP5":"RAW amount rank <=5 among amount>0 rows on trade_date",
        "TURNOVER_TOP1PCT":"RAW amount rank <=ceil(universe_size*1%) among amount>0 rows on trade_date"
      }
    }
    write_json(out/"run_manifest.json",manifest)
    print(json.dumps({
      "status":"PASS_EVENT_BASE_V0_2","mode":mode,"catalog_meta":meta,
      "requested_symbols":len(req),"missing_requested_symbols":missing,
      "event_rows":len(ev),"event_counts":counts,"output_dir":str(out),
      "truth_probe":str(probe_file) if probe_file else None
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
        print("SELF_TEST_OK")
        return 0
    return run(a.mode,a.symbols)


if __name__=="__main__":
    raise SystemExit(main())
