# Optimus Seed Debt Resolution V0.6 — Source Freeze + APPLY Preview

## Canonical location for the V0.6 PLAN

Do **not** place the machine-readable plan under the code repository.

Use:

```text
D:\positionx_data\research\big_opportunity\optimus\
plans\seed_debt_resolution_v0_6\20261009\
```

Files:

```text
OPTIMUS_SEED_DEBT_RESOLUTION_V0_6_PLAN.json
OPTIMUS_SEED_DEBT_RESOLUTION_V0_6_PLAN.md
SHA256SUMS.txt
```

Reason: this is a research decision artifact and future APPLY input, not source code.

## Phase A — Freeze authoritative external sources

The script freezes four sources:

1. Joyson official 2026-07-17 robot-controller commercialization page.
2. Joyson official 2026-08-25 robot-controller delivery page.
3. Hengli official IPA electric-cylinder product page.
4. Zhejiang Rongtai Investor Relations Activity Record 2026-003 from the Shanghai Stock Exchange Roadshow Center (`sns.sseinfo.com`).

Each frozen object is stored as raw bytes and SHA256 hashed. Required phrases are verified. No mirror fallback is used.

Output:

```text
D:\positionx_data\research\big_opportunity\optimus\
source_freeze_v0_6\<capture_id>\
```

## Phase B — APPLY preview

After a PASS source freeze:

```powershell
python scripts\run_optimus_seed_debt_v06.py preview-apply
```

Outputs:

```text
D:\positionx_data\research\big_opportunity\optimus\
seed_debt_resolution_v0_6\apply_preview\<capture_id>\
```

including:

- `APPLY_PREVIEW.json`
- `HUMAN_REVIEW_DECISIONS_PREVIEW.jsonl`
- `CANONICAL_FACTS_PREVIEW.jsonl`
- `LIFECYCLE_TRANSITIONS_PREVIEW.jsonl`

No Registry or DB is opened. This is deliberately one step before mutation.

## Safety boundary

```text
Registry open/write/reconcile   NO
Canonicalization mutation      NO
Evidence inbox write           NO
Broker/order                   NO
Production DB                  NO
```

After preview, upload `APPLY_PREVIEW.json`. The next step can then build a separate guarded APPLY command with a read-only precondition check and explicit user approval.
