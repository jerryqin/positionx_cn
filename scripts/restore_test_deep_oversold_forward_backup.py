from __future__ import annotations
import argparse, shutil, sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path: sys.path.insert(0, str(PROJECT_ROOT))
from research.forward_backup import latest_backup_dir, verify_backup_dir

def main() -> int:
    p = argparse.ArgumentParser(description="Restore Forward backup into a sandbox only")
    p.add_argument("--backup-root", default=r"D:\positionx_data\backup\positionx_forward")
    p.add_argument("--backup-dir")
    p.add_argument("--restore-root", default=r"D:\positionx_data\restore_test\positionx_forward")
    a = p.parse_args()
    target = Path(a.backup_dir) if a.backup_dir else latest_backup_dir(Path(a.backup_root))
    if target is None: raise SystemExit("NO_FORWARD_BACKUP_FOUND")
    verify_backup_dir(target)
    out = Path(a.restore_root)/target.name
    if out.exists(): shutil.rmtree(out)
    shutil.copytree(target, out)
    verify_backup_dir(out)
    print(f"RESTORE_TEST_OK: {out}")
    print("Production PositionX files were NOT overwritten.")
    return 0
if __name__ == "__main__": raise SystemExit(main())
