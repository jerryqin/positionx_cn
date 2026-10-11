# Optimus 雷赛智能新证据 Canonical Fact V0.5.4 — PLAN ONLY

## Decision boundary

Target prospective proposal:

```text
EACQ_5afda139f7d4dd9a1f33b7bb
002979.SZ 雷赛智能
L6 / T0
EVIDENCE_PROPOSAL
```

Recommended human decision:

```text
APPROVE_CANONICAL_FACT
```

The source PDF itself must **not** become canonical strength.

Correct V0.5.1 architecture remains:

```text
Raw/Acquired Source Evidence
→ stage as PROVISIONAL
→ reviewed Fact
→ independent materialized Canonical Evidence
→ source anchor becomes REJECTED:
  SOURCE_ABSORBED_BY_CANONICAL_FACT_V0_5_1:<fact_id>
```

## Proposed Fact

```text
fact_key
LEISAI_2026_HUMANOID_MOTION_CONTROL_CORE_COMPONENT_CAPACITY_EXPANSION

canonical level
L6

Tesla link
T0
```

Fact meaning:

```text
雷赛披露智能装备运动控制核心零部件研发及产业化项目，
拟新建智能化产线并显著扩产，明确覆盖人形机器人等高端领域，
并规划逐步释放 40% → 70% → 100% 产能。

这证明机器人相关核心运动控制零部件的扩产/产业化建设。

这不证明：
Tesla/Optimus 客户关系、供应商身份、定点、订单或供货。
```

## Expected post-APPLY semantics

This package does not APPLY anything. A later guarded APPLY should preserve:

```text
Registry candidates       24
ACTIVE_CANONICAL          22
DISCOVERY_ONLY             2

002979.SZ
ACTIVE_CANONICAL / POSSIBLE
max evidence level remains 9
max Tesla link remains 0

Canonical Facts           24 → 25
Materialized fact evidence 24 → 25
Registry evidence          +2
Registry review decisions  +1
Theme Stage               unchanged
```

Because Registry content changes after a future APPLY, the current V0.7.4 steady-state
baseline will intentionally fail until a new baseline revision is generated.

## Install

```powershell
python .\install_optimus_leisai_canonical_fact_v054_plan.py `
  --project-root D:\positionx_cn
```

## Validate and generate PLAN

```powershell
cd D:\positionx_cn

python -m unittest tests.test_optimus_leisai_canonical_fact_v054_plan -v

python scripts\run_optimus_leisai_canonical_fact_v054_plan.py check

python scripts\run_optimus_leisai_canonical_fact_v054_plan.py plan

python scripts\run_optimus_leisai_canonical_fact_v054_plan.py status
```

No `apply` command exists in this package.
