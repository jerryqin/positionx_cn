# PositionX Doctor V0.1

`PositionX Doctor` is the global, read-only health entrypoint for PositionX-CN.

Primary command:

```powershell
python scripts\run_positionx_doctor.py check `
  --project-root D:\positionx_cn `
  --data-root D:\positionx_data
```

## Scope

V0.1 covers the foundation and all 15 P3 domains. It deliberately distinguishes
**health evidence** from **operational action**.

It checks Git, Python/runtime lock, settings, data root/local assets, xtquant discoverability,
broker/reconcile source surfaces, Windows Task Scheduler, subsystem source/data evidence,
execution safety flags, and recovery/governance prerequisites.

## Safety contract

Doctor V0.1 does **not** execute PositionX operational scripts, connect to or mutate the broker,
submit/cancel orders, mutate production databases, install/change scheduled tasks, or write
project/data files.

The broker domain is therefore deliberately WARN in V0.1 when broker code exists: live read-only
broker connectivity has not yet been proven safe as a standalone probe.

## Status

`HEALTHY` means no blockers/warnings, `DEGRADED` means warnings without blockers, and `BLOCKED`
means at least one blocking failure. Exit code is 0 for HEALTHY/DEGRADED and nonzero for BLOCKED.

`--mode operational` is default and requires xtquant to be import-discoverable.
`--mode foundation` allows a foundation check on a non-QMT machine.
