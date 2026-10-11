from __future__ import annotations

r"""
PositionX — Gap Recovery Candidate Audit V0.1

Goal
----
Test one clean long hypothesis without threshold tuning:

    GAP_DOWN_BOTTOM1PCT + same-day full recovery to >= previous close
    -> T+1 exact market-session OPEN
    -> T+20 exact market-session CLOSE

Controls:
- GAP_DOWN_NO_FULL_RECOVERY
- GAP_UP_FULL_FAILURE       (gap up but closes <= previous close)
- GAP_UP_HOLDS_PREVCLOSE   (gap up and still closes > previous close)

No parameter sweep.
No new Event Base version.
Uses accepted Research Lens V0.2 outcomes/alpha and joins only signal-day RAW OHLC/pre_close
for state classification.

Research-only; no broker/order/execution/production SQLite/catalog mutation.
"""

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

CATALOG = Path(r"D:\positionx_data\catalog\market_data.duckdb")
LENS_ROOT = Path(r"D:\positionx_data\events\research_lens_v0_2")
OUT_ROOT = Path(r"D:\positionx_data\events\gap_recovery_candidate_v0_1")


def qp(p: Path) -> str:
    return str(p).replace("\\", "/").replace("'", "''")


def latest_lens():
    for p in sorted([x for x in LENS_ROOT.glob("full_*") if x.is_dir()], reverse=True):
        mf = p / "run_manifest.json"
        f = p / "event_outcomes_alpha_v02.parquet"
        if not mf.exists() or not f.exists():
            continue
        m = json.loads(mf.read_text(encoding="utf-8"))
        if m.get("status") == "PASS_EVENT_RESEARCH_LENS_V0_2":
            return p, m
    raise RuntimeError("No accepted Event Research Lens V0.2 found.")


