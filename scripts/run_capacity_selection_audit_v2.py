from __future__ import annotations
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research.capacity_selection_audit import (
    extract_candidates,
    random_audit,
    replay_selection,
    return_stats,
    summarize_replay,
)
from research.watchlist_adapter import save_watchlist_snapshot


def fmt_pct(x):
    return "NA" if x is None else f"{100.0*float(x):.3f}%"

def fmt_num(x):
    return "NA" if x is None else f"{float(x):.3f}"


def main() -> int:
    p = argparse.ArgumentParser(description="Selection-only Capacity Audit v2")
    p.add_argument(
        "--input",
        default="data/research/portfolio_simulation_pit_fillable_latest.json",
    )
    p.add_argument("--capacities", default="10,20,30,40,50,60,70,80")
    p.add_argument("--random-capacities", default="10,20,30,40,50,60")
    p.add_argument("--seeds", type=int, default=300)
    p.add_argument(
        "--output",
        default="data/research/capacity_selection_audit_pit_fillable_latest.json",
    )
    args = p.parse_args()

    src = PROJECT_ROOT / args.input
    if not src.exists():
        raise SystemExit(f"input missing: {src}")
    data = json.loads(src.read_text(encoding="utf-8"))
    if data.get("status") != "OK":
        raise SystemExit(f"input status is not OK: {data.get('status')}")

    standard = data.get("standard")
    if not isinstance(standard, dict):
        raise SystemExit("input has no standard portfolio result")

    candidates = extract_candidates(standard)
    expected_total = int(standard.get("summary", {}).get("signals_executable") or 0)
    if expected_total and len(candidates) != expected_total:
        raise SystemExit(
            f"candidate reconstruction mismatch: extracted={len(candidates)} expected={expected_total}"
        )

    # Fail closed: replay the stored 10-position baseline exactly.
    baseline_cap = int(standard.get("summary", {}).get("max_positions") or 10)
    replay10 = summarize_replay(
        replay_selection(candidates, baseline_cap, mode="SYMBOL_ASC"),
        len(candidates),
    )
    stored = standard.get("summary", {})
    checks = {
        "selected": (replay10["counts"]["selected"], int(stored.get("trades_executed") or 0)),
        "no_slot": (replay10["counts"]["no_slot"], int(stored.get("skipped_no_slot") or 0)),
        "duplicate": (replay10["counts"]["duplicate"], int(stored.get("skipped_duplicate_symbol") or 0)),
    }
    bad = {k: v for k, v in checks.items() if v[0] != v[1]}
    if bad:
        raise SystemExit(f"BLOCKED_REPLAY_MISMATCH: {bad}")

    capacities = sorted({int(x) for x in args.capacities.split(",") if x.strip()})
    random_caps = sorted({int(x) for x in args.random_capacities.split(",") if x.strip()})

    asc = []
    for cap in capacities:
        asc.append(
            summarize_replay(
                replay_selection(candidates, cap, mode="SYMBOL_ASC"),
                len(candidates),
            )
        )

    random_results = [random_audit(candidates, cap, args.seeds) for cap in random_caps]

    full_event_stats = return_stats(candidates)
    saturation = next((x for x in asc if x["counts"]["no_slot"] == 0), None)

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "SELECTION_ONLY_CAPACITY_AUDIT",
        "version": "v2",
        "status": "OK",
        "safety": {
            "uses_broker": False,
            "production_db_opened": False,
            "creates_trading_plan": False,
            "can_submit_order": False,
            "writes_reference_db": False,
            "changes_strategy_parameters": False,
            "uses_portfolio_money_curve": False,
        },
        "methodology": {
            "important": (
                "No capital, slot cash, compounding, exposure or position sizing is used. "
                "This isolates capacity selection only."
            ),
            "same_day_exit_not_reusable_at_open": True,
            "no_duplicate_symbol_while_held": True,
            "symbol_asc": "deterministic diagnostic only",
            "random": "shuffle only candidates sharing the same entry date",
        },
        "input": {
            "source": str(src),
            "candidates": len(candidates),
            "baseline_replay_checks": checks,
        },
        "all_candidate_event_stats": full_event_stats,
        "symbol_asc_by_capacity": asc,
        "first_symbol_asc_capacity_with_zero_no_slot": (
            saturation["max_positions"] if saturation else None
        ),
        "random_tie_break": random_results,
    }

    out_path = PROJECT_ROOT / args.output
    out_path.parent.mkdir(parents=True, exist_ok=True)
    save_watchlist_snapshot(out_path, out)

    print(json.dumps(out["safety"], ensure_ascii=False, indent=2))
    print("\nBASELINE REPLAY CHECK:")
    print(json.dumps(checks, ensure_ascii=False, indent=2))

    print("\nALL CANDIDATES:")
    print(
        f"n={full_event_stats['n']} avg_net={fmt_pct(full_event_stats['avg'])} "
        f"median={fmt_pct(full_event_stats['median'])} "
        f"win={fmt_pct(full_event_stats['win_rate'])} "
        f"PF={fmt_num(full_event_stats['profit_factor'])}"
    )

    print("\nSELECTION-ONLY — SYMBOL_ASC")
    print(" cap | selected | no-slot | duplicate | coverage | avg-net | median | win | PF")
    print("-" * 90)
    for x in asc:
        s = x["selected"]
        c = x["counts"]
        print(
            f"{x['max_positions']:4d} | {c['selected']:8d} | {c['no_slot']:7d} | "
            f"{c['duplicate']:9d} | {fmt_pct(x['coverage']):>8} | "
            f"{fmt_pct(s['avg']):>8} | {fmt_pct(s['median']):>8} | "
            f"{fmt_pct(s['win_rate']):>8} | {fmt_num(s['profit_factor']):>6}"
        )

    print("\nRANDOM SAME-DAY TIE-BREAK (selected-event stats, no money model)")
    for r in random_results:
        a, pf = r["avg_net_return"], r["profit_factor"]
        print(
            f"cap={r['capacity']:3d} seeds={r['seeds']} "
            f"avg_net p05/med/p95={fmt_pct(a.get('p05'))}/"
            f"{fmt_pct(a.get('median'))}/{fmt_pct(a.get('p95'))} "
            f"PF p05/med/p95={fmt_num(pf.get('p05'))}/"
            f"{fmt_num(pf.get('median'))}/{fmt_num(pf.get('p95'))}"
        )

    print(
        "\nFIRST ZERO-NO-SLOT CAPACITY:",
        out["first_symbol_asc_capacity_with_zero_no_slot"],
    )
    print(f"OUTPUT: {out_path}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
