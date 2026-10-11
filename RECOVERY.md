# PositionX-CN Recovery

The Git repository is the source/config/test/documentation recovery source after the initial baseline commit.
Runtime data is deliberately outside Git and must be restored from separately governed backups.

Basic sequence:
1. Restore/clone source revision.
2. Rebuild the Python environment from dependency specifications.
3. Restore research/runtime data from approved backup.
4. Restore QMT/broker software and local connectivity configuration.
5. Run tests and read-only checks.
6. Reconcile broker state.
7. Keep live execution disabled until recovery checks pass.

Known debt: some tracked files still contain absolute paths under D:\positionx_cn,
D:\positionx_data and D:\qmt.
