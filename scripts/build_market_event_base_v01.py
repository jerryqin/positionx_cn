from __future__ import annotations
import argparse, json, math, os
from datetime import datetime
from pathlib import Path
import pandas as pd

CATALOG = Path(r"D:\positionx_data\catalog\market_data.duckdb")
OUT_ROOT = Path(r"D:\positionx_data\events\market_derived_v0_1")
VIEW = "market.daily_bars_v1"
UNIVERSE_ID = "A_SHARE_SHSZ_RAW_DAILY_V1"
VERSION = "0.1"

SMOKE_SYMBOLS = [
"603986.SH","601689.SH","002050.SZ","600699.SH","688017.SH","603667.SH","603009.SH",
"300308.SZ","300502.SZ","601138.SH","688041.SH","300476.SZ","603019.SH","603259.SH",
"002821.SZ","301080.SZ","688235.SH","600276.SH","688506.SH","001368.SZ",
"000001.SZ","000333.SZ","002594.SZ","300750.SZ","600000.SH","600519.SH","601318.SH",
"688981.SH","002415.SZ","000858.SZ","601398.SH","601857.SH","601888.SH","600036.SH",
"000725.SZ","300274.SZ","002230.SZ","600030.SH","601012.SH","000651.SZ"
]

def safe(v):
    if v is None or isinstance(v,(str,int,float,bool)):
        if isinstance(v,float) and not math.isfinite(v): return None
        return v
    if hasattr(v,"item"):
        try:return safe(v.item())
        except:pass
    return str(v)

def write_json(p,obj):
    p.parent.mkdir(parents=True,exist_ok=True)
    t=p.with_suffix(p.suffix+".tmp")
    t.write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=safe),encoding="utf-8")
    os.replace(t,p)

def connect():
    if not CATALOG.exists(): raise RuntimeError(f"Catalog not found: {CATALOG}")
    import duckdb
    return duckdb.connect(str(CATALOG),read_only=True)

def catalog_meta(con):
    r=con.execute(f"""select count(*),count(distinct symbol),min(trade_date),max(trade_date) from {VIEW}""").fetchone()
    return {"rows":int(r[0]),"symbols":int(r[1]),"first_date":str(r[2]),"last_date":str(r[3])}

def present_symbols(con,req):
    if not req:return [],[]
    q=",".join("?" for _ in req)
    got={r[0] for r in con.execute(f"select distinct symbol from {VIEW} where symbol in ({q})",req).fetchall()}
    return [s for s in req if s in got],[s for s in req if s not in got]

def price_sql(filter_sql=""):
    return f"""
with x as (
 select symbol,trade_date,"close",
   row_number() over(partition by symbol order by trade_date) trade_idx,
   count(*) over(partition by symbol order by trade_date rows between 250 preceding and 1 preceding) prior250,
   max("close") over(partition by symbol order by trade_date rows between 250 preceding and 1 preceding) prev250max,
   min("close") over(partition by symbol order by trade_date rows between 250 preceding and 1 preceding) prev250min,
   max("close") over(partition by symbol order by trade_date rows between unbounded preceding and 1 preceding) prevallmax,
   min("close") over(partition by symbol order by trade_date rows between unbounded preceding and 1 preceding) prevallmin
 from {VIEW} {filter_sql}
), cal as (
 select trade_date,lead(trade_date) over(order by trade_date) next_trade_date
 from (select distinct trade_date from {VIEW})
), ev as (
 select symbol,trade_date,trade_idx,"close" event_value,prev250max reference_value,'PRICE_52W_HIGH' event_type
 from x where prior250=250 and "close">prev250max
 union all
 select symbol,trade_date,trade_idx,"close",prev250min,'PRICE_52W_LOW'
 from x where prior250=250 and "close"<prev250min
 union all
 select symbol,trade_date,trade_idx,"close",prevallmax,'PRICE_ALL_TIME_HIGH'
 from x where trade_idx>1 and "close">prevallmax
 union all
 select symbol,trade_date,trade_idx,"close",prevallmin,'PRICE_ALL_TIME_LOW'
 from x where trade_idx>1 and "close"<prevallmin
)
select md5(symbol||'|'||event_type||'|'||strftime(trade_date,'%Y%m%d')) event_id,
 symbol,trade_date,event_type,cast(event_value as double) event_value,
 cast(reference_value as double) reference_value,
 cast(null as bigint) market_rank,cast(null as double) percentile,cast(null as bigint) universe_size,
 trade_idx,trade_date+interval 15 hour event_knowable_at,c.next_trade_date effective_date
from ev join cal c using(trade_date)
"""

