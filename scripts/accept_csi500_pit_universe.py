from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research.deep_oversold_universe import CSI500, accept_universe_assets

DEFAULT_ASSETS = Path(
    "D:/positionx_data/incoming/eva/20260925_foundation_assets/"
    "data/research/px_assets"
)

def main() -> int:
    p = argparse.ArgumentParser(description="Accept verified CSI500 PIT universe into PositionX research")
    p.add_argument("--assets-dir", default=str(DEFAULT_ASSETS))
    p.add_argument(
        "--destination",
        default="data/research/universe_assets/csi500",
    )
    args = p.parse_args()

    dst = PROJECT_ROOT / args.destination
    manifest = accept_universe_assets(
        Path(args.assets_dir),
        dst,
        CSI500,
        dataset_id="CSI500_PIT_ACCEPTED_V1_20260926",
    )
    print("CSI500 PIT ACCEPTED")
    print(json.dumps({
        "dataset_id": manifest["dataset_id"],
        "status": manifest["status"],
        "daily_rows": manifest["audit"]["daily_rows"],
        "trading_days": manifest["audit"]["trading_days"],
        "size_min": manifest["audit"]["size_min"],
        "size_max": manifest["audit"]["size_max"],
        "symbols_ever": manifest["audit"]["symbols_ever_daily"],
        "history_rows": manifest["audit"]["history_rows"],
        "checks": manifest["audit"]["checks"],
        "sha256": manifest["audit"]["sha256"],
    }, ensure_ascii=False, indent=2))
    print(f"MANIFEST: {dst/'acceptance_manifest.json'}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
