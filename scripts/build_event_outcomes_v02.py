from __future__ import annotations

r"""
PositionX — Event Outcome Base V0.2 (Phase-Aware)

Input
-----
Latest accepted full Event Base V0.3a:
  D:\positionx_data\events\market_derived_v0_3a\full_*

Purpose
-------
Attach clean, tradable forward-return labels to all 10 event families.

Core semantics
--------------
All event families are compared using the SAME conservative entry rule:

    signal/event at T
    entry = T+1 exact market-session OPEN

This is valid for:
- CLOSE-known events: event is known by T close
- OPEN-known GAP events: event is known after T open, but V0.2 deliberately waits
  until T+1 open so it never assumes a fill at the same opening print that revealed
  the gap.

Exit horizons:
    T+1 / T+3 / T+5 / T+20 / T+60 exact market-session CLOSE

Primary research horizon:
    T+1 OPEN -> T+20 CLOSE

Price basis:
    PIT_BACK_RATIO_COMPARABLE_V1

Diagnostics:
- same-day open->close return is included ONLY for OPEN-phase events and is explicitly
  marked diagnostic/non-entry-safe.
- entry one-price upper-limit and T+20 exit one-price lower-limit are flagged.
- no broker / no execution / no production SQLite / no catalog mutation.
"""

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

CATALOG = Path(r"D:\positionx_data\catalog\market_data.duckdb")
EVENT_ROOT = Path(r"D:\positionx_data\events\market_derived_v0_3a")
OUT_ROOT = Path(r"D:\positionx_data\events\outcomes_v0_2")
PRICE_BASIS = "PIT_BACK_RATIO_COMPARABLE_V1"


def qp(p: Path) -> str:
    return str(p).replace("\\", "/").replace("'", "''")


def latest_event_full():
    for p in sorted([x for x in EVENT_ROOT.glob("full_*") if x.is_dir()], reverse=True):
        mf = p / "run_manifest.json"
        ev = p / "events.parquet"
        if not mf.exists() or not ev.exists():
            continue
        m = json.loads(mf.read_text(encoding="utf-8"))
        if (
            m.get("status") == "PASS_EVENT_BASE_V0_3A"
            and m.get("mode") == "full"
            and m.get("dataset_version") == "0.3a"
        ):
            return p, m
    raise RuntimeError("No accepted Event Base V0.3a full build found.")


