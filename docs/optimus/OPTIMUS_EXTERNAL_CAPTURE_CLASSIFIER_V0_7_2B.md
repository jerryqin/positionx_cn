# PositionX Optimus V0.7.2B — External Capture + Proposal Classification

V0.7.2A has passed with:

```text
9 / 9 tests PASS
Source Registry = 8
capture_count = 0
Registry / Canonicalization / Theme mutation = false
```

V0.7.2B now adds the operational layer:

```text
exact registered official URL
→ raw download
→ V0.7.2A immutable External Inbox
→ local text extraction
→ proposal-only classifier
→ human review queue
```

## No-hindsight gate

The prospective case started:

```text
2026-10-08T00:00:00+08:00
```

V0.7.2B therefore never uses `observed_at` as an event date.

```text
event_time
else published_at
else UNKNOWN
```

A strong milestone published before the case start becomes:

```text
HISTORICAL_BASELINE_ONLY
```

It can enrich baseline context but **cannot** retroactively become a Stage2 trigger.

A strong signal with no reliable `event_time` or `published_at` becomes:

```text
HUMAN_REVIEW_REQUIRED
```

not `STAGE2_GATE_CANDIDATE`.

## Queue semantics

```text
prospective official contradiction
→ CONTRADICTION_ALERT

prospective THEME_OFFICIAL
+ Optimus/humanoid context
+ strong milestone
→ STAGE2_GATE_CANDIDATE

prospective supplier official
+ explicit Tesla/Optimus context
+ commercialization milestone
→ STAGE2_GATE_CANDIDATE

prospective supplier commercialization
without named Tesla gate
→ EVIDENCE_PROPOSAL

historical pre-case signal
→ HISTORICAL_BASELINE_ONLY

unknown effective time + material signal
→ HUMAN_REVIEW_REQUIRED
```

Every nonzero signal remains review-only. V0.7.2B does **not** assign Canonical Fact,
evidence level, Tesla-link level, F-status, candidate state, or Theme stage.

## Retrieval discipline

The runner downloads only exact URLs whose hosts are already registered in V0.7.2A.

Search engines are recall-only. Search snippets are never classified.

SEC retrieval requires the environment variable:

```powershell
$env:POSITIONX_SEC_CONTACT_EMAIL = "your-contact-email@example.com"
```

This is only needed when the manifest contains `sec.gov`.

## Install

```powershell
python .\install_optimus_external_classifier_v072b.py `
  --project-root D:\positionx_cn
```

## Validate

```powershell
cd D:\positionx_cn

python -m unittest tests.test_optimus_external_classifier_v072b -v

python scripts\run_optimus_external_classifier_v072b.py check
python scripts\run_optimus_external_classifier_v072b.py plan
python scripts\run_optimus_external_classifier_v072b.py status
```

Before the first run, status should be:

```text
ARMED_OPTIMUS_EXTERNAL_CAPTURE_CLASSIFIER_V0_7_2B_NO_RUNS
```

## First controlled Tesla official pilot

```powershell
python scripts\run_optimus_external_classifier_v072b.py run-manifest `
  --manifest examples\tesla_official_controlled_pilot_20261010.json

python scripts\run_optimus_external_classifier_v072b.py status
python scripts\run_optimus_external_classifier_v072b.py queue
```

The pilot uses only:

```text
https://www.tesla.com/AI
```

Because that page has no reliable publication/event timestamp in the pilot metadata,
even if a future classifier sees a material milestone it must stop at
`HUMAN_REVIEW_REQUIRED`. A generic Optimus description should normally produce
`NO_NEW_MATERIAL_EVIDENCE`.

## Next

After one clean V0.7.2B controlled run, implement V0.7.2C New Candidate Discovery Queue.
