PositionX — RET120 Frozen Signal Lineage Diagnostic V0.1
Date: 2026-10-01

Purpose
-------
P0 only. Recover and prove the exact v0.2.2 CSI500 ret120 daily-Q5
symbol-date lineage. Fail closed if the frozen artifacts cannot be reconciled.

This patch DOES NOT:
- modify any frozen research artifact
- redefine ret120
- change thresholds
- create a new strategy
- run a portfolio backtest
- touch production SQLite
- touch execution / broker / QMT trading APIs

Required existing frozen artifacts
----------------------------------
D:\positionx_min_live\data\research\edge_discovery\
  trend_persistence_scan_csi500_panel_v0221.parquet
  trend_persistence_scan_csi500_daily_buckets_v0221.parquet
  trend_persistence_scan_csi500_v022_latest.json

Optional consistency artifact:
  trend_persistence_replication_v022_latest.json

Install
-------
Unzip this archive into:

D:\positionx_min_live

It will add only:
  scripts\audit_ret120_frozen_signal_lineage_v01.py
  tests\test_ret120_frozen_signal_lineage_v01.py
  PATCH_README_RET120_FROZEN_SIGNAL_LINEAGE_V01.txt

Run
---
cd /d D:\positionx_min_live

python -m unittest tests.test_ret120_frozen_signal_lineage_v01 -v

python scripts\audit_ret120_frozen_signal_lineage_v01.py

Expected success marker
-----------------------
FINAL_STATUS = PASS_RET120_FROZEN_SIGNAL_LINEAGE_V0_1

Fail-closed marker
------------------
FINAL_STATUS = FAIL_CLOSED_RET120_FROZEN_SIGNAL_LINEAGE_V0_1

Important semantic point
------------------------
The frozen v0.2.2 scanner assigned daily quintiles from the full signal-date
panel first, then applied horizon-label completeness for each H20/H60/H120
aggregate. This patch reproduces that exact order.

The persisted panel does not contain entry_open / exit_close levels.
Therefore P0 can prove exact Q5 symbol-date signal lineage and frozen
feature/label semantics, while historical price levels are classified as:

RECONSTRUCTABLE_SEMANTICS_NOT_FROZEN_LEVELS

P1 must explicitly freeze its price-source/economic-equivalence policy before
the comparable 1% / 30% / 30-position rebuild.
