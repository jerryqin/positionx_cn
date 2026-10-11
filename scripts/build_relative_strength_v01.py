from __future__ import annotations

r"""
PositionX — Cross-Sectional Relative Strength Research V0.1

Fixed hypothesis; no parameter sweep
------------------------------------
Signal date T:
  score = PIT comparable close[T-5 market sessions]
        / PIT comparable close[T-120 market sessions] - 1

Cross-sectional selection:
  RS_TOP10    = top 10% of valid scores on T, ties included
  RS_BOTTOM10 = bottom 10% of valid scores on T, ties included (diagnostic control)

The most recent 5 market sessions are intentionally skipped.

Execution:
  signal known by T close
  entry = T+1 exact market-session OPEN
  exit  = T+20 exact market-session CLOSE

Costs:
  20 bps main / 50 bps shadow, total round-trip.

Primary decision surface:
  DAILY COHORT results, not raw stock-level signal count.
  This avoids pretending repeated daily names are independent observations.

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
from typing import Any, Dict, List

CATALOG = Path(r"D:\positionx_data\catalog\market_data.duckdb")
OUT_ROOT = Path(r"D:\positionx_data\strategies\relative_strength_v0_1")

LOOKBACK_START = 120
SKIP_RECENT = 5
TOP_FRACTION = 0.10
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
    ap = argparse.ArgumentParser(description="PositionX Relative Strength Research V0.1")
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
            FROM (
              SELECT DISTINCT trade_date
              FROM cat.market.daily_bars_v1
            )
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
            JOIN cal c USING (trade_date)
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
                CASE
                  WHEN symbol_obs_idx = 1 THEN 1.0
                  ELSE prev_raw_close / pre_close
                END AS daily_dr
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

        print("BUILD Ret(120,5) cross-sectional scores ...")
        con.execute(f"""
            CREATE TEMP TABLE scores AS
            WITH anchors AS (
              SELECT
                t.symbol,
                t.trade_date AS signal_date,
                t.market_idx AS signal_idx,
                p5.pit_close AS close_tminus5,
                p120.pit_close AS close_tminus120,
                p5.pit_close / p120.pit_close - 1 AS rs_score
              FROM px t
              JOIN cal c5
                ON c5.market_idx = t.market_idx - {SKIP_RECENT}
              JOIN cal c120
                ON c120.market_idx = t.market_idx - {LOOKBACK_START}
              JOIN px p5
                ON p5.symbol = t.symbol
               AND p5.trade_date = c5.trade_date
              JOIN px p120
                ON p120.symbol = t.symbol
               AND p120.trade_date = c120.trade_date
              WHERE p5.pit_close > 0
                AND p120.pit_close > 0
            ),
            ranked AS (
              SELECT *,
                count(*) OVER (PARTITION BY signal_date) AS cross_section_n,
                rank() OVER (
                  PARTITION BY signal_date
                  ORDER BY rs_score DESC
                ) AS rank_desc,
                rank() OVER (
                  PARTITION BY signal_date
                  ORDER BY rs_score ASC
                ) AS rank_asc
              FROM anchors
            )
            SELECT *,
              CAST(ceil(cross_section_n * {TOP_FRACTION}) AS BIGINT) AS cutoff_k,
              CASE
                WHEN rank_desc <= ceil(cross_section_n * {TOP_FRACTION})
                  THEN 'RS_TOP10'
                WHEN rank_asc <= ceil(cross_section_n * {TOP_FRACTION})
                  THEN 'RS_BOTTOM10'
                ELSE 'RS_MIDDLE80'
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
              count(*) FILTER (WHERE profile='RS_TOP10') AS top_rows,
              count(*) FILTER (WHERE profile='RS_BOTTOM10') AS bottom_rows
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
                  WHEN e."open" = e."high"
                   AND e."high" = e."low"
                   AND e."open" > e.pre_close
                    THEN false
                  WHEN x."open" = x."high"
                   AND x."high" = x."low"
                   AND x."close" < x.pre_close
                    THEN false
                  ELSE true
                END AS fillable_20d,

                CASE
                  WHEN e."open" = e."high"
                   AND e."high" = e."low"
                   AND e."open" > e.pre_close
                    THEN true ELSE false
                END AS blocked_one_price_upper_entry,

                CASE
                  WHEN x."open" = x."high"
                   AND x."high" = x."low"
                   AND x."close" < x.pre_close
                    THEN true ELSE false
                END AS blocked_one_price_lower_exit,

                CASE
                  WHEN e.symbol IS NOT NULL AND x.symbol IS NOT NULL
                    THEN x.pit_close / e.pit_open - 1
                END AS gross20
              FROM scores s
              LEFT JOIN cal ce
                ON ce.market_idx = s.signal_idx + 1
              LEFT JOIN cal cx
                ON cx.market_idx = s.signal_idx + {EXIT_SESSIONS}
              LEFT JOIN px e
                ON e.symbol=s.symbol AND e.trade_date=ce.trade_date
              LEFT JOIN px x
                ON x.symbol=s.symbol AND x.trade_date=cx.trade_date
            )
            SELECT *,
              CASE WHEN fillable_20d THEN gross20 - {COST_MAIN} END AS net20bps,
              CASE WHEN fillable_20d THEN gross20 - {COST_SHADOW} END AS net50bps
            FROM o
        """)

        print("BUILD same-date scoring-universe benchmark ...")
        con.execute("""
            CREATE TEMP TABLE benchmark AS
            SELECT
              signal_date,
              count(*) FILTER (WHERE fillable_20d) AS benchmark_n20,
              avg(gross20) FILTER (WHERE fillable_20d) AS benchmark_mean20,
              median(gross20) FILTER (WHERE fillable_20d) AS benchmark_median20
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
              CASE WHEN o.fillable_20d
                THEN o.gross20 - b.benchmark_mean20 END AS alpha_mean_benchmark20,
              CASE WHEN o.fillable_20d
                THEN o.gross20 - b.benchmark_median20 END AS alpha_median_benchmark20
            FROM outcomes o
            JOIN benchmark b USING(signal_date)
            WHERE o.profile IN ('RS_TOP10','RS_BOTTOM10')
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
              count(*) FILTER (WHERE complete_20d) AS complete_n,
              count(*) FILTER (WHERE fillable_20d) AS fillable_n,
              avg(gross20) FILTER (WHERE fillable_20d) AS cohort_mean_gross20,
              median(gross20) FILTER (WHERE fillable_20d) AS cohort_median_gross20,
              avg(net20bps) FILTER (WHERE fillable_20d) AS cohort_mean_net20bps,
              median(net20bps) FILTER (WHERE fillable_20d) AS cohort_median_net20bps,
              avg(net50bps) FILTER (WHERE fillable_20d) AS cohort_mean_net50bps,
              median(net50bps) FILTER (WHERE fillable_20d) AS cohort_median_net50bps,
              max(benchmark_mean20) AS benchmark_mean20,
              max(benchmark_median20) AS benchmark_median20,
              avg(gross20) FILTER (WHERE fillable_20d) - max(benchmark_mean20)
                AS cohort_alpha_mean20,
              median(gross20) FILTER (WHERE fillable_20d) - max(benchmark_median20)
                AS cohort_alpha_median20
            FROM profile_outcomes
            GROUP BY signal_date, profile
        """)

        # Daily cohort pivot, then explicit top-minus-bottom spread.
        con.execute("""
            CREATE TEMP TABLE spread AS
            WITH p AS (
              SELECT
                signal_date,
                max(CASE WHEN profile='RS_TOP10' THEN cohort_mean_gross20 END) AS top_mean_gross20,
                max(CASE WHEN profile='RS_BOTTOM10' THEN cohort_mean_gross20 END) AS bottom_mean_gross20,
                max(CASE WHEN profile='RS_TOP10' THEN cohort_alpha_mean20 END) AS top_alpha_mean20,
                max(CASE WHEN profile='RS_BOTTOM10' THEN cohort_alpha_mean20 END) AS bottom_alpha_mean20,
                max(CASE WHEN profile='RS_TOP10' THEN cohort_mean_net20bps END) AS top_mean_net20bps,
                max(CASE WHEN profile='RS_BOTTOM10' THEN cohort_mean_net20bps END) AS bottom_mean_net20bps,
                max(CASE WHEN profile='RS_TOP10' THEN cohort_mean_net50bps END) AS top_mean_net50bps,
                max(CASE WHEN profile='RS_BOTTOM10' THEN cohort_mean_net50bps END) AS bottom_mean_net50bps
              FROM cohorts
              GROUP BY signal_date
            )
            SELECT *,
              top_mean_gross20 - bottom_mean_gross20 AS top_minus_bottom_gross20,
              top_alpha_mean20 - bottom_alpha_mean20 AS top_minus_bottom_alpha20,
              top_mean_net20bps - bottom_mean_net20bps AS top_minus_bottom_net20bps,
              top_mean_net50bps - bottom_mean_net50bps AS top_minus_bottom_net50bps
            FROM p
            WHERE top_mean_gross20 IS NOT NULL
              AND bottom_mean_gross20 IS NOT NULL
        """)

        # Helper expression for fixed 5y eras.
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

        profile_summary_sql = """
          SELECT
            profile,
            count(*) AS stock_signal_rows,
            count(*) FILTER (WHERE complete_20d) AS complete_20d,
            count(*) FILTER (WHERE fillable_20d) AS fillable_20d,
            count(*) FILTER (WHERE fillable_20d) * 1.0
              / nullif(count(*) FILTER (WHERE complete_20d),0) AS fill_rate_on_complete,
            avg(gross20) FILTER (WHERE fillable_20d) AS avg_gross20,
            median(gross20) FILTER (WHERE fillable_20d) AS median_gross20,
            avg(CASE WHEN gross20>0 THEN 1.0 ELSE 0.0 END)
              FILTER (WHERE fillable_20d) AS win_rate20,
            avg(net20bps) FILTER (WHERE fillable_20d) AS avg_net20bps,
            median(net20bps) FILTER (WHERE fillable_20d) AS median_net20bps,
            avg(net50bps) FILTER (WHERE fillable_20d) AS avg_net50bps,
            median(net50bps) FILTER (WHERE fillable_20d) AS median_net50bps,
            avg(alpha_mean_benchmark20) FILTER (WHERE fillable_20d)
              AS avg_alpha_mean_benchmark20,
            median(alpha_mean_benchmark20) FILTER (WHERE fillable_20d)
              AS median_alpha_mean_benchmark20,
            avg(CASE WHEN alpha_mean_benchmark20>0 THEN 1.0 ELSE 0.0 END)
              FILTER (WHERE fillable_20d) AS alpha_win_mean_benchmark20
          FROM profile_outcomes
          GROUP BY profile
          ORDER BY profile
        """

        cohort_summary_sql = """
          SELECT
            profile,
            count(*) AS signal_days,
            avg(selected_n) AS avg_selected_n,
            avg(fillable_n) AS avg_fillable_n,
            avg(fillable_n * 1.0 / nullif(complete_n,0)) AS avg_fill_rate,
            avg(cohort_mean_gross20) AS avg_cohort_mean_gross20,
            median(cohort_mean_gross20) AS median_cohort_mean_gross20,
            avg(CASE WHEN cohort_mean_gross20>0 THEN 1.0 ELSE 0.0 END)
              AS cohort_positive_rate,
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
            avg(top_minus_bottom_gross20) AS avg_top_minus_bottom_gross20,
            median(top_minus_bottom_gross20) AS median_top_minus_bottom_gross20,
            avg(CASE WHEN top_minus_bottom_gross20>0 THEN 1.0 ELSE 0.0 END)
              AS top_beats_bottom_rate,
            avg(top_minus_bottom_alpha20) AS avg_top_minus_bottom_alpha20,
            median(top_minus_bottom_alpha20) AS median_top_minus_bottom_alpha20
          FROM spread
        """

        spread_era_sql = f"""
          SELECT
            {era_case} AS era_5y,
            count(*) AS signal_days,
            avg(top_minus_bottom_gross20) AS avg_top_minus_bottom_gross20,
            median(top_minus_bottom_gross20) AS median_top_minus_bottom_gross20,
            avg(CASE WHEN top_minus_bottom_gross20>0 THEN 1.0 ELSE 0.0 END)
              AS top_beats_bottom_rate,
            avg(top_minus_bottom_alpha20) AS avg_top_minus_bottom_alpha20,
            median(top_minus_bottom_alpha20) AS median_top_minus_bottom_alpha20
          FROM spread
          GROUP BY era_5y
          ORDER BY era_5y
        """

        # Persist compact research assets.
        con.execute(
            f"COPY ({profile_summary_sql}) TO '{qpath(out/'stock_signal_summary.csv')}' "
            "(HEADER, DELIMITER ',')"
        )
        con.execute(
            f"COPY ({cohort_summary_sql}) TO '{qpath(out/'cohort_summary.csv')}' "
            "(HEADER, DELIMITER ',')"
        )
        con.execute(
            f"COPY ({era_sql}) TO '{qpath(out/'era_summary.csv')}' "
            "(HEADER, DELIMITER ',')"
        )
        con.execute(
            f"COPY ({spread_summary_sql}) TO '{qpath(out/'top_bottom_spread_summary.csv')}' "
            "(HEADER, DELIMITER ',')"
        )
        con.execute(
            f"COPY ({spread_era_sql}) TO '{qpath(out/'top_bottom_spread_era.csv')}' "
            "(HEADER, DELIMITER ',')"
        )
        con.execute(
            f"COPY (SELECT * FROM cohorts ORDER BY signal_date, profile) "
            f"TO '{qpath(out/'daily_cohorts.parquet')}' "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )

        manifest = {
            "status": "PASS_RELATIVE_STRENGTH_RESEARCH_V0_1",
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
                "score": "PIT close[T-5 market sessions] / PIT close[T-120 market sessions] - 1",
                "skip_recent_market_sessions": SKIP_RECENT,
                "lookback_anchor_market_sessions": LOOKBACK_START,
                "top_fraction": TOP_FRACTION,
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
                "RS_TOP10": int(score_stats[7]),
                "RS_BOTTOM10": int(score_stats[8]),
            },
            "strict_non_st_pit_applied": False,
            "historical_research_ready": False,
            "output_dir": str(out),
            "outputs": [
                "stock_signal_summary.csv",
                "cohort_summary.csv",
                "era_summary.csv",
                "top_bottom_spread_summary.csv",
                "top_bottom_spread_era.csv",
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
