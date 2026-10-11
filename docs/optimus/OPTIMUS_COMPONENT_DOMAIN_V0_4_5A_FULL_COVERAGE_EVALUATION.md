# Component Domain V0.4.5a — Full Coverage Candidate Evaluation

The first V0.4.5 candidate replay was **not valid as a systemic evaluation** because it
recomputed only 1 of the 50 rows. The V0.7.5 audit had already proven that all 50 rows had
local PDFs and were recomputable, so the 1/50 result was an evaluator coverage bug.

Root cause: the first candidate evaluator did not restore `acquisition` / local PDF metadata
from Registry `raw_payload_json`; only the currently queued LeiSai proposal retained that
metadata in the proposal queue.

V0.4.5a avoids this failure mode entirely. It replays the candidate policy over the exact,
already-frozen V0.7.5 audit asset:

```text
audit manifest payload
789c79c1e505b6d9e68d5cac3dfff6fb56f558bfd5fa9e7263edc7447dc080c0

audit rows SHA256
da7e49b69df9a0ceb2facc8dd27fba8d8e47eefccd7cac67cd615d74d09a4ad8

coverage
50 / 50
```

No PDF is reparsed and no DB is opened.

## Run

```powershell
python .\install_optimus_component_domain_v045a_full_coverage_eval.py `
  --project-root D:\positionx_cn

cd D:\positionx_cn

python -m unittest tests.test_optimus_component_domain_v045a_full_coverage_eval -v
python scripts\run_optimus_component_domain_v045a_full_coverage_eval.py check
python scripts\run_optimus_component_domain_v045a_full_coverage_eval.py run
python scripts\run_optimus_component_domain_v045a_full_coverage_eval.py status
```

Do not approve a production V0.4.5 patch until this full-coverage replay has been reviewed.
