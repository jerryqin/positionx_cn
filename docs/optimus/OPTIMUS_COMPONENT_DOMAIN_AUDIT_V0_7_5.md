# Optimus Component-Domain Audit V0.7.5

Purpose: diagnose the LeiSai `component_domain` mismatch and determine whether the
frozen V0.4.4 first-match rule creates a broader systematic classification risk.

## Known target

```text
Source anchor
EACQ_5afda139f7d4dd9a1f33b7bb

Stored source domain
reducers / precision transmission

Materialized canonical evidence
CFE_ef6afb1d4f48e8166d81bfdb

Canonical domain
motion control core components / humanoid robotics
```

The audit does not reinterpret or mutate any Registry/Canonical state.

## What it tests

Frozen V0.4.4 uses an ordered `COMPONENT_MAP` and returns the first matching domain.
The audit reuses the exact frozen PDF parser and context-window function, then records
all component domains present in those same windows.

It flags:

```text
ORDER_SENSITIVE
  >=2 component domains are present in the same scoped evidence text.

FIRST_MATCH_MINORITY_RISK
  the stored/first-match domain appears in fewer context windows than another domain.

SOURCE_CANONICAL_DIVERGENCE
  source-anchor component domain differs from the final materialized canonical domain.
```

These are diagnostic flags, not automatic corrections.

## Scope limitation

The systemic scan covers V0.4.4 evidence rows for which the original local PDF remains
available. Rows without a local PDF are reported but not recomputed. OCR is not used.

## Install

```powershell
python .\install_optimus_component_domain_audit_v075.py `
  --project-root D:\positionx_cn
```

## Run

```powershell
cd D:\positionx_cn

python -m unittest tests.test_optimus_component_domain_audit_v075 -v

python scripts\run_optimus_component_domain_audit_v075.py check

python scripts\run_optimus_component_domain_audit_v075.py run

python scripts\run_optimus_component_domain_audit_v075.py status
```

The audit writes only under:

```text
D:\positionx_data\research\big_opportunity\optimus\component_domain_audit_v0_7_5
```

No Registry, Canonicalization, Theme Stage, broker, order, or production DB mutation occurs.
