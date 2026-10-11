# Optimus V0.4.5 First Scheduled Run Acceptance

Read-only acceptance for the first 08:30 Current-22 scheduled run after the
V0.4.5 future-only production upgrade.

It verifies:

```text
new V0.7.4.2 steady baseline is still frozen
scheduler wrapper is the known V0.7.4 wrapper
Evidence Acquisition check is V0.4.5
scheduled run is for the requested date
intake quality is PASS and query_errors is empty
cross-parser source-dedupe counters are present
all new proposals carry parser_version V0.4.5
AMBIGUOUS_MULTI_DOMAIN proposals are HUMAN_REVIEW_REQUIRED
no mutation flags are true
```

It does not write any file.

## Install

```powershell
python .\install_optimus_v045_first_scheduled_acceptance.py `
  --project-root D:\positionx_cn
```

## Inspect 2026-10-11

```powershell
cd D:\positionx_cn

python -m unittest tests.test_optimus_v045_first_scheduled_acceptance -v
python scripts\run_optimus_v045_first_scheduled_acceptance.py check
python scripts\run_optimus_v045_first_scheduled_acceptance.py inspect --date 2026-10-11
python scripts\run_optimus_current22_scheduled_v074.py attention
```

`WAITING_...` means today's scheduled log is not present yet. It is not a failure.
