# PositionX Optimus Prospective Intake V0.7.1

## Purpose

V0.7.1 implements the first operational lane after V0.6 Seed Debt closure:

```text
CNINFO official daily intake
→ frozen 22-symbol filter
→ V0.4.4 full-text milestone classifier
→ dedupe against Registry + prior monitor queue
→ proposal_queue.jsonl
→ HUMAN REVIEW
```

It deliberately **does not** ingest `evidence_provisional.jsonl` into Registry.

## Scope in V0.7.1

Only the frozen 22 `ACTIVE_CANONICAL` A-share candidates are monitored in this lane.
It uses **all CNINFO announcements per date**, then filters locally by symbol. This
avoids the old blank-keyword/keyword recall mistake for generic-titled periodic
reports.

V0.7.1 does **not yet** implement:
- new-candidate discovery outside the 22;
- Tesla official / US regulatory / global supplier web intake;
- automatic Theme stage changes.

Those belong in a later lane after this daily proposal pipeline is proven stable.

## Queue semantics

```text
HARD contradiction              -> CONTRADICTION_ALERT
named Tesla/Optimus T3/T4       -> STAGE2_GATE_CANDIDATE
unnamed-head T2                  -> HUMAN_REVIEW_REQUIRED
other new material evidence     -> EVIDENCE_PROPOSAL
known Registry evidence         -> skip
already queued monitor evidence -> skip
```

`STAGE2_GATE_CANDIDATE` means **review candidate only**. It never mutates Theme Stage.

## Frozen dependencies

```text
V0.7 script:
c1687cb93f2b2096daf421ea5234573224e558651f440b52cb482288b0daacf0

V0.7 config:
c928e4d0b32645279ec6037ce335565ec63ea964024999e7a860717ff3872659

Evidence V0.4.4 source:
10e5a8288177c76e207fa264f74e18c3a878d95cf191585ca8a576643475a691

Registry V0.3.1:
3955c6deb314bee4bf6821ae9cc0d2f09c606d0fbd695f34877afa3d4b782884
```

## Install

```powershell
python .\install_optimus_prospective_intake_v071.py `
  --project-root D:\positionx_cn
```

## Validate

```powershell
cd D:\positionx_cn

python -m unittest tests.test_optimus_prospective_intake_v071 -v

python scripts\run_optimus_prospective_intake_v071.py check
python scripts\run_optimus_prospective_intake_v071.py status
```

Before the first daily capture, `status` should say `...NO_RUNS`.

## First controlled capture

For a deterministic one-day pilot:

```powershell
python scripts\run_optimus_prospective_intake_v071.py run-date --date 2026-10-10
```

Then:

```powershell
python scripts\run_optimus_prospective_intake_v071.py status
python scripts\run_optimus_prospective_intake_v071.py queue
```

If CNINFO has no relevant announcements, `proposal_count = 0` is a valid PASS.

## Normal daily use

```powershell
python scripts\run_optimus_prospective_intake_v071.py daily --days 1
```

Use `--days 3` after weekends/holidays when desired.

## Safety

Every run writes only immutable research artifacts under:

```text
D:\positionx_data\research\big_opportunity\optimus\prospective_monitor_v0_7_1
```

It does not write Evidence Inbox, Registry, Canonicalization, broker, order, or
production DB state.
