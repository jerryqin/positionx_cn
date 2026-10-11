from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research.deep_oversold_independence_audit import audit, load_fillable_events
from research.watchlist_adapter import save_watchlist_snapshot


def _fmt(v):
    return "-" if v is None else f"{v:+.4%}"


def show_block(title, block):
    print(f"\n{title}")
    print(f"  stock-events={block['count']}")
    for entry in ("open", "close"):
        print(f"  {entry.upper()}")
        ev = block[entry]["stock_event_weighted"]["excess"]
        day = block[entry]["signal_date_equal_weighted"]["excess"]
        print(
            f"    stock-event weighted: n={ev['n']} avg={_fmt(ev['avg'])} "
            f"median={_fmt(ev['median'])} trim5={_fmt(ev['trimmed_mean_5pct'])}"
        )
        print(
            f"    signal-date equal-wt: n={day['n']} avg={_fmt(day['avg'])} "
            f"median={_fmt(day['median'])} trim5={_fmt(day['trimmed_mean_5pct'])}"
        )
        folds = block[entry]["signal_date_equal_weighted"]["folds_excess"]
        print("    date-equal folds:")
        for f in folds:
            print(
                f"      {f['fold']} n={f['n']} avg={_fmt(f['avg'])} "
                f"median={_fmt(f['median'])}"
            )
    c = block["concentration"]
    print(
        f"  unique dates={c['unique_signal_dates']} symbols={c['unique_symbols']} "
        f"events/date median={c['events_per_signal_date']['median']:.1f} "
        f"p90={c['events_per_signal_date']['p90']:.1f} "
        f"max={c['events_per_signal_date']['max']}"
    )
    print(
        f"  top10 signal-date event share={c['top10_signal_dates_event_share']:.2%} "
        f"top10 symbol event share={c['top10_symbols_event_share']:.2%}"
    )


def main():
    p = argparse.ArgumentParser(description="Deep Oversold Independence / Weighting Audit v1")
    p.add_argument(
        "--events",
        default="data/research/deep_oversold_replication_events_latest.csv",
    )
    p.add_argument(
        "--output",
        default="data/research/deep_oversold_independence_audit_latest.json",
    )
    args = p.parse_args()

    events_path = PROJECT_ROOT / args.events
    if not events_path.exists():
        raise SystemExit(f"missing events CSV: {events_path}")

    events = load_fillable_events(str(events_path))
    result = audit(events)

    safety = {
        "uses_broker": False,
        "production_db_opened": False,
        "creates_trading_plan": False,
        "can_submit_order": False,
        "writes_reference_db": False,
        "changes_strategy_parameters": False,
    }

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "DEEP_OVERSOLD_INDEPENDENCE_AUDIT_V1",
        "status": "OK",
        "safety": safety,
        "purpose": (
            "Test whether the huge Deep Oversold result survives correlated-event "
            "weighting and repeated same-symbol signals. This does not alter the hypothesis."
        ),
        "methodology": {
            "stock_event_weighted": "each symbol-date event has equal weight",
            "signal_date_equal_weighted": (
                "average stocks within each signal date first, then give every signal date equal weight; "
                "this is closer to Eva's event-day estimator"
            ),
            "dedup_symbol_while_held": (
                "keep first signal while flat; ignore additional same-symbol signals before prior exit"
            ),
        },
        "results": result,
    }

    out_path = PROJECT_ROOT / args.output
    out_path.parent.mkdir(parents=True, exist_ok=True)
    save_watchlist_snapshot(out_path, out)

    print(json.dumps(safety, ensure_ascii=False, indent=2))
    print("\nDEEP OVERSOLD INDEPENDENCE AUDIT v1")

    show_block("A) ALL FILLABLE STOCK-DAY EVENTS", result["all_fillable_stock_days"])

    d = result["dedup_symbol_while_held"]
    print(
        f"\nDEDUP IMPACT: kept={d['count']} removed={d['removed']} "
        f"kept_fraction={d['kept_fraction']:.2%}"
    )
    show_block("B) FIRST SIGNAL WHILE FLAT", d)

    print("\nINTERPRETATION:")
    print("  1) If date-equal weighting stays positive, the Edge is not only a crash-day count artifact.")
    print("  2) If first-signal-while-flat stays positive, repeated daily oversold triggers are not the whole Edge.")
    print("  3) Only after both survive should we move to shared-equity portfolio economics.")

    print(f"\nOUTPUT: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
