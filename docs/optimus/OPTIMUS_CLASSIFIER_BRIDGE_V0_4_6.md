# PositionX Optimus Classifier Bridge V0.4.6

## Purpose

Bridge the **complete V0.4.6 candidate-stock-scoped source corpus** into the
exact frozen **V0.4.4 Evidence Proposal Engine**.

This phase does not acquire any new source material. It reuses the already
downloaded local PDFs and produces a proposal-only review queue.

## Frozen boundary

The bridge refuses to run unless:

```text
scripts/run_optimus_evidence_acquisition_v04.py
SHA256 =
10e5a8288177c76e207fa264f74e18c3a878d95cf191585ca8a576643475a691
```

Classifier contract:

```text
OPTIMUS_EVIDENCE_ACQUISITION_V0_4_4
```

It calls only the existing `build_evidence(..., local_pdf_path=...)` path.

## Safety

The imported frozen module is additionally patched at runtime so the following
paths raise immediately if accidentally called:

- `cninfo_post`
- `fetch_url_bytes`
- `ingest_with_registry`
- `load_registry_module`

Therefore this bridge performs:

```text
network query             = false
PDF download              = false
DB open                   = false
evidence inbox write      = false
Registry ingest           = false
Registry reconcile        = false
Canonicalization          = false
broker/order              = false
```

## Install

```powershell
python .\install_optimus_classifier_bridge_v046.py --project-root D:\positionx_cn
```

## Validate

```powershell
cd D:\positionx_cn

python -m unittest tests.test_optimus_classifier_bridge_v046 -v

python scripts\run_optimus_classifier_bridge_v046.py check
```

The default source is the latest valid PASS capture below:

```text
D:\positionx_data\research\big_opportunity\optimus\source_recall_v0_4_6\backfill
```

## Run

```powershell
python scripts\run_optimus_classifier_bridge_v046.py run
```

Expected terminal status:

```text
PASS_OPTIMUS_CLASSIFIER_BRIDGE_V0_4_6
```

The output directory contains:

- `manifest.json`
- `evidence_proposals_v0_4_4.jsonl`
- `audit_all_206.jsonl`
- `review_queue.json`

## After PASS

Do not ingest the proposal JSONL into Registry yet.

Upload these two files for human review / Seed Debt Resolution V0.6 PLAN:

```text
manifest.json
review_queue.json
```

Only after PLAN review should any Registry or Canonicalization mutation be
considered.
