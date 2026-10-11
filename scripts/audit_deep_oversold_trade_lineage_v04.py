from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
RESEARCH_DIR = ROOT / "data" / "research"
REGIME = RESEARCH_DIR / "deep_oversold_csi500_regime_events_v1.parquet"
OUT_DIR = RESEARCH_DIR / "edge_discovery" / "deep_oversold_trade_lineage_v04"

TARGET_COUNT = 1365
TARGET_WEIGHT = 0.01
MAX_GROSS = 0.30
MAX_SLOTS = int(round(MAX_GROSS / TARGET_WEIGHT))

KEY = ["symbol", "signal_date", "entry_date", "exit_date"]


def normalize_date(v: Any) -> str:
    s = "".join(ch for ch in str(v) if ch.isdigit())
    if len(s) < 8:
        raise ValueError(f"Invalid date: {v!r}")
    return s[:8]


def load_events() -> pd.DataFrame:
    if not REGIME.exists():
        raise FileNotFoundError(REGIME)

    q = REGIME.as_posix().replace("'", "''")
    con = duckdb.connect()
    try:
        df = con.execute(
            f"""
            SELECT
                symbol,
                signal_date,
                entry_date,
                exit_date,
                signal_ret20
            FROM read_parquet('{q}')
            """
        ).fetchdf()
    finally:
        con.close()

    for c in ("signal_date", "entry_date", "exit_date"):
        df[c] = df[c].map(normalize_date)
    df["symbol"] = df["symbol"].astype(str).str.upper().str.strip()

    if df.duplicated(KEY).any():
        raise RuntimeError("Duplicate core trade keys in regime source.")

    return df.sort_values(
        ["entry_date", "signal_date", "symbol"]
    ).reset_index(drop=True)


