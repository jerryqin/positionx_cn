# PositionX Optimus Global Discovery Monitor V0.7.2 — PLAN ONLY

V0.7.1 first controlled capture is clean:

```text
date = 2026-10-10
announcement_count = 1
proposal_count = 0
scanned_not_relevant = 1
query_errors = 0
theme_stage_change = false
Registry / Canonicalization mutation = false
```

This validates the CNINFO daily intake lane itself. V0.7.2 therefore expands **coverage**,
not authority.

## Why V0.7.2 is separate

V0.7.1 answers:

> What new official evidence appeared for the current frozen 22 A-share names?

V0.7.2 must answer two different questions:

> Did Tesla/Optimus itself publish evidence that can move Theme Stage1 toward Stage2?

and

> Are there important suppliers outside the current 22 that deserve discovery review?

Those cannot be safely solved by adding more keywords to V0.7.1.

## Three lanes

### 1. THEME_OFFICIAL

Highest-value lane. Sources are Tesla official / regulatory / named-executive official.
Search engines may discover URLs, but search snippets are **recall only**, never the fact.

A hit becomes only:

```text
STAGE2_GATE_CANDIDATE
```

Human review and a separate Theme Stage PLAN are still required.

### 2. SUPPLIER_GLOBAL_OFFICIAL

Official company/IR/exchange evidence outside CNINFO, including HKEX/SEC/overseas filings.

Outputs remain proposal-only. No automatic Registry or Canonicalization write.

### 3. NEW_CANDIDATE_DISCOVERY

Companies outside the current 22 may enter:

```text
DISCOVERY_PROPOSAL
→ HUMAN_REVIEW_REQUIRED
→ APPROVED_DISCOVERY_ONLY
→ SEPARATE_REGISTRY_PLAN_REQUIRED
```

They never jump directly to `ACTIVE_CANONICAL`.

## Critical anti-hype rules

The following remain insufficient:

```text
share-price strength
broker supply-chain diagram
media mapping without primary evidence
generic robot products
policy support
T2 unnamed head-customer clues
```

`T2 != Tesla`.

## Implementation sequence

```text
V0.7.2A  Source registry + normalized external inbox
V0.7.2B  Theme official + supplier global read-only capture
V0.7.2C  New-candidate discovery queue
```

All three are read-only research lanes.

## Install

```powershell
python .\install_optimus_global_discovery_monitor_v072_plan.py `
  --project-root D:\positionx_cn
```

## Validate

```powershell
cd D:\positionx_cn

python -m unittest tests.test_optimus_global_discovery_monitor_v072 -v

python scripts\run_optimus_global_discovery_monitor_v072.py check
python scripts\run_optimus_global_discovery_monitor_v072.py plan
python scripts\run_optimus_global_discovery_monitor_v072.py status
```

Expected:

```text
PASS_OPTIMUS_GLOBAL_DISCOVERY_MONITOR_V0_7_2_CHECK
PASS_OPTIMUS_GLOBAL_DISCOVERY_MONITOR_V0_7_2_PLAN
ARMED_OPTIMUS_GLOBAL_DISCOVERY_MONITOR_V0_7_2_PLAN_ONLY
```

After these pass, implement **V0.7.2A Source Registry + Normalized External Inbox**.
