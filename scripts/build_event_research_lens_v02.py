from __future__ import annotations

r"""
PositionX — Event Research Lens V0.2 (Phase-Aware / Tradable)

Input
-----
Latest accepted Event Outcome Base V0.2:
  D:\positionx_data\events\outcomes_v0_2\full_*

Purpose
-------
Evaluate event families under the SAME tradable semantics already frozen in Outcome V0.2:

  signal/event at T
  entry = T+1 exact market-session OPEN
  exit  = T+20 exact market-session CLOSE

Adds:
- same-signal-date whole-market T+1-open -> T+20-close baseline
- alpha vs market median / mean
- first-in-60d view
- cluster-start view
- 5-year stability
- selected same-day event-combination view

Important
---------
This is provisional historical research because strict historical ST PIT coverage is
still incomplete. No broker / no execution / no production SQLite / no catalog mutation.
"""

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

CATALOG = Path(r"D:\positionx_data\catalog\market_data.duckdb")
OUTCOME_ROOT = Path(r"D:\positionx_data\events\outcomes_v0_2")
OUT_ROOT = Path(r"D:\positionx_data\events\research_lens_v0_2")
PRICE_BASIS = "PIT_BACK_RATIO_COMPARABLE_V1"


def qp(p: Path) -> str:
    return str(p).replace("\\", "/").replace("'", "''")


def latest_outcomes():
    for p in sorted([x for x in OUTCOME_ROOT.glob("full_*") if x.is_dir()], reverse=True):
        mf = p / "run_manifest.json"
        f = p / "event_outcomes_v02.parquet"
        if not mf.exists() or not f.exists():
            continue
        m = json.loads(mf.read_text(encoding="utf-8"))
        if m.get("status") == "PASS_EVENT_OUTCOMES_V0_2":
            return p, m
    raise RuntimeError("No accepted Event Outcome V0.2 full build found.")


