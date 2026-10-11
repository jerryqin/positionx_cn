from __future__ import annotations

class LedgerResolutionError(RuntimeError):
    pass

def choose_base_ledger(ledger_counts: dict[str, int], expected_active_positions: int) -> str | None:
    expected = int(expected_active_positions)
    if expected < 0:
        raise LedgerResolutionError("expected_active_positions < 0")

    clean = {str(k): int(v) for k, v in ledger_counts.items() if int(v) >= 0}
    base_like = sorted([k for k in clean if k.upper().startswith("BASE")])

    if not base_like:
        if expected == 0:
            return None
        raise LedgerResolutionError(
            f"NO_BASE_LEDGER expected_active_positions={expected} ledger_counts={clean}"
        )

    if len(base_like) != 1:
        raise LedgerResolutionError(
            f"AMBIGUOUS_BASE_LEDGER candidates={base_like} ledger_counts={clean}"
        )

    chosen = base_like[0]
    if clean[chosen] != expected:
        raise LedgerResolutionError(
            f"BASE_LEDGER_COUNT_MISMATCH ledger={chosen} rows={clean[chosen]} "
            f"expected_active_positions={expected}"
        )
    return chosen