def simulate_slot_cap(
    events: pd.DataFrame,
    exits_release_before_open: bool,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Reconstruct only the frozen portfolio slot constraint.

    With 1% target weight and 30% max gross:
        max simultaneous positions = 30

    Two event-order conventions are tested:
    - exits_release_before_open=False (economically correct for OPEN entry /
      CLOSE exit): a position whose exit_date == today's entry_date still
      occupies a slot at today's OPEN.
    - exits_release_before_open=True: diagnostic alternative only.

    No ranking parameter is introduced. On a day with more candidates than
    free slots, deterministic symbol order is used only to materialize an
    auditable accepted/rejected key set; the accepted COUNT is independent of
    that order because all candidates consume one identical slot.
    """
    active = []  # dicts of accepted positions
    accepted = []
    rejected = []
    daily = []

    for entry_date, g in events.groupby("entry_date", sort=True):
        if exits_release_before_open:
            still_active = [
                p for p in active
                if p["exit_date"] >= entry_date
            ]
            # Release strictly earlier than entry date only? For the
            # "release before open" alternative, same-day exits should be free:
            still_active = [
                p for p in active
                if p["exit_date"] > entry_date
            ]
        else:
            # OPEN entry / CLOSE exit: same-day exits remain active at OPEN.
            still_active = [
                p for p in active
                if p["exit_date"] >= entry_date
            ]

        active = still_active
        active_before = len(active)
        free_slots = max(0, MAX_SLOTS - active_before)

        candidates = g.sort_values(
            ["signal_date", "symbol"]
        ).to_dict(orient="records")

        accepted_today = candidates[:free_slots]
        rejected_today = candidates[free_slots:]

        for r in accepted_today:
            rr = dict(r)
            rr["lineage_action"] = "ACCEPT"
            rr["active_before_entry"] = active_before
            rr["free_slots_before_entry"] = free_slots
            accepted.append(rr)
            active.append(dict(r))

        for r in rejected_today:
            rr = dict(r)
            rr["lineage_action"] = "REJECT_MAX_GROSS"
            rr["active_before_entry"] = active_before
            rr["free_slots_before_entry"] = free_slots
            rejected.append(rr)

        daily.append({
            "entry_date": entry_date,
            "active_before_entry": active_before,
            "candidate_count": len(candidates),
            "free_slots": free_slots,
            "accepted_count": len(accepted_today),
            "rejected_count": len(rejected_today),
            "active_after_entry": len(active),
        })

        if len(active) > MAX_SLOTS:
            raise RuntimeError(
                f"Slot cap violated on {entry_date}: {len(active)} > {MAX_SLOTS}"
            )

    return (
        pd.DataFrame(accepted),
        pd.DataFrame(rejected),
        pd.DataFrame(daily),
    )


def summarize_rejections(rejected: pd.DataFrame) -> dict[str, Any]:
    if rejected.empty:
        return {
            "rows": 0,
            "dates": 0,
            "symbols": 0,
            "top_dates": [],
            "top_symbols": [],
        }

    top_dates = rejected["entry_date"].value_counts().head(30)
    top_symbols = rejected["symbol"].value_counts().head(30)

    return {
        "rows": int(len(rejected)),
        "dates": int(rejected["entry_date"].nunique()),
        "symbols": int(rejected["symbol"].nunique()),
        "top_dates": [
            {"entry_date": str(k), "rejected": int(v)}
            for k, v in top_dates.items()
        ],
        "top_symbols": [
            {"symbol": str(k), "rejected": int(v)}
            for k, v in top_symbols.items()
        ],
    }


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    events = load_events()

    if len(events) != 1468:
        raise RuntimeError(
            f"Expected 1468 regime events from prior lineage audit; got {len(events)}."
        )

    specs = [
        ("OPEN_ENTRY_CLOSE_EXIT", False),
        ("DIAGNOSTIC_RELEASE_SAME_DAY_EXIT_BEFORE_OPEN", True),
    ]

    results = {}

    for name, release_same_day in specs:
        accepted, rejected, daily = simulate_slot_cap(
            events,
            exits_release_before_open=release_same_day,
        )

        accepted.to_parquet(
            OUT_DIR / f"{name.lower()}_accepted.parquet",
            index=False,
        )
        rejected.to_csv(
            OUT_DIR / f"{name.lower()}_rejected.csv",
            index=False,
            encoding="utf-8-sig",
        )
        daily.to_csv(
            OUT_DIR / f"{name.lower()}_daily_slots.csv",
            index=False,
            encoding="utf-8-sig",
        )

        results[name] = {
            "release_same_day_exit_before_open": release_same_day,
            "accepted": int(len(accepted)),
            "rejected": int(len(rejected)),
            "exact_target_1365": int(len(accepted)) == TARGET_COUNT,
            "max_active_after_entry": int(daily["active_after_entry"].max()),
            "days_at_full_cap": int((daily["active_after_entry"] == MAX_SLOTS).sum()),
            "days_with_rejections": int((daily["rejected_count"] > 0).sum()),
            "max_rejected_on_one_day": int(daily["rejected_count"].max()),
            "rejection_summary": summarize_rejections(rejected),
        }

    exact = [
        name
        for name, r in results.items()
        if r["exact_target_1365"]
    ]

    audit = {
        "audit_id": "DEEP_OVERSOLD_TRADE_LINEAGE_V04",
        "source": str(REGIME),
        "source_events": int(len(events)),
        "frozen_reference_count": TARGET_COUNT,
        "frozen_portfolio_constraints": {
            "target_weight": TARGET_WEIGHT,
            "max_gross": MAX_GROSS,
            "max_slots": MAX_SLOTS,
            "entry": "T+1 OPEN",
            "exit": "S20 CLOSE",
        },
        "results": results,
        "exact_matching_variants": exact,
        "canonical_selected_automatically": False,
        "notes": [
            "No ranking/tuning parameter was introduced.",
            "Same-day candidate ordering only materializes a key set; accepted count is driven by identical-slot availability.",
            "OPEN_ENTRY_CLOSE_EXIT is the economically correct timing convention for the frozen strategy.",
        ],
    }

    (OUT_DIR / "audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("=" * 110)
    print("DEEP OVERSOLD CSI500 TRADE LINEAGE AUDIT V0.4")
    print("=" * 110)
    print("Source regime events:", len(events))
    print("Frozen trade-count reference:", TARGET_COUNT)
    print(
        f"Portfolio constraint: target={TARGET_WEIGHT:.0%} "
        f"max_gross={MAX_GROSS:.0%} max_slots={MAX_SLOTS}"
    )
    print()

    for name, r in results.items():
        marker = "  <== EXACT 1365" if r["exact_target_1365"] else ""
        print(name)
        print("-" * 110)
        print(
            f"accepted={r['accepted']} "
            f"rejected={r['rejected']} "
            f"maxActive={r['max_active_after_entry']} "
            f"daysAtCap={r['days_at_full_cap']} "
            f"daysWithReject={r['days_with_rejections']} "
            f"maxRejectOneDay={r['max_rejected_on_one_day']}"
            f"{marker}"
        )
        rs = r["rejection_summary"]
        if rs["top_dates"]:
            print(
                "top reject dates:",
                ", ".join(
                    f"{x['entry_date']}={x['rejected']}"
                    for x in rs["top_dates"][:15]
                ),
            )
        print()

    print("Exact matching variants:", exact if exact else "NONE")
    print("Output:", OUT_DIR)
    print("No canonical population was written into production/research source files.")
    print("No strategy or production database was modified.")


if __name__ == "__main__":
    main()
