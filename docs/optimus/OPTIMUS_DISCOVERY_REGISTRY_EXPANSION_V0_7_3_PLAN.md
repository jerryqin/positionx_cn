# PositionX Optimus V0.7.3 — Discovery → Registry Expansion PLAN

## Human-review recommendation

The validated V0.7.2C queue contains exactly two discovery proposals:

```text
603416.SH 信捷电气
→ recommend APPROVE_DISCOVERY_ONLY
→ strength: STRONG_DISCOVERY

603809.SH 豪能股份
→ recommend APPROVE_DISCOVERY_ONLY
→ strength: MEDIUM_DISCOVERY
```

Both are historical official-source discoveries. Both have:

```text
tesla_robot_link_eligible = false
```

Therefore this plan fixes Registry Tesla link at:

```text
tesla_link_level = 0
```

No Tesla T3/T4, no Theme Stage change.

600353.SH 旭光电子 remains:

```text
WAITING_CAPTURE_REPAIR
registry_action = NONE
```

## Why Registry evidence is deliberately Level 1 / PROVISIONAL

This bridge is for **candidate admission**, not canonical evidence promotion.

The two rows are planned as:

```text
review_state     = PROVISIONAL
evidence_level   = 1
evidence_type    = APPROVED_DISCOVERY_ONLY
tesla_link_level = 0
```

After a later APPLY, Registry V0.3 reconciliation should therefore produce:

```text
lifecycle_state = DISCOVERY_ONLY
f_status        = SPECULATIVE
canonical       = false
needs_review    = true
```

A separate future evidence review/canonicalization decision is required before either
candidate can become ACTIVE_CANONICAL.

## Expected post-APPLY preview

```text
candidate_count       22 → 24
ACTIVE_CANONICAL      22 → 22
DISCOVERY_ONLY         0 → 2
ACTIVE_SEED            0 → 0
canonical candidates  22 → 22
```

## This package is PLAN-ONLY

It contains no `apply` command and does not mutate Registry.

After installing:

```powershell
cd D:\positionx_cn

python -m unittest tests.test_optimus_discovery_registry_expansion_v073 -v

python scripts\run_optimus_discovery_registry_expansion_v073.py check

python scripts\run_optimus_discovery_registry_expansion_v073.py plan

python scripts\run_optimus_discovery_registry_expansion_v073.py status
```

Review the plan output. An APPLY package should only be generated after explicit human approval.
