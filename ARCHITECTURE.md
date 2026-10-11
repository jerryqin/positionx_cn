# PositionX-CN Architecture

Status: initial Git baseline snapshot, 2026-10-11.

PositionX-CN is an independently runnable local quantitative research and execution-governance system.
ChatGPT/other AI assistants are not runtime dependencies.

Current broker/market integration still contains MiniQMT/xtquant coupling.
The migration target is an adapter boundary: MarketDataProvider / BrokerProvider / ExecutionProvider,
with QMT acting as an external bridge/transport rather than owning strategy semantics.

Source code/config/tests/docs belong in Git. Runtime databases, Parquet assets, logs, backups,
delivery payloads and broker-local state remain outside the Git source baseline.
