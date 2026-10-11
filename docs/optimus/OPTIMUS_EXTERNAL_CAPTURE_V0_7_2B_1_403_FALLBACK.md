# V0.7.2B.1 Hotfix — Tesla HTTP 403 Manual-Capture Fallback

Observed real pilot failure:

```text
HTTPError 403 Forbidden
source = https://www.tesla.com/AI
classified_count = 0
```

The empty queue from that run is **not** evidence that Tesla published nothing; the
record never reached the classifier.

This hotfix does not change classification, no-hindsight, Registry, Canonicalization,
or Theme-stage contracts.

## Behavioral change

For:

```text
SRC_TESLA_CORPORATE_OFFICIAL
```

direct HTTP still runs first. If the registered official source returns HTTP 403:

```text
HTTP 403
→ WAITING_MANUAL_CAPTURE
→ manual_capture_request.json
→ no classifier result
→ no false NO_NEW_MATERIAL_EVIDENCE
```

No attempt is made to bypass Tesla's access controls.

## Manual fallback

Open the exact official URL in your normal browser and save the page as **HTML only**,
for example:

```text
D:\positionx_data\incoming\tesla_ai_20261010.html
```

Do not edit the saved file.

Then run:

```powershell
python scripts\run_optimus_external_classifier_v072b.py manual-capture `
  --manifest examples\tesla_official_controlled_pilot_20261010.json `
  --raw-file D:\positionx_data\incoming\tesla_ai_20261010.html
```

The raw bytes still pass through V0.7.2A Source Registry + immutable External Inbox
before classification.

`event_time` remains NULL unless independently supplied by the approved manifest.
It is never backfilled from the browser save time or `observed_at`.

## Install

The installer only replaces the exact original V0.7.2B script/config hashes:

```text
old script:
ab8fe502acc1d2d24186af4c5b614e99f3bc7eda207f07abb631dbf204c352b1

old config:
c4566c2ce36ea7fff79299f300f20708001b40195ce971ca89be2828b7c17a78
```

Unexpected local modifications fail closed.

```powershell
python .\install_optimus_external_classifier_v072b1_403_hotfix.py `
  --project-root D:\positionx_cn
```

## Validate

```powershell
cd D:\positionx_cn

python -m unittest tests.test_optimus_external_classifier_v072b1_403_fallback -v
python -m unittest tests.test_optimus_external_classifier_v072b -v

python scripts\run_optimus_external_classifier_v072b.py check
python scripts\run_optimus_external_classifier_v072b.py plan
```

Then perform the manual capture command above and run:

```powershell
python scripts\run_optimus_external_classifier_v072b.py status
python scripts\run_optimus_external_classifier_v072b.py queue
```
