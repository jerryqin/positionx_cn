# Optimus Candidate Registry V0.3.1 — Ops Hardening

This patch does not change candidate admission rules, F-state rules, seed universe, registry tables, or canonical review semantics.

## Changes

1. CNINFO discovery diagnostics now distinguish:
   - rows_seen
   - valid_a_share_rows
   - dropped_invalid_or_out_of_scope_symbol
   - duplicate_events
   - existing_symbol_hits / new_symbol_hits
   - per-keyword statistics
   - examples of rejected secCode rows

2. Adds `maintenance-queue` covering:
   - provisional evidence
   - seed audit debt
   - stale canonical candidates
   - discovery-only candidates
   - recent state changes

3. `status` includes maintenance counts.

4. `daily` reports maintenance summary after discovery / inbox ingestion / reconcile / snapshot.

## Safety

Research-only. No broker, orders, production DB, Deep/RET120, Portfolio Shadow, G4, or trading changes.
