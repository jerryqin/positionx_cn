from __future__ import annotations

r"""
PositionX — Earnings Event Data Readiness Audit V0.1

Purpose
-------
Read-only audit for whether the current Daily PIT intake can support:
1) forward earnings-event research,
2) historical PIT-safe earnings-event research,
3) classic PEAD / earnings-surprise research.

This script does NOT:
- query broker/order/execution APIs
- mutate production SQLite
- mutate market catalog
- mutate research DB
- backfill CNINFO
- query IWA cloud

It only inspects:
- scripts/daily_pit_intake_v01.py
- data/research/positionx_research.duckdb
- capture metadata and already-written raw parquet paths referenced by that DB
"""

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
INTAKE_SCRIPT = ROOT / "scripts" / "daily_pit_intake_v01.py"
RESEARCH_DB = ROOT / "data" / "research" / "positionx_research.duckdb"
OUT_ROOT = Path(r"D:\positionx_data\events\earnings_readiness_v0_1")

TARGET_DATASETS = (
    "cninfo_earnings_announcements",
    "forecast",
    "express",
    "income",
    "fina_indicator",
)

CLOUD_DATASETS = ("forecast", "express", "income", "fina_indicator")

TITLE_RULES = [
    ("FORECAST", re.compile(r"业绩预告|业绩预增|业绩预减|预盈|预亏|首亏|续亏|扭亏")),
    ("EXPRESS", re.compile(r"业绩快报")),
    ("ANNUAL", re.compile(r"年度报告|年报")),
    ("SEMIANNUAL", re.compile(r"半年度报告|半年报")),
    ("QUARTERLY", re.compile(r"季度报告|季报")),
    ("OPERATING_DATA", re.compile(r"主要经营数据|经营业绩")),
]


def safe(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if hasattr(v, "item"):
        try:
            return safe(v.item())
        except Exception:
            pass
    return str(v)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=safe), encoding="utf-8")


