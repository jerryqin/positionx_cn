from __future__ import annotations

"""
PositionX — A_SHARE_SHSZ_NON_ST_DAILY_V1 baseline freeze.

Purpose
-------
Freeze the accepted 2026-09-28 raw daily dataset after:
- 27/27 batches DONE
- 5,228 symbols
- 16,646,355 daily rows
- 5,021 current eligible
- 207 expired archive
- PASS_RAW_DAILY_DATASET
- PASS_WITH_DOCUMENTED_SOURCE_ANOMALIES

This script does NOT mutate the source dataset, execution SQLite, broker state,
or research DuckDB.

Default baseline is compact:
- copies metadata / state / manifest / verification / catalog
- computes SHA256 for every Parquet part
- records a dataset-level root hash

Optional --full-copy-data physically copies all Parquet files into the baseline.
"""

import argparse
import csv
import hashlib
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List

DATASET_ROOT = Path(r"D:\positionx_data\market\a_share_daily_v1\A_SHARE_SHSZ_NON_ST_DAILY_V1_20260928")
CATALOG = Path(r"D:\positionx_data\catalog\market_data.duckdb")
BASELINE_ROOT = Path(r"D:\positionx_data\backup\market_data\BASELINE_20260928")

EXPECTED = {
    "dataset_id": "A_SHARE_SHSZ_NON_ST_DAILY_V1_20260928",
    "as_of": "20260928",
    "daily_rows": 16646355,
    "daily_symbols": 5228,
    "current_universe": 5021,
    "expired_archive": 207,
    "quality_status": "PASS_WITH_DOCUMENTED_SOURCE_ANOMALIES",
    "verify_reason": "PASS_RAW_DAILY_DATASET",
}


