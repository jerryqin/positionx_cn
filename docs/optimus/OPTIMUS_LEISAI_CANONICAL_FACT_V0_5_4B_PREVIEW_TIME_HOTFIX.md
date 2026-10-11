# 雷赛 Canonical Fact V0.5.4b — PLAN Preview Timestamp Hotfix

## Root cause

V0.5.4 PLAN preview built a synthetic fact row using:

```text
canonicalized_at = PENDING_APPLY
```

The frozen V0.5.1 materializer maps that field to the materialized evidence `observed_at`.
The Registry validator correctly parses `observed_at` as a datetime, so the placeholder
caused:

```text
DateParseError: Unknown datetime string format: PENDING_APPLY
```

This is a PLAN-preview construction bug, not evidence drift and not a Registry issue.

## Fix

The preview now uses a valid current CST ISO-8601 timestamp:

```text
2026-10-10T...+08:00
```

This timestamp exists only to make the PLAN preview schema-valid.

It is **not** frozen as the future APPLY time. The frozen V0.5.1 engine sets the real
`canonicalized_at` again when APPLY actually runs.

## Safety

```text
PLAN config                 unchanged
Registry mutation           false
Canonicalization mutation   false
Theme Stage mutation        false
```

## Validate

```powershell
cd D:\positionx_cn

python -m unittest tests.test_optimus_leisai_canonical_fact_v054b_preview_time -v
python -m unittest tests.test_optimus_leisai_canonical_fact_v054a_t0_zero -v
python -m unittest tests.test_optimus_leisai_canonical_fact_v054_plan -v

python scripts\run_optimus_leisai_canonical_fact_v054_plan.py check
python scripts\run_optimus_leisai_canonical_fact_v054_plan.py plan
python scripts\run_optimus_leisai_canonical_fact_v054_plan.py status
```