def write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--event-dir")
    a = ap.parse_args()

    if a.event_dir:
        event_dir = Path(a.event_dir)
        manifest = json.loads((event_dir / "run_manifest.json").read_text(encoding="utf-8"))
    else:
        event_dir, manifest = latest_event_full()

    event_file = event_dir / "events.parquet"
    if manifest.get("status") != "PASS_EVENT_BASE_V0_3A" or manifest.get("mode") != "full":
        raise RuntimeError("Input is not an accepted Event Base V0.3a full build.")
    if not event_file.exists():
        raise RuntimeError(f"Missing events.parquet: {event_file}")
    if not CATALOG.exists():
        raise RuntimeError(f"Missing market catalog: {CATALOG}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = OUT_ROOT / f"full_{stamp}"
    out.mkdir(parents=True, exist_ok=False)

    outcome_file = out / "event_outcomes_v02.parquet"

    import duckdb
    con = duckdb.connect(str(CATALOG), read_only=True)
    try:
        ep = qp(event_file)
        op = qp(outcome_file)

        q = f"""
        WITH cal AS (
          SELECT
            trade_date,
            row_number() OVER(ORDER BY trade_date) AS cal_seq
          FROM (SELECT DISTINCT trade_date FROM market.daily_bars_v1)
        ),
        raw AS (
          SELECT
            symbol,trade_date,"open","high","low","close",pre_close,suspend_flag,
            lag("close") OVER(
              PARTITION BY symbol ORDER BY trade_date
            ) AS prev_raw_close
          FROM market.daily_bars_v1
        ),
        ratio AS (
          SELECT *,
            CASE
              WHEN prev_raw_close IS NULL THEN 1.0
              WHEN prev_raw_close<=0 OR pre_close<=0 THEN NULL
              ELSE prev_raw_close/pre_close
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
        px AS (
          SELECT
            symbol,trade_date,
            "open","high","low","close",pre_close,suspend_flag,
            "open"*cum_dr AS pit_open,
            "high"*cum_dr AS pit_high,
            "low"*cum_dr AS pit_low,
            "close"*cum_dr AS pit_close
          FROM basis
        ),
        e AS (
          SELECT * FROM read_parquet('{ep}')
        ),
        ed AS (
          SELECT
            e.*,
            c.cal_seq,
            c1.trade_date AS date_1d,
            c3.trade_date AS date_3d,
            c5.trade_date AS date_5d,
            c20.trade_date AS date_20d,
            c60.trade_date AS date_60d
          FROM e
          JOIN cal c ON c.trade_date=e.trade_date
          LEFT JOIN cal c1  ON c1.cal_seq = c.cal_seq + 1
          LEFT JOIN cal c3  ON c3.cal_seq = c.cal_seq + 3
          LEFT JOIN cal c5  ON c5.cal_seq = c.cal_seq + 5
          LEFT JOIN cal c20 ON c20.cal_seq = c.cal_seq + 20
          LEFT JOIN cal c60 ON c60.cal_seq = c.cal_seq + 60
        ),
        j AS (
          SELECT
            ed.*,

            p0.pit_open AS event_day_pit_open,
            p0.pit_close AS event_day_pit_close,

            p1."open" AS entry_raw_open,
            p1."high" AS entry_raw_high,
            p1."low" AS entry_raw_low,
            p1.pre_close AS entry_raw_pre_close,
            p1.suspend_flag AS entry_suspend,
            p1.pit_open AS entry_pit_open,
            p1.pit_close AS close_1d,

            p3.pit_close AS close_3d,
            p5.pit_close AS close_5d,

            p20."high" AS exit20_raw_high,
            p20."low" AS exit20_raw_low,
            p20."close" AS exit20_raw_close,
            p20.pre_close AS exit20_raw_pre_close,
            p20.suspend_flag AS exit20_suspend,
            p20.pit_close AS close_20d,

            p60.pit_close AS close_60d
          FROM ed
          LEFT JOIN px p0
            ON p0.symbol=ed.symbol AND p0.trade_date=ed.trade_date
          LEFT JOIN px p1
            ON p1.symbol=ed.symbol AND p1.trade_date=ed.date_1d
          LEFT JOIN px p3
            ON p3.symbol=ed.symbol AND p3.trade_date=ed.date_3d
          LEFT JOIN px p5
            ON p5.symbol=ed.symbol AND p5.trade_date=ed.date_5d
          LEFT JOIN px p20
            ON p20.symbol=ed.symbol AND p20.trade_date=ed.date_20d
          LEFT JOIN px p60
            ON p60.symbol=ed.symbol AND p60.trade_date=ed.date_60d
        ),
        f AS (
          SELECT *,
            date_1d IS NOT NULL
              AND entry_pit_open IS NOT NULL
              AND entry_pit_open>0
              AND coalesce(entry_suspend,1)=0 AS entry_available,

            coalesce(
              abs(entry_raw_open-entry_raw_high)<1e-10
              AND abs(entry_raw_open-entry_raw_low)<1e-10
              AND entry_raw_open>entry_raw_pre_close,
              false
            ) AS locked_upper_entry,

            date_20d IS NOT NULL
              AND close_20d IS NOT NULL
              AND close_20d>0
              AND coalesce(exit20_suspend,1)=0 AS exit20_available,

            coalesce(
              abs(exit20_raw_close-exit20_raw_high)<1e-10
              AND abs(exit20_raw_close-exit20_raw_low)<1e-10
              AND exit20_raw_close<exit20_raw_pre_close,
              false
            ) AS locked_lower_exit20
          FROM j
        )
        SELECT *,
          entry_available AND NOT locked_upper_entry AS fillable_entry_v01,

          entry_available AND NOT locked_upper_entry
            AND exit20_available AND NOT locked_lower_exit20
            AS tradable_complete_20d_v01,

          CASE WHEN entry_available AND NOT locked_upper_entry
                    AND close_1d IS NOT NULL
               THEN close_1d/entry_pit_open-1 END AS ret_t1open_t1close,

          CASE WHEN entry_available AND NOT locked_upper_entry
                    AND close_3d IS NOT NULL
               THEN close_3d/entry_pit_open-1 END AS ret_t1open_t3close,

          CASE WHEN entry_available AND NOT locked_upper_entry
                    AND close_5d IS NOT NULL
               THEN close_5d/entry_pit_open-1 END AS ret_t1open_t5close,

          CASE WHEN entry_available AND NOT locked_upper_entry
                    AND exit20_available AND NOT locked_lower_exit20
               THEN close_20d/entry_pit_open-1 END AS ret_t1open_t20close,

          CASE WHEN entry_available AND NOT locked_upper_entry
                    AND close_60d IS NOT NULL
               THEN close_60d/entry_pit_open-1 END AS ret_t1open_t60close,

          CASE WHEN event_phase='OPEN'
                    AND event_day_pit_open IS NOT NULL
                    AND event_day_pit_close IS NOT NULL
               THEN event_day_pit_close/event_day_pit_open-1 END
               AS diagnostic_same_day_open_to_close,

          CASE WHEN event_phase='OPEN'
               THEN 'DIAGNOSTIC_ONLY_SAME_OPEN_NOT_ENTRY_SAFE'
               ELSE NULL END AS same_day_diagnostic_guard,

          '{PRICE_BASIS}' AS outcome_price_basis,
          'T_PLUS_1_EXACT_MARKET_SESSION_OPEN' AS entry_semantics,
          'FUTURE_LABEL_ONLY_DO_NOT_USE_AS_SIGNAL' AS leakage_guard
        FROM f
        """

        print("BUILD phase-aware event outcomes V0.2 ...")
        con.execute(f"COPY ({q}) TO '{op}' (FORMAT PARQUET, COMPRESSION ZSTD)")

        base = f"read_parquet('{op}')"
        st = con.execute(f"""
          SELECT
            count(*) AS rows,
            count(DISTINCT event_id) AS unique_ids,
            count(*)-count(DISTINCT event_id) AS duplicate_ids,
            count(*) FILTER(WHERE event_day_pit_close IS NULL) AS missing_event_price,
            count(*) FILTER(WHERE fillable_entry_v01) AS fillable_entry,
            count(*) FILTER(WHERE tradable_complete_20d_v01) AS tradable20,
            count(*) FILTER(WHERE locked_upper_entry) AS blocked_upper,
            count(*) FILTER(WHERE locked_lower_exit20) AS blocked_lower20
          FROM {base}
        """).fetchone()

        if int(st[2]) != 0:
            raise RuntimeError(f"Duplicate event_id={st[2]}")
        if int(st[3]) != 0:
            raise RuntimeError(f"Missing event-day comparable price={st[3]}")

        def csv(sql: str, name: str):
            con.execute(f"COPY ({sql}) TO '{qp(out/name)}' (HEADER, DELIMITER ',')")

        csv(f"""
          SELECT
            event_type,
            event_phase,
            count(*) AS events,
            count(*) FILTER(WHERE fillable_entry_v01) AS fillable_entry,
            avg(CASE WHEN fillable_entry_v01 THEN 1.0 ELSE 0.0 END) AS entry_fill_rate,
            count(*) FILTER(WHERE locked_upper_entry) AS blocked_upper_entry,
            count(*) FILTER(WHERE tradable_complete_20d_v01) AS n20,
            avg(ret_t1open_t1close) FILTER(WHERE fillable_entry_v01) AS avg_ret_1d,
            median(ret_t1open_t1close) FILTER(WHERE fillable_entry_v01) AS median_ret_1d,
            avg(ret_t1open_t5close) FILTER(WHERE fillable_entry_v01) AS avg_ret_5d,
            median(ret_t1open_t5close) FILTER(WHERE fillable_entry_v01) AS median_ret_5d,
            avg(ret_t1open_t20close) FILTER(WHERE tradable_complete_20d_v01) AS avg_ret_20d,
            median(ret_t1open_t20close) FILTER(WHERE tradable_complete_20d_v01) AS median_ret_20d,
            avg(CASE WHEN ret_t1open_t20close>0 THEN 1.0 ELSE 0.0 END)
              FILTER(WHERE tradable_complete_20d_v01) AS win_rate_20d,
            count(*) FILTER(WHERE close_60d IS NOT NULL AND fillable_entry_v01) AS n60,
            avg(ret_t1open_t60close)
              FILTER(WHERE close_60d IS NOT NULL AND fillable_entry_v01) AS avg_ret_60d,
            median(ret_t1open_t60close)
              FILTER(WHERE close_60d IS NOT NULL AND fillable_entry_v01) AS median_ret_60d
          FROM {base}
          GROUP BY event_type,event_phase
          ORDER BY event_type
        """, "event_type_tradable_summary.csv")

        csv(f"""
          SELECT
            event_type,
            first_in_60d,
            count(*) FILTER(WHERE tradable_complete_20d_v01) AS n20,
            avg(ret_t1open_t20close)
              FILTER(WHERE tradable_complete_20d_v01) AS avg_ret_20d,
            median(ret_t1open_t20close)
              FILTER(WHERE tradable_complete_20d_v01) AS median_ret_20d,
            avg(CASE WHEN ret_t1open_t20close>0 THEN 1.0 ELSE 0.0 END)
              FILTER(WHERE tradable_complete_20d_v01) AS win_rate_20d
          FROM {base}
          GROUP BY event_type,first_in_60d
          ORDER BY event_type,first_in_60d DESC
        """, "event_first60_tradable_summary.csv")

        csv(f"""
          SELECT
            event_type,
            count(*) AS events,
            avg(diagnostic_same_day_open_to_close)
              FILTER(WHERE diagnostic_same_day_open_to_close IS NOT NULL)
              AS avg_same_day_open_to_close,
            median(diagnostic_same_day_open_to_close)
              FILTER(WHERE diagnostic_same_day_open_to_close IS NOT NULL)
              AS median_same_day_open_to_close
          FROM {base}
          WHERE event_phase='OPEN'
          GROUP BY event_type
          ORDER BY event_type
        """, "gap_same_day_diagnostic.csv")

        result = {
            "status": "PASS_EVENT_OUTCOMES_V0_2",
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event_source_dir": str(event_dir),
            "event_source_rows": int(manifest["event_rows"]),
            "outcome_rows": int(st[0]),
            "unique_event_ids": int(st[1]),
            "duplicate_event_ids": int(st[2]),
            "missing_event_price": int(st[3]),
            "fillable_entry_rows": int(st[4]),
            "tradable_complete_20d_rows": int(st[5]),
            "blocked_locked_upper_entry": int(st[6]),
            "blocked_locked_lower_exit20": int(st[7]),
            "entry_semantics": "T+1 exact market-session OPEN for ALL event phases",
            "gap_same_day_open_to_close": "diagnostic only; same opening print is not treated as entry-safe",
            "outcome_price_basis": PRICE_BASIS,
            "labels_are_future_information": True,
            "strict_non_st_pit_applied": False,
            "historical_research_ready": False,
            "output_dir": str(out),
        }
        write_json(out/"run_manifest.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        con.close()


if __name__=="__main__":
    raise SystemExit(main())
