from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict
import hashlib
import json
import shutil

import duckdb

FORWARD_TABLES = (
    "forward_sessions",
    "forward_signals",
    "forward_positions",
    "forward_trades",
    "forward_equity",
)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def table_counts(db_path: Path) -> Dict[str, int]:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        existing = {
            r[0] for r in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()
        }
        return {
            table: (
                int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                if table in existing else -1
            )
            for table in FORWARD_TABLES
        }
    finally:
        con.close()


def checkpoint(db_path: Path) -> None:
    con = duckdb.connect(str(db_path))
    try:
        con.execute("CHECKPOINT")
    finally:
        con.close()


def verify_backup_dir(backup_dir: Path) -> Dict[str, Any]:
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"manifest missing: {manifest_path}")
    manifest = load_json(manifest_path)
    files = manifest.get("files", [])
    if not files:
        raise RuntimeError("manifest contains no files")

    verified = []
    for item in files:
        p = backup_dir / item["name"]
        if not p.exists():
            raise RuntimeError(f"backup file missing: {p}")
        actual = sha256_file(p)
        if actual != item["sha256"]:
            raise RuntimeError(
                f"SHA256 mismatch for {p.name}: expected={item['sha256']} actual={actual}"
            )
        verified.append(p.name)

    db_name = manifest["database"]["backup_name"]
    db_path = backup_dir / db_name
    counts = table_counts(db_path)
    if counts != manifest["database"]["table_counts"]:
        raise RuntimeError(
            f"DuckDB table-count mismatch: manifest={manifest['database']['table_counts']} actual={counts}"
        )

    state_name = manifest.get("state", {}).get("backup_name")
    state = None
    if state_name:
        state = load_json(backup_dir / state_name)
        expected_last = manifest["state"].get("last_processed_date")
        if state.get("last_processed_date") != expected_last:
            raise RuntimeError(
                "state last_processed_date mismatch: "
                f"expected={expected_last} actual={state.get('last_processed_date')}"
            )

    return {
        "ok": True,
        "backup_dir": str(backup_dir),
        "verified_files": verified,
        "table_counts": counts,
        "state": state,
    }


def _copy_and_record(src: Path, dst: Path, files: list[dict]) -> None:
    shutil.copy2(src, dst)
    files.append({
        "name": dst.name,
        "size": dst.stat().st_size,
        "sha256": sha256_file(dst),
        "source": str(src),
    })


def create_backup(
    *,
    project_root: Path,
    backup_root: Path,
    scheduler_log: Path | None = None,
    force: bool = False,
    only_if_session_today: bool = False,
    retention_days: int = 90,
) -> Dict[str, Any]:
    db = project_root / "data/research/positionx_research.duckdb"
    state_path = project_root / "data/research/forward_deep_oversold_csi500_v1_1/state.json"
    latest_path = project_root / "data/research/forward_deep_oversold_csi500_v1_1/latest.json"
    if not db.exists():
        raise RuntimeError(f"research DuckDB missing: {db}")
    if not state_path.exists():
        raise RuntimeError(f"Forward state missing: {state_path}")

    state = load_json(state_path)
    last_processed = str(state.get("last_processed_date") or "")
    today = datetime.now().strftime("%Y%m%d")

    if only_if_session_today and last_processed != today and not force:
        return {
            "ok": True,
            "status": "SKIPPED_NO_NEW_FORWARD_SESSION",
            "today": today,
            "last_processed_date": last_processed,
        }

    backup_key = (
        last_processed
        if last_processed and last_processed > "20260924"
        else f"BASELINE_{today}"
    )
    final_dir = backup_root / backup_key
    if final_dir.exists():
        verified = verify_backup_dir(final_dir)
        return {
            "ok": True,
            "status": "EXISTING_VERIFIED",
            "backup_dir": str(final_dir),
            "verify": verified,
        }

    backup_root.mkdir(parents=True, exist_ok=True)
    staging = backup_root / f".{backup_key}.staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    checkpoint(db)

    files: list[dict] = []
    db_dst = staging / "positionx_research.duckdb"
    _copy_and_record(db, db_dst, files)
    state_dst = staging / "state.json"
    _copy_and_record(state_path, state_dst, files)

    latest_backup_name = None
    if latest_path.exists():
        latest_dst = staging / "latest.json"
        _copy_and_record(latest_path, latest_dst, files)
        latest_backup_name = latest_dst.name

    log_backup_name = None
    if scheduler_log and scheduler_log.exists():
        log_dst = staging / scheduler_log.name
        _copy_and_record(scheduler_log, log_dst, files)
        log_backup_name = log_dst.name

    counts = table_counts(db_dst)
    manifest = {
        "version": 1,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "backup_key": backup_key,
        "restore_contract": (
            "Restore database + state/latest as one matched snapshot; never mix backup keys."
        ),
        "database": {"backup_name": db_dst.name, "table_counts": counts},
        "state": {
            "backup_name": state_dst.name,
            "status": state.get("status"),
            "historical_cutoff": state.get("historical_cutoff"),
            "started_at": state.get("started_at"),
            "last_processed_date": state.get("last_processed_date"),
            "completed_trades": state.get("completed_trades"),
        },
        "latest": {"backup_name": latest_backup_name},
        "scheduler_log": {"backup_name": log_backup_name},
        "files": files,
    }
    (staging / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    verify_backup_dir(staging)
    staging.rename(final_dir)

    cutoff = datetime.now() - timedelta(days=retention_days)
    removed = []
    for child in backup_root.iterdir():
        if not child.is_dir() or child.name.startswith(".") or child.name.startswith("BASELINE_"):
            continue
        if len(child.name) == 8 and child.name.isdigit():
            try:
                d = datetime.strptime(child.name, "%Y%m%d")
            except ValueError:
                continue
            if d < cutoff:
                shutil.rmtree(child)
                removed.append(child.name)

    return {
        "ok": True,
        "status": "CREATED",
        "backup_dir": str(final_dir),
        "removed_by_retention": removed,
        "verify": verify_backup_dir(final_dir),
    }


def latest_backup_dir(backup_root: Path) -> Path | None:
    if not backup_root.exists():
        return None
    dirs = [
        p for p in backup_root.iterdir()
        if p.is_dir() and not p.name.startswith(".") and (p / "manifest.json").exists()
    ]
    return sorted(dirs, key=lambda p: p.stat().st_mtime, reverse=True)[0] if dirs else None
