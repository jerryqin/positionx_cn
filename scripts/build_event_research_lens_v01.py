from __future__ import annotations

"""
PositionX — Event Research Lens V0.1

Purpose
-------
Turn Event Outcome Base V0.1 into a more defensible research view by adding:

1) date-matched whole-market forward-return baselines;
2) event alpha vs market median/mean;
3) cluster-start (consecutive_count=1) summaries;
4) first-in-60d summaries;
5) 5-year stability summaries;
6) selected same-day event-combination summaries.

This is research-only. No broker, execution, production SQLite, or catalog mutation.
"""

import argparse, json, os
from datetime import datetime
from pathlib import Path

CATALOG = Path(r"D:\positionx_data\catalog\market_data.duckdb")
OUTCOME_ROOT = Path(r"D:\positionx_data\events\outcomes_v0_1")
OUT_ROOT = Path(r"D:\positionx_data\events\research_lens_v0_1")
PRICE_BASIS = "PIT_BACK_RATIO_COMPARABLE_V1"

def qp(p: Path) -> str:
    return str(p).replace("\\", "/").replace("'", "''")

def latest_outcomes():
    for p in sorted([x for x in OUTCOME_ROOT.glob("full_*") if x.is_dir()], reverse=True):
        mf=p/"run_manifest.json"; f=p/"event_outcomes.parquet"
        if mf.exists() and f.exists():
            m=json.loads(mf.read_text(encoding="utf-8"))
            if m.get("status")=="PASS_EVENT_OUTCOMES_V0_1":
                return p,m
    raise RuntimeError("No accepted Event Outcome V0.1 full build found.")

