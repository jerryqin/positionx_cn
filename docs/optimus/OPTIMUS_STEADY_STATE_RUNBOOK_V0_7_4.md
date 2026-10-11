# PositionX Optimus — V0.7.4 Steady-State Runbook / Freeze Baseline

## Frozen operating state

```text
Registry SHA
975ab7ff3d8f9d5b644defcd8f4a3479969f1b4c7b08b3cf0b7eac23f3ef69f4

Registry candidates        24
ACTIVE_CANONICAL           22
DISCOVERY_ONLY              2
ACTIVE_SEED                  0
canonical candidates       22
provisional evidence         2
```

The daily canonical monitoring universe stays at **22**.

```text
603416.SH 信捷电气
→ DISCOVERY_ONLY / SPECULATIVE / tesla_link_level=0

603809.SH 豪能股份
→ DISCOVERY_ONLY / SPECULATIVE / tesla_link_level=0

600353.SH 旭光电子
→ WAITING_CAPTURE_REPAIR / no Registry action
```

`WAITING_CAPTURE_REPAIR` is a valid state. Do not OCR merely to make the scan fully green.

## Theme state

```text
Stage 1 / Late Incubation
PRE-RAMP
INDUSTRIALIZATION
confidence = HIGH
```

Routine monitoring never mutates Theme Stage.

A Stage 1 → Stage 2 proposal requires prospective official evidence that binds, in the
same semantic context:

```text
Tesla / Optimus
+
robot / humanoid
+
commercial milestone
```

Generic Tesla automotive business elsewhere in the document does not qualify.

## Daily cycle

### 1. Preflight

```powershell
cd D:\positionx_cn

python scripts\run_optimus_steady_state_v074.py check
```

Expected:

```text
PASS_OPTIMUS_STEADY_STATE_V0_7_4_CHECK
```

A SHA or Registry-baseline mismatch is a STOP condition. Do not edit the expected hash just
to make the check pass.

### 2. Current canonical 22 / CNINFO

```powershell
python scripts\run_optimus_prospective_intake_v071.py daily --days 1

python scripts\run_optimus_prospective_intake_v071.py status

python scripts\run_optimus_prospective_intake_v071.py queue
```

603416 and 603809 are excluded from this lane.

### 3. External official capture processing

When a controlled external manifest is available:

```powershell
python scripts\run_optimus_external_classifier_v072b.py run-manifest `
  --manifest <manifest.json>

python scripts\run_optimus_external_classifier_v072b.py status

python scripts\run_optimus_external_classifier_v072b.py queue
```

For an approved browser/manual fallback:

```powershell
python scripts\run_optimus_external_classifier_v072b.py manual-capture `
  --manifest <single-record-manifest.json> `
  --raw-file <official-file>
```

Search snippets are recall-only and never classifier input.

### 4. Candidate discovery

After new `NEW_CANDIDATE_DISCOVERY` captures enter the External Inbox:

```powershell
python scripts\run_optimus_candidate_discovery_v072c.py scan-inbox

python scripts\run_optimus_candidate_discovery_v072c.py status

python scripts\run_optimus_candidate_discovery_v072c.py queue
```

C.8 skips existing Registry members before content parsing.

Validated C.8 baseline:

```text
proposal_count                                   0
existing_registry_skipped_before_classification  4
waiting_capture_repair_count                     1
```

## B.8 dedupe

Every successful classification is processed, including:

```text
NO_NEW_MATERIAL_EVIDENCE
HISTORICAL_BASELINE_ONLY
EVIDENCE_PROPOSAL
STAGE2_GATE_CANDIDATE
```

These are not processed:

```text
ERROR
WAITING_MANUAL_CAPTURE
```

`reclassify-latest` is the explicit policy-change escape hatch.

Validated real dedupe probe:

```text
classified_count            0
queue_count                 0
skipped_already_classified  1
error_count                 0
```

## Human-review triggers

Review instead of continuing automatically when any of these appears:

```text
EVIDENCE_PROPOSAL
STAGE2_GATE_CANDIDATE
CONTRADICTION_ALERT
HUMAN_REVIEW_REQUIRED
DISCOVERY_PROPOSAL count > 0
error_count > 0
manual_capture_required_count > 0
WAITING_MANUAL_CAPTURE
Registry SHA drift
seed_audit_debt > 0
stale_canonical > 0
unexpected DISCOVERY_ONLY/provisional-count change
```

`ERROR` and `WAITING_MANUAL_CAPTURE` are processing states, not negative evidence.

## Registry governance

Required admission path:

```text
DISCOVERY_PROPOSAL
→ human review
→ APPROVED_DISCOVERY_ONLY
→ separate Registry PLAN
→ explicit approval
→ guarded Registry APPLY
→ post-APPLY verification
→ new steady-state baseline revision
```

Any legitimate future Registry APPLY will change the Registry SHA and intentionally make
this V0.7.4 checker fail until the next baseline revision is approved.

## Canonicalization governance

Never auto-promote:

```text
DISCOVERY_ONLY
→ ACTIVE_CANONICAL
```

Promotion requires a separate evidence-review/canonicalization process. Provisional evidence
never auto-promotes.

## Frozen history — do not rerun

V0.6 Seed Debt Resolution is frozen. Do not rerun its Batch1/2/3 or APPLY.

V0.7.3 Registry Expansion is frozen:

```text
PLAN payload SHA
cf6dad84cb60e67d3e27962b5b8dcf80da0ae862b561abb25c148143d0d5142e

APPLY result payload SHA
b6b1d7e3c96867d0526fb63b9278efe89e41ff77026230e085cad73953c47878

post Registry SHA
975ab7ff3d8f9d5b644defcd8f4a3479969f1b4c7b08b3cf0b7eac23f3ef69f4
```

Do not rerun V0.7.3 PLAN/APPLY after the V0.7.4 code rebase.

## Periodic health check

```powershell
python scripts\run_optimus_steady_state_v074.py check

python scripts\run_optimus_candidate_registry_v03.py status

python scripts\run_optimus_candidate_registry_v03.py maintenance-queue
```

Healthy baseline:

```text
seed_audit_debt       0
stale_canonical       0
DISCOVERY_ONLY        2
provisional evidence  2
```

The last two are intended shadow-pool state, not maintenance debt.

## When to reopen engineering

Only reopen the code when a real run reveals one of these:

```text
false positive / false negative
official-source transport change
dedupe/idempotence violation
explicitly approved Registry change
source-authority/temporal-contract change
safety invariant failure
```

Otherwise operate the frozen stack.

## Safety invariants

```text
research_only
Registry mutation requires separate PLAN + explicit APPLY approval
Canonicalization mutation = false in routine monitoring
Theme Stage mutation = false in routine monitoring
broker = false
orders = false
production_db = false
```

Real trades remain outside this research pipeline and require manual approval.
