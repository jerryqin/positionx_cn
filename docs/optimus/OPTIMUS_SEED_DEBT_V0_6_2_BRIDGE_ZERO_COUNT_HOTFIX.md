# V0.6.2 Bridge Zero-Count Guard Hotfix

## Root cause

`find_bridge()` used:

```python
int(m.get("parse_error_count") or -1)
int(m.get("local_pdf_read_error_count") or -1)
```

For a successful bridge manifest the values are exactly `0`. In Python, `0 or -1`
evaluates to `-1`, so every correct zero-error bridge was rejected and the script
ended with:

```text
EXACT_SEVEN_PROPOSAL_BRIDGE_NOT_FOUND
```

This hotfix changes only that guard to preserve legitimate zero values:

```python
int(m.get("parse_error_count", -1))
int(m.get("local_pdf_read_error_count", -1))
```

No APPLY plan, evidence, Registry, Canonicalization code, source freeze, approval
hash, or database content is changed.

## Exact replacement guard

Expected installed pre-hotfix script SHA256:

```text
713296e31c561d0d90bdd26719335e451ad2eee107eaa8b00cca848ec021b1c6
```

Patched script SHA256:

```text
956f9af8be15910d55d54d0bb9d186ca3d8ced8c032c430e9dbf21423461cbfd
```

The installer refuses any different existing script.

## After install

```powershell
cd D:\positionx_cn

python -m unittest tests.test_optimus_seed_debt_v06_2_bridge_zero_count -v
python -m unittest tests.test_optimus_seed_debt_v06_apply -v

python scripts\run_optimus_seed_debt_v06_apply.py check
python scripts\run_optimus_seed_debt_v06_apply.py plan
```

Do **not** run APPLY until both `check` and `plan` PASS.
