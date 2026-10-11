from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from datetime import datetime

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from opportunity.trend_start_event_eval import evaluate_event_onsets

PANEL = Path(r"D:\positionx_data\research\opportunity_engine\trend_ignition_v0_1\trend_ignition_v0_1_panel.parquet")
OUT = Path(r"D:\positionx_data\research\opportunity_engine\trend_start_event_eval_v0_2")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["check", "run"])
    args = p.parse_args()

    if args.command == "check":
        result = {
            "status": "PASS_TREND_START_EVENT_EVAL_V0_2_CHECK" if PANEL.exists()
                      else "BLOCKED_TREND_START_EVENT_EVAL_V0_2_CHECK",
            "source_panel": str(PANEL),
            "source_panel_exists": PANEL.exists(),
            "research_only": True,
            "changes_frozen_v01_signal": False,
            "changes_frozen_v01_gate": False,
            "purpose": "Post-mortem and better event-level ruler; V0.1 remains FAST_REJECT.",
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if PANEL.exists() else 3

    if not PANEL.exists():
        print(json.dumps({
            "status": "FAIL_CLOSED_TREND_START_EVENT_EVAL_V0_2",
            "error": f"source panel missing: {PANEL}"
        }, ensure_ascii=False, indent=2))
        return 3

    df = pd.read_parquet(PANEL)
    result = evaluate_event_onsets(df)
    result = {
        "status": "PASS_TREND_START_EVENT_DIAGNOSTIC_V0_2",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "research_id": "OPPORTUNITY_TREND_START_EVENT_EVAL_V0_2",
        "source_panel": str(PANEL),
        "frozen_v01_verdict": "FAST_REJECT_TREND_IGNITION_MECHANISM_V0_1",
        **result,
        "interpretation": (
            "This diagnostic cannot promote Trend Ignition V0.1. "
            "Use it to design the next independent Opportunity detector and to replace "
            "dense daily-row labels with event/onset evaluation."
        ),
    }

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "trend_start_event_eval_v0_2_summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"OUTPUT: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
