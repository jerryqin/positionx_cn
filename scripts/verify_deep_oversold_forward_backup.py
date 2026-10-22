from __future__ import annotations
import argparse, json, sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path: sys.path.insert(0, str(PROJECT_ROOT))
from research.forward_backup import latest_backup_dir, verify_backup_dir

def main() -> int:
    p = argparse.ArgumentParser(description="Verify PositionX true-Forward backup")
    p.add_argument("--backup-root", default=r"D:\positionx_data\backup\positionx_forward")
    p.add_argument("--backup-dir")
    p.add_argument("--latest", action="store_true")
    p.add_argument("--segment-profile")
    a = p.parse_args()
    if a.segment_profile:
        from scripts.deep_forward_segment_v01 import load_profile
        a.backup_root=load_profile(a.segment_profile)["backup_root"]
    target = Path(a.backup_dir) if a.backup_dir else latest_backup_dir(Path(a.backup_root))
    if target is None: raise SystemExit("NO_FORWARD_BACKUP_FOUND")
    print(json.dumps(verify_backup_dir(target), ensure_ascii=False, indent=2)); return 0
if __name__ == "__main__": raise SystemExit(main())
