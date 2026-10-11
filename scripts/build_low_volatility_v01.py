from __future__ import annotations

r"""
PositionX — Cross-Sectional Low Volatility Research V0.1

Fixed hypothesis; no parameter sweep
------------------------------------
Signal date T:
  realized_vol_60 = sample stddev of the previous 60 exact market-session
                    PIT comparable close-to-close returns, through T.

Cross-sectional selection:
  LOW_VOL20  = lowest 20% realized_vol_60, ties included
  HIGH_VOL20 = highest 20% realized_vol_60, ties included (diagnostic control)

Execution:
  signal known at T close
  entry = T+1 exact market-session OPEN
  exit  = T+20 exact market-session CLOSE

Costs:
  20 bps main / 50 bps shadow, total round-trip.

Primary decision surface:
  DAILY equal-weight cohort diagnostics.

Benchmark:
  same-signal-date valid scoring universe, same T+1 OPEN -> T+20 CLOSE
  and same fillability semantics.

Research-only. No broker, order, production SQLite, scheduler, state, or catalog mutation.
Historical ST PIT remains incomplete; results are provisional.
"""

import argparse
import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any

CATALOG = Path(r"D:\positionx_data\catalog\market_data.duckdb")
OUT_ROOT = Path(r"D:\positionx_data\strategies\low_volatility_v0_1")

VOL_LOOKBACK = 60
FRACTION = 0.20
EXIT_SESSIONS = 20
COST_MAIN = 0.002
COST_SHADOW = 0.005
PRICE_BASIS = "PIT_BACK_RATIO_COMPARABLE_V1"


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


def qpath(p: Path) -> str:
    return str(p).replace("\\", "/").replace("'", "''")


