from __future__ import annotations
import argparse, json, sys
from dataclasses import asdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research.forward_breakout import ForwardPolicy, forward_health

DEFAULT_STATE = PROJECT_ROOT / "data" / "research" / "forward_breakout_v1" / "state.json"


def main() -> int:
    p = argparse.ArgumentParser(description="PositionX BreakoutRetest Forward Report")
    p.add_argument("--state", default=str(DEFAULT_STATE))
    args = p.parse_args()

    path = Path(args.state)
    if not path.exists():
        raise SystemExit(f"forward state not found: {path}")
    state = json.loads(path.read_text(encoding="utf-8"))
    policy = ForwardPolicy()
    h = forward_health(state, policy)

    print("BREAKOUT FORWARD SIMULATION")
    print(json.dumps(h, ensure_ascii=False, indent=2))

    print("\nACTIVE POSITIONS:")
    positions = state.get("positions") or {}
    if not positions:
        print("  none")
    else:
        for sym, x in sorted(positions.items()):
            print(
                f"  {sym} signal={x['signal_date']} entry={x['entry_date']} "
                f"entry_px={x['entry_price']:.3f} weight={x.get('target_weight_at_entry')}"
            )

    print("\nPENDING NEXT SESSION:")
    pending = state.get("pending") or []
    if not pending:
        print("  none")
    else:
        for x in pending:
            print(
                f"  {x['symbol']} signal={x['signal_date']} "
                f"breakout={x.get('breakout_date')} reason={x.get('reason')}"
            )

    if h["status"] == "COLLECTING":
        print(
            "\nCOLLECTING: first formal review requires both "
            f"{h['completed_trades_target']} completed trades and date >= {h['review_not_before']}."
        )
    else:
        print("\nREVIEW_DUE: forward sample reached the predeclared review threshold.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
