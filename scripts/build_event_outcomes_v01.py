from __future__ import annotations
import argparse, json, os
from datetime import datetime
from pathlib import Path

CATALOG=Path(r"D:\positionx_data\catalog\market_data.duckdb")
EVENT_ROOT=Path(r"D:\positionx_data\events\market_derived_v0_2")
OUT_ROOT=Path(r"D:\positionx_data\events\outcomes_v0_1")
PRICE_BASIS="PIT_BACK_RATIO_COMPARABLE_V1"

def qp(p): return str(p).replace("\\","/").replace("'","''")

def latest_full():
    for p in sorted([x for x in EVENT_ROOT.glob("full_*") if x.is_dir()], reverse=True):
        mf=p/"run_manifest.json"; ev=p/"events.parquet"
        if mf.exists() and ev.exists():
            m=json.loads(mf.read_text(encoding="utf-8"))
            if m.get("status")=="PASS_EVENT_BASE_V0_2" and m.get("mode")=="full":
                return p,m
    raise RuntimeError("No accepted Event Base V0.2 full build found.")

def write_json(path,obj):
    t=path.with_suffix(path.suffix+".tmp")
    t.write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    os.replace(t,path)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--event-dir")
    a=ap.parse_args()
    if a.event_dir:
        event_dir=Path(a.event_dir)
        manifest=json.loads((event_dir/"run_manifest.json").read_text(encoding="utf-8"))
    else:
        event_dir,manifest=latest_full()
    event_file=event_dir/"events.parquet"
    if manifest.get("status")!="PASS_EVENT_BASE_V0_2" or manifest.get("mode")!="full":
        raise RuntimeError("Event input is not accepted V0.2 full.")
    if not CATALOG.exists() or not event_file.exists():
        raise RuntimeError("Missing catalog or events.parquet")

    stamp=datetime.now().strftime("%Y%m%d_%H%M%S")
    out=OUT_ROOT/f"full_{stamp}"
    out.mkdir(parents=True,exist_ok=False)
    outcome=out/"event_outcomes.parquet"

    import duckdb
    con=duckdb.connect(str(CATALOG),read_only=True)
    try:
        ep,op=qp(event_file),qp(outcome)
        q=f"""
        WITH raw AS (
          SELECT symbol,trade_date,"close" raw_close,pre_close,
                 lag("close") OVER(PARTITION BY symbol ORDER BY trade_date) prev_raw_close
          FROM market.daily_bars_v1
        ),
        r AS (
          SELECT *,
                 CASE WHEN prev_raw_close IS NULL THEN 1.0
                      WHEN prev_raw_close<=0 OR pre_close<=0 THEN NULL
                      ELSE prev_raw_close/pre_close END daily_dr
          FROM raw
        ),
        b AS (
          SELECT *,
                 exp(sum(CASE WHEN daily_dr IS NULL THEN 0.0 ELSE ln(daily_dr) END)
                     OVER(PARTITION BY symbol ORDER BY trade_date
                          ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)) cum_dr
          FROM r
        ),
        p AS (
          SELECT symbol,trade_date,raw_close,raw_close*cum_dr pit_close FROM b
        ),
        f AS (
          SELECT symbol,trade_date,raw_close,pit_close,
                 lead(trade_date,1) OVER w d1, lead(trade_date,3) OVER w d3,
                 lead(trade_date,5) OVER w d5, lead(trade_date,20) OVER w d20,
                 lead(trade_date,60) OVER w d60,
                 lead(pit_close,1) OVER w c1, lead(pit_close,3) OVER w c3,
                 lead(pit_close,5) OVER w c5, lead(pit_close,20) OVER w c20,
                 lead(pit_close,60) OVER w c60,
                 max(pit_close) OVER(PARTITION BY symbol ORDER BY trade_date ROWS BETWEEN 1 FOLLOWING AND 20 FOLLOWING) mx20,
                 min(pit_close) OVER(PARTITION BY symbol ORDER BY trade_date ROWS BETWEEN 1 FOLLOWING AND 20 FOLLOWING) mn20,
                 max(pit_close) OVER(PARTITION BY symbol ORDER BY trade_date ROWS BETWEEN 1 FOLLOWING AND 60 FOLLOWING) mx60,
                 min(pit_close) OVER(PARTITION BY symbol ORDER BY trade_date ROWS BETWEEN 1 FOLLOWING AND 60 FOLLOWING) mn60
          FROM p WINDOW w AS (PARTITION BY symbol ORDER BY trade_date)
        ),
        e AS (SELECT * FROM read_parquet('{ep}'))
        SELECT e.*,
               f.raw_close outcome_event_raw_close,
               f.pit_close outcome_event_pit_close,
               f.d1 date_1d,f.d3 date_3d,f.d5 date_5d,f.d20 date_20d,f.d60 date_60d,
               CASE WHEN f.c1  IS NOT NULL THEN f.c1 /f.pit_close-1 END ret_1d,
               CASE WHEN f.c3  IS NOT NULL THEN f.c3 /f.pit_close-1 END ret_3d,
               CASE WHEN f.c5  IS NOT NULL THEN f.c5 /f.pit_close-1 END ret_5d,
               CASE WHEN f.c20 IS NOT NULL THEN f.c20/f.pit_close-1 END ret_20d,
               CASE WHEN f.c60 IS NOT NULL THEN f.c60/f.pit_close-1 END ret_60d,
               CASE WHEN f.d20 IS NOT NULL THEN f.mx20/f.pit_close-1 END mfe_20d,
               CASE WHEN f.d20 IS NOT NULL THEN f.mn20/f.pit_close-1 END mae_20d,
               CASE WHEN f.d60 IS NOT NULL THEN f.mx60/f.pit_close-1 END mfe_60d,
               CASE WHEN f.d60 IS NOT NULL THEN f.mn60/f.pit_close-1 END mae_60d,
               f.d1 IS NOT NULL complete_1d,
               f.d5 IS NOT NULL complete_5d,
               f.d20 IS NOT NULL complete_20d,
               f.d60 IS NOT NULL complete_60d,
               '{PRICE_BASIS}' outcome_price_basis,
               'FUTURE_LABEL_ONLY_DO_NOT_USE_AS_SIGNAL' leakage_guard
        FROM e LEFT JOIN f ON e.symbol=f.symbol AND e.trade_date=f.trade_date
        """
        print("BUILD event_outcomes.parquet ...")
        con.execute(f"COPY ({q}) TO '{op}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        base=f"read_parquet('{op}')"
        st=con.execute(f"""SELECT count(*),count(distinct event_id),
             count(*)-count(distinct event_id),
             count(*) filter(where outcome_event_pit_close is null),
             count(*) filter(where complete_1d),
             count(*) filter(where complete_5d),
             count(*) filter(where complete_20d),
             count(*) filter(where complete_60d)
             FROM {base}""").fetchone()
        if st[2] or st[3]:
            raise RuntimeError(f"Validation failed duplicates={st[2]} missing_price={st[3]}")

        def csv(sql,name):
            con.execute(f"COPY ({sql}) TO '{qp(out/name)}' (HEADER, DELIMITER ',')")

        csv(f"""SELECT event_type,count(*) events,
            count(*) filter(where complete_20d) n20,
            avg(ret_1d) filter(where complete_1d) avg_ret_1d,
            median(ret_1d) filter(where complete_1d) median_ret_1d,
            avg(ret_5d) filter(where complete_5d) avg_ret_5d,
            median(ret_5d) filter(where complete_5d) median_ret_5d,
            avg(ret_20d) filter(where complete_20d) avg_ret_20d,
            median(ret_20d) filter(where complete_20d) median_ret_20d,
            avg(case when ret_20d>0 then 1.0 else 0.0 end) filter(where complete_20d) win_rate_20d,
            avg(mfe_20d) filter(where complete_20d) avg_mfe_20d,
            avg(mae_20d) filter(where complete_20d) avg_mae_20d,
            count(*) filter(where complete_60d) n60,
            avg(ret_60d) filter(where complete_60d) avg_ret_60d,
            median(ret_60d) filter(where complete_60d) median_ret_60d
            FROM {base} GROUP BY event_type ORDER BY event_type""","event_type_summary.csv")

        csv(f"""SELECT event_type,first_in_60d,
            count(*) filter(where complete_20d) n20,
            avg(ret_20d) filter(where complete_20d) avg_ret_20d,
            median(ret_20d) filter(where complete_20d) median_ret_20d,
            avg(case when ret_20d>0 then 1.0 else 0.0 end) filter(where complete_20d) win_rate_20d,
            avg(mfe_20d) filter(where complete_20d) avg_mfe_20d,
            avg(mae_20d) filter(where complete_20d) avg_mae_20d
            FROM {base} GROUP BY event_type,first_in_60d
            ORDER BY event_type,first_in_60d DESC""","event_first60_summary.csv")

        csv(f"""WITH x AS (
              SELECT *,CASE WHEN consecutive_count>=5 THEN '5+' ELSE cast(consecutive_count as varchar) END bucket
              FROM {base})
            SELECT event_type,bucket,
            count(*) filter(where complete_20d) n20,
            avg(ret_20d) filter(where complete_20d) avg_ret_20d,
            median(ret_20d) filter(where complete_20d) median_ret_20d,
            avg(case when ret_20d>0 then 1.0 else 0.0 end) filter(where complete_20d) win_rate_20d,
            avg(mfe_20d) filter(where complete_20d) avg_mfe_20d,
            avg(mae_20d) filter(where complete_20d) avg_mae_20d
            FROM x GROUP BY event_type,bucket ORDER BY event_type,bucket""","event_streak_summary.csv")

        result={
          "status":"PASS_EVENT_OUTCOMES_V0_1",
          "generated_at":datetime.now().astimezone().isoformat(timespec="seconds"),
          "event_source_dir":str(event_dir),
          "event_source_rows":int(manifest["event_rows"]),
          "outcome_rows":int(st[0]),
          "unique_event_ids":int(st[1]),
          "duplicate_event_ids":int(st[2]),
          "missing_event_price":int(st[3]),
          "complete_1d":int(st[4]),"complete_5d":int(st[5]),
          "complete_20d":int(st[6]),"complete_60d":int(st[7]),
          "outcome_price_basis":PRICE_BASIS,
          "labels_are_future_information":True,
          "alpha_not_in_v0_1":True,
          "output_dir":str(out)
        }
        write_json(out/"run_manifest.json",result)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    finally:
        con.close()

if __name__=="__main__":
    raise SystemExit(main())
