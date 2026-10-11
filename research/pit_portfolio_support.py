from __future__ import annotations

from bisect import bisect_right
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Sequence

from research.fillability_gate import normalize_trade_date


def audit_next_market_session(
    events: Sequence[Mapping[str, Any]],
    market_dates: Sequence[str],
) -> Dict[str, Any]:
    cal = sorted({normalize_trade_date(d) for d in market_dates})
    mismatches = []
    no_next = 0
    for e in events:
        signal = normalize_trade_date(e["signal_date"])
        entry = normalize_trade_date(e["entry_date"])
        i = bisect_right(cal, signal)
        if i >= len(cal):
            no_next += 1
            continue
        expected = cal[i]
        if entry != expected:
            mismatches.append({
                "symbol": str(e.get("symbol")),
                "signal_date": signal,
                "entry_date": entry,
                "expected_next_market_session": expected,
            })
    return {
        "events_checked": len(events),
        "calendar_dates": len(cal),
        "entry_next_market_session_mismatches": len(mismatches),
        "no_next_market_session": no_next,
        "mismatch_sample": mismatches[:50],
        "pass": len(mismatches) == 0,
    }


def load_market_calendar_from_parquet(
    parquet_path: str | Path,
    start_date: str,
    end_date: str,
) -> list[str]:
    try:
        import duckdb
    except ImportError as exc:
        raise RuntimeError("duckdb is required") from exc

    p = Path(parquet_path)
    if not p.exists():
        raise FileNotFoundError(p)

    con = duckdb.connect()
    try:
        rows = con.execute(
            """
            SELECT DISTINCT strftime(CAST(trade_date AS DATE), '%Y%m%d') AS d
            FROM read_parquet(?)
            WHERE CAST(trade_date AS DATE)
                  BETWEEN strptime(?, '%Y%m%d')::DATE
                      AND strptime(?, '%Y%m%d')::DATE
            ORDER BY d
            """,
            [str(p), normalize_trade_date(start_date), normalize_trade_date(end_date)],
        ).fetchall()
    finally:
        con.close()
    return [str(r[0]) for r in rows]


def add_capacity_rates(summary: Dict[str, Any]) -> Dict[str, Any]:
    executable = int(summary.get("signals_executable") or 0)
    no_slot = int(summary.get("skipped_no_slot") or 0)
    dup = int(summary.get("skipped_duplicate_symbol") or 0)
    summary["capacity_skip_rate"] = no_slot / executable if executable else 0.0
    summary["total_skip_rate"] = (no_slot + dup) / executable if executable else 0.0
    return summary
