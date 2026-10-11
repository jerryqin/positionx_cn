# Optimus Component Domain V0.4.5 — Future-Only Production Patch

Explicitly approved scope:

```text
future-only V0.4.5
historical V0.4.4 remains frozen
no historical reclassification
ambiguous multi-domain -> HUMAN_REVIEW_REQUIRED
B.8 dedupe contract review included
downstream steady-state rebase included
```

Evaluation gate:

```text
full coverage                  50 / 50
old order-sensitive rows       27
old minority-risk rows          8
minority-risk still old         0
candidate changed rows         22
dominant automatic changes      4
ambiguous/human-review         18

evaluation manifest
e60422d69675d9dcf3a0631c0e5f65c160b9e7b3af927af962c28aa2f6e80666

evaluation rows
7f9bd284490141ce030d1ea7714c22d2b9d7f9f4b1d05af4c20f284cd92a4106
```

## Production policy

V0.4.5 still uses the existing V0.4.4 context windows and component vocabulary.

A different domain may replace the legacy first-match domain only when:

```text
window-count margin >= 2
AND top / second >= 1.5
```

Otherwise multi-domain material is:

```text
AMBIGUOUS_MULTI_DOMAIN_PENDING_REVIEW
```

and V0.7.1 sends it to:

```text
HUMAN_REVIEW_REQUIRED
```

## Future-only protection

Historical V0.4.4 evidence IDs are never rewritten.

V0.7.1 now performs cross-parser source dedupe using:

```text
symbol + source_sha256
fallback: symbol + source_ref
```

Therefore scheduler catch-up will not recreate an already-seen V0.4.4 source as a new
V0.4.5 proposal merely because parser_version changed.

The direct evidence-acquisition script also source-dedupes against Registry and refuses
historical backfill/reclassification commands under the V0.4.5 production policy.

## B.8 review

B.8 remains version `V0_7_2B_8`.

Result:

```text
PASS_NO_CAPTURE_IDENTITY_CHANGE
```

External capture dedupe is based on normalized `capture_identity`, not the CNINFO
component-domain parser version. Historical external captures therefore stay processed
and are not replayed by the parser upgrade. Historical replay still requires the
existing explicit reclassify path.

## Mutated runtime files

10 runtime code/config files are changed; Registry/Canonicalization data is not.

New steady baseline:

```text
OPTIMUS_STEADY_STATE_V0_7_4_2_COMPONENT_DOMAIN_V0_4_5_FREEZE_BASELINE
SHA256 7ed350965ac08df8398aa2972b31d875dc8576e6ece08d16f79f4ad3473ba343
```

## Install tooling/payload only

```powershell
python .\install_optimus_component_domain_v045_production_patch.py `
  --project-root D:\positionx_cn
```

## Validate and apply

```powershell
cd D:\positionx_cn

python -m unittest tests.test_optimus_component_domain_v045_production_patch -v

python scripts\run_optimus_component_domain_v045_production_patch.py check

python scripts\run_optimus_component_domain_v045_production_patch.py apply

python scripts\run_optimus_component_domain_v045_production_patch.py status
```

The APPLY backs up every changed runtime file, installs exact payload bytes, runs:

```text
evidence V0.4.5 check
V0.7.1 check
B.8 classifier check
C8 discovery check
full steady-state check
```

and restores all 10 preimages if any final validation fails.

No scheduler re-registration is required.
