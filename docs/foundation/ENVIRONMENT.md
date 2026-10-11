# PositionX-CN Environment Contract

This file records the environment facts required to reconstruct the current governed system.
It never records secret values.

## Python
- Supported baseline interpreter: `3.10.11`

## Direct requirements observed at P2A

| Requirement | Installed at audit |
|---|---|
| `pandas>=2.0` | `2.3.3` |
| `numpy>=1.24` | `1.26.4` |
| `PyYAML>=6.0` | `6.0.3` |
| `PySide6>=6.6` | `6.11.2` |
| `xtquant` | `250807.1.2` |

## Environment-variable names referenced in tracked source

- `IWAOS_DATA_ROOT`
- `POSITIONX_RET120_DEEP_P3`
- `POSITIONX_RET120_FAMILY_P1`
- `POSITIONX_SEC_CONTACT_EMAIL`
- `POSITIONX_STRATEGY2_OUT`
- `PYTHONIOENCODING`
- `PYTHONUTF8`
- `USERDOMAIN`
- `USERNAME`

Values belong in local machine configuration, secret storage, or scheduler/task configuration as appropriate; never in Git.

## Dependency lock status
- P2A `pip check`: return code `0`.
- P2A `pip freeze --all` entries: `65`.
- Safe freeze lines eligible for lock snapshot: `65`.
- Unsafe/local/VCS freeze lines requiring review: `0`.

If the guarded P2B plan classifies the freeze snapshot as safe, `requirements.lock.txt` may be added as a reconstruction snapshot. It does not replace the human-reviewed direct dependency contract in `requirements.txt`.


## Runtime dependency lock policy

`requirements.lock.txt` is generated from the recursive installed dependency closure of the five direct runtime requirements in `requirements.txt`, not from the entire virtual environment.

- Runtime dependency-closure packages pinned: `13`.
- Installed `pip freeze --all` entries outside that closure: `52`.
- AI-provider packages found outside the runtime dependency closure: `openai`.
- Those out-of-closure packages remain local environment artifacts and are **not** promoted into PositionX-CN project requirements.
- The full `pip freeze --all` snapshot remains P2A audit evidence under `D:\positionx_data`; it is not the runtime lock contract.
