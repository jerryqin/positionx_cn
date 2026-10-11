# PositionX Optimus V0.7.2A — Source Registry + Normalized External Inbox

## Purpose

V0.7.2A creates the boundary between **external retrieval** and **evidence inference**.

```text
external official source bytes
        ↓
source registry validation
        ↓
normalized immutable capture
        ↓
external_inbox_v0_7_2a
        ↓
NO FACT INFERENCE YET
```

This layer never writes Registry, Canonicalization, Theme Stage, broker, orders, or
production DB.

## Seed source registry

The initial registry contains 8 records:

```text
SRC_TESLA_IR_OFFICIAL
SRC_TESLA_CORPORATE_OFFICIAL
SRC_SEC_TESLA_EDGAR
SRC_SEC_GENERIC
SRC_HKEXNEWS_OFFICIAL
SRC_SSE_OFFICIAL
SRC_SZSE_OFFICIAL
SRC_CNINFO_EXISTING_LANE
```

CNINFO is explicitly `external_inbox_allowed=false` because V0.7.1 already owns that
lane. This prevents duplicate ingestion.

Unknown company-official domains are **not trusted automatically**. They must first
go through a separate reviewed source-registry update.

## Normalized capture contract

Required:

```text
source_id
lane
entity_name
source_url
document_title
observed_at       # timezone-aware
raw_file_path
```

Optional:

```text
symbol
entity_identifier
published_at
event_time
capture_method
media_type
language
search_recall
```

`event_time` may be NULL. It is never silently replaced by `observed_at`.

The following inference fields are rejected at this boundary:

```text
fact_id / fact_key / claim_text
evidence_level / tesla_link_level
f_status / candidate_state / canonical_state
theme_stage
```

## Identity and versioning

```text
capture_identity =
SHA256(source_id + canonical_url + raw_sha256)
```

Same URL + same bytes is idempotent. Same URL + changed bytes becomes a new capture
version.

## Install

```powershell
python .\install_optimus_external_inbox_v072a.py `
  --project-root D:\positionx_cn
```

## Validate installation

```powershell
cd D:\positionx_cn

python -m unittest tests.test_optimus_external_inbox_v072a -v

python scripts\run_optimus_external_inbox_v072a.py check
python scripts\run_optimus_external_inbox_v072a.py plan
python scripts\run_optimus_external_inbox_v072a.py sources
python scripts\run_optimus_external_inbox_v072a.py status
```

## Validate a captured file without writing

Copy `examples/external_capture_template.json`, point `raw_file_path` at the actual
downloaded HTML/PDF/text file, update `observed_at`, then:

```powershell
python scripts\run_optimus_external_inbox_v072a.py validate `
  --input D:\path\capture.json
```

## Ingest a captured file

```powershell
python scripts\run_optimus_external_inbox_v072a.py ingest `
  --input D:\path\capture.json
```

The only write is under:

```text
D:\positionx_data\research\big_opportunity\optimus\external_inbox_v0_7_2a
```

## Next

After V0.7.2A passes, V0.7.2B can implement read-only retrieval/classification for
Tesla official and registered global supplier sources, using this inbox as the
immutable source boundary.
