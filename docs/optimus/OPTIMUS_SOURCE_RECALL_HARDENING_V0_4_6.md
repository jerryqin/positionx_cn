# PositionX Optimus Source Recall Hardening V0.4.6 — Phase A

## Status

This package implements the **source-recall repair layer** discovered by R1.

R1 established that a CNINFO blank global query can claim hundreds of pages
while deep pages repeat earlier data. Therefore prior `query_quality=COMPLETE`
proved transport completion, not source-recall completeness.

V0.4.6 Phase A deliberately does **not** touch the frozen V0.4.4 classifier.

## Architecture

```text
Registry/Seed candidate
        ↓
company-name query (bootstrap orgId only)
        ↓
exact secCode match
        ↓
stock-scoped CNINFO query
        ↓
pagination-integrity guard
        ↓
dedupe announcementId
        ↓
optional PDF download
        ↓
immutable recall corpus
        ↓
[V0.4.4 classifier bridge — later phase]
```

Known-candidate acquisition is now separated from broad-market discovery.

## Safety

No DuckDB is opened. No Registry/Canonical state is changed. No evidence is
ingested. No classifier is called. No broker/order/production system is touched.

## Install

```powershell
python .\install_optimus_source_recall_v046.py --project-root D:\positionx_cn
```

Installer is additive and idempotent. Existing differing files fail closed
unless `--force` is explicitly supplied.

## Validate

```powershell
cd D:\positionx_cn
python -m unittest tests.test_optimus_source_recall_v046 -v
python scripts\run_optimus_source_recall_v046.py check
```

`check` reports the SHA of the existing V0.4.x script for audit only. It does
not edit it.

## Next action: targeted replay only

Run exactly:

```powershell
python scripts\run_optimus_source_recall_v046.py targeted-replay
```

This replays:

- 拓普集团 / 2026-08-28
  - 1225521853
  - 1225521772
- 恒立液压 / 2026-08-25
  - 1225499629
  - 1225497703

Expected result:

```text
PASS_OPTIMUS_SOURCE_RECALL_V0_4_6_TARGETED_REPLAY
```

Do **not** run the full 180-day backfill yet.

## Optional PDF confirmation

Only if targeted replay passes:

```powershell
python scripts\run_optimus_source_recall_v046.py targeted-replay --download-pdfs
```

## Full Seed Debt backfill — HOLD until targeted replay reviewed

Command is already implemented but should not be run yet:

```powershell
python scripts\run_optimus_source_recall_v046.py backfill-seed-debt-range `
  --start 2026-04-12 `
  --end 2026-10-09 `
  --download-pdfs
```

The built-in candidate set is the five remaining V0.6 Seed Debt companies.

## Integrity guard

The source layer fails closed if:

- a candidate-scoped query unexpectedly requires more than 100 pages;
- duplicate-row ratio exceeds 10% on a nontrivial result;
- unique IDs are materially inconsistent with upstream reported totals.

The old global blank-query result is therefore no longer accepted as evidence of
source completeness.

## What remains after Phase A

After targeted replay passes:

1. review the replay manifest;
2. decide whether to rebuild the 180-day five-company source corpus;
3. bridge the rebuilt corpus into the existing frozen V0.4.4 classifier;
4. human-review resulting proposals / Canonical Facts;
5. only then return to Seed Debt Resolution V0.6 PLAN/APPLY.
