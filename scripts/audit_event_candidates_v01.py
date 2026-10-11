from __future__ import annotations

"""
PositionX — Event Candidate Tradability Audit V0.1

Fixed hypotheses only; no threshold sweep.

Profiles
--------
1. ATH_FIRST60_ALL
2. ATH_FIRST60_EX_TOP1PCT
3. ATH_FIRST60_WITH_TOP1PCT
4. TOP1PCT_FIRST60_LONG_DIAGNOSTIC

Trade semantics
---------------
signal = event known at T close
entry  = T+1 exact market-session OPEN
exit   = T+20 exact market-session CLOSE

Comparable prices use PIT_BACK_RATIO_COMPARABLE_V1.

Fillability V0.1:
- entry/exit rows must exist and not be suspended
- entry blocks a locked one-price upper board
- exit blocks a locked one-price lower board

Costs are simple total round-trip deductions:
- 20 bps
- 50 bps

Research-only. No broker/order/production SQLite/catalog mutation.
"""

import argparse, json, os
from datetime import datetime
from pathlib import Path

CATALOG = Path(r"D:\positionx_data\catalog\market_data.duckdb")
LENS_ROOT = Path(r"D:\positionx_data\events\research_lens_v0_1")
OUT_ROOT = Path(r"D:\positionx_data\events\candidate_audit_v0_1")
PRICE_BASIS = "PIT_BACK_RATIO_COMPARABLE_V1"

def qp(p: Path) -> str:
    return str(p).replace("\\", "/").replace("'", "''")

def latest_lens():
    for p in sorted([x for x in LENS_ROOT.glob("full_*") if x.is_dir()], reverse=True):
        mf=p/"run_manifest.json"; f=p/"event_outcomes_alpha.parquet"
        if mf.exists() and f.exists():
            m=json.loads(mf.read_text(encoding="utf-8"))
            if m.get("status")=="PASS_EVENT_RESEARCH_LENS_V0_1":
                return p,m
    raise RuntimeError("No accepted Event Research Lens V0.1 found.")