def write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lens-dir")
    a = ap.parse_args()

    if a.lens_dir:
        lens = Path(a.lens_dir)
        lm = json.loads((lens / "run_manifest.json").read_text(encoding="utf-8"))
    else:
        lens, lm = latest_lens()

    lf = lens / "event_outcomes_alpha_v02.parquet"
    if lm.get("status") != "PASS_EVENT_RESEARCH_LENS_V0_2":
        raise RuntimeError("Lens input is not accepted V0.2.")
    if not lf.exists() or not CATALOG.exists():
        raise RuntimeError("Missing lens parquet or market catalog.")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = OUT_ROOT / f"full_{stamp}"
    out.mkdir(parents=True, exist_ok=False)

    state_file = out / "gap_state_candidates.parquet"

    import duckdb
    con = duckdb.connect(str(CATALOG), read_only=True)
    try:
        lp = qp(lf)
        sp = qp(state_file)

        q = f"""
        WITH e AS (
          SELECT *
          FROM read_parquet('{lp}')
          WHERE event_type IN ('GAP_DOWN_BOTTOM1PCT','GAP_UP_TOP1PCT')
        ),
        d AS (
          SELECT
            symbol,trade_date,"open","close",pre_close,
            CASE WHEN pre_close>0 THEN "open"/pre_close-1 END AS gap_return,
            CASE WHEN "open">0 THEN "close"/"open"-1 END AS intraday_return,
            CASE WHEN pre_close>0 THEN "close"/pre_close-1 END AS close_vs_preclose
          FROM market.daily_bars_v1
        ),
        x AS (
          SELECT
            e.*,
            d."open" signal_open,
            d."close" signal_close,
            d.pre_close signal_pre_close,
            d.gap_return,
            d.intraday_return,
            d.close_vs_preclose,

            CASE
              WHEN e.event_type='GAP_DOWN_BOTTOM1PCT' AND d."close">=d.pre_close
                THEN 'GAP_DOWN_FULL_RECOVERY'
              WHEN e.event_type='GAP_DOWN_BOTTOM1PCT' AND d."close"<d.pre_close
                THEN 'GAP_DOWN_NO_FULL_RECOVERY'
              WHEN e.event_type='GAP_UP_TOP1PCT' AND d."close"<=d.pre_close
                THEN 'GAP_UP_FULL_FAILURE'
              WHEN e.event_type='GAP_UP_TOP1PCT' AND d."close">d.pre_close
                THEN 'GAP_UP_HOLDS_PREVCLOSE'
              ELSE 'UNCLASSIFIED'
            END AS state_profile,

            CASE WHEN e.consecutive_count=1 THEN true ELSE false END AS cluster_start
          FROM e
          JOIN d USING(symbol,trade_date)
        )
        SELECT *,
          CASE WHEN tradable_complete_20d_v01
               THEN ret_t1open_t20close-0.002 END AS net_ret_20bps,
          CASE WHEN tradable_complete_20d_v01
               THEN ret_t1open_t20close-0.005 END AS net_ret_50bps
        FROM x
        """

        print("BUILD fixed gap-state candidate audit ...")
        con.execute(f"COPY ({q}) TO '{sp}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        base = f"read_parquet('{sp}')"

        st = con.execute(f"""
          SELECT
            count(*),
            count(*) FILTER(WHERE state_profile='UNCLASSIFIED'),
            count(*) FILTER(WHERE tradable_complete_20d_v01)
          FROM {base}
        """).fetchone()
        if int(st[1]) != 0:
            raise RuntimeError(f"Unclassified gap rows={st[1]}")

        def csv(sql: str, name: str):
            con.execute(f"COPY ({sql}) TO '{qp(out/name)}' (HEADER, DELIMITER ',')")

        metrics = """
          count(*) AS raw_events,
          count(*) FILTER(WHERE tradable_complete_20d_v01) AS n20,
          avg(CASE WHEN tradable_complete_20d_v01 THEN 1.0 ELSE 0.0 END) AS tradable_rate,

          avg(ret_t1open_t20close)
            FILTER(WHERE tradable_complete_20d_v01) AS avg_gross20,
          median(ret_t1open_t20close)
            FILTER(WHERE tradable_complete_20d_v01) AS median_gross20,
          avg(CASE WHEN ret_t1open_t20close>0 THEN 1.0 ELSE 0.0 END)
            FILTER(WHERE tradable_complete_20d_v01) AS win_rate20,

          avg(alpha_median_20d)
            FILTER(WHERE tradable_complete_20d_v01) AS avg_alpha20,
          median(alpha_median_20d)
            FILTER(WHERE tradable_complete_20d_v01) AS median_alpha20,
          avg(CASE WHEN alpha_median_20d>0 THEN 1.0 ELSE 0.0 END)
            FILTER(WHERE tradable_complete_20d_v01) AS alpha_win20,

          avg(net_ret_20bps)
            FILTER(WHERE tradable_complete_20d_v01) AS avg_net20bps,
          median(net_ret_20bps)
            FILTER(WHERE tradable_complete_20d_v01) AS median_net20bps,
          avg(net_ret_50bps)
            FILTER(WHERE tradable_complete_20d_v01) AS avg_net50bps,
          median(net_ret_50bps)
            FILTER(WHERE tradable_complete_20d_v01) AS median_net50bps,

          avg(gap_return) AS avg_gap,
          median(gap_return) AS median_gap,
          avg(intraday_return) AS avg_intraday,
          median(intraday_return) AS median_intraday
        """

        csv(f"""
          SELECT state_profile,{metrics}
          FROM {base}
          GROUP BY state_profile
          ORDER BY state_profile
        """, "gap_state_summary.csv")

        csv(f"""
          SELECT state_profile,cluster_start,{metrics}
          FROM {base}
          GROUP BY state_profile,cluster_start
          ORDER BY state_profile,cluster_start DESC
        """, "gap_state_cluster_summary.csv")

        csv(f"""
          SELECT state_profile,first_in_60d,{metrics}
          FROM {base}
          GROUP BY state_profile,first_in_60d
          ORDER BY state_profile,first_in_60d DESC
        """, "gap_state_first60_summary.csv")

        csv(f"""
          SELECT state_profile,era_5y,{metrics}
          FROM {base}
          GROUP BY state_profile,era_5y
          ORDER BY state_profile,era_5y
        """, "gap_state_era_summary.csv")

        # Focused intersection: the long candidate with first-in-60d and cluster start.
        csv(f"""
          SELECT
            state_profile,
            first_in_60d,
            cluster_start,
            {metrics}
          FROM {base}
          WHERE state_profile='GAP_DOWN_FULL_RECOVERY'
          GROUP BY state_profile,first_in_60d,cluster_start
          ORDER BY first_in_60d DESC,cluster_start DESC
        """, "gap_down_full_recovery_focus.csv")

        result = {
            "status": "PASS_GAP_RECOVERY_CANDIDATE_AUDIT_V0_1",
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "lens_source_dir": str(lens),
            "raw_gap_rows": int(st[0]),
            "unclassified_rows": int(st[1]),
            "tradable_complete_20d_rows": int(st[2]),
            "primary_long_hypothesis": "GAP_DOWN_FULL_RECOVERY",
            "long_definition": "GAP_DOWN_BOTTOM1PCT at T and close(T) >= pre_close(T)",
            "entry_exit": "T+1 exact market-session OPEN -> T+20 exact market-session CLOSE",
            "costs_total_roundtrip_bps": [20, 50],
            "parameter_sweep": False,
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
