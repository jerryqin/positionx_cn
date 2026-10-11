from __future__ import annotations

r"""
PositionX — Forecast Semantic Corpus Manual Gate V0.2

Read-only audit of:
  D:\positionx_data\events\forecast_semantic_corpus_v0_1\semantic_events_all.parquet
  D:\positionx_data\events\forecast_semantic_corpus_v0_1\monthly_quality.csv

Purpose:
- fix the V0.1 manual-review sampling defect where LIMIT 100 after ordering
  exclusions first could yield an all-EXCLUDED review file;
- create a balanced, deterministic manual-review sample;
- surface suspicious FOLLOWUP_DOCUMENT classifications before Strategy audit.

No network, OCR, DB/catalog/broker/order mutation.
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

DEFAULT_ROOT = Path(r"D:\positionx_data\events\forecast_semantic_corpus_v0_1")
DEFAULT_PARQUET = DEFAULT_ROOT / "semantic_events_all.parquet"
DEFAULT_MONTHLY = DEFAULT_ROOT / "monthly_quality.csv"
DEFAULT_OUT = DEFAULT_ROOT / "manual_gate_v0_2"

REVIEW_COLUMNS = [
    "announcementId","symbol","secCode","secName","event_time","event_precision",
    "event_date","title","event_kind","explicit_st_at_event",
    "strategy_eligible_semantics","exclusion_reason","report_period",
    "current_profit_min_yuan","current_profit_max_yuan","current_profit_evidence",
    "prior_profit_min_yuan","prior_profit_max_yuan","prior_profit_evidence",
    "reported_net_profit_yoy_min_pct","reported_net_profit_yoy_max_pct",
    "reported_yoy_evidence","profit_state","profit_change_vs_prior_abs_pct",
    "change_type","change_basis","parser_confidence","pdf_path","pdf_sha256"
]


def qpath(p: Path) -> str:
    return str(p).replace("\\", "/").replace("'", "''")


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    args = ap.parse_args()

    root = Path(args.root)
    pq = root / "semantic_events_all.parquet"
    monthly_path = root / "monthly_quality.csv"
    out = root / "manual_gate_v0_2"
    out.mkdir(parents=True, exist_ok=True)

    if not pq.exists():
        raise SystemExit(f"ABORT: missing {pq}")
    if not monthly_path.exists():
        raise SystemExit(f"ABORT: missing {monthly_path}")

    con = duckdb.connect(":memory:")
    p = qpath(pq)

    # Full-corpus diagnostics.
    overall = con.execute(f"""
        SELECT
          count(*) AS rows,
          sum(CASE WHEN strategy_eligible_semantics THEN 1 ELSE 0 END) AS eligible,
          sum(CASE WHEN NOT strategy_eligible_semantics THEN 1 ELSE 0 END) AS excluded,
          sum(CASE WHEN strategy_eligible_semantics AND parser_confidence='HIGH' THEN 1 ELSE 0 END) AS eligible_high,
          sum(CASE WHEN strategy_eligible_semantics AND parser_confidence='MEDIUM' THEN 1 ELSE 0 END) AS eligible_medium,
          sum(CASE WHEN strategy_eligible_semantics AND parser_confidence='LOW' THEN 1 ELSE 0 END) AS eligible_low
        FROM read_parquet('{p}')
    """).fetchone()

    suspicious = con.execute(f"""
        SELECT
          count(*) AS suspicious_followup_rows,
          sum(CASE WHEN regexp_matches(title, '业绩预告.*风险提示') THEN 1 ELSE 0 END) AS risk_prompt_titles,
          sum(CASE WHEN current_profit_min_yuan IS NOT NULL THEN 1 ELSE 0 END) AS with_current_profit,
          sum(CASE WHEN change_type IS NOT NULL AND change_type <> 'UNKNOWN' THEN 1 ELSE 0 END) AS with_direction
        FROM read_parquet('{p}')
        WHERE event_kind='FOLLOWUP_DOCUMENT'
          AND (
               regexp_matches(title, '业绩预告.*风险提示')
            OR current_profit_min_yuan IS NOT NULL
            OR (change_type IS NOT NULL AND change_type <> 'UNKNOWN')
          )
    """).fetchone()

    # 1) 18 excluded rows, prioritizing suspicious follow-ups, then subsidiary,
    # then ST. This is where the current manual file surfaced possible false exclusions.
    excluded = con.execute(f"""
        WITH x AS (
          SELECT *,
            CASE
              WHEN event_kind='FOLLOWUP_DOCUMENT' AND regexp_matches(title, '业绩预告.*风险提示') THEN 0
              WHEN event_kind='FOLLOWUP_DOCUMENT' AND current_profit_min_yuan IS NOT NULL THEN 1
              WHEN event_kind='SUBSIDIARY_FORECAST' THEN 2
              WHEN event_kind='FOLLOWUP_DOCUMENT' THEN 3
              WHEN explicit_st_at_event THEN 4
              ELSE 5
            END AS pri,
            row_number() OVER (
              PARTITION BY
                CASE
                  WHEN event_kind='FOLLOWUP_DOCUMENT' AND regexp_matches(title, '业绩预告.*风险提示') THEN 'RISK_PROMPT'
                  WHEN event_kind='FOLLOWUP_DOCUMENT' AND current_profit_min_yuan IS NOT NULL THEN 'FOLLOWUP_WITH_VALUES'
                  WHEN event_kind='SUBSIDIARY_FORECAST' THEN 'SUBSIDIARY'
                  WHEN event_kind='FOLLOWUP_DOCUMENT' THEN 'FOLLOWUP'
                  WHEN explicit_st_at_event THEN 'ST'
                  ELSE 'OTHER'
                END
              ORDER BY announcementId
            ) AS rn
          FROM read_parquet('{p}')
          WHERE NOT strategy_eligible_semantics
        )
        SELECT * EXCLUDE(pri,rn)
        FROM x
        WHERE rn <= 6
        ORDER BY pri, announcementId
        LIMIT 18
    """).df()
    excluded["review_bucket"] = "EXCLUDED_STRESS"

    # 2) 24 eligible non-HIGH rows, stratified by year and confidence/change type.
    nonhigh = con.execute(f"""
        WITH x AS (
          SELECT *,
            year(try_cast(event_time AS TIMESTAMP)) AS event_year,
            row_number() OVER (
              PARTITION BY year(try_cast(event_time AS TIMESTAMP)),
                           parser_confidence,
                           coalesce(change_type,'NULL')
              ORDER BY announcementId
            ) AS rn
          FROM read_parquet('{p}')
          WHERE strategy_eligible_semantics
            AND parser_confidence <> 'HIGH'
        )
        SELECT * EXCLUDE(event_year,rn)
        FROM x
        WHERE rn <= 2
        ORDER BY
          CASE parser_confidence WHEN 'LOW' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END,
          event_time, announcementId
        LIMIT 24
    """).df()
    nonhigh["review_bucket"] = "ELIGIBLE_NONHIGH_STRESS"

    # 3) 30 eligible HIGH rows, representative across year + semantic direction.
    high = con.execute(f"""
        WITH x AS (
          SELECT *,
            year(try_cast(event_time AS TIMESTAMP)) AS event_year,
            row_number() OVER (
              PARTITION BY year(try_cast(event_time AS TIMESTAMP)),
                           coalesce(change_type,'NULL')
              ORDER BY announcementId
            ) AS rn
          FROM read_parquet('{p}')
          WHERE strategy_eligible_semantics
            AND parser_confidence='HIGH'
        )
        SELECT * EXCLUDE(event_year,rn)
        FROM x
        WHERE rn <= 2
        ORDER BY change_type, event_time, announcementId
        LIMIT 30
    """).df()
    high["review_bucket"] = "ELIGIBLE_HIGH_REPRESENTATIVE"

    review = pd.concat([excluded, nonhigh, high], ignore_index=True)
    review = review[["review_bucket"] + [c for c in REVIEW_COLUMNS if c in review.columns]]
    review_path = out / "manual_review_stratified_72.csv"
    review.to_csv(review_path, index=False, encoding="utf-8-sig")

    # Exact suspicious FOLLOWUP rows for parser-classification inspection.
    susp_df = con.execute(f"""
        SELECT {",".join(REVIEW_COLUMNS)}
        FROM read_parquet('{p}')
        WHERE event_kind='FOLLOWUP_DOCUMENT'
          AND (
               regexp_matches(title, '业绩预告.*风险提示')
            OR current_profit_min_yuan IS NOT NULL
            OR (change_type IS NOT NULL AND change_type <> 'UNKNOWN')
          )
        ORDER BY event_time, announcementId
    """).df()
    susp_path = out / "suspicious_followup_rows.csv"
    susp_df.to_csv(susp_path, index=False, encoding="utf-8-sig")

    monthly = pd.read_csv(monthly_path)
    large = monthly[monthly["eligible_rows"].fillna(0) >= 100].copy()
    weak_large = large[
        (large["current_profit_rate"] < 0.90)
        | (large["change_type_rate"] < 0.85)
        | (large["high_confidence_rate"] < 0.85)
    ]

    summary = {
        "status": "MANUAL_GATE_SAMPLE_READY",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": str(pq),
        "overall": {
            "rows": int(overall[0]),
            "eligible": int(overall[1]),
            "excluded": int(overall[2]),
            "eligible_high": int(overall[3]),
            "eligible_medium": int(overall[4]),
            "eligible_low": int(overall[5]),
        },
        "v01_sampling_defect": (
            "manual_review_100 could be all EXCLUDED because exclusions were sorted first "
            "and LIMIT 100 was applied before guaranteeing eligible strata."
        ),
        "review_sample": {
            "rows": int(len(review)),
            "bucket_counts": {str(k): int(v) for k, v in review["review_bucket"].value_counts().to_dict().items()},
            "confidence_counts": {str(k): int(v) for k, v in review["parser_confidence"].value_counts(dropna=False).to_dict().items()},
            "change_type_counts": {str(k): int(v) for k, v in review["change_type"].value_counts(dropna=False).to_dict().items()},
        },
        "suspicious_followup": {
            "rows": int(suspicious[0]),
            "risk_prompt_titles": int(suspicious[1]),
            "with_current_profit": int(suspicious[2]),
            "with_direction": int(suspicious[3]),
            "output": str(susp_path),
        },
        "large_month_quality_watch": weak_large.to_dict("records"),
        "manual_gate_passed": False,
        "decision_rule": (
            "Do not mark manual gate PASS until the stratified review is inspected. "
            "If suspicious true forecasts are classified FOLLOWUP_DOCUMENT, fix event_kind "
            "semantics and rebuild only affected corpus logic before Strategy audit."
        ),
        "outputs": [str(review_path), str(susp_path), str(out / "manual_gate_summary.json")],
        "no_network": True,
        "no_ocr": True,
        "no_database_mutation": True,
    }
    write_json(out / "manual_gate_summary.json", summary)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"REVIEW: {review_path}")
    print(f"SUSPICIOUS: {susp_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
