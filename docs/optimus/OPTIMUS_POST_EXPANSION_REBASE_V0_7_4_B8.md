# PositionX Optimus V0.7.4 — Post-Expansion Rebase + B.8 Dedupe

## Frozen post-V0.7.3 Registry baseline

```text
Registry SHA
975ab7ff3d8f9d5b644defcd8f4a3479969f1b4c7b08b3cf0b7eac23f3ef69f4

candidate_count        24
ACTIVE_CANONICAL       22
DISCOVERY_ONLY          2
ACTIVE_SEED             0
provisional_evidence    2
canonical_candidates   22
```

The two DISCOVERY_ONLY symbols are:

```text
603416.SH 信捷电气
603809.SH 豪能股份
```

## Monitoring partition

The original 22-symbol canonical monitoring universe stays frozen.

```text
V0.7.1 current CNINFO lane:
22 ACTIVE_CANONICAL only

603416 / 603809:
NOT silently added to current-22 lane
NOT eligible for NEW_CANDIDATE_DISCOVERY again
remain Registry DISCOVERY_ONLY shadow candidates
```

This separates:

```text
Registry membership = 24
canonical monitor universe = 22
discovery-only pool = 2
```

## B.8 dedupe hardening

The old routine dedupe scanned `classification_queue.jsonl` only.

That was insufficient because:

```text
NO_NEW_MATERIAL_EVIDENCE
→ successful classification
→ absent from queue
→ same capture could be classified again
```

B.8 now writes:

```text
processed_capture_identities.jsonl
```

for **every successful classification**, including NO_NEW and HISTORICAL.

For migration, routine dedupe also reads legacy `audit.jsonl` rows whose status is
`CLASSIFIED`.

It deliberately does **not** mark these as processed:

```text
ERROR
WAITING_MANUAL_CAPTURE
```

`reclassify-latest` remains the explicit policy-change escape hatch and bypasses routine
dedupe.

## C.7 existing-Registry exclusion

NEW_CANDIDATE_DISCOVERY now excludes:

```text
the frozen ACTIVE_CANONICAL 22
+
603416.SH / 603809.SH DISCOVERY_ONLY
```

so an already-admitted Registry candidate cannot be rediscovered as a new candidate.

## Historical V0.7.3

V0.7.3 PLAN/APPLY is frozen history. After installing this rebase, do **not** rerun its
`check` or `apply` because operational dependency hashes have legitimately advanced.
Its stored APPLY result and Registry audit history remain authoritative.

## Install

```powershell
python .\install_optimus_post_expansion_rebase_v074_b8.py `
  --project-root D:\positionx_cn
```

## Validate

```powershell
cd D:\positionx_cn

python -m unittest tests.test_optimus_post_expansion_rebase_v074_b8 -v

python -m unittest tests.test_optimus_tesla_robot_binding_v072b7 -v
python -m unittest tests.test_optimus_candidate_discovery_v072c -v

python scripts\run_optimus_prospective_monitor_v07.py check
python scripts\run_optimus_prospective_intake_v071.py check
python scripts\run_optimus_global_discovery_monitor_v072.py check
python scripts\run_optimus_external_inbox_v072a.py check
python scripts\run_optimus_external_classifier_v072b.py check
python scripts\run_optimus_candidate_discovery_v072c.py check
```

After all checks pass, run a **controlled dedupe probe** before scheduling:

1. Use one already-successfully-classified exact capture through the same routine path.
2. Expect `skipped_already_classified = 1`.
3. Confirm no new proposal / Registry / canonicalization / Theme Stage mutation.

Do not broaden the scheduler until this real dedupe probe passes.
