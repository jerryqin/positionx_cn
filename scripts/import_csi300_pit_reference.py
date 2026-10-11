from __future__ import annotations
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
PACKAGED_DB = ROOT / "data" / "research" / "positionx_reference.db"
TARGET_DB = Path.cwd() / "data" / "research" / "positionx_reference.db"

if not PACKAGED_DB.exists():
    raise SystemExit(f"Packaged DB missing: {PACKAGED_DB}")

TARGET_DB.parent.mkdir(parents=True, exist_ok=True)

if TARGET_DB.resolve() == PACKAGED_DB.resolve():
    print(f"READY: {TARGET_DB}")
else:
    if TARGET_DB.exists():
        backup = TARGET_DB.with_suffix(".db.bak_before_csi300_pit_v1")
        if not backup.exists():
            shutil.copy2(TARGET_DB, backup)
        # Merge only the CSI300 reference tables from packaged DB.
        conn = sqlite3.connect(TARGET_DB)
        conn.execute("ATTACH DATABASE ? AS src", (str(PACKAGED_DB),))
        conn.execute("BEGIN IMMEDIATE")
        try:
            for ddl_row in conn.execute(
                """SELECT sql FROM src.sqlite_master
                   WHERE type='table'
                     AND name IN ('dataset_registry','csi300_membership_daily',
                                  'csi300_membership_interval','csi300_adjustment',
                                  'csi300_quality_record')
                   ORDER BY name"""
            ):
                if ddl_row[0]:
                    conn.execute(ddl_row[0])
            for table in (
                "dataset_registry",
                "csi300_membership_daily",
                "csi300_membership_interval",
                "csi300_adjustment",
                "csi300_quality_record",
            ):
                conn.execute(f"DELETE FROM {table}")
                conn.execute(f"INSERT INTO {table} SELECT * FROM src.{table}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        shutil.copy2(PACKAGED_DB, TARGET_DB)

with sqlite3.connect(TARGET_DB) as conn:
    daily = conn.execute("SELECT COUNT(*) FROM csi300_membership_daily").fetchone()[0]
    intervals = conn.execute("SELECT COUNT(*) FROM csi300_membership_interval").fetchone()[0]
    dates = conn.execute("SELECT MIN(trade_date), MAX(trade_date), COUNT(DISTINCT trade_date) FROM csi300_membership_daily").fetchone()
    current = conn.execute("SELECT COUNT(*) FROM csi300_membership_daily WHERE trade_date='20260924'").fetchone()[0]
    reentry = conn.execute("SELECT COUNT(*) FROM csi300_membership_interval WHERE symbol='601298.SH'").fetchone()[0]

print("CSI300 REFERENCE DB READY")
print(f"path={TARGET_DB}")
print(f"daily_rows={daily} interval_rows={intervals}")
print(f"date_range={dates[0]}..{dates[1]} trade_dates={dates[2]}")
print(f"20260924_members={current}")
print(f"601298_interval_runs={reentry}")
