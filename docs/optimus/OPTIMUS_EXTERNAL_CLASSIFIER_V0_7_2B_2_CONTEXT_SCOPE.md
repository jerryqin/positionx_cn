# V0.7.2B.2 Hotfix — Context-Scoped Milestone Matching

## Trigger

The first successful manual Tesla capture produced:

```text
HUMAN_REVIEW_REQUIRED
reason = MATERIAL_SIGNAL_BUT_EVENT_TIME_UNKNOWN
signal_tags = [MASS_PRODUCTION]
```

Human review shows that this is a **cross-section false positive**:

```text
Tesla Optimus section
→ generic humanoid-robot description

FSD Chip section
→ "bring it to mass production in our vehicles"
```

The document-level classifier saw `Optimus` somewhere and `mass production` somewhere
else, then incorrectly combined them.

## Decision

That queue item should be reviewed as:

```text
REJECT_CLASSIFIER_FALSE_POSITIVE
reason = CROSS_SECTION_MILESTONE_NOT_OPTIMUS
```

It is not Optimus Stage2 evidence.

## B.2 correction

Milestones used for decisions must now share a context block with the relevant subject:

```text
THEME_OFFICIAL:
Optimus/humanoid context + milestone in same sentence/line
OR
short Optimus heading + immediately following block

SUPPLIER proposal:
robot/humanoid context + commercialization milestone

SUPPLIER Stage2 gate:
Tesla/Optimus named context + commercialization milestone
```

Whole-document tags are retained only as:

```text
global_signal_tags
cross_section_signal_tags
```

for audit. They no longer make a queue item by themselves.

## No-hindsight and transport contracts

Unchanged:

```text
event_time > published_at > UNKNOWN
observed_at never substitutes
pre-case evidence = HISTORICAL_BASELINE_ONLY
HTTP 403 manual fallback remains active
```

## Reclassify the already-ingested Tesla capture

After installing, no re-download or re-save is required:

```powershell
python scripts\run_optimus_external_classifier_v072b.py reclassify-latest

python scripts\run_optimus_external_classifier_v072b.py status
python scripts\run_optimus_external_classifier_v072b.py queue
```

Expected for the current Tesla AI capture:

```text
queue_count = 0
reason in audit = CROSS_SECTION_SIGNAL_REJECTED
global_signal_tags includes MASS_PRODUCTION
theme_context_signal_tags does not include MASS_PRODUCTION
```

The old run remains immutable for audit; the new reclassification run supersedes it
operationally.

## Install

```powershell
python .\install_optimus_external_classifier_v072b2_context_hotfix.py `
  --project-root D:\positionx_cn
```

The installer only patches the exact B.1 script/config:

```text
B.1 script:
575a1aeee42f46e85ce2c2ca758f29205634b3c3106fc1034dc4e30805c1a017

B.1 config:
ffb803f3adfa148b7d3f5e4e34f5fb44cd3465e438f179e2d53659a18e02f1b1
```
