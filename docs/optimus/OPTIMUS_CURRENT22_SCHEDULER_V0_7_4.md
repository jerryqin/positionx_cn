# PositionX Optimus Current-22 Scheduler V0.7.4

## Schedule

Recommended Windows Task Scheduler policy:

```text
Daily trigger              08:30 local time
Start when available       YES
Failure retry              every 30 minutes
Retry count                8
Network required           YES
Allow battery start        YES
Stop on battery            NO
Multiple instances         IgnoreNew
Execution time limit       2 hours
```

If the PC is shut down at 08:30, Windows cannot execute the task then. `StartWhenAvailable`
causes the missed daily trigger to run after the PC becomes available. The PositionX wrapper
also computes a dynamic catch-up window from the last successful `date_max`.

Examples:

```text
last success Sunday, run Monday:
--days 2

last success Friday, PC off weekend, run Monday:
--days 4

same-day failure retry:
--days 2
```

The wrapper caps automatic catch-up at 30 calendar days. A larger gap fails closed and
requires reviewed manual backfill.

## Failure retry vs human review

Task Scheduler retries only when the scheduled wrapper exits non-zero, for example:

```text
steady-state preflight failure
CNINFO query error
intake quality failure
process/transport failure
catch-up > 30 days
```

A valid `EVIDENCE_PROPOSAL` is **not** treated as task failure. The run succeeds, but
the scheduler persists it in:

```text
D:\positionx_data\research\big_opportunity\optimus\scheduled_current22_v0_7_4\attention_pending.json
```

A later empty/deduped scan does **not** clear this human-review obligation.

Inspect pending review work with:

```powershell
python scripts\run_optimus_current22_scheduled_v074.py attention
```

After the evidence has actually been reviewed and dispositioned, acknowledge that specific
item with:

```powershell
python scripts\run_optimus_current22_scheduled_v074.py ack-attention `
  --evidence-id <EVIDENCE_ID>
```

The scheduler therefore distinguishes transport/process retry from persistent human review.

## Logs

```text
D:\positionx_data\research\big_opportunity\optimus\scheduled_current22_v0_7_4\YYYYMMDD\...
D:\positionx_data\research\big_opportunity\optimus\scheduled_current22_v0_7_4\latest_scheduled.json
```

## Install files

```powershell
python .\install_optimus_current22_scheduler_v074.py `
  --project-root D:\positionx_cn
```

Validate:

```powershell
cd D:\positionx_cn
python -m unittest tests.test_optimus_current22_scheduler_v074 -v
python scripts\run_optimus_current22_scheduled_v074.py run
```

Register the Windows scheduled task:

```powershell
powershell -ExecutionPolicy Bypass -File `
  .\scripts\install_optimus_current22_task_v074.ps1
```

Inspect:

```powershell
Get-ScheduledTask -TaskName "PositionX Optimus Current22 0830" |
  Format-List TaskName,State
```

Test manually from Task Scheduler semantics:

```powershell
Start-ScheduledTask -TaskName "PositionX Optimus Current22 0830"
```

Then:

```powershell
python scripts\run_optimus_current22_scheduled_v074.py status
```

## Remove scheduled task

```powershell
powershell -ExecutionPolicy Bypass -File `
  .\scripts\uninstall_optimus_current22_task_v074.ps1
```

This scheduler never performs Registry APPLY, Canonicalization, Theme Stage mutation,
broker actions, or orders.
