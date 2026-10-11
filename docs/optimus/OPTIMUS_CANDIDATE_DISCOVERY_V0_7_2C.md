# PositionX Optimus V0.7.2C — New Candidate Discovery Queue

V0.7.2B is now operationally clean:

```text
B.2 context tests        6 / 6 PASS
B.1 transport tests      6 / 6 PASS
original classifier      8 / 8 PASS

latest reclassification:
classified_count = 1
queue_count = 0
error_count = 0
manifest_payload_sha256 =
c7b0dff8636a25f2832827565b1adc728c62cb1062d28aaf9d1d5400bc43bf6a
```

V0.7.2C therefore expands **candidate coverage**, while preserving the frozen 22.

## Two-stage discovery

Discovery is deliberately split:

```text
search / media / broker lead
        ↓
DISCOVERY_RECALL_ONLY
        ↓
registered official source
        ↓
V0.7.2A immutable capture
        ↓
V0.7.2B.2 context-scoped classifier
        ↓
V0.7.2C DISCOVERY_PROPOSAL
        ↓
human review
        ↓
SEPARATE_REGISTRY_PLAN_REQUIRED
```

A search result never becomes Evidence.

## Current 22 are excluded

If a lead/capture already matches the current 22 by symbol or normalized company name:

```text
SKIP_CURRENT_22
```

V0.7.2C is for genuinely new candidates only.

## Historical evidence

A pre-2026-10-08 official company-specific robot milestone may support:

```text
DISCOVERY_PROPOSAL
historical_context_only = true
prospective_theme_effect = false
```

This is intentional: historical evidence can establish that a company belongs on the
research radar, but it cannot be backdated into a Theme Stage trigger.

## Cross-section protection

V0.7.2B.2 remains authoritative. A document that says "robot" in one section and
"mass production" in another unrelated section is rejected for discovery if that is
the only signal.

## Install

```powershell
python .\install_optimus_candidate_discovery_v072c.py `
  --project-root D:\positionx_cn
```

## Validate

```powershell
cd D:\positionx_cn

python -m unittest tests.test_optimus_candidate_discovery_v072c -v

python scripts\run_optimus_candidate_discovery_v072c.py check
python scripts\run_optimus_candidate_discovery_v072c.py plan
python scripts\run_optimus_candidate_discovery_v072c.py status
```

Expected before a discovery run:

```text
ARMED_OPTIMUS_CANDIDATE_DISCOVERY_V0_7_2C_NO_RUNS
```

## First safe scan

The current External Inbox contains the Tesla THEME_OFFICIAL capture, not a discovery
capture. Therefore the first scan should safely produce zero new candidates:

```powershell
python scripts\run_optimus_candidate_discovery_v072c.py scan-inbox

python scripts\run_optimus_candidate_discovery_v072c.py status
python scripts\run_optimus_candidate_discovery_v072c.py queue
```

Expected:

```text
proposal_count = 0
registry_mutation = false
```

## Preparing future recall leads

Use `examples\discovery_leads_template.json` as a template:

```powershell
python scripts\run_optimus_candidate_discovery_v072c.py prepare-leads `
  --input D:\path\discovery_leads.json
```

The resulting recall queue is not evidence. Each candidate must still resolve to a
registered official source and pass V0.7.2A/B.2.

## After a real proposal appears

Do not add it to Registry from V0.7.2C. Human review must decide one of:

```text
APPROVED_DISCOVERY_ONLY
REJECTED_DISCOVERY
```

An approved name then requires a **separate Registry PLAN**.
