# PositionX-CN Governance

- Production research/governance mutations use PLAN -> human approval -> guarded APPLY.
- Historical frozen APPLY/rebase packages are not routine commands.
- Broker/execution actions require explicit manual approval.
- A passing research/preview gate is not authorization to submit.
- Raw source evidence is not automatically canonical truth.
- Fail-closed behavior is preferred to silent fallback.

Git:
- main represents reviewed source state.
- Runtime data, credentials, logs, delivery payloads and backups stay out of Git.
- Secrets must never be committed.
- Commit/tag creation remains a human-approved step.
