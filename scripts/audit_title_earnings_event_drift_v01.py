from __future__ import annotations

r"""
PositionX — Title-Classified Earnings Event Drift Audit V0.1

Research question
-----------------
Using only official CNINFO announcement metadata and title semantics:

  FORECAST_POSITIVE_TITLE : title contains 预增 / 扭亏 / 预盈
  FORECAST_NEGATIVE_TITLE : title contains 预减 / 预亏 / 首亏 / 续亏
  FORECAST_GENERIC_TITLE  : explicit earnings forecast but no direction in title
  FORECAST_MIXED_TITLE    : same symbol/date contains both positive & negative title cues
  EXPRESS                 : title-confirmed earnings express event

Does a directional title event show stable post-announcement drift?

No PDF parsing. No cloud financial values. No surprise-number construction.
No parameter sweep.

Timing
------
event_date = official CNINFO event_time calendar date.
entry      = first market-session OPEN strictly AFTER event_date.
exit H     = close of the H-th market session beginning with entry day
             (H = 1, 5, 20, 60).

This is deliberately conservative:
- DAY precision never trades same day.
- SECOND precision also waits until the next market session.
- historical backfill_observed_at is NOT treated as historical first_seen_at.

Primary candidate:
  long FORECAST_POSITIVE_TITLE

Primary risk diagnostic:
  FORECAST_NEGATIVE_TITLE

Research-only. No broker/order/production SQLite/research DB/catalog mutation.
Historical ST PIT is still incomplete, so results remain provisional.
"""

import argparse
import json
import math
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

CATALOG = Path(r"D:\positionx_data\catalog\market_data.duckdb")
EVENTS = Path(
    r"D:\positionx_data\events\cninfo_earnings_history_v0_1"
    r"\runs\pilot_20240101_20260929\canonical_events_all.parquet"
)
OUT_ROOT = Path(r"D:\positionx_data\strategies\title_earnings_event_drift_v0_1")

HORIZONS = (1, 5, 20, 60)
COST_MAIN = 0.002
COST_SHADOW = 0.005
PRICE_BASIS = "PIT_BACK_RATIO_COMPARABLE_V1"

POS_PAT = re.compile(r"预增|扭亏|预盈")
NEG_PAT = re.compile(r"预减|预亏|首亏|续亏")
AMEND_PAT = re.compile(r"修正|更正|补充")


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
    tmp.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, default=safe),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def normalize_symbol(code: str) -> str | None:
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(digits) != 6:
        return None
    if digits.startswith(("5", "6", "9")):
        return digits + ".SH"
    if digits.startswith(("0", "1", "2", "3")):
        return digits + ".SZ"
    # BSE intentionally excluded.
    return None


def classify_forecast_title(title: str) -> str:
    t = str(title or "")
    pos = bool(POS_PAT.search(t))
    neg = bool(NEG_PAT.search(t))
    if pos and neg:
        return "MIXED"
    if pos:
        return "POSITIVE"
    if neg:
        return "NEGATIVE"
    return "GENERIC"


