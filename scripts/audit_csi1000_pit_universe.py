from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research.deep_oversold_universe import CSI1000, audit_universe_assets

DEFAULT_ASSETS = Path(
    "D:/positionx_data/incoming/eva/20260925_foundation_assets/"
    "data/research/px_assets"
)

def main() -> int:
    p = argparse.ArgumentParser(description="Audit CSI1000 PIT universe; audit-only, no acceptance")
    p.add_argument("--assets-dir", default=str(DEFAULT_ASSETS))
    p.add_argument(
        "--output",
        default="data/research/universe_assets/csi1000_audit_latest.json",
    )
    args = p.parse_args()

    audit = audit_universe_assets(Path(args.assets_dir), CSI1000)
    out = PROJECT_ROOT / args.output
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    print("\nCSI1000 STATUS:", "ACCEPTABLE" if audit["accepted"] else "BLOCKED_FOR_REPAIR")
    print("This command does not alter or accept CSI1000.")
    print(f"OUTPUT: {out}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
