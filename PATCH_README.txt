PositionX Daily PIT Intake V0.1
Date: 2026-09-27

Purpose
-------
Start preserving from-now-forward PIT evidence every day.

This is infrastructure, not a Strategy.

Highest rule
------------
For future research, "event happened at" and "PositionX observed it at" are
different timestamps.

    event_time
    observed_at / first_seen_at

A future Strategy may only use information that PositionX had observed by its
decision time.

Architecture
------------
Immutable raw evidence:
    D:\positionx_data\pit_live\raw\

Thin manifests:
    D:\positionx_data\pit_live\manifests\

Thin research catalog:
    D:\positionx_min_live\data\research\positionx_research.duckdb

Production SQLite is never opened.

The raw parquet files are the durable source of truth. The DuckDB catalog is
reconstructable metadata / query state.

Why this is NOT Eva's original fetch_daily_pit.py
-------------------------------------------------
Two material changes:

1. SAME-DAY IMMUTABILITY

Eva's script writes:
    daily_capture\<as_of>\<dataset>.parquet

Running 08:30 and 18:00 on the same day can overwrite the earlier snapshot.

V0.1 writes:
    raw\<YYYYMMDD>\<phase>\<capture_id>\...

Every run is immutable.

2. LATE BACKFILL / OVERWRITE DETECTION

The cloud tables do not expose update_time.

A 7-day query by ann_date will permanently miss a row that arrives today with
an ann_date two months ago.

Therefore V0.1 revisits:
    forecast/express/income/fina_indicator : 180 days
    stock_limit/suspend                    : 30 days
    index_weight                           : 120 days
    stock_basic                            : full snapshot

Raw versions are retained so later in-place changes remain observable.

Forward source-of-truth choices
-------------------------------
Index membership:
    QMT CURRENT sector membership captured PREOPEN.
    Exact count gate:
        CSI300 = 300
        CSI500 = 500
        CSI1000 = 1000

Historical date arguments in QMT are NOT used.

Earnings event time:
    cninfo public announcements.
    announcementId + event_time + PositionX first_seen_at.

IWA cloud financial tables:
    backfill/value snapshots.
    They are NOT trusted as current PEAD event-time truth while their
    freshness is stale/unknown.

Schedule
--------
PREOPEN
    weekdays 08:30
    - QMT current CSI300/500/1000 membership
    - cninfo earnings announcements, recent window

POSTCLOSE
    every day 18:00
    - cninfo earnings announcements
    - IWA cloud rolling/revisit snapshots
    - weekend run preserves weekend first_seen timing

The installer sets retry = every 15 minutes, up to 3 retries.

Preflight
---------
1. Confirm .venv can import iwa_data.

Do NOT put a token in source code.

The supported local setup described by the external audit is to make the
official iwa_data.py importable by the PositionX venv and let it read:

    %USERPROFILE%\.iwaos\storage\db-connections.json

Run:

    python scripts\smoke_test_iwa_daily_pit_v01.py

Expected:
    LOCAL_DAILY_PIT_AUTOMATION=READY

2. Test manually before installing scheduler:

    python scripts\daily_pit_intake_v01.py --phase preopen
    python scripts\daily_pit_intake_v01.py --phase postclose

3. Inspect:

    D:\positionx_data\pit_live\raw\
    D:\positionx_data\pit_live\manifests\

and DuckDB tables:

    pit_capture_runs
    pit_capture_items
    pit_index_membership_observed
    pit_cninfo_announcement_versions

Scheduler install
-----------------
After BOTH manual runs are clean:

    powershell -ExecutionPolicy Bypass -File scripts\install_daily_pit_tasks_v01.ps1

This creates:

    PositionX-DailyPIT-Preopen
    PositionX-DailyPIT-Postclose

Important:
    The task must run as the same Windows user that owns ~/.iwaos.
    The IWA token/plan currently has a future expiry; track renewal separately.

PEAD status
-----------
The current historical Earnings Forecast Drift dataset is NOT accepted.

The PositionX acceptance audit reported:
    first_ann_date non-null = 0.54%
    first-row magnitude coverage ~89.29%

Therefore:
    do not backtest historical Earnings Forecast Drift from that delivery.

cninfo solves FORWARD first-seen capture from now onward. It does not magically
reconstruct historical first-seen payload.

The alternative "overnight gap after earnings announcement" should be treated
as a NEW Strategy hypothesis if tested later, not as a rescue/relabel of the
failed Earnings Forecast Drift historical dataset.

Safety
------
- no broker
- no orders
- no production SQLite
- no execution-core change
- no Deep Oversold change
