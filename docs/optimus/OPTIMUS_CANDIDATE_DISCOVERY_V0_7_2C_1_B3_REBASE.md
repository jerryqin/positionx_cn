# V0.7.2C.1 Hotfix — Rebase Candidate Discovery on B.3

The observed failure is fail-closed behavior, not a discovery logic error:

```text
DEPENDENCY_SHA_MISMATCH v072b_script
actual   = d1309972b7c08a4d490634356b0e54c210929bf7644c2d4316e6e59983ec3268
expected = 4d96857b9f11cb759332b4e267514142bb39c82e6f79345c66e933b843fbc955
```

V0.7.2C was frozen against classifier B.2. The CN official discovery pilot then
correctly upgraded the classifier to B.3 Chinese/context-scoped support. Therefore C
must be explicitly rebased before it may continue.

This hotfix changes only these two dependency hashes:

```text
v072b_script
→ d1309972b7c08a4d490634356b0e54c210929bf7644c2d4316e6e59983ec3268

v072b_config
→ 4cf62921180cf5c35cf9f16870ced0c019b2b54e5fec90181c65edf8d80569e8
```

Candidate-discovery logic, authority, Registry, Canonicalization, and Theme Stage
contracts are unchanged.

## Install

```powershell
python .\install_optimus_candidate_discovery_v072c1_b3_rebase.py `
  --project-root D:\positionx_cn
```

The installer patches only the exact original V0.7.2C config:

```text
old candidate-discovery config:
5d0170ab6672c8a61e6799431449a211430907051bd503de7939e57f2a7ecdd3

new candidate-discovery config:
14084bce45e2a21a1674633fe4616712f26494dd06ee78461dab3b3d0c00d289
```

## Validate

```powershell
cd D:\positionx_cn

python -m unittest tests.test_optimus_candidate_discovery_v072c1_dependency_rebase -v
python -m unittest tests.test_optimus_candidate_discovery_v072c -v

python scripts\run_optimus_candidate_discovery_v072c.py check
```

Then resume the interrupted pilot at the exact failed step:

```powershell
python scripts\run_optimus_candidate_discovery_v072c.py prepare-leads `
  --input examples\discovery_pilot_v072c1_leads.json
```

Do not rerun prior V0.7.2C empty scan or Tesla pilot.
