# Optimus Candidate Registry V0.3

## Purpose
Replace the fixed 22-name CSV as the source of truth with an evidence-driven, append-only lifecycle.

**The candidate count is endogenous. There is no target of 12, 22, 32, or any other fixed number.**

## Layers
1. **Discovery Pool** — automated CNINFO full-text hits; provisional only.
2. **Evidence Registry** — immutable structured evidence rows.
3. **Review Decisions** — append-only CANONICAL / REJECTED decisions; AI/parser cannot silently rewrite evidence.
4. **Candidate Registry** — current materialized state derived from evidence.
5. **State History** — every lifecycle/F-status transition is appended; no physical deletion.
6. **M-Evidence V0.3** — reads active candidates from the registry instead of a static CSV.

## Candidate lifecycle
- `DISCOVERY_ONLY`: found, but no canonical admission evidence yet.
- `ACTIVE_SEED`: V0.2 bootstrap name; retained for continuity, explicitly non-canonical and needs evidence review.
- `ACTIVE_CANONICAL`: admitted by canonical evidence + deterministic rules.
- `REMOVED`: canonical hard contradiction/removal; history retained.

## F-status rule
- `SPECULATIVE`: component-relevant but commercialization below L2.
- `POSSIBLE`: canonical evidence >= L2.
- `PROBABLE`: canonical evidence >= L4 AND Tesla-link level >= 3.
- `CONFIRMED`: canonical primary-source evidence >= L7 AND Tesla-link level >= 4.
- `REMOVED`: canonical hard negative/contradiction.

Tesla-link scale:
- 0 none / no public evidence
- 1 adjacent relationship
- 2 unnamed/implied head customer
- 3 Tesla/Optimus explicitly named, not yet commercial confirmation
- 4 Tesla/Optimus explicitly named with commercialization evidence

## Safety boundary
CNINFO discovery/title parsing creates **PROVISIONAL** evidence only. It cannot auto-promote a company to canonical status. Ambiguous evidence waits in `review-queue`.

## Unattended behavior
`daily` can run without ChatGPT:
- scan CNINFO keywords;
- add new symbols to `DISCOVERY_ONLY`;
- ingest structured JSONL evidence dropped into the inbox;
- reconcile all deterministic states;
- create an immutable registry snapshot.

This closes the registry/lifecycle loop. It does **not** yet parse full announcement PDFs into canonical evidence. A later collector/parser can feed the evidence inbox without changing the registry contract.
