# 雷赛 Canonical Fact V0.5.4a — T0 Zero Hotfix

## Root cause

The PLAN checker used:

```python
int(q.get("tesla_link_level") or -1)
```

For a legitimate T0 proposal:

```python
q["tesla_link_level"] == 0
```

Python treats `0` as false, so the expression became:

```python
int(-1)
```

and the checker incorrectly reported:

```text
TARGET_PROPOSAL_MISMATCH ['tesla_link_level']
```

This was a checker bug, not evidence drift.

## Fix

The checker now distinguishes:

```text
0     = valid T0
None  = missing / invalid
```

using an explicit `is not None` check.

## Safety

This hotfix changes only PLAN validation logic.

```text
Registry mutation          false
Canonicalization mutation  false
Theme Stage mutation       false
Config                     unchanged
```

## Validate

```powershell
cd D:\positionx_cn

python -m unittest tests.test_optimus_leisai_canonical_fact_v054a_t0_zero -v

python -m unittest tests.test_optimus_leisai_canonical_fact_v054_plan -v

python scripts\run_optimus_leisai_canonical_fact_v054_plan.py check

python scripts\run_optimus_leisai_canonical_fact_v054_plan.py plan

python scripts\run_optimus_leisai_canonical_fact_v054_plan.py status
```