def write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=safe), encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser(description="PositionX Low Volatility Research V0.1")
    ap.add_argument("--catalog", default=str(CATALOG))
    ap.add_argument("--out-root", default=str(OUT_ROOT))
    args = ap.parse_args()

    catalog = Path(args.catalog)
    out_root = Path(args.out_root)
    if not catalog.exists():
        raise RuntimeError(f"Catalog not found: {catalog}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = out_root / f"full_{stamp}"
    out.mkdir(parents=True, exist_ok=False)

    import duckdb
    con = duckdb.connect(":memory:")
    try:
        con.execute(f"ATTACH '{qpath(catalog)}' AS cat (READ_ONLY)")

        stats = con.execute("""
            SELECT
              count(*) AS rows,
              count(DISTINCT symbol) AS symbols,
              min(trade_date) AS first_date,
              max(trade_date) AS last_date,
              count(*) - count(DISTINCT (symbol, trade_date)) AS dup_symbol_dates
            FROM cat.market.daily_bars_v1
        """).fetchone()
        if int(stats[4]) != 0:
            raise RuntimeError(f"FAIL_CLOSED duplicate symbol/date rows={stats[4]}")

        print("BUILD market calendar ...")
        con.execute("""
            CREATE TEMP TABLE cal AS
            SELECT
              trade_date,
              row_number() OVER (ORDER BY trade_date) AS market_idx
            FROM (SELECT DISTINCT trade_date FROM cat.market.daily_bars_v1)
            ORDER BY trade_date
        """)

        print("BUILD PIT comparable prices ...")
        con.execute("""
            CREATE TEMP TABLE raw_seq AS
            SELECT
              d.symbol,
              d.trade_date,
              d."open",
              d."high",
              d."low",
              d."close",
              d.volume,
              d.amount,
              d.pre_close,
              d.suspend_flag,
              c.market_idx,
              row_number() OVER (PARTITION BY d.symbol ORDER BY d.trade_date) AS symbol_obs_idx,
              lag(d."close") OVER (PARTITION BY d.symbol ORDER BY d.trade_date) AS prev_raw_close
            FROM cat.market.daily_bars_v1 d
            JOIN cal c USING(trade_date)
        """)

        invalid_ratio = int(con.execute("""
            SELECT count(*)
            FROM raw_seq
            WHERE symbol_obs_idx > 1
              AND (
                prev_raw_close IS NULL OR prev_raw_close <= 0
                OR pre_close IS NULL OR pre_close <= 0
              )
        """).fetchone()[0])
        if invalid_ratio:
            raise RuntimeError(f"FAIL_CLOSED invalid PIT daily-ratio prerequisites={invalid_ratio}")

        con.execute("""
            CREATE TEMP TABLE px AS
            WITH ratio AS (
              SELECT *,
                CASE WHEN symbol_obs_idx=1 THEN 1.0
                     ELSE prev_raw_close / pre_close END AS daily_dr
              FROM raw_seq
            ),
            cum AS (
              SELECT *,
                exp(
                  sum(ln(daily_dr)) OVER (
                    PARTITION BY symbol
                    ORDER BY trade_date
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                  )
                ) AS cum_dr
              FROM ratio
            )
            SELECT *,
              "open"  * cum_dr AS pit_open,
              "high"  * cum_dr AS pit_high,
              "low"   * cum_dr AS pit_low,
              "close" * cum_dr AS pit_close
            FROM cum
        """)

        pit_bad = int(con.execute("""
            SELECT count(*)
            FROM px
            WHERE cum_dr IS NULL OR cum_dr <= 0
               OR pit_open <= 0 OR pit_high <= 0 OR pit_low <= 0 OR pit_close <= 0
        """).fetchone()[0])
        if pit_bad:
            raise RuntimeError(f"FAIL_CLOSED bad PIT comparable rows={pit_bad}")

        print("BUILD exact market-session close-to-close returns ...")
        con.execute("""
            CREATE TEMP TABLE rets AS
            WITH x AS (
              SELECT
                p.*,
                lag(p.pit_close) OVER (PARTITION BY p.symbol ORDER BY p.trade_date) AS prev_pit_close,
                lag(p.market_idx) OVER (PARTITION BY p.symbol ORDER BY p.trade_date) AS prev_market_idx
              FROM px p
            )
            SELECT *,
              CASE
                WHEN prev_market_idx = market_idx - 1 AND prev_pit_close > 0
                  THEN pit_close / prev_pit_close - 1
                ELSE NULL
              END AS ret1
            FROM x
        """)

        print("BUILD trailing 60-session realized volatility ...")
        con.execute(f"""
            CREATE TEMP TABLE vol_state AS
            WITH w AS (
              SELECT *,
                count(ret1) OVER (
                  PARTITION BY symbol ORDER BY market_idx
                  RANGE BETWEEN {VOL_LOOKBACK - 1} PRECEDING AND CURRENT ROW
                ) AS n_ret60,
                stddev_samp(ret1) OVER (
                  PARTITION BY symbol ORDER BY market_idx
                  RANGE BETWEEN {VOL_LOOKBACK - 1} PRECEDING AND CURRENT ROW
                ) AS realized_vol_60
              FROM rets
            )
            SELECT *
            FROM w
            WHERE n_ret60 = {VOL_LOOKBACK}
              AND realized_vol_60 IS NOT NULL
              AND realized_vol_60 > 0
        """)

        print("BUILD cross-sectional low/high volatility ranks ...")
        con.execute(f"""
            CREATE TEMP TABLE scores AS
            WITH ranked AS (
              SELECT
                symbol,
                trade_date AS signal_date,
                market_idx AS signal_idx,
                realized_vol_60,
                count(*) OVER (PARTITION BY trade_date) AS cross_section_n,
                rank() OVER (
                  PARTITION BY trade_date
                  ORDER BY realized_vol_60 ASC
                ) AS rank_low,
                rank() OVER (
                  PARTITION BY trade_date
                  ORDER BY realized_vol_60 DESC
                ) AS rank_high
              FROM vol_state
            )
            SELECT *,
              CAST(ceil(cross_section_n * {FRACTION}) AS BIGINT) AS cutoff_k,
              CASE
                WHEN rank_low <= ceil(cross_section_n * {FRACTION})
                  THEN 'LOW_VOL20'
                WHEN rank_high <= ceil(cross_section_n * {FRACTION})
                  THEN 'HIGH_VOL20'
                ELSE 'MID_VOL60'
              END AS profile
            FROM ranked
        """)

        score_stats = con.execute("""
            SELECT
              count(*) AS score_rows,
              count(DISTINCT signal_date) AS signal_days,
              min(signal_date) AS first_signal_date,
              max(signal_date) AS last_signal_date,
              min(cross_section_n) AS min_cross_section,
              median(cross_section_n) AS median_cross_section,
              max(cross_section_n) AS max_cross_section,
              count(*) FILTER(WHERE profile='LOW_VOL20') AS low_rows,
              count(*) FILTER(WHERE profile='HIGH_VOL20') AS high_rows
            FROM scores
        """).fetchone()

        print("BUILD exact T+1 OPEN -> T+20 CLOSE outcomes ...")
        con.execute(f"""
            CREATE TEMP TABLE outcomes AS
            WITH o AS (
              SELECT
                s.*,
                e.trade_date AS entry_date,
                x.trade_date AS exit_date,
                e.pit_open AS entry_open_pit,
                x.pit_close AS exit_close_pit,

                CASE
                  WHEN e.symbol IS NULL OR x.symbol IS NULL THEN false
                  ELSE true
                END AS complete_20d,

                CASE
                  WHEN e.symbol IS NULL OR x.symbol IS NULL THEN false
                  WHEN e."open"=e."high" AND e."high"=e."low" AND e."open">e.pre_close
                    THEN false
                  WHEN x."open"=x."high" AND x."high"=x."low" AND x."close"<x.pre_close
                    THEN false
                  ELSE true
                END AS fillable_20d,

                CASE
                  WHEN e.symbol IS NOT NULL AND x.symbol IS NOT NULL
                    THEN x.pit_close/e.pit_open - 1
                END AS gross20
              FROM scores s
              LEFT JOIN cal ce ON ce.market_idx=s.signal_idx+1
              LEFT JOIN cal cx ON cx.market_idx=s.signal_idx+{EXIT_SESSIONS}
              LEFT JOIN px e ON e.symbol=s.symbol AND e.trade_date=ce.trade_date
              LEFT JOIN px x ON x.symbol=s.symbol AND x.trade_date=cx.trade_date
            )
            SELECT *,
              CASE WHEN fillable_20d THEN gross20-{COST_MAIN} END AS net20bps,
              CASE WHEN fillable_20d THEN gross20-{COST_SHADOW} END AS net50bps
            FROM o
        """)

        print("BUILD same-date scoring-universe benchmark ...")
        con.execute("""
            CREATE TEMP TABLE benchmark AS
            SELECT
              signal_date,
              count(*) FILTER(WHERE fillable_20d) AS benchmark_n20,
              avg(gross20) FILTER(WHERE fillable_20d) AS benchmark_mean20,
              median(gross20) FILTER(WHERE fillable_20d) AS benchmark_median20
            FROM outcomes
            GROUP BY signal_date
        """)

        con.execute("""
            CREATE TEMP TABLE profile_outcomes AS
            SELECT
              o.*,
              b.benchmark_n20,
              b.benchmark_mean20,
              b.benchmark_median20,
              CASE WHEN o.fillable_20d THEN o.gross20-b.benchmark_mean20 END
                AS alpha_mean_benchmark20,
              CASE WHEN o.fillable_20d THEN o.gross20-b.benchmark_median20 END
                AS alpha_median_benchmark20
            FROM outcomes o
            JOIN benchmark b USING(signal_date)
            WHERE o.profile IN ('LOW_VOL20','HIGH_VOL20')
        """)

        missing_benchmark = int(con.execute("""
            SELECT count(*)
            FROM profile_outcomes
            WHERE fillable_20d
              AND (benchmark_mean20 IS NULL OR benchmark_median20 IS NULL)
        """).fetchone()[0])
        if missing_benchmark:
            raise RuntimeError(f"FAIL_CLOSED missing benchmark rows={missing_benchmark}")

        print("BUILD daily cohort diagnostics ...")
        con.execute("""
            CREATE TEMP TABLE cohorts AS
            SELECT
              signal_date,
              profile,
              max(cross_section_n) AS cross_section_n,
              count(*) AS selected_n,
              count(*) FILTER(WHERE complete_20d) AS complete_n,
              count(*) FILTER(WHERE fillable_20d) AS fillable_n,
              avg(realized_vol_60) AS avg_signal_vol60,
              avg(gross20) FILTER(WHERE fillable_20d) AS cohort_mean_gross20,
              median(gross20) FILTER(WHERE fillable_20d) AS cohort_median_gross20,
              stddev_samp(gross20) FILTER(WHERE fillable_20d) AS cohort_stock_dispersion20,
              avg(net20bps) FILTER(WHERE fillable_20d) AS cohort_mean_net20bps,
              median(net20bps) FILTER(WHERE fillable_20d) AS cohort_median_net20bps,
              avg(net50bps) FILTER(WHERE fillable_20d) AS cohort_mean_net50bps,
              median(net50bps) FILTER(WHERE fillable_20d) AS cohort_median_net50bps,
              max(benchmark_mean20) AS benchmark_mean20,
              avg(gross20) FILTER(WHERE fillable_20d)-max(benchmark_mean20)
                AS cohort_alpha_mean20
            FROM profile_outcomes
            GROUP BY signal_date, profile
        """)

        con.execute("""
            CREATE TEMP TABLE spread AS
            WITH p AS (
              SELECT
                signal_date,
                max(CASE WHEN profile='LOW_VOL20' THEN cohort_mean_gross20 END) AS low_mean_gross20,
                max(CASE WHEN profile='HIGH_VOL20' THEN cohort_mean_gross20 END) AS high_mean_gross20,
                max(CASE WHEN profile='LOW_VOL20' THEN cohort_alpha_mean20 END) AS low_alpha_mean20,
                max(CASE WHEN profile='HIGH_VOL20' THEN cohort_alpha_mean20 END) AS high_alpha_mean20
              FROM cohorts
              GROUP BY signal_date
            )
            SELECT *,
              low_mean_gross20-high_mean_gross20 AS low_minus_high_gross20,
              low_alpha_mean20-high_alpha_mean20 AS low_minus_high_alpha20
            FROM p
            WHERE low_mean_gross20 IS NOT NULL
              AND high_mean_gross20 IS NOT NULL
        """)

        era_case = """
          CASE
            WHEN year(signal_date) <= 1994 THEN '1990-1994'
            WHEN year(signal_date) <= 1999 THEN '1995-1999'
            WHEN year(signal_date) <= 2004 THEN '2000-2004'
            WHEN year(signal_date) <= 2009 THEN '2005-2009'
            WHEN year(signal_date) <= 2014 THEN '2010-2014'
            WHEN year(signal_date) <= 2019 THEN '2015-2019'
            WHEN year(signal_date) <= 2024 THEN '2020-2024'
            ELSE '2025-2026'
          END
        """

        cohort_summary_sql = """
          SELECT
            profile,
            count(*) AS signal_days,
            avg(selected_n) AS avg_selected_n,
            avg(fillable_n) AS avg_fillable_n,
            avg(fillable_n*1.0/nullif(complete_n,0)) AS avg_fill_rate,
            avg(avg_signal_vol60) AS avg_signal_vol60,
            avg(cohort_mean_gross20) AS avg_cohort_mean_gross20,
            median(cohort_mean_gross20) AS median_cohort_mean_gross20,
            avg(CASE WHEN cohort_mean_gross20>0 THEN 1.0 ELSE 0.0 END) AS cohort_positive_rate,
            avg(cohort_stock_dispersion20) AS avg_stock_dispersion20,
            avg(cohort_mean_net20bps) AS avg_cohort_mean_net20bps,
            median(cohort_mean_net20bps) AS median_cohort_mean_net20bps,
            avg(cohort_mean_net50bps) AS avg_cohort_mean_net50bps,
            median(cohort_mean_net50bps) AS median_cohort_mean_net50bps,
            avg(cohort_alpha_mean20) AS avg_cohort_alpha_mean20,
            median(cohort_alpha_mean20) AS median_cohort_alpha_mean20,
            avg(CASE WHEN cohort_alpha_mean20>0 THEN 1.0 ELSE 0.0 END)
              AS cohort_alpha_positive_rate
          FROM cohorts
          GROUP BY profile
          ORDER BY profile
        """

        era_sql = f"""
          SELECT
            profile,
            {era_case} AS era_5y,
            count(*) AS signal_days,
            avg(avg_signal_vol60) AS avg_signal_vol60,
            avg(cohort_mean_gross20) AS avg_cohort_mean_gross20,
            median(cohort_mean_gross20) AS median_cohort_mean_gross20,
            avg(cohort_mean_net20bps) AS avg_cohort_mean_net20bps,
            median(cohort_mean_net20bps) AS median_cohort_mean_net20bps,
            avg(cohort_mean_net50bps) AS avg_cohort_mean_net50bps,
            median(cohort_mean_net50bps) AS median_cohort_mean_net50bps,
            avg(cohort_alpha_mean20) AS avg_cohort_alpha_mean20,
            median(cohort_alpha_mean20) AS median_cohort_alpha_mean20,
            avg(CASE WHEN cohort_alpha_mean20>0 THEN 1.0 ELSE 0.0 END)
              AS cohort_alpha_positive_rate
          FROM cohorts
          GROUP BY profile, era_5y
          ORDER BY profile, era_5y
        """

        spread_summary_sql = """
          SELECT
            count(*) AS signal_days,
            avg(low_minus_high_gross20) AS avg_low_minus_high_gross20,
            median(low_minus_high_gross20) AS median_low_minus_high_gross20,
            avg(CASE WHEN low_minus_high_gross20>0 THEN 1.0 ELSE 0.0 END)
              AS low_beats_high_rate,
            avg(low_minus_high_alpha20) AS avg_low_minus_high_alpha20,
            median(low_minus_high_alpha20) AS median_low_minus_high_alpha20
          FROM spread
        """

        spread_era_sql = f"""
          SELECT
            {era_case} AS era_5y,
            count(*) AS signal_days,
            avg(low_minus_high_gross20) AS avg_low_minus_high_gross20,
            median(low_minus_high_gross20) AS median_low_minus_high_gross20,
            avg(CASE WHEN low_minus_high_gross20>0 THEN 1.0 ELSE 0.0 END)
              AS low_beats_high_rate,
            avg(low_minus_high_alpha20) AS avg_low_minus_high_alpha20,
            median(low_minus_high_alpha20) AS median_low_minus_high_alpha20
          FROM spread
          GROUP BY era_5y
          ORDER BY era_5y
        """

        for sql, name in [
            (cohort_summary_sql, "cohort_summary.csv"),
            (era_sql, "era_summary.csv"),
            (spread_summary_sql, "low_high_spread_summary.csv"),
            (spread_era_sql, "low_high_spread_era.csv"),
        ]:
            con.execute(
                f"COPY ({sql}) TO '{qpath(out/name)}' (HEADER, DELIMITER ',')"
            )

        con.execute(
            f"COPY (SELECT * FROM cohorts ORDER BY signal_date, profile) "
            f"TO '{qpath(out/'daily_cohorts.parquet')}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )

        manifest = {
            "status": "PASS_LOW_VOLATILITY_RESEARCH_V0_1",
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "catalog": str(catalog),
            "source_rows": int(stats[0]),
            "source_symbols": int(stats[1]),
            "source_first_date": safe(stats[2]),
            "source_last_date": safe(stats[3]),
            "duplicate_symbol_dates": int(stats[4]),
            "pit_ratio_invalid_rows": invalid_ratio,
            "pit_bad_rows": pit_bad,
            "price_basis": PRICE_BASIS,
            "fixed_hypothesis": {
                "score": "stddev_samp of prior 60 exact market-session PIT close-to-close returns through T",
                "lookback_market_sessions": VOL_LOOKBACK,
                "low_fraction": FRACTION,
                "high_fraction_control": FRACTION,
                "selection": "tie-safe rank cutoff; all ties included",
                "signal_time": "T close",
                "entry": "T+1 exact market-session OPEN",
                "exit": "T+20 exact market-session CLOSE",
                "cost_main_total_roundtrip_bps": 20,
                "cost_shadow_total_roundtrip_bps": 50,
                "benchmark": "same-signal-date valid scoring universe with same fillability",
                "primary_decision_surface": "daily equal-weight cohort diagnostics",
                "parameter_sweep": False,
            },
            "score_rows": int(score_stats[0]),
            "signal_days": int(score_stats[1]),
            "first_signal_date": safe(score_stats[2]),
            "last_signal_date": safe(score_stats[3]),
            "cross_section_n": {
                "min": int(score_stats[4]),
                "median": float(score_stats[5]),
                "max": int(score_stats[6]),
            },
            "profile_rows": {
                "LOW_VOL20": int(score_stats[7]),
                "HIGH_VOL20": int(score_stats[8]),
            },
            "strict_non_st_pit_applied": False,
            "historical_research_ready": False,
            "output_dir": str(out),
            "outputs": [
                "cohort_summary.csv",
                "era_summary.csv",
                "low_high_spread_summary.csv",
                "low_high_spread_era.csv",
                "daily_cohorts.parquet",
                "run_manifest.json",
            ],
        }
        write_json_atomic(out / "run_manifest.json", manifest)
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
