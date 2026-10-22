from __future__ import annotations
import argparse, json, sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path: sys.path.insert(0, str(PROJECT_ROOT))
from research.forward_backup import create_backup

def main() -> int:
    p = argparse.ArgumentParser(description="PositionX true-Forward backup")
    p.add_argument("--backup-root", default=r"D:\positionx_data\backup\positionx_forward")
    p.add_argument("--scheduler-log")
    p.add_argument("--force", action="store_true")
    p.add_argument("--only-if-session-today", action="store_true")
    p.add_argument("--retention-days", type=int, default=90)
    p.add_argument("--segment-profile")
    a = p.parse_args()
    segment={}
    if a.segment_profile:
        from scripts.deep_forward_segment_v01 import load_profile
        segment=load_profile(a.segment_profile)
        a.backup_root=segment["backup_root"]
    r = create_backup(project_root=PROJECT_ROOT, backup_root=Path(a.backup_root),
        scheduler_log=Path(a.scheduler_log) if a.scheduler_log else None,
        force=a.force, only_if_session_today=a.only_if_session_today,
        retention_days=a.retention_days,
        segment_db=Path(segment["segment_db"]) if segment else None,
        segment_output=Path(segment["output_dir"]) if segment else None,keep_all=bool(segment))
    print(json.dumps(r, ensure_ascii=False, indent=2))

    # Scheduler semantics:
    # - create_backup() may legitimately return ok=true with
    #   SKIPPED_NO_NEW_FORWARD_SESSION.  That is NOT a successful scheduled
    #   forward cycle: the wrapper must retry rather than exit 0.
    # - any explicit ok=false must also propagate as non-zero.
    if not bool(r.get("ok", False)):
        return 2
    if a.only_if_session_today and r.get("status") == "SKIPPED_NO_NEW_FORWARD_SESSION":
        return 3
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
