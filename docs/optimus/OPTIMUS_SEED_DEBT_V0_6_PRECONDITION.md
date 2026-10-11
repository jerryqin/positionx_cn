# Optimus Seed Debt V0.6 — Read-only Precondition

Uploaded APPLY_PREVIEW validation target:

```text
status = READY_FOR_USER_APPROVAL
plan_sha256 = 2c164036dd7dd44ded7211bf6ec33e9e69628e9ca8aa1fe88cfa195144d5a9a5
source_freeze_manifest_sha256 = 5a26a75e93375cdd50d050c349450027edb01748393a6307354527201004adca
preview_payload_sha256 = 1f890d7ea1ed12cf49919b937b8d502f4be60be73644d925bdd7e5e93e43a007
```

This package does **not** APPLY anything.

It invokes only:

```powershell
python scripts\run_optimus_candidate_registry_v03.py status
python scripts\run_optimus_candidate_registry_v03.py maintenance-queue
python scripts\run_optimus_canonicalization_v05.py check
python scripts\run_optimus_canonicalization_v05.py status
```

Expected frozen pre-APPLY state:

```text
candidate_count       = 22
ACTIVE_CANONICAL      = 17
ACTIVE_SEED           = 5
provisional_evidence  = 0
seed_audit_debt       = 5
stale_canonical       = 0
discovery_only        = 0
registry data SHA     = 8e52129b22a2791908d63f4566c2b90651f42f311176238c9dc04b1a390bb6cd
latest batch          = CB_41b0746b2bdb0a3658e01f70
latest result SHA     = 46818ccec9df9c62e98d54cd26557ac6bb1694ef0ab791299a03a61b7640bf3f
```

Exact Seed Debt set:

```text
601100.SH 恒立液压
603009.SH 北特科技
603119.SH 浙江荣泰
601689.SH 拓普集团
600699.SH 均胜电子
```

## Install

```powershell
python .\install_optimus_seed_debt_v06_precondition.py --project-root D:\positionx_cn
```

## Test

```powershell
cd D:\positionx_cn
python -m unittest tests.test_optimus_seed_debt_v06_precondition -v
```

## Run

```powershell
python scripts\run_optimus_seed_debt_v06_precondition.py
```

Output:

```text
D:\positionx_data\research\big_opportunity\optimus\
seed_debt_resolution_v0_6\precondition\<capture_id>\PRECONDITION.json
```

Only `PASS_OPTIMUS_SEED_DEBT_V0_6_PRECONDITION` advances to the explicit user-approval gate.

No Registry/Canonicalization mutation occurs in this step.
