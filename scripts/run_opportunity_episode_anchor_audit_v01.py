from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from opportunity.episode_anchor_audit import audit_episode_anchors

PANEL = Path(r"D:\positionx_data\research\opportunity_engine\trend_ignition_v0_1\trend_ignition_v0_1_panel.parquet")
OUT = Path(r"D:\positionx_data\research\opportunity_engine\episode_anchor_audit_v0_1")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def check():
    required = {
        "trade_date", "symbol",
        "TREND25_60_label", "TREND25_60_mfe",
        "rs20", "rs_accel", "vol_compression", "volume_contraction",
        "distance_to_60d_high", "higher_low_20_vs_60",
    }
    if not PANEL.exists():
        result = {
            "status": "BLOCKED_EPISODE_ANCHOR_AUDIT_V0_1_CHECK",
            "source_panel": str(PANEL),
            "source_panel_exists": False,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 3

    df = pd.read_parquet(PANEL)
    missing = sorted(required - set(df.columns))
    result = {
        "status": "PASS_EPISODE_ANCHOR_AUDIT_V0_1_CHECK" if not missing
                  else "BLOCKED_EPISODE_ANCHOR_AUDIT_V0_1_CHECK",
        "source_panel": str(PANEL),
        "source_panel_exists": True,
        "source_panel_sha256": sha256_file(PANEL),
        "required_columns_ok": not missing,
        "missing_columns": missing,
        "research_only": True,
        "changes_frozen_trend_ignition": False,
        "production_db": False,
        "broker": False,
        "qmt": False,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not missing else 3


def run():
    if not PANEL.exists():
        print(json.dumps({
            "status": "FAIL_CLOSED_EPISODE_ANCHOR_AUDIT_V0_1",
            "error": f"missing panel: {PANEL}",
        }, ensure_ascii=False, indent=2))
        return 3

    panel_sha = sha256_file(PANEL)
    df = pd.read_parquet(PANEL)
    audit, export = audit_episode_anchors(df)

    OUT.mkdir(parents=True, exist_ok=True)
    export_path = OUT / "episode_anchor_audit_v0_1_events.parquet"
    export.to_parquet(export_path, index=False)

    result = {
        "status": "PASS_EPISODE_ANCHOR_AUDIT_V0_1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "research_id": "OPPORTUNITY_EPISODE_ANCHOR_AUDIT_V0_1",
        "source": {
            "panel": str(PANEL),
            "panel_sha256": panel_sha,
            "frozen_parent_verdict": "FAST_REJECT_TREND_IGNITION_MECHANISM_V0_1",
            "transition_study_status": "DIAGNOSTIC_ONLY_PENDING_LABEL_SEMANTICS_AUDIT",
        },
        **audit,
        "artifacts": {
            "event_rows": str(export_path),
            "event_rows_sha256": sha256_file(export_path),
        },
        "safety": {
            "research_only": True,
            "production_db": False,
            "broker": False,
            "qmt": False,
        },
    }

    summary = OUT / "episode_anchor_audit_v0_1_summary.json"
    summary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"OUTPUT: {OUT}")
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["check", "run"])
    args = p.parse_args()
    return check() if args.command == "check" else run()


if __name__ == "__main__":
    raise SystemExit(main())
