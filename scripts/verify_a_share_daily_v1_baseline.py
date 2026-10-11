from __future__ import annotations

"""
Verify PositionX A-share Daily V1 baseline against the frozen source dataset.
Read-only.
"""

import argparse
import csv
import hashlib
import json
from pathlib import Path

DATASET_ROOT = Path(r"D:\positionx_data\market\a_share_daily_v1\A_SHARE_SHSZ_NON_ST_DAILY_V1_20260928")
BASELINE_ROOT = Path(r"D:\positionx_data\backup\market_data\BASELINE_20260928")


def sha256_file(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def root_hash(rows):
    h = hashlib.sha256()
    for r in sorted(rows, key=lambda x: x["relative_path"]):
        h.update(f'{r["relative_path"]}|{r["size_bytes"]}|{r["sha256"]}\n'.encode("utf-8"))
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true",
                    help="skip re-hashing source Parquet; verify baseline metadata only")
    args = ap.parse_args()

    mf = BASELINE_ROOT / "baseline_manifest.json"
    inv = BASELINE_ROOT / "daily_parts_sha256.csv"
    if not mf.exists() or not inv.exists():
        print("VERIFY_FAIL: baseline files missing")
        return 2

    manifest = json.loads(mf.read_text(encoding="utf-8"))
    with inv.open("r", encoding="utf-8-sig", newline="") as f:
        inventory = list(csv.DictReader(f))

    errors = []

    # Verify copied compact assets themselves.
    for item in manifest.get("copied_small_assets", []):
        p = Path(item["baseline_path"])
        if not p.exists():
            errors.append(f"MISSING_BASELINE_ASSET:{p}")
            continue
        if p.stat().st_size != int(item["size_bytes"]):
            errors.append(f"SIZE_MISMATCH_BASELINE_ASSET:{p}")
            continue
        if sha256_file(p) != item["sha256"]:
            errors.append(f"HASH_MISMATCH_BASELINE_ASSET:{p}")

    source_rows = []
    if not args.fast:
        for i, r in enumerate(inventory, 1):
            p = DATASET_ROOT / r["relative_path"]
            if not p.exists():
                errors.append(f"MISSING_SOURCE_PART:{r['relative_path']}")
                continue
            size = p.stat().st_size
            digest = sha256_file(p)
            source_rows.append({
                "relative_path": r["relative_path"],
                "size_bytes": size,
                "sha256": digest,
            })
            if size != int(r["size_bytes"]):
                errors.append(f"SIZE_MISMATCH_SOURCE_PART:{r['relative_path']}")
            if digest != r["sha256"]:
                errors.append(f"HASH_MISMATCH_SOURCE_PART:{r['relative_path']}")
            if i == 1 or i % 100 == 0 or i == len(inventory):
                print(f"[VERIFY HASH] {i}/{len(inventory)} {r['relative_path']}")

        current_root = root_hash(source_rows)
        if current_root != manifest["parquet_root_sha256"]:
            errors.append("ROOT_HASH_MISMATCH")

    full_copy_errors = []
    if manifest.get("mode") == "FULL_COPY":
        for r in inventory:
            p = BASELINE_ROOT / "dataset" / r["relative_path"]
            if not p.exists():
                full_copy_errors.append(f"MISSING_FULL_COPY_PART:{r['relative_path']}")
                continue
            if p.stat().st_size != int(r["size_bytes"]) or sha256_file(p) != r["sha256"]:
                full_copy_errors.append(f"FULL_COPY_MISMATCH:{r['relative_path']}")
        errors.extend(full_copy_errors)

    result = {
        "ok": not errors,
        "baseline_id": manifest["baseline_id"],
        "mode": manifest["mode"],
        "parquet_part_count": len(inventory),
        "expected_root_sha256": manifest["parquet_root_sha256"],
        "errors": errors,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 3


if __name__ == "__main__":
    raise SystemExit(main())
