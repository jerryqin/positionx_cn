# Optimus Post-Leisai Full Rebase V0.7.4.1a

The previous V0.7.4.1 rebase failed safely before mutation. This package fixes two issues:
Windows LF/CRLF byte mismatch and incomplete rebase scope.

It rebases the post-APPLY contract chain V0.7 → V0.7.1 → V0.7.2 → V0.7.2A/B → C8,
plus the top-level steady-state baseline. It does not mutate DuckDB.

Frozen post state:

```text
Registry SHA                 d51bbc34dc0731667cd7c2991b11cb954fd3747c9b1c9c6c6154f40cbbc9ccaa
candidate_count              24
ACTIVE_CANONICAL             22
DISCOVERY_ONLY                2
evidence_count              163
review_decision_count       150
provisional_evidence          2
canonical facts              25
materialized CFE             25
latest batch                 CB_7e2476e590e0bbe674ac8848
PLAN SHA                     748c1ca8206ea355c2b42252817ec407d23ceca22ef2903c182e500733bfe4dc
result manifest              e79e591e4e0db9d1efbd379c74a4891042d2d57b9b613660913d11ed335965ba
```

Install tooling only:

```powershell
python .\install_optimus_post_leisai_full_rebase_v0741a.py `
  --project-root D:\positionx_cn
```

Validate:

```powershell
cd D:\positionx_cn
python -m unittest tests.test_optimus_post_leisai_full_rebase_v0741a -v
python scripts\run_optimus_post_leisai_full_rebase_v0741a.py check
```

Expected before APPLY: `runtime_state = OLD`.

Apply:

```powershell
python scripts\run_optimus_post_leisai_full_rebase_v0741a.py apply
```

The APPLY backs up all 10 affected contract/baseline files, copies exact payload bytes
(no text-mode newline conversion), runs the full steady-state checker, and rolls all files
back if final validation fails.

Final verification:

```powershell
python scripts\run_optimus_post_leisai_full_rebase_v0741a.py status
python scripts\run_optimus_steady_state_v074.py check
python scripts\run_optimus_prospective_intake_v071.py check
python scripts\run_optimus_candidate_discovery_v072c.py check
```

The 08:30 Windows scheduled task does not need re-registration.
