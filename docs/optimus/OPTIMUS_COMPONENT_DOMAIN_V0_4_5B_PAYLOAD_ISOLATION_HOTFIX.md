# Component Domain V0.4.5b — Payload Isolation Hotfix

This hotfix does **not** change the approved V0.4.5 production semantics or any runtime
payload bytes.

## Why the first installed test failed

The previous delivery package used the generic project directory:

```text
D:\positionx_cn\payload\...
```

That directory already contained payload files from the earlier V0.7.4.1a full rebase.
The installer installed the new tool/test files first, then encountered differing files
under the generic payload tree and refused them. The result was a mixed **delivery**
state: new V0.4.5 tests + stale V0.7.4.1a payload.

That exactly explains:

```text
B.8 payload missing component_domain_v0_4_5_review
steady baseline still freezing V0.7.1 script SHA 5b65733f...
```

No production runtime APPLY occurred.

## Fix

The approved payload is now installed under a unique namespace:

```text
D:\positionx_cn\patch_payload\optimus_component_domain_v045\...
```

The patch config points `payload_root` there, and tests resolve payload through the config.
The old generic `D:\positionx_cn\payload` tree is ignored and need not be deleted.

Runtime payload bytes are unchanged from the approved package.

## Install

From the extracted hotfix directory:

```powershell
python .\install_optimus_component_domain_v045b_payload_isolation.py `
  --project-root D:\positionx_cn
```

Then:

```powershell
cd D:\positionx_cn

python -m unittest tests.test_optimus_component_domain_v045_production_patch -v

python scripts\run_optimus_component_domain_v045_production_patch.py check
```

Only after both PASS, run the already-approved production APPLY:

```powershell
python scripts\run_optimus_component_domain_v045_production_patch.py apply
python scripts\run_optimus_component_domain_v045_production_patch.py status
```

No new approval is required because V0.4.5b only fixes package payload isolation.