def sha256_file(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def write_csv_atomic(path: Path, rows: List[Dict[str, Any]], fields: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def list_parquet_parts() -> List[Path]:
    return sorted(DATASET_ROOT.glob("daily_bars/year=*/part-*.parquet"))


def dataset_root_hash(rows: Iterable[Dict[str, Any]]) -> str:
    h = hashlib.sha256()
    for r in sorted(rows, key=lambda x: x["relative_path"]):
        line = f'{r["relative_path"]}|{r["size_bytes"]}|{r["sha256"]}\n'
        h.update(line.encode("utf-8"))
    return h.hexdigest()


def validate_source() -> Dict[str, Any]:
    manifest = json.loads((DATASET_ROOT / "manifest.json").read_text(encoding="utf-8"))
    verification = json.loads((DATASET_ROOT / "verification.json").read_text(encoding="utf-8"))

    checks = {
        "dataset_id": manifest.get("dataset_id") == EXPECTED["dataset_id"],
        "as_of": manifest.get("as_of") == EXPECTED["as_of"],
        "manifest_symbols": int(manifest["scope_policy"]["total_download_symbols"]) == EXPECTED["daily_symbols"],
        "verification_ok": verification.get("ok") is True,
        "verification_reason": verification.get("reason") == EXPECTED["verify_reason"],
        "quality_status": verification.get("quality_status") == EXPECTED["quality_status"],
        "verification_rows": int(verification["parquet_stats"]["rows"]) == EXPECTED["daily_rows"],
        "verification_symbols": int(verification["parquet_stats"]["symbols"]) == EXPECTED["daily_symbols"],
        "no_missing_symbols": verification.get("symbols_missing_from_daily") == [],
        "no_unexpected_symbols": verification.get("unexpected_daily_symbols") == [],
        "no_unexpected_ohlc": verification.get("unexpected_bad_ohlc_rows") == [],
        "known_ohlc_count": len(verification.get("known_bad_ohlc_rows", [])) == 2,
    }
    bad = [k for k, v in checks.items() if not v]
    if bad:
        raise RuntimeError(f"Source dataset failed baseline preconditions: {bad}")

    if not CATALOG.exists():
        raise RuntimeError(f"Catalog not found: {CATALOG}")

    import duckdb
    con = duckdb.connect(str(CATALOG), read_only=True)
    try:
        catalog_counts = {
            "daily_rows": int(con.execute("SELECT count(*) FROM market.daily_bars_v1").fetchone()[0]),
            "daily_symbols": int(con.execute("SELECT count(DISTINCT symbol) FROM market.daily_bars_v1").fetchone()[0]),
            "current_universe": int(con.execute("SELECT count(*) FROM market.current_universe_v1").fetchone()[0]),
            "expired_archive": int(con.execute("SELECT count(*) FROM market.expired_archive_universe_v1").fetchone()[0]),
        }
    finally:
        con.close()

    for k in ("daily_rows", "daily_symbols", "current_universe", "expired_archive"):
        if catalog_counts[k] != EXPECTED[k]:
            raise RuntimeError(f"Catalog count mismatch {k}: got={catalog_counts[k]} expected={EXPECTED[k]}")

    return {
        "manifest": manifest,
        "verification": verification,
        "catalog_counts": catalog_counts,
        "checks": checks,
    }


def copy_small_assets(dst: Path) -> List[Dict[str, Any]]:
    copied = []
    candidates = [
        DATASET_ROOT / "manifest.json",
        DATASET_ROOT / "verification.json",
        DATASET_ROOT / "meta",
        DATASET_ROOT / "state" / "build_state.json",
        DATASET_ROOT / "state" / "failed_symbols.csv",
        CATALOG,
    ]
    for src in candidates:
        if not src.exists():
            continue
        if src.is_dir():
            target = dst / "dataset" / src.name
            shutil.copytree(src, target, dirs_exist_ok=True)
            for p in sorted(target.rglob("*")):
                if p.is_file():
                    copied.append({
                        "baseline_path": str(BASELINE_ROOT / p.relative_to(dst)),
                        "size_bytes": p.stat().st_size,
                        "sha256": sha256_file(p),
                    })
        else:
            if src == CATALOG:
                target = dst / "catalog" / CATALOG.name
            elif src.parent.name == "state":
                target = dst / "dataset" / "state" / src.name
            else:
                target = dst / "dataset" / src.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            copied.append({
                "baseline_path": str(BASELINE_ROOT / target.relative_to(dst)),
                "size_bytes": target.stat().st_size,
                "sha256": sha256_file(target),
            })
    return copied


def full_copy_parts(parts: List[Path], dst: Path) -> None:
    for i, src in enumerate(parts, 1):
        rel = src.relative_to(DATASET_ROOT)
        target = dst / "dataset" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        if i == 1 or i % 100 == 0 or i == len(parts):
            print(f"[FULL COPY] {i}/{len(parts)} {rel}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full-copy-data", action="store_true",
                    help="physically copy all Parquet parts into the baseline")
    args = ap.parse_args()

    if BASELINE_ROOT.exists():
        raise SystemExit(
            f"ABORT: baseline already exists: {BASELINE_ROOT}\n"
            "Nothing modified. Use the verifier instead."
        )

    source = validate_source()

    tmp_root = BASELINE_ROOT.with_name(BASELINE_ROOT.name + ".tmp")
    if tmp_root.exists():
        shutil.rmtree(tmp_root)
    tmp_root.mkdir(parents=True)

    print("=" * 100)
    print("POSITIONX A_SHARE_SHSZ_NON_ST_DAILY_V1 — BASELINE FREEZE")
    print("=" * 100)
    print(f"SOURCE  : {DATASET_ROOT}")
    print(f"CATALOG : {CATALOG}")
    print(f"BASELINE: {BASELINE_ROOT}")
    print(f"MODE    : {'FULL_COPY' if args.full_copy_data else 'COMPACT_HASH_BASELINE'}")
    print()

    parts = list_parquet_parts()
    if not parts:
        shutil.rmtree(tmp_root, ignore_errors=True)
        raise RuntimeError("No Parquet parts found.")

    inventory = []
    total_bytes = 0
    for i, p in enumerate(parts, 1):
        rel = p.relative_to(DATASET_ROOT).as_posix()
        size = p.stat().st_size
        digest = sha256_file(p)
        total_bytes += size
        inventory.append({
            "relative_path": rel,
            "size_bytes": size,
            "sha256": digest,
        })
        if i == 1 or i % 100 == 0 or i == len(parts):
            print(f"[HASH] {i}/{len(parts)} {rel}")

    root_hash = dataset_root_hash(inventory)
    small_assets = copy_small_assets(tmp_root)

    if args.full_copy_data:
        full_copy_parts(parts, tmp_root)

    write_csv_atomic(
        tmp_root / "daily_parts_sha256.csv",
        inventory,
        ["relative_path", "size_bytes", "sha256"],
    )

    baseline_manifest = {
        "baseline_id": "A_SHARE_SHSZ_NON_ST_DAILY_V1_BASELINE_20260928",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_dataset_root": str(DATASET_ROOT),
        "source_catalog": str(CATALOG),
        "dataset_id": EXPECTED["dataset_id"],
        "as_of": EXPECTED["as_of"],
        "mode": "FULL_COPY" if args.full_copy_data else "COMPACT_HASH_BASELINE",
        "source_verified": True,
        "source_catalog_counts": source["catalog_counts"],
        "parquet_part_count": len(parts),
        "parquet_total_bytes": total_bytes,
        "parquet_root_sha256": root_hash,
        "copied_small_assets": small_assets,
        "expected": EXPECTED,
        "historical_research_ready": False,
        "notes": [
            "Raw daily Parquet is accepted and immutable.",
            "Two documented 1994 ex-right OHLC anomalies are preserved.",
            "Strict all-market historical research still requires PIT ST status.",
        ],
    }
    write_json_atomic(tmp_root / "baseline_manifest.json", baseline_manifest)

    # Rename only after every artifact has been written successfully.
    os.replace(tmp_root, BASELINE_ROOT)

    print()
    print(json.dumps({
        "status": "BASELINE_CREATED",
        "baseline": str(BASELINE_ROOT),
        "mode": baseline_manifest["mode"],
        "parquet_parts": len(parts),
        "parquet_total_bytes": total_bytes,
        "parquet_root_sha256": root_hash,
        "catalog_counts": source["catalog_counts"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