def write_json(path,obj):
    t=path.with_suffix(path.suffix+".tmp")
    t.write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    os.replace(t,path)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--lens-dir")
    a=ap.parse_args()

    if a.lens_dir:
        lens=Path(a.lens_dir)
        lm=json.loads((lens/"run_manifest.json").read_text(encoding="utf-8"))
    else:
        lens,lm=latest_lens()
    lf=lens/"event_outcomes_alpha.parquet"
    if lm.get("status")!="PASS_EVENT_RESEARCH_LENS_V0_1" or not lf.exists():
        raise RuntimeError("Lens input is not accepted V0.1.")
    if not CATALOG.exists():
        raise RuntimeError("Market catalog missing.")

    stamp=datetime.now().strftime("%Y%m%d_%H%M%S")
    out=OUT_ROOT/f"full_{stamp}"
    out.mkdir(parents=True,exist_ok=False)
    trades_file=out/"candidate_trades.parquet"

    import duckdb
    con=duckdb.connect(str(CATALOG),read_only=True)
    try:
        lp=qp(lf); tp=qp(trades_file)

        # Fixed signal definitions; TOP1 same-day presence is derived from the event ledger.
        q=f"""
        WITH e AS (
          SELECT * FROM read_parquet('{lp}')
        ),
        daily_event_flags AS (
          SELECT symbol,trade_date,
                 bool_or(event_type='PRICE_ALL_TIME_HIGH') ath,
                 bool_or(event_type='TURNOVER_TOP1PCT') top1
          FROM e
          GROUP BY symbol,trade_date
        ),
        ath AS (
          SELECT DISTINCT symbol,trade_date signal_date
          FROM e
          WHERE event_type='PRICE_ALL_TIME_HIGH' AND first_in_60d
        ),
        top1first AS (
          SELECT DISTINCT symbol,trade_date signal_date
          FROM e
          WHERE event_type='TURNOVER_TOP1PCT' AND first_in_60d
        ),
        signals AS (
          SELECT 'ATH_FIRST60_ALL' profile,a.symbol,a.signal_date
          FROM ath a
          UNION ALL
          SELECT 'ATH_FIRST60_EX_TOP1PCT',a.symbol,a.signal_date
          FROM ath a
          JOIN daily_event_flags f ON f.symbol=a.symbol AND f.trade_date=a.signal_date
          WHERE NOT f.top1
          UNION ALL
          SELECT 'ATH_FIRST60_WITH_TOP1PCT',a.symbol,a.signal_date
          FROM ath a
          JOIN daily_event_flags f ON f.symbol=a.symbol AND f.trade_date=a.signal_date
          WHERE f.top1
          UNION ALL
          SELECT 'TOP1PCT_FIRST60_LONG_DIAGNOSTIC',t.symbol,t.signal_date
          FROM top1first t
        ),
        cal AS (
          SELECT trade_date,
                 row_number() OVER(ORDER BY trade_date) cal_seq
          FROM (SELECT DISTINCT trade_date FROM market.daily_bars_v1)
        ),
        raw AS (
          SELECT symbol,trade_date,"open","high","low","close",pre_close,suspend_flag,
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
        px AS (
          SELECT *,
            exp(sum(CASE WHEN daily_dr IS NULL THEN 0.0 ELSE ln(daily_dr) END)
                OVER(PARTITION BY symbol ORDER BY trade_date
                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)) cum_dr
          FROM r
        ),
        p AS (
          SELECT symbol,trade_date,"open","high","low","close",pre_close,suspend_flag,
                 "open"*cum_dr pit_open,
                 "close"*cum_dr pit_close
          FROM px
        ),
        sig_dates AS (
          SELECT s.*,c.cal_seq,
                 ce.trade_date entry_date,
                 cx.trade_date exit_date
          FROM signals s
          JOIN cal c ON c.trade_date=s.signal_date
          LEFT JOIN cal ce ON ce.cal_seq=c.cal_seq+1
          LEFT JOIN cal cx ON cx.cal_seq=c.cal_seq+20
        ),
        joined AS (
          SELECT s.*,
                 pe."open" entry_open,pe."high" entry_high,pe."low" entry_low,
                 pe."close" entry_close,pe.pre_close entry_pre_close,
                 pe.suspend_flag entry_suspend,
                 pe.pit_open entry_pit_open,
                 px."open" exit_open,px."high" exit_high,px."low" exit_low,
                 px."close" exit_close,px.pre_close exit_pre_close,
                 px.suspend_flag exit_suspend,
                 px.pit_close exit_pit_close
          FROM sig_dates s
          LEFT JOIN p pe ON pe.symbol=s.symbol AND pe.trade_date=s.entry_date
          LEFT JOIN p px ON px.symbol=s.symbol AND px.trade_date=s.exit_date
        ),
        fill AS (
          SELECT *,
            entry_date IS NOT NULL
              AND entry_pit_open IS NOT NULL
              AND entry_pit_open>0
              AND coalesce(entry_suspend,1)=0 AS entry_available,
            exit_date IS NOT NULL
              AND exit_pit_close IS NOT NULL
              AND exit_pit_close>0
              AND coalesce(exit_suspend,1)=0 AS exit_available,
            coalesce(abs(entry_open-entry_high)<1e-10
                     AND abs(entry_open-entry_low)<1e-10
                     AND entry_open>entry_pre_close, false) AS locked_upper_entry,
            coalesce(abs(exit_close-exit_high)<1e-10
                     AND abs(exit_close-exit_low)<1e-10
                     AND exit_close<exit_pre_close, false) AS locked_lower_exit
          FROM joined
        )
        SELECT *,
          entry_available AND exit_available
            AND NOT locked_upper_entry AND NOT locked_lower_exit AS fillable_standard_v01,
          CASE WHEN entry_available AND exit_available
                     AND NOT locked_upper_entry AND NOT locked_lower_exit
               THEN exit_pit_close/entry_pit_open-1 END gross_ret,
          CASE WHEN entry_available AND exit_available
                     AND NOT locked_upper_entry AND NOT locked_lower_exit
               THEN exit_pit_close/entry_pit_open-1-0.002 END net_ret_20bps,
          CASE WHEN entry_available AND exit_available
                     AND NOT locked_upper_entry AND NOT locked_lower_exit
               THEN exit_pit_close/entry_pit_open-1-0.005 END net_ret_50bps
        FROM fill
        """
        print("BUILD fixed candidate trades (T+1 OPEN -> T+20 CLOSE) ...")
        con.execute(f"COPY ({q}) TO '{tp}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        base=f"read_parquet('{tp}')"

        # Date-matched market control with the same exact-session/fillability semantics.
        control=f"""
        WITH cal AS (
          SELECT trade_date,row_number() OVER(ORDER BY trade_date) cal_seq
          FROM (SELECT DISTINCT trade_date FROM market.daily_bars_v1)
        ),
        raw AS (
          SELECT symbol,trade_date,"open","high","low","close",pre_close,suspend_flag,
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
          SELECT symbol,trade_date,"open","high","low","close",pre_close,suspend_flag,
                 "open"*cum_dr pit_open,"close"*cum_dr pit_close
          FROM b
        ),
        dates AS (
          SELECT DISTINCT signal_date FROM {base}
        ),
        d AS (
          SELECT x.signal_date,c.cal_seq,ce.trade_date entry_date,cx.trade_date exit_date
          FROM dates x JOIN cal c ON c.trade_date=x.signal_date
          LEFT JOIN cal ce ON ce.cal_seq=c.cal_seq+1
          LEFT JOIN cal cx ON cx.cal_seq=c.cal_seq+20
        ),
        u AS (
          SELECT d.signal_date,pe.symbol,
                 pe."open" eo,pe."high" eh,pe."low" el,pe.pre_close epc,
                 pe.suspend_flag es,pe.pit_open,
                 px."close" xc,px."high" xh,px."low" xl,px.pre_close xpc,
                 px.suspend_flag xs,px.pit_close
          FROM d
          JOIN p pe ON pe.trade_date=d.entry_date
          JOIN p px ON px.symbol=pe.symbol AND px.trade_date=d.exit_date
        ),
        rr AS (
          SELECT signal_date,
            CASE WHEN coalesce(es,1)=0 AND coalesce(xs,1)=0
                      AND pit_open>0 AND pit_close>0
                      AND NOT (abs(eo-eh)<1e-10 AND abs(eo-el)<1e-10 AND eo>epc)
                      AND NOT (abs(xc-xh)<1e-10 AND abs(xc-xl)<1e-10 AND xc<xpc)
                 THEN pit_close/pit_open-1 END gross_ret
          FROM u
        )
        SELECT signal_date,
               count(gross_ret) control_n,
               avg(gross_ret) control_mean,
               median(gross_ret) control_median
        FROM rr GROUP BY signal_date
        """
        con.execute(f"CREATE TEMP TABLE market_control AS {control}")

        def csv(sql,name):
            con.execute(f"COPY ({sql}) TO '{qp(out/name)}' (HEADER, DELIMITER ',')")

        summary=f"""
        WITH x AS (
          SELECT t.*,m.control_mean,m.control_median,
                 t.gross_ret-m.control_median alpha_median,
                 t.gross_ret-m.control_mean alpha_mean
          FROM {base} t
          LEFT JOIN market_control m USING(signal_date)
        )
        SELECT profile,
          count(*) raw_signals,
          count(*) FILTER(WHERE fillable_standard_v01) fillable,
          avg(CASE WHEN fillable_standard_v01 THEN 1.0 ELSE 0.0 END) fill_rate,
          count(*) FILTER(WHERE locked_upper_entry) blocked_locked_upper_entry,
          count(*) FILTER(WHERE locked_lower_exit) blocked_locked_lower_exit,
          avg(gross_ret) FILTER(WHERE fillable_standard_v01) avg_gross,
          median(gross_ret) FILTER(WHERE fillable_standard_v01) median_gross,
          avg(CASE WHEN gross_ret>0 THEN 1.0 ELSE 0.0 END)
            FILTER(WHERE fillable_standard_v01) win_rate,
          avg(alpha_median) FILTER(WHERE fillable_standard_v01) avg_alpha_median,
          median(alpha_median) FILTER(WHERE fillable_standard_v01) median_alpha_median,
          avg(CASE WHEN alpha_median>0 THEN 1.0 ELSE 0.0 END)
            FILTER(WHERE fillable_standard_v01) alpha_win_rate,
          avg(net_ret_20bps) FILTER(WHERE fillable_standard_v01) avg_net20,
          median(net_ret_20bps) FILTER(WHERE fillable_standard_v01) median_net20,
          avg(net_ret_50bps) FILTER(WHERE fillable_standard_v01) avg_net50,
          median(net_ret_50bps) FILTER(WHERE fillable_standard_v01) median_net50
        FROM x
        GROUP BY profile
        ORDER BY profile
        """
        csv(summary,"candidate_summary.csv")

        era=f"""
        WITH x AS (
          SELECT t.*,m.control_median,
                 t.gross_ret-m.control_median alpha_median,
                 CASE
                   WHEN year(signal_date)<2000 THEN 'PRE_2000'
                   WHEN year(signal_date)<2005 THEN '2000-2004'
                   WHEN year(signal_date)<2010 THEN '2005-2009'
                   WHEN year(signal_date)<2015 THEN '2010-2014'
                   WHEN year(signal_date)<2020 THEN '2015-2019'
                   WHEN year(signal_date)<2025 THEN '2020-2024'
                   ELSE '2025-2026'
                 END era
          FROM {base} t LEFT JOIN market_control m USING(signal_date)
          WHERE fillable_standard_v01
        )
        SELECT profile,era,count(*) n,
               avg(gross_ret) avg_gross,median(gross_ret) median_gross,
               avg(alpha_median) avg_alpha,median(alpha_median) median_alpha,
               avg(CASE WHEN alpha_median>0 THEN 1.0 ELSE 0.0 END) alpha_win,
               avg(net_ret_20bps) avg_net20,median(net_ret_20bps) median_net20,
               avg(net_ret_50bps) avg_net50,median(net_ret_50bps) median_net50
        FROM x GROUP BY profile,era ORDER BY profile,era
        """
        csv(era,"candidate_era_summary.csv")

        # A compact direct comparison for the ATH hypothesis and turnover-overheat split.
        csv(f"""
        SELECT profile,signal_date,symbol,entry_date,exit_date,
               fillable_standard_v01,locked_upper_entry,locked_lower_exit,
               gross_ret,net_ret_20bps,net_ret_50bps
        FROM {base}
        WHERE profile IN ('ATH_FIRST60_EX_TOP1PCT','ATH_FIRST60_WITH_TOP1PCT')
        ORDER BY signal_date DESC,symbol,profile
        LIMIT 500
        ""","recent_ath_samples.csv")

        st=con.execute(f"""SELECT count(*),count(*) FILTER(WHERE fillable_standard_v01),
                          count(*) FILTER(WHERE entry_date IS NULL OR exit_date IS NULL)
                          FROM {base}""").fetchone()

        result={
          "status":"PASS_EVENT_CANDIDATE_AUDIT_V0_1",
          "generated_at":datetime.now().astimezone().isoformat(timespec="seconds"),
          "lens_source_dir":str(lens),
          "profiles":[
            "ATH_FIRST60_ALL",
            "ATH_FIRST60_EX_TOP1PCT",
            "ATH_FIRST60_WITH_TOP1PCT",
            "TOP1PCT_FIRST60_LONG_DIAGNOSTIC"
          ],
          "trade_semantics":"T+1 exact market-session OPEN -> T+20 exact market-session CLOSE",
          "fillability":"STANDARD_OPEN_CLOSE_V0_1 with locked one-price upper-entry/lower-exit blocks",
          "costs_total_roundtrip_bps":[20,50],
          "price_basis":PRICE_BASIS,
          "market_control":"same-signal-date whole-market median/mean with same T+1/T+20 semantics",
          "raw_profile_rows":int(st[0]),
          "fillable_rows":int(st[1]),
          "incomplete_calendar_rows":int(st[2]),
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
