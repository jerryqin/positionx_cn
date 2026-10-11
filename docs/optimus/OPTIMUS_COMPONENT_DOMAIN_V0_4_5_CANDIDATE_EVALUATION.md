# Component Domain V0.4.5 Candidate Evaluation

This package does **not** patch the production classifier.

The V0.7.5 audit established that the frozen V0.4.4 first-match rule is a systemic
order-sensitivity risk:

```text
50/50 rows recomputed
27/50 order-sensitive
8/50 first-match minority-risk
0 stored/recompute mismatches
```

The final zero is reproducibility, not correctness: V0.4.4 reproduces the same
first-match decision deterministically.

## Candidate V0.4.5 policy

Use the existing V0.4.4 `context_windows()` and `COMPONENT_MAP`, but score every domain.

Primary score:

```text
number of context windows containing that domain
```

Secondary diagnostic:

```text
keyword occurrence count
```

Decision:

```text
no domain      -> UNKNOWN_PENDING_REVIEW
one domain     -> that domain
old domain is unique window-count leader
               -> keep old domain
different unique leader with:
  window margin >= 2
  and ratio >= 1.5
               -> choose new dominant domain
all other multi-domain cases
               -> AMBIGUOUS_MULTI_DOMAIN_PENDING_REVIEW
```

This is deliberately conservative. It avoids replacing first-match bias with
"highest raw keyword count wins".

Historical evidence is not reclassified. If adopted, it is future-only under
`OPTIMUS_EVIDENCE_ACQUISITION_V0_4_5`.

## Run

```powershell
python .\install_optimus_component_domain_v045_candidate_eval.py `
  --project-root D:\positionx_cn

cd D:\positionx_cn

python -m unittest tests.test_optimus_component_domain_v045_candidate_eval -v
python scripts\run_optimus_component_domain_v045_candidate_eval.py check
python scripts\run_optimus_component_domain_v045_candidate_eval.py run
python scripts\run_optimus_component_domain_v045_candidate_eval.py status
```

Review `changed_rows` and `company_impact` before approving any production patch.
