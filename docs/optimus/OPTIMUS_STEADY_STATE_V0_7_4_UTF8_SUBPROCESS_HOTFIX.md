# V0.7.4 Steady-State UTF-8 Subprocess Hotfix

## Root cause

`run_optimus_steady_state_v074.py check` calls:

```text
run_optimus_candidate_registry_v03.py maintenance-queue
```

through `subprocess.run(..., capture_output=True)`.

On Windows, the child Python process can inherit GBK for stdout. The maintenance payload
contains the Unicode character:

```text
↔
```

in evidence interpretation text. The child therefore failed **while printing JSON**:

```text
UnicodeEncodeError: 'gbk' codec can't encode character '\u2194'
```

The Registry itself was healthy; the steady-state checker failed at stdout transport.

## Fix

The checker now launches all child Python commands with:

```text
PYTHONIOENCODING=utf-8
PYTHONUTF8=1
```

and decodes captured stdout/stderr explicitly as UTF-8.

This changes no monitoring, classification, Registry, Canonicalization, or Theme Stage
semantics.

## Frozen baseline

Baseline JSON remains byte-identical:

```text
97859e333c144bb0f5812bc5c67c54e6f5b809944df3217a7e2c718b3e1700f5
```

## Validate

```powershell
cd D:\positionx_cn

python -m unittest tests.test_optimus_steady_state_v074_utf8_subprocess -v

python -m unittest tests.test_optimus_steady_state_v074 -v

python scripts\run_optimus_steady_state_v074.py check
```

Expected:

```text
PASS_OPTIMUS_STEADY_STATE_V0_7_4_CHECK
```
