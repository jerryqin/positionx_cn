# PositionX Optimus C.8 — Preclassification Registry Skip

## Trigger

After V0.7.3, Registry contains 24 members:

```text
ACTIVE_CANONICAL 22
DISCOVERY_ONLY    2
```

C.7 correctly prevents 603416.SH / 603809.SH from becoming new proposals, but the filter
ran **after** classification.

That means an obsolete unreadable capture for a company already admitted to Registry could
still raise:

```text
WAITING_CAPTURE_REPAIR
```

and keep the whole discovery scan artificially PARTIAL.

## C.8 rule

For `NEW_CANDIDATE_DISCOVERY` captures:

```text
Registry membership check
    ↓
if already ACTIVE_CANONICAL or DISCOVERY_ONLY
    → SKIP_EXISTING_REGISTRY_MEMBER_BEFORE_CLASSIFICATION
    → do not parse/classify that capture

else
    → normal classifier path
```

This is an execution-order hardening only. Evidence semantics and discovery admission rules
do not change.

Expected real post-C.8 scan with the current inbox:

```text
proposal_count = 0

603416 historical/broken captures
→ skipped before classification

603809 historical capture
→ skipped before classification

600353 image-only capture
→ still WAITING_CAPTURE_REPAIR

scan therefore may remain PARTIAL, but only for the genuinely unresolved outside candidate.
```

No Registry, Canonicalization, Theme Stage, broker, or order mutation.
