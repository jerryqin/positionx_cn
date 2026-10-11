# PositionX Optimus Seed Debt Resolution V0.6 — Guarded APPLY

## Authorization

The user explicitly approved:

```text
批准 V0.6 APPLY
```

This package still fails closed unless the runtime command supplies both exact approved hashes.

## Frozen inputs

```text
Decision PLAN SHA256
2c164036dd7dd44ded7211bf6ec33e9e69628e9ca8aa1fe88cfa195144d5a9a5

Source Freeze manifest SHA256
5a26a75e93375cdd50d050c349450027edb01748393a6307354527201004adca

APPLY_PREVIEW file SHA256
cee5f13eaf01d86ba046c3d3e85d39f2655afc5d8ec309ec50ceca1f2b2fbdea

APPLY_PREVIEW payload SHA256
1f890d7ea1ed12cf49919b937b8d502f4be60be73644d925bdd7e5e93e43a007

PRECONDITION file SHA256
cb84976e642d0b700ebecac22e12e920a3f51ae6955825b02204d59d0990b3ec

PRECONDITION payload SHA256
573d37c5f501a0915c4097fc2dd0bfabf512c7e78e0cb30bd342aa10a5b00b02

V0.6 Canonicalization APPLY PLAN SHA256
f2c3ab4ed4e65eb2c3ba29d1f5effa920ee697e9eb5512f8fa91444612cf0313
```

## Frozen code dependencies

```text
Registry V0.3.1 script
3955c6deb314bee4bf6821ae9cc0d2f09c606d0fbd695f34877afa3d4b782884

Canonicalization V0.5.1 script
3e07d79ad34b5171618a59af50b3efda7b1836b1c9d697b76fcabbc63c988a1f

Evidence V0.4.4/V0.4.5 ops script
10e5a8288177c76e207fa264f74e18c3a878d95cf191585ca8a576643475a691
```

The package does **not** replace any of those frozen scripts.

## Transaction

One research-DB transaction performs:

1. stage the exact 7 frozen V0.4.4 bridge proposals;
2. stage 5 human-reviewed source-normalization evidence rows;
3. append review decisions;
4. create 6 Canonical Facts;
5. materialize 6 independent Canonical Evidence rows through the existing V0.5.1 materializer;
6. reconcile exactly:
   - 601100.SH 恒立液压
   - 603009.SH 北特科技
   - 603119.SH 浙江荣泰
   - 601689.SH 拓普集团
   - 600699.SH 均胜电子
7. validate the complete target **before COMMIT**.

Any failed target assertion rolls back the research-DB transaction.

## Intended post-state

```text
candidate_count                   22
ACTIVE_CANONICAL                  22
ACTIVE_SEED                        0
provisional_evidence               0
seed_audit_debt                    0
stale_canonical                    0
discovery_only                     0

Canonical Facts                   24
Canonical symbols                 22
Materialized Canonical Evidence   24

five resolved candidates:
ACTIVE_CANONICAL / POSSIBLE

Tesla / Optimus link:
T0 for all five

Theme Stage:
unchanged
```

## Install

```powershell
python .\install_optimus_seed_debt_v06_apply.py `
  --project-root D:\positionx_cn `
  --data-root D:\positionx_data
```

## Read-only check and plan

```powershell
cd D:\positionx_cn

python -m unittest tests.test_optimus_seed_debt_v06_apply -v

python scripts\run_optimus_seed_debt_v06_apply.py check

python scripts\run_optimus_seed_debt_v06_apply.py plan
```

Both must PASS before APPLY.

## Approved APPLY command

```powershell
python scripts\run_optimus_seed_debt_v06_apply.py apply `
  --approve-precondition-payload-sha256 573d37c5f501a0915c4097fc2dd0bfabf512c7e78e0cb30bd342aa10a5b00b02 `
  --approve-plan-sha256 f2c3ab4ed4e65eb2c3ba29d1f5effa920ee697e9eb5512f8fa91444612cf0313 `
  --actor Jerry
```

## Verify

```powershell
python scripts\run_optimus_seed_debt_v06_apply.py verify

python scripts\run_optimus_candidate_registry_v03.py status
python scripts\run_optimus_candidate_registry_v03.py maintenance-queue
python scripts\run_optimus_canonicalization_v05.py status
```

## Safety

```text
research-only mutation      YES
broker                      NO
orders                      NO
production DB               NO
physical source deletion    NO
Theme Stage mutation        NO
Tesla auto-mapping          NO
```
