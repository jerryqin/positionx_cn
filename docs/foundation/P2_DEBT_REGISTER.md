# PositionX-CN Foundation Debt Register

Generated from the P1/P2A audit facts. This is a planning register, not an instruction to mass-edit the codebase.

## High-priority debt
- Absolute-path occurrences: `752` across `259` tracked files.
- MiniQMT/xtquant reference files: `78`.
- Global `positionx doctor`: not yet implemented; assigned to P3.
- Dependency lock and Python-version contract: P2 scope.

## Rules
- Do not mass-rewrite all absolute paths in one change.
- Do not begin QMT adapter implementation before P2/P3 are complete.
- Migrate machine-specific paths behind configuration/provider boundaries incrementally.
- Keep live trading disabled during recovery and infrastructure refactors unless separately approved.
