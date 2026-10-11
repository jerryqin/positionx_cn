# 雷赛智能 Canonical Fact V0.5.4 Guarded APPLY

Approved by the user for exactly:

```text
PLAN SHA
748c1ca8206ea355c2b42252817ec407d23ceca22ef2903c182e500733bfe4dc

Batch
CB_7e2476e590e0bbe674ac8848

Fact
CF_fe5714e699b2e5c1a34c979c

Materialized evidence
CFE_ef6afb1d4f48e8166d81bfdb
```

## Apply contract

The APPLY wrapper refuses any different PLAN SHA or changed dependency.

Before the real DB mutation it:

```text
1. validates frozen dependencies;
2. validates exact approved PLAN bytes;
3. re-finds the original EACQ proposal;
4. verifies current Registry pre-state;
5. copies the research DuckDB to a temporary file;
6. runs the complete transaction against the temporary DB;
7. requires exact post-state and materialization audit PASS;
8. creates a byte-for-byte backup of the real DuckDB;
9. executes the real transaction;
10. requires exact post-state and materialization audit PASS;
11. writes immutable APPLY artifacts and Registry snapshot.
```

On any real-apply failure the wrapper restores the DuckDB backup.

## Expected mutation

```text
Registry candidates         24 -> 24
ACTIVE_CANONICAL            22 -> 22
DISCOVERY_ONLY               2 -> 2

Evidence                   161 -> 163
Review decisions           149 -> 150
Provisional                  2 -> 2

Canonical Facts             24 -> 25
Materialized CFE            24 -> 25

002979.SZ lifecycle         ACTIVE_CANONICAL unchanged
002979.SZ F-status          POSSIBLE unchanged
002979.SZ max evidence      9 unchanged
002979.SZ max Tesla link    0 unchanged

Theme Stage                 unchanged
Tesla/Optimus supplier      NOT established
```

## Install

```powershell
python .\install_optimus_leisai_canonical_fact_v054_apply.py `
  --project-root D:\positionx_cn
```

## Run

First:

```powershell
cd D:\positionx_cn

python -m unittest tests.test_optimus_leisai_canonical_fact_v054_apply -v

python scripts\run_optimus_leisai_canonical_fact_v054_apply.py check

python scripts\run_optimus_leisai_canonical_fact_v054_apply.py simulate
```

Only if both PASS, execute the already-approved mutation:

```powershell
python scripts\run_optimus_leisai_canonical_fact_v054_apply.py apply `
  --approve-plan-sha256 748c1ca8206ea355c2b42252817ec407d23ceca22ef2903c182e500733bfe4dc
```

Then:

```powershell
python scripts\run_optimus_leisai_canonical_fact_v054_apply.py status
python scripts\run_optimus_candidate_registry_v03.py status
python scripts\run_optimus_canonicalization_v05.py status
```

Do **not** manually edit the current V0.7.4 steady-state baseline after APPLY. Its SHA check
is expected to fail after legitimate Registry state evolution. Generate a reviewed baseline
rebase from the actual post-APPLY Registry SHA.
