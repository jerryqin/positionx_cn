from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research.csi1000_alias_repair import build_accepted_csi1000

DEFAULT_SOURCE = Path(
    "D:/positionx_data/incoming/eva/20260925_foundation_assets/"
    "data/research/px_assets"
)

def main() -> int:
    p = argparse.ArgumentParser(description="Repair and accept CSI1000 historical code alias")
    p.add_argument("--source-dir", default=str(DEFAULT_SOURCE))
    p.add_argument(
        "--destination",
        default="data/research/universe_assets/csi1000",
    )
    args = p.parse_args()

    dst = PROJECT_ROOT / args.destination
    manifest = build_accepted_csi1000(
        Path(args.source_dir),
        dst,
        dataset_id="CSI1000_PIT_ALIAS_REPAIRED_V1_20260926",
    )
    a = manifest["audit"]
    print("CSI1000 PIT ALIAS REPAIR ACCEPTED")
    print(json.dumps({
        "dataset_id": manifest["dataset_id"],
        "status": manifest["status"],
        "repair_scope": manifest["repair_scope"],
        "source_rows": a["source_rows"],
        "repaired_rows": a["repaired_rows"],
        "source_bad_size_dates": a["source_bad_size_dates"],
        "repaired_bad_size_dates": a["repaired_bad_size_dates"],
        "repaired_size_min": a["repaired_size_min"],
        "repaired_size_max": a["repaired_size_max"],
        "alias_action_rows": len(a["alias_actions"]),
        "accepted_symbols_ever": manifest["accepted_symbols_ever"],
        "accepted_history_rows": manifest["accepted_history_rows"],
        "source_sha256": manifest["source_sha256"],
        "accepted_sha256": manifest["accepted_sha256"],
    }, ensure_ascii=False, indent=2))
    print("\nFirst alias actions:")
    for r in a["alias_actions"][:25]:
        print(" ", r)
    print(f"\nMANIFEST: {dst/'acceptance_manifest.json'}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