def write_json(path,obj):
    t=path.with_suffix(path.suffix+".tmp")
    t.write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    os.replace(t,path)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--outcome-dir")
    a=ap.parse_args()

    if a.outcome_dir:
        od=Path(a.outcome_dir)
        om=json.loads((od/"run_manifest.json").read_text(encoding="utf-8"))
    else:
        od,om=latest_outcomes()
    of=od/"event_outcomes.parquet"
    if om.get("status")!="PASS_EVENT_OUTCOMES_V0_1" or not of.exists():
        raise RuntimeError("Outcome input is not accepted V0.1.")
    if not CATALOG.exists():
        raise RuntimeError("Market catalog missing.")

    stamp=datetime.now().strftime("%Y%m%d_%H%M%S")
    out=OUT_ROOT/f"full_{stamp}"
    out.mkdir(parents=True,exist_ok=False)

    alpha_file=out/"event_outcomes_alpha.parquet"
    import duckdb
    con=duckdb.connect(str(CATALOG),read_only=True)
    try:
        op=qp(of); apath=qp(alpha_file)

        # Rebuild comparable price for all market rows, then compute market-date baselines.
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
        px AS (
          SELECT symbol,trade_date,raw_close*cum_dr pit_close FROM b
        ),
        fw AS (
          SELECT symbol,trade_date,pit_close,
                 lead(pit_close,1)  OVER w c1,
                 lead(pit_close,5)  OVER w c5,
                 lead(pit_close,20) OVER w c20,
                 lead(pit_close,60) OVER w c60
          FROM px
          WINDOW w AS (PARTITION BY symbol ORDER BY trade_date)
        ),
        rets AS (
          SELECT symbol,trade_date,
                 CASE WHEN c1 IS NOT NULL THEN c1/pit_close-1 END r1,
                 CASE WHEN c5 IS NOT NULL THEN c5/pit_close-1 END r5,
                 CASE WHEN c20 IS NOT NULL THEN c20/pit_close-1 END r20,
                 CASE WHEN c60 IS NOT NULL THEN c60/pit_close-1 END r60
          FROM fw
        ),
        market AS (
          SELECT trade_date,
                 avg(r1) market_mean_ret_1d,
                 median(r1) market_median_ret_1d,
                 avg(r5) market_mean_ret_5d,
                 median(r5) market_median_ret_5d,
                 avg(r20) market_mean_ret_20d,
                 median(r20) market_median_ret_20d,
                 avg(r60) market_mean_ret_60d,
                 median(r60) market_median_ret_60d,
                 count(r20) market_n20
          FROM rets
          GROUP BY trade_date
        ),
        e AS (
          SELECT * FROM read_parquet('{op}')
        )
        SELECT e.*,
               m.market_mean_ret_1d,m.market_median_ret_1d,
               m.market_mean_ret_5d,m.market_median_ret_5d,
               m.market_mean_ret_20d,m.market_median_ret_20d,
               m.market_mean_ret_60d,m.market_median_ret_60d,
               m.market_n20,
               e.ret_1d-m.market_median_ret_1d alpha_median_1d,
               e.ret_5d-m.market_median_ret_5d alpha_median_5d,
               e.ret_20d-m.market_median_ret_20d alpha_median_20d,
               e.ret_60d-m.market_median_ret_60d alpha_median_60d,
               e.ret_1d-m.market_mean_ret_1d alpha_mean_1d,
               e.ret_5d-m.market_mean_ret_5d alpha_mean_5d,
               e.ret_20d-m.market_mean_ret_20d alpha_mean_20d,
               e.ret_60d-m.market_mean_ret_60d alpha_mean_60d,
               (consecutive_count=1) cluster_start,
               CASE
                 WHEN year(trade_date)<1995 THEN '1990-1994'
                 WHEN year(trade_date)<2000 THEN '1995-1999'
                 WHEN year(trade_date)<2005 THEN '2000-2004'
                 WHEN year(trade_date)<2010 THEN '2005-2009'
                 WHEN year(trade_date)<2015 THEN '2010-2014'
                 WHEN year(trade_date)<2020 THEN '2015-2019'
                 WHEN year(trade_date)<2025 THEN '2020-2024'
                 ELSE '2025-2026'
               END era_5y
        FROM e
        LEFT JOIN market m USING(trade_date)
        """
        print("BUILD date-matched market baselines and alpha ...")
        con.execute(f"COPY ({q}) TO '{apath}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        base=f"read_parquet('{apath}')"

        st=con.execute(f"""SELECT count(*),count(distinct event_id),
            count(*)-count(distinct event_id),
            count(*) filter(where complete_20d and alpha_median_20d is null)
            FROM {base}""").fetchone()
        if st[2] or st[3]:
            raise RuntimeError(f"Validation failed duplicate={st[2]} missing_alpha20={st[3]}")

        def csv(sql,name):
            con.execute(f"COPY ({sql}) TO '{qp(out/name)}' (HEADER, DELIMITER ',')")

        metrics = """
            count(*) FILTER(WHERE complete_20d) n20,
            avg(ret_20d) FILTER(WHERE complete_20d) avg_ret20,
            median(ret_20d) FILTER(WHERE complete_20d) median_ret20,
            avg(alpha_median_20d) FILTER(WHERE complete_20d) avg_alpha20,
            median(alpha_median_20d) FILTER(WHERE complete_20d) median_alpha20,
            avg(CASE WHEN alpha_median_20d>0 THEN 1.0 ELSE 0.0 END)
                FILTER(WHERE complete_20d) alpha_win20,
            quantile_cont(alpha_median_20d,0.25) FILTER(WHERE complete_20d) alpha_q25,
            quantile_cont(alpha_median_20d,0.75) FILTER(WHERE complete_20d) alpha_q75,
            avg(mfe_20d) FILTER(WHERE complete_20d) avg_mfe20,
            avg(mae_20d) FILTER(WHERE complete_20d) avg_mae20
        """

        csv(f"""SELECT event_type,{metrics}
                FROM {base} GROUP BY event_type ORDER BY event_type""",
            "alpha_event_type_summary.csv")

        csv(f"""SELECT event_type,{metrics}
                FROM {base}
                WHERE cluster_start
                GROUP BY event_type ORDER BY event_type""",
            "alpha_cluster_start_summary.csv")

        csv(f"""SELECT event_type,first_in_60d,{metrics}
                FROM {base}
                GROUP BY event_type,first_in_60d
                ORDER BY event_type,first_in_60d DESC""",
            "alpha_first60_summary.csv")

        csv(f"""SELECT event_type,era_5y,{metrics}
                FROM {base}
                GROUP BY event_type,era_5y
                ORDER BY event_type,era_5y""",
            "era_stability_summary.csv")

        # Same symbol/date combinations. Outcomes are identical across event rows on a date,
        # so collapse once per symbol/date and construct selected combination labels.
        csv(f"""
            WITH d AS (
              SELECT symbol,trade_date,
                bool_or(event_type='PRICE_52W_HIGH') h52,
                bool_or(event_type='PRICE_52W_LOW') l52,
                bool_or(event_type='PRICE_ALL_TIME_HIGH') ath,
                bool_or(event_type='PRICE_ALL_TIME_LOW') atl,
                bool_or(event_type='TURNOVER_TOP1PCT') top1,
                bool_or(event_type='TURNOVER_TOP5') top5,
                max(ret_20d) ret_20d,
                max(alpha_median_20d) alpha_median_20d,
                max(mfe_20d) mfe_20d,
                max(mae_20d) mae_20d,
                bool_or(complete_20d) complete_20d
              FROM {base}
              GROUP BY symbol,trade_date
            ),
            combos AS (
              SELECT *,
                unnest([
                  CASE WHEN ath AND top1 THEN 'ATH_AND_TOP1PCT' END,
                  CASE WHEN ath AND top5 THEN 'ATH_AND_TOP5' END,
                  CASE WHEN h52 AND top1 THEN '52WH_AND_TOP1PCT' END,
                  CASE WHEN h52 AND top5 THEN '52WH_AND_TOP5' END,
                  CASE WHEN atl AND top1 THEN 'ATL_AND_TOP1PCT' END,
                  CASE WHEN l52 AND top1 THEN '52WL_AND_TOP1PCT' END
                ]) combo
              FROM d
            )
            SELECT combo,
              count(*) FILTER(WHERE complete_20d) n20,
              avg(ret_20d) FILTER(WHERE complete_20d) avg_ret20,
              median(ret_20d) FILTER(WHERE complete_20d) median_ret20,
              avg(alpha_median_20d) FILTER(WHERE complete_20d) avg_alpha20,
              median(alpha_median_20d) FILTER(WHERE complete_20d) median_alpha20,
              avg(CASE WHEN alpha_median_20d>0 THEN 1.0 ELSE 0.0 END)
                FILTER(WHERE complete_20d) alpha_win20,
              avg(mfe_20d) FILTER(WHERE complete_20d) avg_mfe20,
              avg(mae_20d) FILTER(WHERE complete_20d) avg_mae20
            FROM combos
            WHERE combo IS NOT NULL
            GROUP BY combo
            ORDER BY combo
            ""","event_combo_summary.csv")

        result={
          "status":"PASS_EVENT_RESEARCH_LENS_V0_1",
          "generated_at":datetime.now().astimezone().isoformat(timespec="seconds"),
          "outcome_source_dir":str(od),
          "rows":int(st[0]),
          "unique_event_ids":int(st[1]),
          "duplicate_event_ids":int(st[2]),
          "missing_alpha20_on_complete20":int(st[3]),
          "benchmark":"same-trade-date whole-market cross-sectional median/mean forward return",
          "preferred_alpha":"alpha_median_20d",
          "price_basis":PRICE_BASIS,
          "strict_non_st_pit_applied":False,
          "historical_research_ready":False,
          "output_dir":str(out)
        }
        write_json(out/"run_manifest.json",result)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    finally:
        con.close()

if __name__=="__main__":
    raise SystemExit(main())
