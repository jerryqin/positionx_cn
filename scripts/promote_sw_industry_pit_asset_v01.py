from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

import validate_eva_ret120_industry_pit_v02 as validator


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "research" / "positionx_research.duckdb"
RECEIPT_DIR = ROOT / "data" / "research" / "reference" / "sw_industry_pit"

DEFAULT_SOURCE_DIR = Path(
    r"D:\positionx_data\incoming\eva\20260927_ret120_industry_pit"
)
ARCHIVE_ROOT = Path(
    r"D:\positionx_data\reference\industry\sw_pit"
)

ASSET_ID = "SW_INDUSTRY_PIT_L1_V20260927"
ASSET_VERSION = "20260927"
RESEARCH_START = "20200803"
RESEARCH_END = "20260402"
EXPECTED_CSV_SHA256 = (
    "df94c64e5365b82d3f76cff76c1465f4e1f39b257757ec01151d3291ceb7c8f9"
)
BOUNDARY = "20210730"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def tree_manifest(root: Path):
    rows = []
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rel = p.relative_to(root).as_posix()
            rows.append({
                "path": rel,
                "size": p.stat().st_size,
                "sha256": sha256_file(p),
            })
    return rows


def manifest_digest(rows) -> str:
    payload = json.dumps(
        rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def archive_delivery(source_dir: Path, target_dir: Path):
    source_manifest = tree_manifest(source_dir)
    source_digest = manifest_digest(source_manifest)

    if target_dir.exists():
        target_manifest = tree_manifest(target_dir)
        target_digest = manifest_digest(target_manifest)
        if target_digest != source_digest:
            raise RuntimeError(
                "Durable archive already exists but differs from incoming delivery.\n"
                f"archive={target_dir}\n"
                f"incoming_digest={source_digest}\n"
                f"archive_digest={target_digest}"
            )
        return source_manifest, source_digest, "ALREADY_ARCHIVED_IDENTICAL"

    target_dir.parent.mkdir(parents=True, exist_ok=True)

    staging = Path(
        tempfile.mkdtemp(
            prefix=target_dir.name + ".staging.",
            dir=str(target_dir.parent),
        )
    )

    try:
        # tempfile created the staging directory itself; copy contents into it.
        for item in source_dir.iterdir():
            dst = staging / item.name
            if item.is_dir():
                shutil.copytree(item, dst)
            else:
                shutil.copy2(item, dst)

        staged_manifest = tree_manifest(staging)
        staged_digest = manifest_digest(staged_manifest)

        if staged_digest != source_digest:
            raise RuntimeError(
                "Archive staging verification failed: copied tree hash differs."
            )

        staging.rename(target_dir)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise

    return source_manifest, source_digest, "ARCHIVED_NEW"


def prepare_ingest_df(df: pd.DataFrame):
    x = df.copy()
    x["effective_from"] = pd.to_datetime(
        x["effective_from"], format="%Y%m%d"
    ).dt.date
    x["effective_to_exclusive"] = pd.to_datetime(
        x["effective_to_exclusive"].replace({None: pd.NA, "": pd.NA}),
        format="%Y%m%d",
        errors="coerce",
    ).dt.date
    x.insert(0, "asset_id", ASSET_ID)
    x["ingested_at"] = pd.Timestamp.utcnow().tz_localize(None)
    return x


def ingest_duckdb(
    csv_df: pd.DataFrame,
    source_dir: Path,
    archive_dir: Path,
    csv_sha: str,
    tree_digest: str,
):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH))

    try:
        con.execute("BEGIN TRANSACTION")
        con.execute("CREATE SCHEMA IF NOT EXISTS reference")

        con.execute("""
            CREATE TABLE IF NOT EXISTS reference.sw_industry_pit_l1 (
                asset_id VARCHAR NOT NULL,
                symbol VARCHAR NOT NULL,
                effective_from DATE NOT NULL,
                effective_to_exclusive DATE,
                industry_code VARCHAR NOT NULL,
                industry_name VARCHAR NOT NULL,
                standard VARCHAR NOT NULL,
                level VARCHAR NOT NULL,
                source_id VARCHAR NOT NULL,
                source_url VARCHAR NOT NULL,
                provenance_note VARCHAR NOT NULL,
                ingested_at TIMESTAMP NOT NULL
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS reference.sw_industry_pit_assets (
                asset_id VARCHAR PRIMARY KEY,
                asset_family VARCHAR NOT NULL,
                asset_version VARCHAR NOT NULL,
                classification_family VARCHAR NOT NULL,
                taxonomy_versions VARCHAR NOT NULL,
                level VARCHAR NOT NULL,
                row_count BIGINT NOT NULL,
                symbol_count BIGINT NOT NULL,
                research_start VARCHAR,
                research_end VARCHAR,
                source_cutoff VARCHAR,
                source_csv_sha256 VARCHAR NOT NULL,
                archive_tree_sha256 VARCHAR NOT NULL,
                source_delivery_path VARCHAR NOT NULL,
                archive_path VARCHAR NOT NULL,
                taxonomy_boundary VARCHAR NOT NULL,
                crosswalk_policy VARCHAR NOT NULL,
                known_risks_json VARCHAR NOT NULL,
                ingested_at TIMESTAMP NOT NULL
            )
        """)

        ingest_df = prepare_ingest_df(csv_df)
        con.register("_sw_ingest", ingest_df)

        con.execute(
            "DELETE FROM reference.sw_industry_pit_l1 WHERE asset_id = ?",
            [ASSET_ID],
        )
        con.execute(
            "DELETE FROM reference.sw_industry_pit_assets WHERE asset_id = ?",
            [ASSET_ID],
        )

        con.execute("""
            INSERT INTO reference.sw_industry_pit_l1
            SELECT
                asset_id,
                symbol,
                effective_from,
                effective_to_exclusive,
                industry_code,
                industry_name,
                standard,
                level,
                source_id,
                source_url,
                provenance_note,
                ingested_at
            FROM _sw_ingest
        """)

        risks = json.dumps(
            {
                "K1": (
                    "Official SWS master is a rolling publication; historical "
                    "immutability cannot be proven. Treat dates as SWS current "
                    "statement of history."
                ),
                "K2": (
                    "Some source effective timestamps are intraday; delivery "
                    "uses the calendar date."
                ),
                "K3": (
                    "002001.SZ has one documented short record-level source "
                    "disagreement in 2024."
                ),
                "K4": (
                    "SW2014->SW2021 taxonomy transition creates taxonomy "
                    "artifacts. Downstream analysis must be date-local/era-"
                    "stratified; no silent crosswalk."
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )

        now = pd.Timestamp.utcnow().tz_localize(None)
        con.execute("""
            INSERT INTO reference.sw_industry_pit_assets VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
        """, [
            ASSET_ID,
            "sw_industry_pit_l1",
            ASSET_VERSION,
            "Shenwan/SWS",
            "SW2014|SW2021",
            "L1",
            int(len(csv_df)),
            int(csv_df["symbol"].nunique()),
            RESEARCH_START,
            RESEARCH_END,
            "20260924",
            csv_sha,
            tree_digest,
            str(source_dir),
            str(archive_dir),
            BOUNDARY,
            "NO_SILENT_CROSSWALK; DATE_LOCAL_OR_ERA_STRATIFIED_ONLY",
            risks,
            now,
        ])

        con.execute("""
            CREATE OR REPLACE VIEW reference.sw_industry_pit_l1_current AS
            SELECT d.*
            FROM reference.sw_industry_pit_l1 d
            WHERE d.asset_id = (
                SELECT asset_id
                FROM reference.sw_industry_pit_assets
                ORDER BY asset_version DESC, ingested_at DESC
                LIMIT 1
            )
        """)

        con.unregister("_sw_ingest")

        checks = {
            "rows": con.execute(
                "SELECT COUNT(*) FROM reference.sw_industry_pit_l1 "
                "WHERE asset_id = ?",
                [ASSET_ID],
            ).fetchone()[0],
            "symbols": con.execute(
                "SELECT COUNT(DISTINCT symbol) FROM reference.sw_industry_pit_l1 "
                "WHERE asset_id = ?",
                [ASSET_ID],
            ).fetchone()[0],
            "standards": [
                r[0] for r in con.execute(
                    "SELECT DISTINCT standard "
                    "FROM reference.sw_industry_pit_l1 "
                    "WHERE asset_id = ? ORDER BY 1",
                    [ASSET_ID],
                ).fetchall()
            ],
            "overlaps": con.execute("""
                WITH x AS (
                    SELECT
                        symbol,
                        effective_from,
                        effective_to_exclusive,
                        LEAD(effective_from) OVER (
                            PARTITION BY symbol ORDER BY effective_from
                        ) AS next_start
                    FROM reference.sw_industry_pit_l1
                    WHERE asset_id = ?
                )
                SELECT COUNT(*)
                FROM x
                WHERE effective_to_exclusive IS NOT NULL
                  AND next_start IS NOT NULL
                  AND effective_to_exclusive > next_start
            """, [ASSET_ID]).fetchone()[0],
        }

        if checks["rows"] != len(csv_df):
            raise RuntimeError(f"DuckDB row-count mismatch: {checks}")
        if checks["symbols"] != csv_df["symbol"].nunique():
            raise RuntimeError(f"DuckDB symbol-count mismatch: {checks}")
        if checks["overlaps"] != 0:
            raise RuntimeError(f"DuckDB overlap check failed: {checks}")
        if checks["standards"] != ["SW2014", "SW2021"]:
            raise RuntimeError(f"DuckDB standard check failed: {checks}")

        con.execute("COMMIT")
        return checks

    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--source-dir",
        default=str(DEFAULT_SOURCE_DIR),
        help="Eva delivery directory to promote.",
    )
    args = ap.parse_args()

    source_dir = Path(args.source_dir)
    if not source_dir.exists():
        raise FileNotFoundError(source_dir)

    csv_path = source_dir / "ret120_industry_pit.csv"
    notes_path = source_dir / "SOURCE_NOTES.md"

    if not csv_path.exists():
        raise FileNotFoundError(csv_path)
    if not notes_path.exists():
        raise FileNotFoundError(notes_path)

    print("=" * 100)
    print("PROMOTE SW INDUSTRY PIT ASSET V0.1")
    print("=" * 100)
    print("Source:", source_dir)

    validation = validator.validate_file(csv_path)
    if not validation["usable"]:
        raise RuntimeError(
            "Validation v0.2 says asset is not usable. "
            "Do not archive/ingest."
        )

    csv_sha = validation["sha256"]
    if csv_sha != EXPECTED_CSV_SHA256:
        raise RuntimeError(
            "Primary CSV SHA-256 differs from audited Eva delivery.\n"
            f"expected={EXPECTED_CSV_SHA256}\n"
            f"actual={csv_sha}"
        )

    archive_dir = ARCHIVE_ROOT / ASSET_ID
    manifest, tree_digest, archive_status = archive_delivery(
        source_dir,
        archive_dir,
    )

    checks = ingest_duckdb(
        validation["df"],
        source_dir,
        archive_dir,
        csv_sha,
        tree_digest,
    )

    RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
    receipt = {
        "promotion_id": "SW_INDUSTRY_PIT_ASSET_PROMOTION_V01",
        "asset_id": ASSET_ID,
        "asset_version": ASSET_VERSION,
        "archive_status": archive_status,
        "source_dir": str(source_dir),
        "archive_dir": str(archive_dir),
        "source_csv_sha256": csv_sha,
        "archive_tree_sha256": tree_digest,
        "archive_file_count": len(manifest),
        "duckdb_path": str(DB_PATH),
        "duckdb_table": "reference.sw_industry_pit_l1",
        "duckdb_asset_table": "reference.sw_industry_pit_assets",
        "duckdb_current_view": "reference.sw_industry_pit_l1_current",
        "validation": {
            "rows": int(len(validation["df"])),
            "symbols": int(validation["df"]["symbol"].nunique()),
            "standards": validation["standards"],
            "interval_issues": len(validation["interval_issues"]),
            "taxonomy_era_issues": len(validation["era_issues"]),
            "coverage": validation["coverage"].to_dict(orient="records"),
        },
        "duckdb_checks": checks,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }

    receipt_path = RECEIPT_DIR / f"{ASSET_ID}_receipt.json"
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("Validation: PASS")
    print("Archive:", archive_status)
    print("Archive path:", archive_dir)
    print("Archive files:", len(manifest))
    print("Archive tree SHA-256:", tree_digest)
    print()
    print("DuckDB:", DB_PATH)
    print("Table: reference.sw_industry_pit_l1")
    print("Asset registry: reference.sw_industry_pit_assets")
    print("Current view: reference.sw_industry_pit_l1_current")
    print("Rows:", checks["rows"])
    print("Symbols:", checks["symbols"])
    print("Standards:", checks["standards"])
    print("Overlaps:", checks["overlaps"])
    print()
    print("Receipt:", receipt_path)
    print("Promotion complete. Production SQLite was not touched.")


if __name__ == "__main__":
    main()
