# Optimus Post-Leisai Closeout — V0.7.4.1 Rebase

## Verified real APPLY state

The approved LeiSai canonicalization completed successfully:

```text
PLAN SHA
748c1ca8206ea355c2b42252817ec407d23ceca22ef2903c182e500733bfe4dc

Batch
CB_7e2476e590e0bbe674ac8848

Fact
CF_fe5714e699b2e5c1a34c979c

CFE
CFE_ef6afb1d4f48e8166d81bfdb

Post Registry SHA
d51bbc34dc0731667cd7c2991b11cb954fd3747c9b1c9c6c6154f40cbbc9ccaa

Result manifest
e79e591e4e0db9d1efbd379c74a4891042d2d57b9b613660913d11ed335965ba
```

Post state:

```text
candidate_count              24
ACTIVE_CANONICAL             22
DISCOVERY_ONLY                2
evidence_count              163
review_decision_count       150
provisional_evidence          2
canonical facts              25
materialized CFE             25
```

## Read-only status bug

The APPLY itself passed, but `check` before APPLY and `status` after APPLY hit:

```text
Cannot execute CREATE ... database ... read-only mode
```

Cause: the inspection helper called `v05.init_tables()` while connected read-only.

The V0.5.4c hotfix makes schema inspection truly read-only and lets post-APPLY status
tolerate the expected steady-baseline SHA change.

## Baseline rebase

The old V0.7.4 baseline contains the pre-APPLY Registry SHA. It must be rebased before the
08:30 scheduler can pass its steady-state preflight.

The rebase changes only:

```text
config/research/optimus_steady_state_baseline_v074.json
```

It does not modify DuckDB, Registry, Canonicalization, Theme Stage, broker, or orders.

## Install

```powershell
python .\install_optimus_post_leisai_rebase_v0741.py `
  --project-root D:\positionx_cn
```

## Validate post-APPLY status

```powershell
cd D:\positionx_cn

python -m unittest tests.test_optimus_leisai_v054c_readonly_status -v
python -m unittest tests.test_optimus_steady_state_rebase_v0741 -v

python scripts\run_optimus_leisai_canonical_fact_v054_apply.py status
python scripts\run_optimus_leisai_canonical_fact_v054_apply.py check
```

`check` should now report that the approved plan is already applied rather than trying to
compare against the old pre-state.

## Rebase steady state

```powershell
python scripts\run_optimus_steady_state_rebase_v0741.py check

python scripts\run_optimus_steady_state_rebase_v0741.py apply

python scripts\run_optimus_steady_state_rebase_v0741.py status

python scripts\run_optimus_steady_state_v074.py check
```

The final command must again return:

```text
PASS_OPTIMUS_STEADY_STATE_V0_7_4_CHECK
```

The Windows 08:30 scheduled task itself does not need re-registration.
