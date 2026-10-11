from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
RESEARCH_DIR = ROOT / "data" / "research"
REGIME = RESEARCH_DIR / "deep_oversold_csi500_regime_events_v1.parquet"
OUT_DIR = RESEARCH_DIR / "edge_discovery" / "deep_oversold_trade_lineage_v03"

TARGET_COUNT = 1365
KEY = ["symbol", "signal_date", "entry_date", "exit_date"]


def normalize_date(v: Any) -> str:
    s = "".join(ch for ch in str(v) if ch.isdigit())
    if len(s) < 8:
        raise ValueError(f"Invalid date: {v!r}")
    return s[:8]


def load_events() -> pd.DataFrame:
    if not REGIME.exists():
        raise FileNotFoundError(REGIME)

    # PositionX research parquet files are read through DuckDB so the
    # environment does not require optional pandas parquet engines
    # (pyarrow / fastparquet).
    q = REGIME.as_posix().replace("'", "''")
    con = duckdb.connect()
    try:
        df = con.execute(
            f"SELECT * FROM read_parquet('{q}')"
        ).fetchdf()
    finally:
        con.close()

    missing = [c for c in KEY if c not in df.columns]
    if missing:
        raise RuntimeError(f"Missing required columns: {missing}")

    df = df.copy()

    for c in ("signal_date", "entry_date", "exit_date"):
        df[c] = df[c].map(normalize_date)

    df["symbol"] = df["symbol"].astype(str).str.upper().str.strip()

    dup = df.duplicated(KEY, keep=False)
    if dup.any():
        raise RuntimeError(
            f"Regime event source has {int(dup.sum())} duplicated core-key rows."
        )

    return df.sort_values(
        ["symbol", "signal_date", "entry_date", "exit_date"]
    ).reset_index(drop=True)


