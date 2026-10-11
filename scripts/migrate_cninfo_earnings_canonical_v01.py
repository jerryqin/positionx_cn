from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import duckdb

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "research" / "positionx_research.duckdb"

EARNINGS_PAT = re.compile(
    r"业绩预告|业绩快报|业绩预增|业绩预减|预盈|预亏|"
    r"首亏|续亏|扭亏|年度业绩|半年度业绩|季度业绩|"
    r"主要经营数据|经营业绩|业绩预披露"
)
FORECAST_TITLE_PAT = re.compile(
    r"业绩预告|业绩预增|业绩预减|预盈|预亏|首亏|续亏|扭亏"
)
EXPRESS_TITLE_PAT = re.compile(r"业绩快报")


def classify(title: Any) -> str | None:
    t = str(title or "")
    if EXPRESS_TITLE_PAT.search(t):
        return "EXPRESS"
    if FORECAST_TITLE_PAT.search(t):
        return "FORECAST"
    if EARNINGS_PAT.search(t):
        return "OTHER_EARNINGS"
    return None


def payload_hash_row(row: dict[str, Any]) -> str:
    s = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def main() -> int:
    if not DB.exists():
        raise RuntimeError(f"Research DB missing: {DB}")

    con = duckdb.connect(str(DB))
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pit_cninfo_earnings_event_versions (
                announcement_id VARCHAR,
                payload_hash VARCHAR,
                first_seen_at TIMESTAMPTZ,
                last_seen_at TIMESTAMPTZ,
                event_time VARCHAR,
                event_precision VARCHAR,
                sec_code VARCHAR,
                sec_name VARCHAR,
                title VARCHAR,
                event_class VARCHAR,
                query_keyword VARCHAR,
                pdf_url VARCHAR,
                raw_file_path VARCHAR
            )
        """)

        captures = con.execute("""
            SELECT
              i.capture_id,
              CAST(r.observed_at AS VARCHAR) AS observed_at,
              i.file_path
            FROM pit_capture_items i
            JOIN pit_capture_runs r USING(capture_id)
            WHERE i.dataset='cninfo_earnings_announcements'
              AND i.health='FRESH_QUERY_OK'
              AND i.file_path IS NOT NULL
            ORDER BY r.observed_at, i.capture_id
        """).fetchall()

        existing = con.execute("""
            SELECT announcement_id, payload_hash, CAST(first_seen_at AS VARCHAR)
            FROM pit_cninfo_earnings_event_versions
        """).fetchall()
        first_seen_by_id = {}
        versions = set()
        for ann_id, ph, first_seen in existing:
            first_seen_by_id.setdefault(str(ann_id), first_seen)
            versions.add((str(ann_id), str(ph)))

        raw_rows = 0
        confirmed_rows_seen = 0
        inserted_versions = 0
        refreshed_versions = 0
        missing_files = []

        con.execute("BEGIN TRANSACTION")
        for capture_id, observed_at, file_path in captures:
            p = Path(str(file_path))
            if not p.exists():
                missing_files.append(str(p))
                continue

            q = str(p).replace("'", "''")
            df = con.execute(f"SELECT * FROM read_parquet('{q}')").df()
            raw_rows += len(df)
            if "announcementTitle" not in df.columns:
                raise RuntimeError(f"announcementTitle missing: {p}")

            df["event_class"] = df["announcementTitle"].map(classify)
            edf = df.loc[df["event_class"].notna()].copy()
            confirmed_rows_seen += len(edf)

            for rec in edf.to_dict("records"):
                ann_id = str(rec.get("announcementId"))
                canonical = {
                    "announcementId": rec.get("announcementId"),
                    "secCode": rec.get("secCode"),
                    "secName": rec.get("secName"),
                    "announcementTitle": rec.get("announcementTitle"),
                    "announcementTime": rec.get("announcementTime"),
                    "storageTime": rec.get("storageTime"),
                    "adjunctUrl": rec.get("adjunctUrl"),
                }
                ph = payload_hash_row(canonical)

                if (ann_id, ph) in versions:
                    con.execute("""
                        UPDATE pit_cninfo_earnings_event_versions
                        SET last_seen_at=CAST(? AS TIMESTAMPTZ)
                        WHERE announcement_id=? AND payload_hash=?
                    """, [observed_at, ann_id, ph])
                    refreshed_versions += 1
                    continue

                first_seen = first_seen_by_id.get(ann_id, observed_at)
                con.execute("""
                    INSERT INTO pit_cninfo_earnings_event_versions
                    VALUES (?, ?, CAST(? AS TIMESTAMPTZ), CAST(? AS TIMESTAMPTZ),
                            ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, [
                    ann_id,
                    ph,
                    first_seen,
                    observed_at,
                    rec.get("event_time_iso"),
                    rec.get("event_precision"),
                    str(rec.get("secCode") or ""),
                    str(rec.get("secName") or ""),
                    str(rec.get("announcementTitle") or ""),
                    str(rec.get("event_class") or ""),
                    str(rec.get("query_keyword") or ""),
                    str(rec.get("pdf_url") or ""),
                    str(p),
                ])
                first_seen_by_id.setdefault(ann_id, first_seen)
                versions.add((ann_id, ph))
                inserted_versions += 1

        con.execute("COMMIT")

        final = con.execute("""
            SELECT
              count(*) AS versions,
              count(DISTINCT announcement_id) AS ids,
              min(try_cast(event_time AS TIMESTAMP)) AS min_event_time,
              max(try_cast(event_time AS TIMESTAMP)) AS max_event_time
            FROM pit_cninfo_earnings_event_versions
        """).fetchone()

        classes = dict(con.execute("""
            SELECT event_class, count(*)
            FROM pit_cninfo_earnings_event_versions
            GROUP BY event_class
            ORDER BY event_class
        """).fetchall())

        result = {
            "status": "PASS_CNINFO_CANONICAL_MIGRATION_V0_1",
            "captures_scanned": len(captures),
            "raw_query_match_rows_seen": raw_rows,
            "title_confirmed_rows_seen_across_captures": confirmed_rows_seen,
            "inserted_versions": inserted_versions,
            "refreshed_existing_versions": refreshed_versions,
            "missing_files": missing_files,
            "canonical_versions": int(final[0]),
            "canonical_announcement_ids": int(final[1]),
            "canonical_min_event_time": str(final[2]) if final[2] is not None else None,
            "canonical_max_event_time": str(final[3]) if final[3] is not None else None,
            "canonical_event_class_counts": classes,
            "note": "Raw full-text query-match catalog was not deleted or rewritten.",
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
