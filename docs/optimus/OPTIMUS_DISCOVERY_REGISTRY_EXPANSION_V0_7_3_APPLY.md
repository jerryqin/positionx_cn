# PositionX Optimus V0.7.3 — Discovery Registry Expansion APPLY

This package was generated only after explicit approval:

```text
批准 V0.7.3 APPLY：603416.SH + 603809.SH，
均以 DISCOVERY_ONLY / SPECULATIVE / tesla_link_level=0 加入 Registry。
```

## Exact approved input

```text
PLAN payload SHA
cf6dad84cb60e67d3e27962b5b8dcf80da0ae862b561abb25c148143d0d5142e

evidence JSONL SHA
72c351f96216b69d2161e24d376a834c35fcac1a1eef5d1cb4be37184686a91f

human review SHA
856c335676669507a0ec2f3b0c2460d6c8ac7029847ba22cf3c2a6459d06b27a

Registry precondition SHA
da749d2c79b64695fddbbda575900d6e047be39e479e3a836eb73c0d1fbbe2ca
```

Only:

```text
603416.SH
603809.SH
```

may be added.

## Apply semantics

Each row is ingested as:

```text
review_state     = PROVISIONAL
evidence_level   = 1
evidence_type    = APPROVED_DISCOVERY_ONLY
tesla_link_level = 0
```

Reconciliation must produce:

```text
DISCOVERY_ONLY / SPECULATIVE
canonical = false
needs_review = true
```

No canonical review decision is issued.

## Guard sequence

The APPLY wrapper performs:

```text
1. Verify exact code/config hashes.
2. Re-run V0.7.3 PLAN check.
3. Verify approved PLAN/evidence/human-review hashes.
4. Verify Registry exact precondition SHA and counts.
5. Clone current DuckDB.
6. Apply to the clone first.
7. Verify all postconditions on the clone.
8. Back up the real DuckDB.
9. Apply to the real Registry.
10. Verify real postconditions.
11. Require real Registry digest == simulation digest.
12. Restore DB backup on caught postcondition/apply failure.
```

The backup is retained in the APPLY output directory even after success.

## Expected post state

```text
candidate_count        24
ACTIVE_CANONICAL       22
DISCOVERY_ONLY          2
ACTIVE_SEED             0
canonical_candidates   22

603416.SH  DISCOVERY_ONLY / SPECULATIVE / Tesla link 0
603809.SH  DISCOVERY_ONLY / SPECULATIVE / Tesla link 0
```

## Install and validate

```powershell
python .\install_optimus_discovery_registry_expansion_v073_apply.py `
  --project-root D:\positionx_cn

cd D:\positionx_cn

python -m unittest tests.test_optimus_discovery_registry_expansion_v073_apply -v

python scripts\run_optimus_discovery_registry_expansion_v073_apply.py check
```

Only if `check` is PASS, run:

```powershell
python scripts\run_optimus_discovery_registry_expansion_v073_apply.py apply `
  --approval-token V0.7.3_603416_603809_APPROVED
```

Then verify:

```powershell
python scripts\run_optimus_discovery_registry_expansion_v073_apply.py status

python scripts\run_optimus_candidate_registry_v03.py status

python scripts\run_optimus_candidate_registry_v03.py maintenance-queue
```
