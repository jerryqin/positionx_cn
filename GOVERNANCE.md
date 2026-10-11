# PositionX-CN Governance — P2 Draft

## Git baseline
- Tag: `positionx-cn-baseline-20261011`
- Commit: `0d174edc6c5e02d71132afa2be0381c8d4b6f121`
- Tree: `13699795e80aaf7bf5fd345e37e386a1f8ef60cf`

## Change control
- `main` represents reviewed source state.
- Production research/governance mutations use PLAN -> explicit human approval -> guarded APPLY.
- Historical frozen APPLY/rebase packages are not routine commands.
- Secrets, broker credentials, runtime DBs, logs, QMT userdata and bulk data assets stay outside source Git.

## Trading control
- Real order submission requires explicit manual approval.
- Approval and submission are distinct states.
- Broker/manual activity not produced by PositionX remains attributable as EXTERNAL.

## Failure policy
- Prefer fail-closed behavior to silent fallback.
- Do not manufacture missing PIT/evidence data.
- Do not lower evidence standards to force a green status.
