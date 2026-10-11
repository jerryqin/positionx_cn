# PositionX Optimus R1 Source Recall Probe V0.6

## Purpose

This is the **read-only R1 probe** for `Seed Debt Resolution V0.6`.

It answers one narrow question:

> Why did known authoritative announcements fail to appear in the V0.4.5
> seed-debt artifact set?

It does **not** alter the frozen V0.4.4 classifier and does not mutate Registry,
evidence, canonicalization, DuckDB, broker, order, or production state.

## Built-in anchors

### 2026-08-25 — 恒立液压 601100.SH

- `1225499629`: positive control, known present in prior V0.4.5 artifact log.
- `1225497703`: 2026 H1 report summary, known authoritative CNINFO static PDF,
  but absent from the prior one-artifact 8/25 seed-scope result.

### 2026-08-28 — 拓普集团 601689.SH

- `1225521853`: 2026 H1 full report; known authoritative CNINFO static PDF;
  absent from prior V0.4.5 artifact set.
- `1225521772`: 2026 H1 report summary; same-day corroborating document.

## What it tests

For each anchor date the probe runs:

1. The **blank global CNINFO fulltext query** matching the V0.4.5 query shape.
2. A **company-name keyword query**.
3. When the keyword query exposes `orgId`, a **stock-scoped query** using the
   upstream `secCode,orgId` token.
4. Direct read-only reachability check for the known CNINFO static PDF.
5. Optional sorted blank query with `--sorted-variant`.

It records:

- reported total pages/count;
- requested pages;
- page sizes;
- raw rows;
- unique announcement IDs;
- duplicate ratio;
- IDs repeated across pages;
- whether the last page is suspiciously full;
- anchor presence/page;
- upstream secCode/orgId;
- normalized symbol and A-share validity;
- direct static-PDF reachability.

## Main diagnoses

- `Q0_GLOBAL_BLANK_QUERY_RECALL_GAP`
- `Q0_GLOBAL_BLANK_QUERY_RECALL_GAP_STOCK_SCOPE_RECOVERS`
- `Q0_UNSORTED_GLOBAL_QUERY_RECALL_OR_PAGINATION_GAP`
- `Q0_CNINFO_QUERY_RECALL_OR_INDEXING_GAP`
- `Q1_NORMALIZATION_MISMATCH`
- `Q1_A_SHARE_VALIDITY_FILTER_RISK`
- `RAW_GLOBAL_PRESENT_DOWNSTREAM_REVIEW_REQUIRED`

No diagnosis from this probe can establish a V0.4.4 classifier defect, because
the classifier is intentionally not invoked.

## Install

From the extracted patch directory:

```powershell
python .\install_optimus_source_recall_v06.py --project-root D:\positionx_cn
```

The installer is additive and idempotent. If a target file already differs, it
fails closed unless `--force` is explicitly supplied.

## Validate locally

```powershell
cd D:\positionx_cn
python -m unittest tests.test_optimus_source_recall_v06 -v
python scripts\audit_optimus_source_recall_v06.py --help
```

## Recommended first run: 拓普 only

```powershell
cd D:\positionx_cn
python scripts\audit_optimus_source_recall_v06.py `
  --date 2026-08-28
```

This is the shortest decisive probe. It should preserve raw API pages by
default.

Then inspect the final console block and:

```text
D:\positionx_data\research\big_opportunity\optimus\source_recall_probe_v0_6\<capture_id>\result.json
```

## Second run: 恒立 only

```powershell
python scripts\audit_optimus_source_recall_v06.py `
  --date 2026-08-25
```

## Optional pagination stability test

Only if the first probe still leaves Q0 ambiguous:

```powershell
python scripts\audit_optimus_source_recall_v06.py `
  --date 2026-08-28 `
  --sorted-variant
```

## Safety

The script:

- never opens PositionX DuckDB;
- never ingests evidence;
- never reconciles Registry;
- never writes Canonical Facts;
- never calls the V0.4.4 classifier;
- never touches broker/order/production systems;
- only writes diagnostic output under `source_recall_probe_v0_6`.
