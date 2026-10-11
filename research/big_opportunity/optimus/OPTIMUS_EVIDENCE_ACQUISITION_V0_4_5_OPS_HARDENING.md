# Optimus Evidence Acquisition V0.4.5

Scope: operations hardening only. V0.4.4 classifier semantics are unchanged.

Changes:
1. Adds `backfill-seed-debt-range --start YYYY-MM-DD --end YYYY-MM-DD`.
2. This mode reads the current Registry maintenance queue and scopes to `seed_audit_debt` symbols only.
3. For each date in the requested range, it fetches **all CNINFO announcements** (empty search key), then filters locally to current seed-audit-debt symbols before downloading/parsing PDFs.
4. `status` now exposes `query_stats` and `query_quality`, so a run with query errors cannot look equivalent to a clean zero-hit run.
5. No automatic canonical upgrade. All extracted evidence remains PROVISIONAL.
6. Research-only; no broker, orders, or production DB.

Why: keyword-search `daily` is useful for broad discovery but cannot prove absence of evidence in generic-titled periodic reports or IR records. V0.4.5 adds an exhaustive source-date scan for the remaining seed audit debt without changing the frozen evidence classifier.
