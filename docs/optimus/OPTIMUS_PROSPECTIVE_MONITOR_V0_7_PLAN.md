# PositionX Optimus Prospective Monitor V0.7 — PLAN ONLY

V0.6 Seed Debt Resolution is finished and frozen. V0.7 changes the objective from
"finish the candidate registry" to "prospectively detect genuinely new evidence".

## Current frozen anchor

```text
Registry SHA:
da749d2c79b64695fddbbda575900d6e047be39e479e3a836eb73c0d1fbbe2ca

ACTIVE_CANONICAL = 22
ACTIVE_SEED = 0
seed_audit_debt = 0

Canonical Facts = 24
Materialized Canonical Evidence = 24

Latest Batch:
CB_86947f97badfc5934a257aa4

V0.6 Result Manifest:
da56ebb611f8c5320dc094f36c2a4d31fa7a80373eb1c84c8977337086e6315f
```

## V0.7 contract

V0.7 watches two different evidence layers and never conflates them:

1. **Theme Stage2 gate evidence**
   - Tesla / Optimus named supplier nomination / sourcing
   - named validation
   - named tooling / capacity commitment
   - named PO
   - named mass-production confirmation
   - build ramp / internal deployment / external deployment

2. **Supplier commercialization evidence**
   - small-batch delivery
   - batch / mass-production supply
   - customer development / validation
   - robot-component capacity build
   - order scheduling
   - robot-component shipment / revenue milestones

Supplier L9 alone **does not** move the Theme to Stage2.

## Outputs

The future V0.7.1 intake layer may only emit:

```text
NO_NEW_MATERIAL_EVIDENCE
EVIDENCE_PROPOSAL
HUMAN_REVIEW_REQUIRED
STAGE2_GATE_CANDIDATE
CONTRADICTION_ALERT
```

No automatic Canonicalization. No automatic candidate-state mutation. No automatic
Theme stage mutation.

## Install

```powershell
python .\install_optimus_prospective_monitor_v07_plan.py `
  --project-root D:\positionx_cn
```

## Validate

```powershell
cd D:\positionx_cn

python -m unittest tests.test_optimus_prospective_monitor_v07 -v

python scripts\run_optimus_prospective_monitor_v07.py check
python scripts\run_optimus_prospective_monitor_v07.py plan
python scripts\run_optimus_prospective_monitor_v07.py status
```

Expected:

```text
PASS_OPTIMUS_PROSPECTIVE_MONITOR_V0_7_BASELINE_CHECK
PASS_OPTIMUS_PROSPECTIVE_MONITOR_V0_7_PLAN
ARMED_OPTIMUS_PROSPECTIVE_MONITOR_V0_7_PLAN_ONLY
```

## Next implementation phase

After this contract is frozen, V0.7.1 will add a **read-only daily intake and proposal
queue**. It will not touch Registry or Canonicalization tables. Human review remains
the gate before any later PLAN/APPLY cycle.
