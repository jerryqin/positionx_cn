from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import duckdb

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "research" / "positionx_research.duckdb"

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

        raw_rows = 0
        canonical_rows_seen = 0
        missing_files = []

        # Rebuild derived canonical table from immutable raw evidence.
        con.execute("BEGIN TRANSACTION")
        con.execute("DELETE FROM pit_cninfo_earnings_event_versions")

        first_seen_by_id = {}
        versions = set()

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
            edf = df.loc[df["event_class"].isin(["FORECAST", "EXPRESS"])].copy()
            canonical_rows_seen += len(edf)

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
                key = (ann_id, ph)

                if key in versions:
                    con.execute("""
                        UPDATE pit_cninfo_earnings_event_versions
                        SET last_seen_at=CAST(? AS TIMESTAMPTZ)
                        WHERE announcement_id=? AND payload_hash=?
                    """, [observed_at, ann_id, ph])
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
                versions.add(key)

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

        titles = [
            {
                "event_time": str(r[0]),
                "sec_code": r[1],
                "sec_name": r[2],
                "event_class": r[3],
                "title": r[4],
            }
            for r in con.execute("""
                SELECT event_time, sec_code, sec_name, event_class, title
                FROM pit_cninfo_earnings_event_versions
                ORDER BY event_time, sec_code
            """).fetchall()
        ]

        result = {
            "status": "PASS_CNINFO_CANONICAL_REBUILD_V0_2",
            "canonical_semantics": "EXPLICIT_FORECAST_OR_EXPRESS_TITLE_ONLY",
            "captures_scanned": len(captures),
            "raw_query_match_rows_seen": raw_rows,
            "canonical_rows_seen_across_captures": canonical_rows_seen,
            "missing_files": missing_files,
            "canonical_versions": int(final[0]),
            "canonical_announcement_ids": int(final[1]),
            "canonical_min_event_time": str(final[2]) if final[2] is not None else None,
            "canonical_max_event_time": str(final[3]) if final[3] is not None else None,
            "canonical_event_class_counts": classes,
            "canonical_titles": titles,
            "excluded_example_reason": (
                "业绩说明会预告 is not an earnings forecast/express event even if "
                "a broad earnings phrase appears in the title."
            ),
            "note": "Only the derived canonical table was rebuilt; immutable raw evidence was unchanged.",
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