def turnover_sql(output_filter=""):
    return f"""
with ranked as (
 select symbol,trade_date,amount,
   row_number() over(partition by trade_date order by amount desc,symbol) amount_rank,
   count(*) over(partition by trade_date) universe_size
 from {VIEW} where amount>0
), idx as (
 select symbol,trade_date,row_number() over(partition by symbol order by trade_date) trade_idx from {VIEW}
), cal as (
 select trade_date,lead(trade_date) over(order by trade_date) next_trade_date
 from (select distinct trade_date from {VIEW})
), ev as (
 select r.*,'TURNOVER_TOP5' event_type from ranked r where amount_rank<=5
 union all
 select r.*,'TURNOVER_TOP1PCT' from ranked r where amount_rank<=greatest(1,ceil(universe_size*0.01))
)
select md5(e.symbol||'|'||e.event_type||'|'||strftime(e.trade_date,'%Y%m%d')) event_id,
 e.symbol,e.trade_date,e.event_type,cast(e.amount as double) event_value,
 cast(null as double) reference_value,cast(e.amount_rank as bigint) market_rank,
 1.0-((e.amount_rank-1.0)/e.universe_size) percentile,cast(e.universe_size as bigint) universe_size,
 i.trade_idx,e.trade_date+interval 15 hour event_knowable_at,c.next_trade_date effective_date
from ev e join idx i using(symbol,trade_date) join cal c using(trade_date)
{output_filter}
"""

def enrich(df,observed_at):
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
    breaks=(gap.isna()|(gap!=1))
    groups=breaks.groupby([d["symbol"],d["event_type"]]).cumsum()
    d["consecutive_count"]=d.groupby([d["symbol"],d["event_type"],groups]).cumcount()+1
    d["universe_id"]=UNIVERSE_ID
    d["event_version"]=VERSION
    d["source"]="DERIVED_FROM_MARKET.DAILY_BARS_V1"
    d["observation_mode"]="HISTORICAL_RECONSTRUCTION_FROM_ACCEPTED_MARKET_DATA"
    d["observed_at"]=pd.Timestamp(observed_at)
    d["strict_non_st_pit_applied"]=False
    return d

def run(mode,symbols_arg):
    obs=datetime.now().astimezone().isoformat(timespec="seconds")
    con=connect()
    try:
        meta=catalog_meta(con)
        if mode=="smoke":
            req=[s.strip().upper() for s in symbols_arg.split(",") if s.strip()] if symbols_arg else list(SMOKE_SYMBOLS)
            present,missing=present_symbols(con,req)
            if not present: raise RuntimeError("No smoke symbols present in catalog.")
            ph=",".join("?" for _ in present)
            p=con.execute(price_sql(f"where symbol in ({ph})"),present).fetchdf()
            t=con.execute(turnover_sql(f"where e.symbol in ({ph})"),present).fetchdf()
        else:
            req=[];missing=[]
            p=con.execute(price_sql()).fetchdf()
            t=con.execute(turnover_sql()).fetchdf()
        ev=enrich(pd.concat([p,t],ignore_index=True),obs)
    finally:
        con.close()

    if ev["event_id"].duplicated().any(): raise RuntimeError("Duplicate event_id.")
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

    if mode=="smoke" and "001368.SZ" in req:
        m=(ev["symbol"].eq("001368.SZ")&
           ev["trade_date"].between(pd.Timestamp("2026-09-15"),pd.Timestamp("2026-09-25")))
        ev.loc[m].to_csv(out/"truth_probe_001368_SZ_20260915_20260925.csv",index=False,encoding="utf-8-sig")

    defs={
      "PRICE_52W_HIGH":"close > max(previous 250 symbol observations close), requires 250 prior observations",
      "PRICE_52W_LOW":"close < min(previous 250 symbol observations close), requires 250 prior observations",
      "PRICE_ALL_TIME_HIGH":"close > all prior closes; first observation excluded",
      "PRICE_ALL_TIME_LOW":"close < all prior closes; first observation excluded",
      "TURNOVER_TOP5":"amount rank <= 5 among amount>0 rows on trade_date",
      "TURNOVER_TOP1PCT":"amount rank <= ceil(universe_size*1%) among amount>0 rows on trade_date"
    }
    write_json(out/"run_manifest.json",{
      "generated_at":obs,"mode":mode,"catalog":str(CATALOG),"source_view":VIEW,
      "catalog_meta":meta,"universe_id":UNIVERSE_ID,"event_version":VERSION,
      "strict_non_st_pit_applied":False,"historical_research_ready":False,
      "definitions":defs,"requested_symbols":req,"missing_requested_symbols":missing,
      "event_rows":int(len(ev)),"output_dir":str(out)
    })

    counts=ev.groupby("event_type").size().sort_values(ascending=False).to_dict() if not ev.empty else {}
    print(json.dumps({
      "status":"PASS_EVENT_BASE_V0_1","mode":mode,"catalog_meta":meta,
      "requested_symbols":len(req),"missing_requested_symbols":missing,
      "event_rows":len(ev),"event_counts":counts,"output_dir":str(out),
      "truth_probe":str(out/"truth_probe_001368_SZ_20260915_20260925.csv") if mode=="smoke" and "001368.SZ" in req else None
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
        print("SELF_TEST_OK");return 0
    return run(a.mode,a.symbols)

if __name__=="__main__":
    raise SystemExit(main())
