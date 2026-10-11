# Optimus Seed Debt V0.6.3 — Nullable Event-Time / NaT Materialization Hotfix

## Root cause

The V0.6 plan intentionally leaves the Hengli IPA official product-page
`event_time` unknown (`None`). That is correct: the page proves application
relevance, but V0.6 must not invent an event date from the later source-freeze
observation time.

During V0.5.1 materialization, the source row is re-read through DuckDB
`fetchdf()`. SQL NULL `TIMESTAMPTZ` becomes `pandas.NaT`. The frozen Registry
V0.3.1 `normalize_ts()` serializes that sentinel as literal `"NaT"`.
DuckDB then rejects it when the materialized Canonical Evidence row is inserted:

```text
Conversion Error: invalid timestamp field format: "NaT"
```

The failed APPLY transaction rolls back on exception. V0.6.3 does not change the
decision PLAN or any evidence fact. It adds a V0.6-local materialization wrapper
that maps only missing/NaT timestamp sentinels back to Python `None` (SQL NULL).

It does **not** replace:
- Registry V0.3.1
- Canonicalization V0.5.1
- Evidence V0.4.4/V0.4.5

It does **not** fabricate `event_time = observed_at`.

## Exact script guard

Expected pre-hotfix V0.6.2 script SHA256:

```text
956f9af8be15910d55d54d0bb9d186ca3d8ced8c032c430e9dbf21423461cbfd
```

V0.6.3 script SHA256:

```text
dd37979a7ffcc171bd70525e7aa08c5e78bf31cc50e258a185365eae39b22af9
```

The installer refuses any other existing APPLY script.

## Re-validation

```powershell
cd D:\positionx_cn

python -m unittest tests.test_optimus_seed_debt_v06_3_nat_materialization -v
python -m unittest tests.test_optimus_seed_debt_v06_apply -v

python scripts\run_optimus_seed_debt_v06_apply.py check
python scripts\run_optimus_seed_debt_v06_apply.py plan
```

`check` is also the rollback confirmation: it requires the exact frozen pre-state
(17 ACTIVE_CANONICAL / 5 ACTIVE_SEED / 18 facts and the pre-APPLY Registry SHA).

Do not rerun APPLY unless both commands PASS.
