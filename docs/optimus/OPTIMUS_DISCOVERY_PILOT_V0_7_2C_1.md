# PositionX Optimus V0.7.2C.1 — Chinese Official Discovery Pilot

## Why B.3 is required

V0.7.2B.2 fixed context scope, but its milestone vocabulary was English-first. A real
A-share discovery pilot would otherwise miss Chinese official phrases such as:

```text
人形机器人
规模化批量供货
小批量供货
批量供应
新产品验证
产能建设
量产爬坡
```

V0.7.2B.3 adds conservative Chinese milestone/context patterns **without changing
authority or mutation rules**.

## Pilot names

All three are outside the frozen 22:

```text
603416.SH 信捷电气
603809.SH 豪能股份
600353.SH 旭光电子
```

Expected evidence character:

```text
信捷电气
2026-09-16 official SSE roadshow
humanoid-joint parts + scaled batch supply
→ historical discovery evidence

豪能股份
2025-09-13 official SSE PDF
humanoid/robot reducer applications + some batch supply
→ historical discovery evidence

旭光电子
2026-10-08 official SSE roadshow
humanoid-robot controller product validation
→ prospective precommercial discovery evidence
```

None of these facts by themselves confirm Tesla/Optimus supply.

## Install B.3 + pilot assets

```powershell
python .\install_optimus_discovery_pilot_v072c1.py `
  --project-root D:\positionx_cn
```

## Regression

```powershell
cd D:\positionx_cn

python -m unittest tests.test_optimus_external_classifier_v072b3_chinese -v
python -m unittest tests.test_optimus_external_classifier_v072b2_context_scope -v
python -m unittest tests.test_optimus_external_classifier_v072b1_403_fallback -v
python -m unittest tests.test_optimus_external_classifier_v072b -v
```

## Step 1 — recall queue

```powershell
python scripts\run_optimus_candidate_discovery_v072c.py prepare-leads `
  --input examples\discovery_pilot_v072c1_leads.json
```

Expected:

```text
accepted_lead_count = 3
current_22_skipped = 0
```

## Step 2 — exact official capture + classifier

```powershell
python scripts\run_optimus_external_classifier_v072b.py run-manifest `
  --manifest examples\discovery_pilot_v072c1_capture_manifest.json
```

Because the first two sources predate 2026-10-08, they must not become prospective
Theme triggers. The third is prospective but is only company-specific validation.

## Step 3 — candidate discovery scan

```powershell
python scripts\run_optimus_candidate_discovery_v072c.py scan-inbox

python scripts\run_optimus_candidate_discovery_v072c.py status
python scripts\run_optimus_candidate_discovery_v072c.py queue
```

A valid result may contain `DISCOVERY_PROPOSAL` items. No item may auto-enter Registry.

## Fail-closed note

If any official source returns HTTP/network failure, stop and inspect the audit.
Do not interpret a missing capture as "no evidence".
