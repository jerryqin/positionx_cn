# PositionX-CN Operations

Daily operations verify system/broker health, scheduled jobs, forward pipelines, human-review queues,
broker reconciliation and required market/PIT assets. Historical APPLY/rebase packages are not daily tasks.

Safety invariants:
- Broker holdings are the account truth source.
- Manual approval remains mandatory for real trading.
- A preview/readiness PASS is not authorization to submit an order.
- User/broker manual activity remains attributable as EXTERNAL.
- Governed research mutations use PLAN -> explicit approval -> APPLY.
- Fail closed on unexplained state mismatch.