def main() -> int:
    ap = argparse.ArgumentParser(description="PositionX title-classified earnings drift audit V0.1")
    ap.add_argument("--catalog", default=str(CATALOG))
    ap.add_argument("--events", default=str(EVENTS))
    ap.add_argument("--out-root", default=str(OUT_ROOT))
    args = ap.parse_args()

    catalog = Path(args.catalog)
    events_path = Path(args.events)
    out_root = Path(args.out_root)
    if not catalog.exists():
        raise RuntimeError(f"Catalog not found: {catalog}")
    if not events_path.exists():
        raise RuntimeError(f"Events parquet not found: {events_path}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = out_root / f"audit_{stamp}"
    out.mkdir(parents=True, exist_ok=False)

    import duckdb
    con = duckdb.connect(":memory:")
    try:
        con.execute(f"ATTACH '{qpath(catalog)}' AS cat (READ_ONLY)")

        base_stats = con.execute("""
            SELECT
              count(*) AS rows,
              count(DISTINCT symbol) AS symbols,
              min(trade_date) AS first_date,
              max(trade_date) AS last_date,
              count(*) - count(DISTINCT (symbol, trade_date)) AS dup
            FROM cat.market.daily_bars_v1
        """).fetchone()
        if int(base_stats[4]) != 0:
            raise RuntimeError(f"FAIL_CLOSED duplicate symbol/date rows={base_stats[4]}")

        print("LOAD and classify canonical CNINFO events ...")
        ev = con.execute(
            f"SELECT * FROM read_parquet('{qpath(events_path)}')"
        ).df()

        required = {"announcementId","secCode","announcementTitle","event_time",
                    "event_precision","event_class"}
        missing = sorted(required - set(ev.columns))
        if missing:
            raise RuntimeError(f"Events parquet missing columns: {missing}")

        if len(ev) != ev["announcementId"].nunique():
            raise RuntimeError("FAIL_CLOSED duplicate announcementId in canonical events parquet")

        unexpected_classes = sorted(
            set(ev["event_class"].dropna().astype(str)) - {"FORECAST","EXPRESS"}
        )
        if unexpected_classes:
            raise RuntimeError(f"FAIL_CLOSED unexpected event classes={unexpected_classes}")

        ev["symbol"] = ev["secCode"].map(normalize_symbol)
        ev["event_time_ts"] = pd.to_datetime(ev["event_time"], errors="coerce")
        ev["event_date"] = ev["event_time_ts"].dt.date
        ev["amendment_flag"] = ev["announcementTitle"].fillna("").map(
            lambda x: bool(AMEND_PAT.search(str(x)))
        )

        forecast_mask = ev["event_class"].eq("FORECAST")
        ev.loc[forecast_mask, "direction_raw"] = (
            ev.loc[forecast_mask, "announcementTitle"]
            .fillna("")
            .map(classify_forecast_title)
        )
        ev.loc[~forecast_mask, "direction_raw"] = None

        unsupported_symbol = int(ev["symbol"].isna().sum())
        missing_time = int(ev["event_date"].isna().sum())
        if missing_time:
            raise RuntimeError(f"FAIL_CLOSED missing event_time rows={missing_time}")

        ev_valid = ev.loc[ev["symbol"].notna()].copy()

        # Collapse same symbol/date so multiple documents do not multiply portfolio weight.
        # Forecast gets one semantic cluster; EXPRESS is a separate event family.
        clusters = []

        for (symbol, event_date), g in ev_valid.groupby(["symbol","event_date"], sort=False):
            fg = g[g["event_class"] == "FORECAST"]
            if len(fg):
                dirs = set(fg["direction_raw"].dropna().astype(str))
                if "POSITIVE" in dirs and "NEGATIVE" in dirs:
                    direction = "MIXED"
                elif "POSITIVE" in dirs:
                    direction = "POSITIVE"
                elif "NEGATIVE" in dirs:
                    direction = "NEGATIVE"
                elif "MIXED" in dirs:
                    direction = "MIXED"
                else:
                    direction = "GENERIC"
                clusters.append({
                    "symbol": symbol,
                    "event_date": event_date,
                    "event_family": "FORECAST",
                    "profile": f"FORECAST_{direction}_TITLE",
                    "announcement_count": int(len(fg)),
                    "day_precision_count": int((fg["event_precision"]=="DAY").sum()),
                    "second_precision_count": int((fg["event_precision"]=="SECOND").sum()),
                    "amendment_count": int(fg["amendment_flag"].sum()),
                })

            eg = g[g["event_class"] == "EXPRESS"]
            if len(eg):
                clusters.append({
                    "symbol": symbol,
                    "event_date": event_date,
                    "event_family": "EXPRESS",
                    "profile": "EXPRESS",
                    "announcement_count": int(len(eg)),
                    "day_precision_count": int((eg["event_precision"]=="DAY").sum()),
                    "second_precision_count": int((eg["event_precision"]=="SECOND").sum()),
                    "amendment_count": int(eg["amendment_flag"].sum()),
                })

        cdf = pd.DataFrame(clusters)
        if cdf.empty:
            raise RuntimeError("No usable SH/SZ event clusters")

        cdf["event_date"] = pd.to_datetime(cdf["event_date"])
        cdf["year"] = cdf["event_date"].dt.year.astype(int)

        profile_counts = (
            cdf.groupby("profile", dropna=False)
            .agg(
                event_clusters=("profile","size"),
                symbols=("symbol","nunique"),
                event_days=("event_date","nunique"),
                announcements=("announcement_count","sum"),
                amendments=("amendment_count","sum"),
                day_precision=("day_precision_count","sum"),
                second_precision=("second_precision_count","sum"),
            )
            .reset_index()
            .sort_values("profile")
        )
        profile_counts.to_csv(out/"profile_counts.csv", index=False, encoding="utf-8-sig")

        con.register("event_clusters_df", cdf)

        print("BUILD market calendar and PIT comparable prices ...")
        con.execute("""
            CREATE TEMP TABLE cal AS
            SELECT
              trade_date,
              row_number() OVER (ORDER BY trade_date) AS market_idx
            FROM (SELECT DISTINCT trade_date FROM cat.market.daily_bars_v1)
            ORDER BY trade_date
        """)

        con.execute("""
            CREATE TEMP TABLE raw_seq AS
            SELECT
              d.symbol, d.trade_date, d."open", d."high", d."low", d."close",
              d.volume, d.amount, d.pre_close, d.suspend_flag,
              c.market_idx,
              row_number() OVER (PARTITION BY d.symbol ORDER BY d.trade_date) AS symbol_obs_idx,
              lag(d."close") OVER (PARTITION BY d.symbol ORDER BY d.trade_date) AS prev_raw_close
            FROM cat.market.daily_bars_v1 d
            JOIN cal c USING(trade_date)
        """)

        invalid_ratio = int(con.execute("""
            SELECT count(*)
            FROM raw_seq
            WHERE symbol_obs_idx>1
              AND (prev_raw_close IS NULL OR prev_raw_close<=0
                   OR pre_close IS NULL OR pre_close<=0)
        """).fetchone()[0])
        if invalid_ratio:
            raise RuntimeError(f"FAIL_CLOSED invalid PIT ratio prerequisites={invalid_ratio}")

        con.execute("""
            CREATE TEMP TABLE px AS
            WITH ratio AS (
              SELECT *,
                CASE WHEN symbol_obs_idx=1 THEN 1.0
                     ELSE prev_raw_close/pre_close END AS daily_dr
              FROM raw_seq
            ),
            cum AS (
              SELECT *,
                exp(sum(ln(daily_dr)) OVER (
                    PARTITION BY symbol ORDER BY trade_date
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                )) AS cum_dr
              FROM ratio
            )
            SELECT *,
              "open"*cum_dr AS pit_open,
              "close"*cum_dr AS pit_close
            FROM cum
        """)

        pit_bad = int(con.execute("""
            SELECT count(*) FROM px
            WHERE cum_dr IS NULL OR cum_dr<=0 OR pit_open<=0 OR pit_close<=0
        """).fetchone()[0])
        if pit_bad:
            raise RuntimeError(f"FAIL_CLOSED bad PIT comparable rows={pit_bad}")

        print("MAP events to conservative next-session OPEN ...")
        con.execute("""
            CREATE TEMP TABLE mapped AS
            SELECT
              e.*,
              c.trade_date AS entry_date,
              c.market_idx AS entry_idx
            FROM event_clusters_df e
            LEFT JOIN LATERAL (
              SELECT trade_date, market_idx
              FROM cal
              WHERE trade_date > CAST(e.event_date AS DATE)
              ORDER BY trade_date
              LIMIT 1
            ) c ON true
        """)

        # Build event outcomes at fixed horizons.
        union_parts = []
        for h in HORIZONS:
            exit_offset = h - 1
            union_parts.append(f"""
                SELECT
                  m.*,
                  {h} AS horizon,
                  x.trade_date AS exit_date,
                  p0.pit_open AS entry_open_pit,
                  p1.pit_close AS exit_close_pit,
                  CASE
                    WHEN p0.symbol IS NULL OR p1.symbol IS NULL THEN false
                    ELSE true
                  END AS complete_h,
                  CASE
                    WHEN p0.symbol IS NULL OR p1.symbol IS NULL THEN false
                    WHEN p0."open"=p0."high" AND p0."high"=p0."low" AND p0."open">p0.pre_close
                      THEN false
                    WHEN p1."open"=p1."high" AND p1."high"=p1."low" AND p1."close"<p1.pre_close
                      THEN false
                    ELSE true
                  END AS fillable_h,
                  CASE WHEN p0.symbol IS NOT NULL AND p1.symbol IS NOT NULL
                    THEN p1.pit_close/p0.pit_open-1 END AS gross_ret
                FROM mapped m
                LEFT JOIN cal x ON x.market_idx=m.entry_idx+{exit_offset}
                LEFT JOIN px p0 ON p0.symbol=m.symbol AND p0.trade_date=m.entry_date
                LEFT JOIN px p1 ON p1.symbol=m.symbol AND p1.trade_date=x.trade_date
            """)
        con.execute("CREATE TEMP TABLE outcomes AS " + " UNION ALL ".join(union_parts))

        print("BUILD same-entry-date market benchmarks ...")
        con.execute("""
            CREATE TEMP TABLE needed AS
            SELECT DISTINCT entry_date, entry_idx, horizon
            FROM outcomes
            WHERE entry_date IS NOT NULL
        """)

        bench_union = []
        for h in HORIZONS:
            off = h - 1
            bench_union.append(f"""
                SELECT
                  n.entry_date,
                  {h} AS horizon,
                  count(*) FILTER(WHERE fillable) AS benchmark_n,
                  avg(ret) FILTER(WHERE fillable) AS benchmark_mean,
                  median(ret) FILTER(WHERE fillable) AS benchmark_median
                FROM (
                  SELECT
                    n.entry_date,
                    CASE
                      WHEN p0.symbol IS NULL OR p1.symbol IS NULL THEN false
                      WHEN p0."open"=p0."high" AND p0."high"=p0."low" AND p0."open">p0.pre_close
                        THEN false
                      WHEN p1."open"=p1."high" AND p1."high"=p1."low" AND p1."close"<p1.pre_close
                        THEN false
                      ELSE true
                    END AS fillable,
                    CASE WHEN p0.symbol IS NOT NULL AND p1.symbol IS NOT NULL
                      THEN p1.pit_close/p0.pit_open-1 END AS ret
                  FROM needed n
                  JOIN cal x ON x.market_idx=n.entry_idx+{off}
                  JOIN px p0 ON p0.trade_date=n.entry_date
                  JOIN px p1 ON p1.symbol=p0.symbol AND p1.trade_date=x.trade_date
                  WHERE n.horizon={h}
                ) n
                GROUP BY n.entry_date
            """)
        con.execute("CREATE TEMP TABLE benchmark AS " + " UNION ALL ".join(bench_union))

        con.execute(f"""
            CREATE TEMP TABLE enriched AS
            SELECT
              o.*,
              b.benchmark_n,
              b.benchmark_mean,
              b.benchmark_median,
              CASE WHEN o.fillable_h THEN o.gross_ret-b.benchmark_mean END AS alpha_mean,
              CASE WHEN o.fillable_h THEN o.gross_ret-b.benchmark_median END AS alpha_median,
              CASE WHEN o.fillable_h THEN o.gross_ret-{COST_MAIN} END AS net20bps,
              CASE WHEN o.fillable_h THEN o.gross_ret-{COST_SHADOW} END AS net50bps
            FROM outcomes o
            LEFT JOIN benchmark b
              ON b.entry_date=o.entry_date AND b.horizon=o.horizon
        """)

        missing_bench = int(con.execute("""
            SELECT count(*) FROM enriched
            WHERE fillable_h AND benchmark_mean IS NULL
        """).fetchone()[0])
        if missing_bench:
            raise RuntimeError(f"FAIL_CLOSED missing benchmark rows={missing_bench}")

        summary_sql = """
          SELECT
            profile, horizon,
            count(*) AS clusters,
            count(*) FILTER(WHERE complete_h) AS complete,
            count(*) FILTER(WHERE fillable_h) AS fillable,
            count(*) FILTER(WHERE fillable_h)*1.0/
              nullif(count(*) FILTER(WHERE complete_h),0) AS fill_rate,
            avg(gross_ret) FILTER(WHERE fillable_h) AS avg_gross,
            median(gross_ret) FILTER(WHERE fillable_h) AS median_gross,
            avg(CASE WHEN gross_ret>0 THEN 1.0 ELSE 0.0 END)
              FILTER(WHERE fillable_h) AS win_rate,
            avg(alpha_mean) FILTER(WHERE fillable_h) AS avg_alpha_mean,
            median(alpha_mean) FILTER(WHERE fillable_h) AS median_alpha_mean,
            avg(CASE WHEN alpha_mean>0 THEN 1.0 ELSE 0.0 END)
              FILTER(WHERE fillable_h) AS alpha_win_rate,
            avg(net20bps) FILTER(WHERE fillable_h) AS avg_net20bps,
            median(net20bps) FILTER(WHERE fillable_h) AS median_net20bps,
            avg(net50bps) FILTER(WHERE fillable_h) AS avg_net50bps,
            median(net50bps) FILTER(WHERE fillable_h) AS median_net50bps
          FROM enriched
          GROUP BY profile,horizon
          ORDER BY profile,horizon
        """
        yearly_sql = """
          SELECT
            profile, year, horizon,
            count(*) AS clusters,
            count(*) FILTER(WHERE fillable_h) AS fillable,
            avg(gross_ret) FILTER(WHERE fillable_h) AS avg_gross,
            median(gross_ret) FILTER(WHERE fillable_h) AS median_gross,
            avg(alpha_mean) FILTER(WHERE fillable_h) AS avg_alpha_mean,
            median(alpha_mean) FILTER(WHERE fillable_h) AS median_alpha_mean,
            avg(CASE WHEN alpha_mean>0 THEN 1.0 ELSE 0.0 END)
              FILTER(WHERE fillable_h) AS alpha_win_rate,
            avg(net20bps) FILTER(WHERE fillable_h) AS avg_net20bps,
            avg(net50bps) FILTER(WHERE fillable_h) AS avg_net50bps
          FROM enriched
          GROUP BY profile,year,horizon
          ORDER BY profile,year,horizon
        """

        con.execute(
            f"COPY ({summary_sql}) TO '{qpath(out/'profile_outcome_summary.csv')}' "
            "(HEADER, DELIMITER ',')"
        )
        con.execute(
            f"COPY ({yearly_sql}) TO '{qpath(out/'yearly_summary.csv')}' "
            "(HEADER, DELIMITER ',')"
        )
        con.execute(
            f"COPY (SELECT * FROM enriched ORDER BY event_date,symbol,profile,horizon) "
            f"TO '{qpath(out/'event_outcomes.parquet')}' "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )

        # Directional spread uses benchmark-adjusted returns; compare event cohorts by year/horizon.
        spread = con.execute("""
            WITH s AS (
              SELECT
                year, horizon, profile,
                avg(alpha_mean) FILTER(WHERE fillable_h) AS avg_alpha,
                median(alpha_mean) FILTER(WHERE fillable_h) AS median_alpha,
                count(*) FILTER(WHERE fillable_h) AS n
              FROM enriched
              WHERE profile IN ('FORECAST_POSITIVE_TITLE','FORECAST_NEGATIVE_TITLE')
              GROUP BY year,horizon,profile
            )
            SELECT
              year,horizon,
              max(CASE WHEN profile='FORECAST_POSITIVE_TITLE' THEN n END) AS positive_n,
              max(CASE WHEN profile='FORECAST_NEGATIVE_TITLE' THEN n END) AS negative_n,
              max(CASE WHEN profile='FORECAST_POSITIVE_TITLE' THEN avg_alpha END) AS positive_avg_alpha,
              max(CASE WHEN profile='FORECAST_NEGATIVE_TITLE' THEN avg_alpha END) AS negative_avg_alpha,
              max(CASE WHEN profile='FORECAST_POSITIVE_TITLE' THEN median_alpha END) AS positive_median_alpha,
              max(CASE WHEN profile='FORECAST_NEGATIVE_TITLE' THEN median_alpha END) AS negative_median_alpha,
              max(CASE WHEN profile='FORECAST_POSITIVE_TITLE' THEN avg_alpha END)
                - max(CASE WHEN profile='FORECAST_NEGATIVE_TITLE' THEN avg_alpha END)
                AS positive_minus_negative_avg_alpha,
              max(CASE WHEN profile='FORECAST_POSITIVE_TITLE' THEN median_alpha END)
                - max(CASE WHEN profile='FORECAST_NEGATIVE_TITLE' THEN median_alpha END)
                AS positive_minus_negative_median_alpha
            FROM s
            GROUP BY year,horizon
            ORDER BY year,horizon
        """).df()
        spread.to_csv(out/"directional_spread_yearly.csv", index=False, encoding="utf-8-sig")

        profile_counts = pd.read_csv(out/"profile_counts.csv")
        profile_counts_dict = profile_counts.set_index("profile")["event_clusters"].to_dict()

        manifest = {
            "status": "PASS_TITLE_EARNINGS_EVENT_DRIFT_AUDIT_V0_1",
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "catalog": str(catalog),
            "events": str(events_path),
            "source_daily_rows": int(base_stats[0]),
            "source_daily_symbols": int(base_stats[1]),
            "source_daily_first_date": safe(base_stats[2]),
            "source_daily_last_date": safe(base_stats[3]),
            "canonical_announcement_rows": int(len(ev)),
            "canonical_supported_shsz_rows": int(len(ev_valid)),
            "unsupported_or_bse_rows": unsupported_symbol,
            "event_clusters": int(len(cdf)),
            "profile_clusters": {str(k): int(v) for k,v in profile_counts_dict.items()},
            "fixed_hypothesis": {
                "positive_title_terms": ["预增","扭亏","预盈"],
                "negative_title_terms": ["预减","预亏","首亏","续亏"],
                "amendment_terms_diagnostic_only": ["修正","更正","补充"],
                "entry": "first market-session OPEN strictly after official event calendar date",
                "horizons_market_sessions": list(HORIZONS),
                "cost_main_total_roundtrip_bps": 20,
                "cost_shadow_total_roundtrip_bps": 50,
                "benchmark": "same-entry-date whole-market valid universe",
                "parameter_sweep": False,
            },
            "price_basis": PRICE_BASIS,
            "pit_ratio_invalid_rows": invalid_ratio,
            "pit_bad_rows": pit_bad,
            "historical_backfill_first_seen_replay_available": False,
            "historical_surprise_value_pit_ready": False,
            "classic_pead_ready": False,
            "strict_non_st_pit_applied": False,
            "decision_scope": (
                "Candidate discovery only. A positive result must be extended to a longer "
                "historical announcement corpus before Strategy #2 acceptance."
            ),
            "output_dir": str(out),
            "outputs": [
                "profile_counts.csv",
                "profile_outcome_summary.csv",
                "yearly_summary.csv",
                "directional_spread_yearly.csv",
                "event_outcomes.parquet",
                "run_manifest.json",
            ],
        }
        write_json_atomic(out/"run_manifest.json", manifest)
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
