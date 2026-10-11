# PositionX-CN Architecture — P2 Draft

## Baseline
- Git commit: `0d174edc6c5e02d71132afa2be0381c8d4b6f121`
- Git tree: `13699795e80aaf7bf5fd345e37e386a1f8ef60cf`
- Baseline tag: `positionx-cn-baseline-20261011`

## Architectural principle
PositionX-CN is an independently runnable local Python system. External AI assistants are optional development/audit helpers, not runtime dependencies.

## Current layers
- Data / Evidence
- Signal / Research
- Regime
- Position
- Risk
- Action / Preview
- Manual approval
- Broker / market adapter

## External boundaries
- Environment-variable names referenced in tracked source: `9`.
- MiniQMT/xtquant-coupled tracked files detected by P2A: `78`.
- Absolute-path occurrences detected by P2A: `752`.

## QMT migration target
Introduce explicit `MarketDataProvider`, `BrokerProvider`, and `ExecutionProvider` boundaries. Keep QMT as transport/I/O integration rather than strategy owner. Build the replacement read-only first, then dual-run before cutover.

## Source vs runtime state
Git owns source/config/tests/docs. Runtime databases, Parquet assets, logs, broker-local state, backups and governed data assets remain outside source Git and require their own provenance/backup controls.