def suppress_while_active(
    df: pd.DataFrame,
    allow_signal_on_exit_date: bool,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Per-symbol state machine.

    Frozen semantic being tested:
      keep the first signal while actually flat;
      suppress subsequent signals while the prior accepted trade is active.

    allow_signal_on_exit_date=True:
      EXIT_THEN_SIGNAL
      If prior trade exits at today's close, today's close signal is allowed.
      Accept when signal_date >= prior accepted exit_date.

    allow_signal_on_exit_date=False:
      SIGNAL_BEFORE_EXIT
      Today's signal is still considered blocked if prior trade exits today.
      Accept only when signal_date > prior accepted exit_date.
    """
    accepted_rows = []
    suppressed_rows = []

    for symbol, g in df.groupby("symbol", sort=True):
        last_accepted = None
        last_exit = None

        for row in g.itertuples(index=False):
            signal_date = str(row.signal_date)
            exit_date = str(row.exit_date)

            if last_exit is None:
                is_flat = True
            elif allow_signal_on_exit_date:
                is_flat = signal_date >= last_exit
            else:
                is_flat = signal_date > last_exit

            payload = row._asdict()

            if is_flat:
                payload.update({
                    "lineage_action": "ACCEPT",
                    "blocked_by_signal_date": None,
                    "blocked_by_entry_date": None,
                    "blocked_by_exit_date": None,
                    "days_relation": None,
                })
                accepted_rows.append(payload)
                last_accepted = row
                last_exit = exit_date
            else:
                payload.update({
                    "lineage_action": "SUPPRESS_WHILE_ACTIVE",
                    "blocked_by_signal_date": str(last_accepted.signal_date),
                    "blocked_by_entry_date": str(last_accepted.entry_date),
                    "blocked_by_exit_date": str(last_accepted.exit_date),
                    "days_relation": (
                        f"{signal_date} < {last_exit}"
                        if allow_signal_on_exit_date
                        else f"{signal_date} <= {last_exit}"
                    ),
                })
                suppressed_rows.append(payload)

    return (
        pd.DataFrame(accepted_rows),
        pd.DataFrame(suppressed_rows),
    )


def validate_accepted_nonoverlap(
    accepted: pd.DataFrame,
    allow_signal_on_exit_date: bool,
) -> int:
    violations = 0

    for _, g in accepted.groupby("symbol", sort=True):
        g = g.sort_values(["signal_date", "entry_date"])
        prev_exit = None

        for row in g.itertuples(index=False):
            if prev_exit is not None:
                if allow_signal_on_exit_date:
                    bad = str(row.signal_date) < prev_exit
                else:
                    bad = str(row.signal_date) <= prev_exit
                violations += int(bad)

            prev_exit = str(row.exit_date)

    return violations


def summarize_suppressed(suppressed: pd.DataFrame) -> dict[str, Any]:
    if suppressed.empty:
        return {
            "rows": 0,
            "symbols": 0,
            "top_symbols": [],
        }

    vc = (
        suppressed["symbol"]
        .value_counts()
        .head(30)
    )

    return {
        "rows": int(len(suppressed)),
        "symbols": int(suppressed["symbol"].nunique()),
        "top_symbols": [
            {"symbol": str(symbol), "suppressed": int(n)}
            for symbol, n in vc.items()
        ],
    }


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    events = load_events()

    if len(events) != 1468:
        raise RuntimeError(
            f"Expected 1468 CSI500 regime events from v0.2 lineage audit; "
            f"got {len(events)}."
        )

    variants = {}

    specs = [
        ("EXIT_THEN_SIGNAL", True),
        ("SIGNAL_BEFORE_EXIT", False),
    ]

    for name, allow_same_day in specs:
        accepted, suppressed = suppress_while_active(
            events,
            allow_signal_on_exit_date=allow_same_day,
        )

        violations = validate_accepted_nonoverlap(
            accepted,
            allow_signal_on_exit_date=allow_same_day,
        )

        accepted_path = OUT_DIR / f"{name.lower()}_accepted.parquet"
        suppressed_path = OUT_DIR / f"{name.lower()}_suppressed.csv"

        accepted.to_parquet(accepted_path, index=False)
        suppressed.to_csv(
            suppressed_path,
            index=False,
            encoding="utf-8-sig",
        )

        variants[name] = {
            "allow_signal_on_exit_date": allow_same_day,
            "accepted": int(len(accepted)),
            "suppressed": int(len(suppressed)),
            "exact_target_1365": int(len(accepted)) == TARGET_COUNT,
            "nonoverlap_violations": int(violations),
            "suppressed_summary": summarize_suppressed(suppressed),
            "accepted_path": str(accepted_path),
            "suppressed_path": str(suppressed_path),
        }

    exact = [
        name
        for name, result in variants.items()
        if result["exact_target_1365"]
        and result["nonoverlap_violations"] == 0
    ]

    audit = {
        "audit_id": "DEEP_OVERSOLD_TRADE_LINEAGE_V03A",
        "source": str(REGIME),
        "source_events": int(len(events)),
        "target_reference": TARGET_COUNT,
        "tested_semantic": (
            "Per-symbol duplicate suppression: keep first signal while flat; "
            "suppress later signals while prior accepted trade remains active."
        ),
        "variants": variants,
        "exact_matching_variants": exact,
        "canonical_selected_automatically": False,
        "decision_rule": (
            "An exact 1365 match is strong lineage evidence but should be "
            "accepted only if its event-order convention matches the frozen "
            "Deep Oversold implementation semantics."
        ),
    }

    (OUT_DIR / "audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("=" * 110)
    print("DEEP OVERSOLD CSI500 TRADE LINEAGE AUDIT V0.3A")
    print("=" * 110)
    print("Source regime events:", len(events))
    print("Frozen trade-count reference:", TARGET_COUNT)
    print()

    for name, result in variants.items():
        marker = "  <== EXACT 1365" if result["exact_target_1365"] else ""
        print(name)
        print("-" * 110)
        print(
            f"accepted={result['accepted']} "
            f"suppressed={result['suppressed']} "
            f"nonoverlap_violations={result['nonoverlap_violations']}"
            f"{marker}"
        )
        ss = result["suppressed_summary"]
        print(
            f"suppressed symbols={ss['symbols']}"
        )
        if ss["top_symbols"]:
            print(
                "top suppressed symbols:",
                ", ".join(
                    f"{x['symbol']}={x['suppressed']}"
                    for x in ss["top_symbols"][:15]
                ),
            )
        print()

    print("Exact matching variants:", exact if exact else "NONE")
    print("Output:", OUT_DIR)
    print("No canonical selection was written into strategy/execution code.")
    print("No source data or production database was modified.")


if __name__ == "__main__":
    main()
