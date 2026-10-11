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

from opportunity.trend_onset_transition import evaluate_transition_study

PANEL = Path(r"D:\positionx_data\research\opportunity_engine\trend_ignition_v0_1\trend_ignition_v0_1_panel.parquet")
OUT = Path(r"D:\positionx_data\research\opportunity_engine\trend_onset_transition_v0_1")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def check_panel_columns(path: Path) -> tuple[bool, list[str], list[str]]:
    required = [
        "trade_date", "symbol",
        "rs20", "rs_accel",
        "vol_compression", "volume_contraction",
        "distance_to_60d_high", "higher_low_20_vs_60",
        "TREND25_60_label",
    ]
    if not path.exists():
        return False, required, []
    df = pd.read_parquet(path)
    missing = [c for c in required if c not in df.columns]
    return len(missing) == 0, missing, list(df.columns)


def cmd_check():
    ok, missing, cols = check_panel_columns(PANEL)
    result = {
        "status": "PASS_TREND_ONSET_TRANSITION_V0_1_CHECK"
                  if ok else "BLOCKED_TREND_ONSET_TRANSITION_V0_1_CHECK",
        "source_panel": str(PANEL),
        "source_panel_exists": PANEL.exists(),
        "source_panel_sha256": sha256_file(PANEL) if PANEL.exists() else None,
        "required_columns_ok": ok,
        "missing_columns": missing,
        "source_columns": cols,
        "research_only": True,
        "production_db": False,
        "broker": False,
        "qmt": False,
        "purpose": (
            "Discover state transitions/inflections around Trend Episode onsets; "
            "not a tradable detector and not parameter tuning of rejected Trend Ignition V0.1."
        ),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if ok else 3


def cmd_run():
    ok, missing, _ = check_panel_columns(PANEL)
    if not ok:
        print(json.dumps({
            "status": "FAIL_CLOSED_TREND_ONSET_TRANSITION_V0_1",
            "error": f"panel missing required columns: {missing}",
        }, ensure_ascii=False, indent=2))
        return 3

    source_sha = sha256_file(PANEL)
    panel = pd.read_parquet(PANEL)
    study, events = evaluate_transition_study(panel)

    OUT.mkdir(parents=True, exist_ok=True)
    events_path = OUT / "trend_onset_transition_v0_1_events.parquet"
    events.to_parquet(events_path, index=False)

    result = {
        "status": "PASS_TREND_ONSET_TRANSITION_STUDY_V0_1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "research_id": "OPPORTUNITY_TREND_ONSET_TRANSITION_V0_1",
        "source": {
            "panel": str(PANEL),
            "panel_sha256": source_sha,
            "frozen_parent_verdict": "FAST_REJECT_TREND_IGNITION_MECHANISM_V0_1",
        },
        **study,
        "artifacts": {
            "event_rows": str(events_path),
            "event_rows_sha256": sha256_file(events_path),
        },
        "interpretation": (
            "Use transition_shortlist only to choose the next independent research hypothesis. "
            "Do not tune Trend Ignition V0.1 and do not authorize Forward/Portfolio/Execution."
        ),
        "safety": {
            "research_only": True,
            "production_db": False,
            "broker": False,
            "qmt": False,
        },
    }

    summary_path = OUT / "trend_onset_transition_v0_1_summary.json"
    summary_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"OUTPUT: {OUT}")
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["check", "run"])
    args = p.parse_args()
    if args.command == "check":
        return cmd_check()
    return cmd_run()


if __name__ == "__main__":
    raise SystemExit(main())