def write_csv(path: Path, rows: List[Dict[str, Any]], fields: Optional[List[str]] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        if fields:
            with path.open("w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
        else:
            path.write_text("", encoding="utf-8-sig")
        return
    if fields is None:
        fields = []
        seen = set()
        for r in rows:
            for k in r:
                if k not in seen:
                    fields.append(k)
                    seen.add(k)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def classify_title(title: str) -> str:
    t = str(title or "")
    hits = [name for name, pat in TITLE_RULES if pat.search(t)]
    return "+".join(hits) if hits else "OTHER"


def static_contract(script_text: str) -> Dict[str, Any]:
    checks = {
        "cninfo_forward_source_declared": 'cninfo is forward event-time source' in script_text,
        "cloud_backfill_role_declared": 'BACKFILL' in script_text and 'VALUE SNAPSHOT' in script_text,
        "event_time_observed_at_separate": '"event_time_and_observed_at_separate": True' in script_text,
        "cninfo_forward_truth_manifest": '"cninfo_is_forward_earnings_event_time_source": True' in script_text,
        "cloud_backfill_manifest": '"cloud_financial_tables_are_backfill_snapshot_sources": True' in script_text,
        "cninfo_version_table": "pit_cninfo_announcement_versions" in script_text,
        "first_seen_at_present": "first_seen_at" in script_text,
        "last_seen_at_present": "last_seen_at" in script_text,
        "payload_hash_present": "payload_hash" in script_text,
        "cninfo_event_time_from_announcement_time": 'df["announcementTime"]' in script_text
            and 'df["event_time_iso"]' in script_text,
        "cloud_event_tables_health_unknown": 'name in ("forecast", "express", "income", "fina_indicator")'
            in script_text and 'return "UNKNOWN"' in script_text,
        "cloud_revisit_180_days": '"revisit_days": 180' in script_text,
        "raw_immutable_manifest": '"raw_snapshots_immutable": True' in script_text,
    }

    # Current query breadth matters: broad EARNINGS_PAT does not itself imply broad retrieval.
    keywords = re.findall(r'for kw in \((.*?)\):', script_text, flags=re.S)
    current_kw = []
    if keywords:
        current_kw = re.findall(r'"([^"]+)"', keywords[0])

    m = re.search(r"fetch_cninfo_window\(days\s*=\s*(\d+)", script_text)
    default_days = int(m.group(1)) if m else None

    return {
        "checks": checks,
        "all_core_contract_checks_pass": all(checks.values()),
        "cninfo_query_keywords": current_kw,
        "cninfo_window_days_parameter": default_days,
        "query_scope_assessment": (
            "FORECAST_AND_EXPRESS_FOCUSED"
            if set(current_kw) == {"业绩预告", "业绩快报"}
            else "OTHER"
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="PositionX Earnings Event Data Readiness Audit V0.1")
    ap.add_argument("--intake-script", default=str(INTAKE_SCRIPT))
    ap.add_argument("--research-db", default=str(RESEARCH_DB))
    ap.add_argument("--out-root", default=str(OUT_ROOT))
    args = ap.parse_args()

    intake = Path(args.intake_script)
    db = Path(args.research_db)
    out_root = Path(args.out_root)

    if not intake.exists():
        raise RuntimeError(f"Intake script missing: {intake}")
    if not db.exists():
        raise RuntimeError(f"Research DB missing: {db}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = out_root / f"audit_{stamp}"
    out.mkdir(parents=True, exist_ok=False)

    script_text = intake.read_text(encoding="utf-8")
    contract = static_contract(script_text)

    import duckdb
    con = duckdb.connect(str(db), read_only=True)
    try:
        tables = {
            r[0] for r in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()
        }

        required_tables = {
            "pit_capture_runs",
            "pit_capture_items",
            "pit_cninfo_announcement_versions",
        }
        missing_tables = sorted(required_tables - tables)

        capture_rows: List[Dict[str, Any]] = []
        health_rows: List[Dict[str, Any]] = []
        cninfo_summary: Dict[str, Any] = {
            "table_present": "pit_cninfo_announcement_versions" in tables,
        }
        title_rows: List[Dict[str, Any]] = []
        latest_raw_rows: List[Dict[str, Any]] = []

        if "pit_capture_items" in tables:
            capture_rows = [
                dict(zip(
                    ["capture_id", "dataset", "source", "rows", "source_max_date",
                     "health", "file_path", "sha256", "error"],
                    row
                ))
                for row in con.execute(
                    """
                    SELECT capture_id, dataset, source, rows, source_max_date,
                           health, file_path, sha256, error
                    FROM pit_capture_items
                    WHERE dataset IN (?, ?, ?, ?, ?)
                    ORDER BY capture_id, dataset
                    """,
                    list(TARGET_DATASETS),
                ).fetchall()
            ]

            grouped: Dict[str, List[Dict[str, Any]]] = {}
            for r in capture_rows:
                grouped.setdefault(str(r["dataset"]), []).append(r)

            for ds in TARGET_DATASETS:
                g = grouped.get(ds, [])
                hc = Counter(str(x.get("health")) for x in g)
                dates = sorted(str(x["source_max_date"]) for x in g if x.get("source_max_date"))
                health_rows.append({
                    "dataset": ds,
                    "captures": len(g),
                    "rows_sum_across_captures": sum(int(x.get("rows") or 0) for x in g),
                    "min_source_max_date": dates[0] if dates else None,
                    "max_source_max_date": dates[-1] if dates else None,
                    "health_counts": json.dumps(hc, ensure_ascii=False, sort_keys=True),
                    "latest_capture_id": g[-1]["capture_id"] if g else None,
                    "latest_rows": int(g[-1]["rows"]) if g else None,
                    "latest_health": g[-1]["health"] if g else None,
                    "latest_file_path": g[-1]["file_path"] if g else None,
                })

            # Inspect schema/date range of latest already-captured raw parquet only.
            # No cloud/API call is made.
            for h in health_rows:
                p = h.get("latest_file_path")
                ds = h["dataset"]
                row = {
                    "dataset": ds,
                    "file_path": p,
                    "file_exists": False,
                    "schema_columns": None,
                    "rows": None,
                    "ann_date_min": None,
                    "ann_date_max": None,
                    "end_date_min": None,
                    "end_date_max": None,
                }
                if p:
                    pp = Path(str(p))
                    if pp.exists():
                        row["file_exists"] = True
                        qp = str(pp).replace("'", "''")
                        try:
                            desc = con.execute(
                                f"DESCRIBE SELECT * FROM read_parquet('{qp}')"
                            ).fetchall()
                            cols = [str(x[0]) for x in desc]
                            row["schema_columns"] = ",".join(cols)
                            row["rows"] = int(con.execute(
                                f"SELECT count(*) FROM read_parquet('{qp}')"
                            ).fetchone()[0])
                            if "ann_date" in cols:
                                z = con.execute(
                                    f"SELECT min(ann_date), max(ann_date) "
                                    f"FROM read_parquet('{qp}')"
                                ).fetchone()
                                row["ann_date_min"], row["ann_date_max"] = map(safe, z)
                            if "end_date" in cols:
                                z = con.execute(
                                    f"SELECT min(end_date), max(end_date) "
                                    f"FROM read_parquet('{qp}')"
                                ).fetchone()
                                row["end_date_min"], row["end_date_max"] = map(safe, z)
                        except Exception as e:
                            row["inspect_error"] = f"{type(e).__name__}: {e}"
                latest_raw_rows.append(row)

        if "pit_cninfo_announcement_versions" in tables:
            z = con.execute(
                """
                SELECT
                  count(*) AS versions,
                  count(DISTINCT announcement_id) AS announcement_ids,
                  min(try_cast(event_time AS TIMESTAMP)) AS min_event_time,
                  max(try_cast(event_time AS TIMESTAMP)) AS max_event_time,
                  min(first_seen_at) AS min_first_seen_at,
                  max(first_seen_at) AS max_first_seen_at,
                  count(*) FILTER(WHERE event_precision='SECOND') AS second_precision_versions,
                  count(*) FILTER(WHERE event_precision='DAY') AS day_precision_versions,
                  count(*) FILTER(WHERE event_time IS NULL OR trim(event_time)='') AS missing_event_time
                FROM pit_cninfo_announcement_versions
                """
            ).fetchone()

            versioned = int(con.execute(
                """
                SELECT count(*)
                FROM (
                  SELECT announcement_id
                  FROM pit_cninfo_announcement_versions
                  GROUP BY announcement_id
                  HAVING count(*) > 1
                )
                """
            ).fetchone()[0])

            cninfo_summary.update({
                "versions": int(z[0]),
                "announcement_ids": int(z[1]),
                "min_event_time": safe(z[2]),
                "max_event_time": safe(z[3]),
                "min_first_seen_at": safe(z[4]),
                "max_first_seen_at": safe(z[5]),
                "second_precision_versions": int(z[6]),
                "day_precision_versions": int(z[7]),
                "missing_event_time": int(z[8]),
                "announcement_ids_with_multiple_versions": versioned,
            })

            titles = con.execute(
                """
                SELECT announcement_id, event_time, first_seen_at, sec_code, sec_name, title
                FROM pit_cninfo_announcement_versions
                QUALIFY row_number() OVER (
                  PARTITION BY announcement_id ORDER BY last_seen_at DESC
                ) = 1
                ORDER BY try_cast(event_time AS TIMESTAMP), announcement_id
                """
            ).fetchall()
            for ann_id, event_time, first_seen, sec_code, sec_name, title in titles:
                title_rows.append({
                    "announcement_id": ann_id,
                    "event_time": event_time,
                    "first_seen_at": safe(first_seen),
                    "sec_code": sec_code,
                    "sec_name": sec_name,
                    "title_class": classify_title(title),
                    "title": title,
                })

        # Derive readiness conservatively.
        core_contract = bool(contract["all_core_contract_checks_pass"])
        cn_ids = int(cninfo_summary.get("announcement_ids") or 0)
        min_event = cninfo_summary.get("min_event_time")
        max_event = cninfo_summary.get("max_event_time")

        history_days = None
        if min_event and max_event:
            try:
                a = datetime.fromisoformat(str(min_event).replace(" ", "T"))
                b = datetime.fromisoformat(str(max_event).replace(" ", "T"))
                history_days = (b.date() - a.date()).days
            except Exception:
                pass

        forward_event_time_ready = (
            core_contract
            and cn_ids > 0
            and int(cninfo_summary.get("missing_event_time") or 0) == 0
        )

        # Five years is not a magical statistical threshold; it is simply a conservative
        # readiness floor to reject "a few forward days" as historical research.
        historical_metadata_ready = bool(history_days is not None and history_days >= 365 * 5)

        # By source contract the cloud financial values are backfill snapshots, not
        # forward event-time truth, because no reliable upstream update_time is available.
        historical_surprise_value_pit_ready = False

        classic_pead_ready = (
            historical_metadata_ready
            and historical_surprise_value_pit_ready
        )

        title_class_counts = Counter(r["title_class"] for r in title_rows)

        decision = {
            "forward_cninfo_event_time_ready": forward_event_time_ready,
            "historical_cninfo_metadata_ready": historical_metadata_ready,
            "historical_surprise_value_pit_ready": historical_surprise_value_pit_ready,
            "classic_pead_ready": classic_pead_ready,
            "history_span_days_observed": history_days,
            "recommended_next_research_asset": (
                "CNINFO_HISTORICAL_ANNOUNCEMENT_METADATA_BACKFILL_V0_1"
                if not historical_metadata_ready
                else "TITLE_CLASSIFIED_EARNINGS_EVENT_DRIFT_V0_1"
            ),
            "reason": [
                "CNINFO is structurally suitable as forward event-time evidence because event_time and observed_at are separated and announcement versions are retained.",
                "Current CNINFO intake is retrieval-focused on 业绩预告/业绩快报, not a complete historical earnings-report corpus.",
                "IWA forecast/express/income/fina_indicator are explicitly backfill/value snapshots with UNKNOWN forward freshness and no trusted upstream update timestamp.",
                "Therefore cloud values must not be used as historical PIT surprise truth for classic PEAD yet.",
                "The clean next path is historical CNINFO announcement metadata first; title categories can support a lower-risk event-drift hypothesis before PDF/value extraction.",
            ],
        }

        result = {
            "status": "PASS_EARNINGS_EVENT_READINESS_AUDIT_V0_1",
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "intake_script": str(intake),
            "intake_script_sha256": sha256_file(intake),
            "research_db": str(db),
            "missing_required_tables": missing_tables,
            "static_contract": contract,
            "cninfo_catalog": cninfo_summary,
            "cninfo_latest_title_class_counts": dict(title_class_counts),
            "decision": decision,
            "output_dir": str(out),
        }

        write_json(out / "readiness_summary.json", result)
        write_csv(out / "capture_health.csv", health_rows)
        write_csv(out / "latest_raw_schema.csv", latest_raw_rows)
        write_csv(out / "cninfo_latest_announcements.csv", title_rows)
        write_json(out / "static_contract.json", contract)

        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