def write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outcome-dir")
    a = ap.parse_args()

    if a.outcome_dir:
        od = Path(a.outcome_dir)
        om = json.loads((od / "run_manifest.json").read_text(encoding="utf-8"))
    else:
        od, om = latest_outcomes()

    outcome_file = od / "event_outcomes_v02.parquet"
    if om.get("status") != "PASS_EVENT_OUTCOMES_V0_2":
        raise RuntimeError("Outcome input is not accepted V0.2.")
    if not outcome_file.exists():
        raise RuntimeError(f"Missing: {outcome_file}")
    if not CATALOG.exists():
        raise RuntimeError(f"Missing market catalog: {CATALOG}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = OUT_ROOT / f"full_{stamp}"
    out.mkdir(parents=True, exist_ok=False)
    alpha_file = out / "event_outcomes_alpha_v02.parquet"

    import duckdb
    con = duckdb.connect(str(CATALOG), read_only=True)
    try:
        op = qp(outcome_file)
        apath = qp(alpha_file)

        # Whole-market control with the SAME exact-session and fillability semantics.
        control_sql = """
        WITH cal AS (
          SELECT trade_date,row_number() OVER(ORDER BY trade_date) AS cal_seq
          FROM (SELECT DISTINCT trade_date FROM market.daily_bars_v1)
        ),
        raw AS (
          SELECT
            symbol,trade_date,"open","high","low","close",pre_close,suspend_flag,
            lag("close") OVER(PARTITION BY symbol ORDER BY trade_date) AS prev_raw_close
          FROM market.daily_bars_v1
        ),
        r AS (
          SELECT *,
            CASE WHEN prev_raw_close IS NULL THEN 1.0
                 WHEN prev_raw_close<=0 OR pre_close<=0 THEN NULL
                 ELSE prev_raw_close/pre_close END AS daily_dr
          FROM raw
        ),
        b AS (
          SELECT *,
            exp(
              sum(CASE WHEN daily_dr IS NULL THEN 0.0 ELSE ln(daily_dr) END)
              OVER(
                PARTITION BY symbol ORDER BY trade_date
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
              )
            ) AS cum_dr
          FROM r
        ),
        p AS (
          SELECT
            symbol,trade_date,"open","high","low","close",pre_close,suspend_flag,
            "open"*cum_dr AS pit_open,
            "close"*cum_dr AS pit_close
          FROM b
        ),
        d AS (
          SELECT
            c.trade_date AS signal_date,
            c.cal_seq,
            c1.trade_date AS entry_date,
            c20.trade_date AS exit_date
          FROM cal c
          LEFT JOIN cal c1 ON c1.cal_seq=c.cal_seq+1
          LEFT JOIN cal c20 ON c20.cal_seq=c.cal_seq+20
        ),
        u AS (
          SELECT
            d.signal_date,
            pe.symbol,
            pe."open" AS eo,pe."high" AS eh,pe."low" AS el,pe.pre_close AS epc,
            pe.suspend_flag AS es,pe.pit_open,
            px."close" AS xc,px."high" AS xh,px."low" AS xl,px.pre_close AS xpc,
            px.suspend_flag AS xs,px.pit_close
          FROM d
          JOIN p pe ON pe.trade_date=d.entry_date
          JOIN p px ON px.symbol=pe.symbol AND px.trade_date=d.exit_date
        ),
        rr AS (
          SELECT
            signal_date,
            CASE
              WHEN coalesce(es,1)=0 AND coalesce(xs,1)=0
                AND pit_open>0 AND pit_close>0
                AND NOT (
                  abs(eo-eh)<1e-10 AND abs(eo-el)<1e-10 AND eo>epc
                )
                AND NOT (
                  abs(xc-xh)<1e-10 AND abs(xc-xl)<1e-10 AND xc<xpc
                )
              THEN pit_close/pit_open-1
            END AS gross_ret
          FROM u
        )
        SELECT
          signal_date,
          count(gross_ret) AS control_n,
          avg(gross_ret) AS control_mean_ret20,
          median(gross_ret) AS control_median_ret20
        FROM rr
        GROUP BY signal_date
        """
        print("BUILD same-date tradable market control ...")
        con.execute(f"CREATE TEMP TABLE market_control AS {control_sql}")

        q = f"""
        WITH e AS (
          SELECT * FROM read_parquet('{op}')
        )
        SELECT
          e.*,
          m.control_n,
          m.control_mean_ret20,
          m.control_median_ret20,

          CASE WHEN e.tradable_complete_20d_v01
               THEN e.ret_t1open_t20close-m.control_median_ret20 END AS alpha_median_20d,
          CASE WHEN e.tradable_complete_20d_v01
               THEN e.ret_t1open_t20close-m.control_mean_ret20 END AS alpha_mean_20d,

          (e.consecutive_count=1) AS cluster_start,

          CASE
            WHEN year(e.trade_date)<1995 THEN '1990-1994'
            WHEN year(e.trade_date)<2000 THEN '1995-1999'
            WHEN year(e.trade_date)<2005 THEN '2000-2004'
            WHEN year(e.trade_date)<2010 THEN '2005-2009'
            WHEN year(e.trade_date)<2015 THEN '2010-2014'
            WHEN year(e.trade_date)<2020 THEN '2015-2019'
            WHEN year(e.trade_date)<2025 THEN '2020-2024'
            ELSE '2025-2026'
          END AS era_5y
        FROM e
        LEFT JOIN market_control m
          ON m.signal_date=e.trade_date
        """
        print("BUILD phase-aware tradable alpha asset ...")
        con.execute(f"COPY ({q}) TO '{apath}' (FORMAT PARQUET, COMPRESSION ZSTD)")

        base = f"read_parquet('{apath}')"
        st = con.execute(f"""
          SELECT
            count(*),
            count(DISTINCT event_id),
            count(*)-count(DISTINCT event_id),
            count(*) FILTER(
              WHERE tradable_complete_20d_v01 AND alpha_median_20d IS NULL
            )
          FROM {base}
        """).fetchone()

        if int(st[2]) != 0:
            raise RuntimeError(f"Duplicate event IDs: {st[2]}")
        if int(st[3]) != 0:
            raise RuntimeError(f"Missing alpha on tradable complete20 rows: {st[3]}")

        def csv(sql: str, name: str):
            con.execute(f"COPY ({sql}) TO '{qp(out/name)}' (HEADER, DELIMITER ',')")

        metrics = """
          count(*) FILTER(WHERE tradable_complete_20d_v01) AS n20,
          avg(ret_t1open_t20close)
            FILTER(WHERE tradable_complete_20d_v01) AS avg_ret20,
          median(ret_t1open_t20close)
            FILTER(WHERE tradable_complete_20d_v01) AS median_ret20,
          avg(alpha_median_20d)
            FILTER(WHERE tradable_complete_20d_v01) AS avg_alpha20,
          median(alpha_median_20d)
            FILTER(WHERE tradable_complete_20d_v01) AS median_alpha20,
          avg(CASE WHEN alpha_median_20d>0 THEN 1.0 ELSE 0.0 END)
            FILTER(WHERE tradable_complete_20d_v01) AS alpha_win20,
          quantile_cont(alpha_median_20d,0.25)
            FILTER(WHERE tradable_complete_20d_v01) AS alpha_q25,
          quantile_cont(alpha_median_20d,0.75)
            FILTER(WHERE tradable_complete_20d_v01) AS alpha_q75
        """

        csv(f"""
          SELECT event_type,event_phase,{metrics}
          FROM {base}
          GROUP BY event_type,event_phase
          ORDER BY event_type
        """, "alpha_event_type_summary.csv")

        csv(f"""
          SELECT event_type,first_in_60d,{metrics}
          FROM {base}
          GROUP BY event_type,first_in_60d
          ORDER BY event_type,first_in_60d DESC
        """, "alpha_first60_summary.csv")

        csv(f"""
          SELECT event_type,{metrics}
          FROM {base}
          WHERE cluster_start
          GROUP BY event_type
          ORDER BY event_type
        """, "alpha_cluster_start_summary.csv")

        csv(f"""
          SELECT event_type,era_5y,{metrics}
          FROM {base}
          GROUP BY event_type,era_5y
          ORDER BY event_type,era_5y
        """, "era_stability_summary.csv")

        # Same-day combinations. Collapse duplicate event rows to symbol/date once.
        # Any combo containing RETURN/TURNOVER is only known by T close, so T+1 OPEN is clean.
        csv(f"""
          WITH d AS (
            SELECT
              symbol,trade_date,

              bool_or(event_type='RETURN_TOP1PCT') AS ret_top,
              bool_or(event_type='RETURN_BOTTOM1PCT') AS ret_bottom,
              bool_or(event_type='GAP_UP_TOP1PCT') AS gap_up,
              bool_or(event_type='GAP_DOWN_BOTTOM1PCT') AS gap_down,
              bool_or(event_type='TURNOVER_TOP1PCT') AS turnover_top1,

              max(ret_t1open_t20close) AS ret20,
              max(alpha_median_20d) AS alpha20,
              bool_or(tradable_complete_20d_v01) AS complete20
            FROM {base}
            GROUP BY symbol,trade_date
          ),
          combos AS (
            SELECT *,
              unnest([
                CASE WHEN ret_top AND turnover_top1
                  THEN 'RETURN_TOP1PCT_AND_TURNOVER_TOP1PCT' END,
                CASE WHEN ret_bottom AND turnover_top1
                  THEN 'RETURN_BOTTOM1PCT_AND_TURNOVER_TOP1PCT' END,
                CASE WHEN gap_up AND turnover_top1
                  THEN 'GAP_UP_TOP1PCT_AND_TURNOVER_TOP1PCT' END,
                CASE WHEN gap_down AND turnover_top1
                  THEN 'GAP_DOWN_BOTTOM1PCT_AND_TURNOVER_TOP1PCT' END,
                CASE WHEN gap_up AND ret_top
                  THEN 'GAP_UP_TOP1PCT_AND_RETURN_TOP1PCT' END,
                CASE WHEN gap_down AND ret_bottom
                  THEN 'GAP_DOWN_BOTTOM1PCT_AND_RETURN_BOTTOM1PCT' END,
                CASE WHEN gap_up AND ret_top AND turnover_top1
                  THEN 'GAP_UP_RETURN_TOP_AND_TURNOVER_TOP1PCT' END,
                CASE WHEN gap_down AND ret_bottom AND turnover_top1
                  THEN 'GAP_DOWN_RETURN_BOTTOM_AND_TURNOVER_TOP1PCT' END
              ]) AS combo
            FROM d
          )
          SELECT
            combo,
            count(*) FILTER(WHERE complete20) AS n20,
            avg(ret20) FILTER(WHERE complete20) AS avg_ret20,
            median(ret20) FILTER(WHERE complete20) AS median_ret20,
            avg(alpha20) FILTER(WHERE complete20) AS avg_alpha20,
            median(alpha20) FILTER(WHERE complete20) AS median_alpha20,
            avg(CASE WHEN alpha20>0 THEN 1.0 ELSE 0.0 END)
              FILTER(WHERE complete20) AS alpha_win20,
            quantile_cont(alpha20,0.25)
              FILTER(WHERE complete20) AS alpha_q25,
            quantile_cont(alpha20,0.75)
              FILTER(WHERE complete20) AS alpha_q75
          FROM combos
          WHERE combo IS NOT NULL
          GROUP BY combo
          ORDER BY combo
        """, "event_combo_summary.csv")

        # Focused summary for only the four new event families, to keep the next review compact.
        csv(f"""
          SELECT
            event_type,
            first_in_60d,
            era_5y,
            {metrics}
          FROM {base}
          WHERE event_type IN (
            'RETURN_TOP1PCT','RETURN_BOTTOM1PCT',
            'GAP_UP_TOP1PCT','GAP_DOWN_BOTTOM1PCT'
          )
          GROUP BY event_type,first_in_60d,era_5y
          ORDER BY event_type,first_in_60d DESC,era_5y
        """, "new_events_first60_era_matrix.csv")

        result = {
            "status": "PASS_EVENT_RESEARCH_LENS_V0_2",
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "outcome_source_dir": str(od),
            "rows": int(st[0]),
            "unique_event_ids": int(st[1]),
            "duplicate_event_ids": int(st[2]),
            "missing_alpha20_on_tradable20": int(st[3]),
            "benchmark": "same-signal-date whole-market T+1 exact OPEN -> T+20 exact CLOSE with same fillability semantics",
            "preferred_alpha": "alpha_median_20d",
            "price_basis": PRICE_BASIS,
            "strict_non_st_pit_applied": False,
            "historical_research_ready": False,
            "output_dir": str(out),
        }
        write_json(out / "run_manifest.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
