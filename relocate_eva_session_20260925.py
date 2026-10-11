from __future__ import annotations
import argparse
import csv
import hashlib
import os
from pathlib import Path
import shutil
import sys

PROJECT = Path(r"D:\positionx_min_live")
TARGET = Path(r"D:\positionx_data\incoming\eva\20260925_foundation_assets")
HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "safe_to_move_manifest.csv"
PROTECTED = HERE / "protected_manifest.csv"

def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def load_rows(path: Path):
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true",
                    help="Actually relocate files. Default is dry-run.")
    args = ap.parse_args()

    safe_rows = load_rows(MANIFEST)
    protected_rows = load_rows(PROTECTED)

    print("PROJECT :", PROJECT)
    print("TARGET  :", TARGET)
    print("MODE    :", "EXECUTE" if args.execute else "DRY-RUN")
    print("SAFE    :", len(safe_rows))
    print("PROTECTED:", len(protected_rows))
    print()

    missing = []
    bad_hash = []
    total_bytes = 0

    # Preflight every source before making any change.
    for r in safe_rows:
        src = PROJECT / Path(r["path"])
        if not src.exists():
            missing.append(str(src))
            continue
        total_bytes += int(float(r["size_bytes"]))
        expected = (r.get("sha256") or "").strip().lower()
        if expected:
            actual = sha256(src)
            if actual.lower() != expected:
                bad_hash.append((str(src), expected, actual))

    print(f"preflight_total_bytes={total_bytes}")
    print(f"missing={len(missing)} bad_hash={len(bad_hash)}")

    if missing:
        print("\nMISSING:")
        for x in missing[:30]:
            print(" -", x)
    if bad_hash:
        print("\nHASH MISMATCH:")
        for src, exp, act in bad_hash[:30]:
            print(" -", src)
            print("   expected:", exp)
            print("   actual  :", act)

    if missing or bad_hash:
        raise SystemExit("BLOCKED: preflight failed; nothing moved.")

    print("\nPROTECTED (left in project):")
    for r in protected_rows:
        print(" -", r["path"])

    if not args.execute:
        print("\nDRY-RUN PASS: no files changed.")
        print("Run again with --execute to copy -> verify -> delete the 102 safe files.")
        return

    TARGET.mkdir(parents=True, exist_ok=True)
    moved = 0
    already = 0

    for i, r in enumerate(safe_rows, 1):
        rel = Path(r["path"])
        src = PROJECT / rel
        dst = TARGET / rel
        expected = (r.get("sha256") or "").strip().lower()

        dst.parent.mkdir(parents=True, exist_ok=True)

        if dst.exists():
            dst_hash = sha256(dst)
            if expected and dst_hash.lower() != expected:
                raise SystemExit(
                    f"BLOCKED: target exists with different content: {dst}"
                )
            # Already archived correctly; safe to remove source after source preflight.
            src.unlink()
            already += 1
            print(f"[{i:03d}/{len(safe_rows)}] VERIFIED_EXISTING -> removed source  {rel}")
            continue

        # Safer than direct move: copy first, verify, then delete source.
        shutil.copy2(src, dst)
        if expected:
            dst_hash = sha256(dst)
            if dst_hash.lower() != expected:
                dst.unlink(missing_ok=True)
                raise SystemExit(f"BLOCKED: post-copy hash mismatch: {dst}")

        src.unlink()
        moved += 1
        print(f"[{i:03d}/{len(safe_rows)}] RELOCATED  {rel}")

    # Only prune empty task-specific directories, never project structural roots.
    prune_roots = [
        PROJECT / "data" / "research" / "px_assets",
        PROJECT / "data" / "research" / "audit_independent_20260925",
        PROJECT / "data" / "research" / "csi300_pit" / "raw",
        PROJECT / "data" / "research" / "raw_layer",
    ]
    for root in prune_roots:
        if not root.exists():
            continue
        for p in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if p.is_dir():
                try:
                    p.rmdir()
                except OSError:
                    pass
        try:
            root.rmdir()
        except OSError:
            pass

    print("\nDONE")
    print("relocated_new =", moved)
    print("removed_after_existing_verified =", already)
    print("protected_left_in_project =", len(protected_rows))
    print("archive_root =", TARGET)

if __name__ == "__main__":
    main()
