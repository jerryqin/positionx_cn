from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research.csi1000_anomaly_forensics import analyze_assets

DEFAULT_ASSETS = Path(
    "D:/positionx_data/incoming/eva/20260925_foundation_assets/"
    "data/research/px_assets"
)

def main() -> int:
    p = argparse.ArgumentParser(description="CSI1000 PIT anomaly forensics v1 — read only")
    p.add_argument("--assets-dir", default=str(DEFAULT_ASSETS))
    p.add_argument(
        "--output",
        default="data/research/universe_assets/csi1000_anomaly_forensics_latest.json",
    )
    args = p.parse_args()

    result = analyze_assets(Path(args.assets_dir), expected_size=1000)
    out = PROJECT_ROOT / args.output
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\nCSI1000 PIT ANOMALY FORENSICS v1")
    print(f"bad_blocks={result['bad_blocks']} bad_dates={result['bad_dates']}")
    for i, b in enumerate(result["blocks"], 1):
        print(f"\nBLOCK {i}: {b['first_bad_date']}..{b['last_bad_date']}")
        print(
            f"  prev={b['prev_clean_date']} size={b['prev_clean_size']} "
            f"next={b['next_clean_date']} size={b['next_clean_size']} "
            f"days={b['bad_trading_days']} constant={b['bad_set_constant_across_block']}"
        )
        print("  first_bad_vs_prev:")
        print("    added  :", b["first_bad_vs_prev"]["added"])
        print("    removed:", b["first_bad_vs_prev"]["removed"])
        print("  next_vs_last_bad:")
        print("    added  :", b["next_vs_last_bad"]["added"])
        print("    removed:", b["next_vs_last_bad"]["removed"])
        print("  bad_vs_next:")
        print("    only_in_bad :", b["first_bad_vs_next"]["only_in_bad"])
        print("    only_in_next:", b["first_bad_vs_next"]["only_in_next"])
        print("  shape:", b["shape_classification"])

        if b["adjustment_context"]:
            eff = sorted(set(str(x["effective_date"]) for x in b["adjustment_context"]))
            print("  nearby adjustment effective dates:", eff)
            print("  scheduled overlap:", json.dumps(b["scheduled_overlap"], ensure_ascii=False))

        if b["raw_snapshot_context"]:
            print("  raw snapshots:")
            for x in b["raw_snapshot_context"]:
                print(
                    f"    {x['snapshot_date']} size={x['snapshot_size']} "
                    f"fp={x['snapshot_fingerprint']}"
                )
        else:
            print("  raw snapshots: unavailable (cached parquet not found)")

    print("\nNO REPAIR WAS PERFORMED.")
    print("Use this output to decide which membership semantics are correct.")
    print(f"OUTPUT: {out}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
