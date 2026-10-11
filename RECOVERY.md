# PositionX-CN Recovery — P2 Draft

## Source recovery anchor
- Baseline tag: `positionx-cn-baseline-20261011`
- Baseline commit: `0d174edc6c5e02d71132afa2be0381c8d4b6f121`
- Baseline tree: `13699795e80aaf7bf5fd345e37e386a1f8ef60cf`

## Current environment facts
- Python: `3.10.11`
- Python executable at audit time: `D:\positionx_cn\.venv\Scripts\python.exe`
- `pip check` return code: `0`
- Direct requirements recorded: `5`.
- `pip freeze --all` entries captured by P2A: `65`.

## Recovery sequence
1. Restore or clone the reviewed Git revision.
2. Rebuild the Python environment from a future locked dependency artifact.
3. Restore governed runtime/research data from separately verified backup.
4. Restore QMT/broker software and local connectivity configuration.
5. Run unit tests plus the future global `positionx doctor`.
6. Reconcile broker state.
7. Keep live execution disabled until all recovery gates pass.

## Known recovery debt
- Absolute-path occurrences requiring portability hardening: `752`.
- Environment-variable names requiring a documented local configuration contract: `9`.
- No dependency lockfile exists yet; P2/P3 must establish reproducible environment reconstruction.
