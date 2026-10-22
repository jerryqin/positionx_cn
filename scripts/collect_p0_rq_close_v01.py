"""Manual exact-day RQ transition increment. No Scheduler, QMT, or active pointer.

Reuses the accepted version, RQ get_price/is_suspended/get_ex_factor, and the
existing DuckDB + manifest + COMPLETE layout. Authentication stays in process.
Only a supplier-ready complete day may be published; failed captures stay TEMP.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import p0_rq_input as rq

FIELDS = ['open', 'high', 'low', 'close', 'volume', 'total_turnover',
          'prev_close', 'limit_up', 'limit_down', 'num_trades']
ASSET_ROOT = Path(r'D:\positionx_data\market\p0_daily_inputs_v01')
ACTION_CATALOG = Path(r'D:\positionx_data\rqdata\catalog\rqdata_foundation.duckdb')


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str,
                                    allow_nan=False) + '\n', encoding='utf-8')


def normalize(value):
    if value is None:
        return pd.DataFrame()
    x = value.copy()
    return x.reset_index() if any(n is not None for n in x.index.names) else x


def init_provider():
    # SDK imported only on the explicitly requested network path; plugins disabled.
    import rqdatac
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        kw = dict(auto_load_plugins=False, connect_timeout=10, timeout=30)
        if os.environ.get('RQDATAC_CONF') or os.environ.get('RQDATAC2_CONF'):
            rqdatac.init(**kw)
        elif os.environ.get('RQDATA_LICENSE'):
            rqdatac.init(username='license', password=os.environ['RQDATA_LICENSE'], **kw)
        else:
            raise rq.InputBlocked('RQDATA_AUTH_MISSING')
    return rqdatac


def ready_receipt(provider, business_date, now):
    day = pd.Timestamp(business_date).date()
    t = pd.Timestamp(now)
    rq.require_trading_date(day)
    if t.tzinfo is None or day != t.tz_convert(rq.CST).date():
        raise rq.InputBlocked('ONLY_ACTUAL_CURRENT_DAY_CAPTURE_ALLOWED')
    if t < t.normalize() + pd.Timedelta(hours=15, minutes=30):
        return {'status': 'PENDING', 'reason': 'COMPLETE_CLOSE_WINDOW_NOT_REACHED',
                'business_date': str(day), 'observed_at': t.isoformat()}
    r = normalize(provider.is_data_ready(categories='stock_daybar', expected_date=day, market='cn'))
    if (len(r) != 1 or r.iloc[0].get('market') != 'cn'
            or r.iloc[0].get('category') != 'stock_daybar'
            or not isinstance(r.iloc[0].get('ready'), (bool, np.bool_))
            or not r.iloc[0]['ready'] or pd.Timestamp(r.iloc[0]['latest_date']).date() != day):
        return {'status': 'PENDING', 'reason': 'SUPPLIER_STOCK_DAYBAR_NOT_READY',
                'business_date': str(day), 'observed_at': pd.Timestamp(rq.clock()).isoformat(),
                'response': json.loads(r.to_json(orient='records', date_format='iso'))}
    return {'status': 'PASS', 'business_date': str(day),
            'available_at': pd.Timestamp(rq.clock()).isoformat(),
            'response': json.loads(r.to_json(orient='records', date_format='iso')),
            'availability_semantics': 'conservative actual ready-response observation, not historical publication time'}


def validate_day(bars, status, ids, day):
    expected = set(ids)
    for frame in (bars, status):
        if (frame.empty or frame.duplicated(['order_book_id', 'date']).any()
                or set(frame.order_book_id) != expected
                or not pd.to_datetime(frame.date).dt.date.eq(pd.Timestamp(day).date()).all()):
            raise rq.InputBlocked('INCREMENT_EXACT_DAY_OR_POOL_INCOMPLETE')
    if status.status_suspended.isna().any() or not status.status_suspended.map(lambda v: isinstance(v, (bool, np.bool_))).all():
        raise rq.InputBlocked('INCREMENT_STATUS_UNKNOWN')
    x = bars.merge(status, on=['order_book_id', 'date'], validate='one_to_one')
    prices = x[['open', 'high', 'low', 'close']].to_numpy(dtype=float)
    if (not np.isfinite(x[FIELDS].to_numpy(dtype=float)).all()
            or (prices <= 0).any() or (x[['volume', 'total_turnover', 'num_trades']] < 0).any().any()
            or (x.high < x[['open', 'close', 'low']].max(axis=1)).any()
            or (x.low > x[['open', 'close', 'high']].min(axis=1)).any()
            or (x.price_tick <= 0).any() or not np.isfinite(x.price_tick).all()):
        raise rq.InputBlocked('INCREMENT_VALUE_QUALITY_FAILED')
    # Contradictory actual trade/status evidence is a data error, never a fill.
    if (x.status_suspended & ((x.volume > 0) | (x.num_trades > 0))).any():
        raise rq.InputBlocked('INCREMENT_SUSPENSION_TRADE_CONFLICT')
    return {'status': 'PASS', 'rows': len(x), 'symbols': len(expected),
            'suspended': int(x.status_suspended.sum()),
            'zero_trade_rows': int(((x.volume == 0) | (x.num_trades == 0)).sum()),
            'executions_created': 0}


def capture_action_terms(provider, ids, previous, day, work):
    """Only newly changed cohort members; no OHLC/history/foundation refresh."""
    if not ids:
        return
    observed = pd.Timestamp(rq.clock()).isoformat()
    records = []
    for name, method in [('dividend', provider.get_dividend), ('split', provider.get_split),
                         ('rights', provider.get_allotment)]:
        frame = normalize(method(ids, start_date=previous, end_date=day))
        records.append(frame)
        if not frame.empty:
            frame.to_parquet(Path(work) / ('action_raw_' + name + '.parquet'), index=False)
    observed = pd.Timestamp(rq.clock()).isoformat()
    try:
        actions = rq.normalize_actions(*records, pd.DataFrame(), observed_at=observed)
    except rq.InputBlocked as exc:
        write_json(Path(work) / 'action_capture.json', dict(status='BLOCKED', reason=str(exc),
            observed_at=observed, symbols=ids, start=str(previous), end=str(day)))
        return
    if not actions.empty:
        actions = actions.loc[pd.to_datetime(actions.ex_date).dt.date.eq(pd.Timestamp(day).date())]
        if not actions.empty:
            actions.to_parquet(Path(work) / 'captured_actions.parquet', index=False)
    write_json(Path(work) / 'action_capture.json', dict(status='OBSERVED', observed_at=observed,
        symbols=ids, start=str(previous), end=str(day), events=len(actions),
        scope='existing RQ source; changed cohort members only; actual observation, no historical PIT'))


def bind_local_actions(con, business_date, observed_at, source_catalog=ACTION_CATALOG):
    """Bind existing RQ actions for the exact 120-bar DEEP engineering cohort.

    membership_reference is a labelled planning cohort, never a historical PIT.
    A later true PIT member outside this explicit coverage remains blocked by
    the reader. No action download, inferred announcement time, or price edits.
    """
    ids = [r[0] for r in con.execute("SELECT order_book_id FROM membership_reference WHERE index_code='000905'").fetchall()]
    if len(set(ids)) != 500:
        return {'corporate_action_coverage_complete': False, 'corporate_action_reason': 'EXACT500_ENGINEERING_COHORT_MISSING'}
    bars = con.execute('''SELECT order_book_id,date FROM raw_daily
        WHERE order_book_id IN (SELECT unnest(?)) AND date<=?
        QUALIFY row_number() OVER (PARTITION BY order_book_id ORDER BY date DESC)<=120''',
        [ids, pd.Timestamp(business_date).date()]).df()
    counts = bars.groupby('order_book_id').size()
    if len(counts) != 500 or (counts != 120).any():
        return {'corporate_action_coverage_complete': False, 'corporate_action_reason': 'DEEP_120_COVERAGE_MISSING'}
    start, end = bars.date.min().date(), pd.Timestamp(business_date).date()
    source_hash = rq.sha(source_catalog)
    with duckdb.connect(str(source_catalog), read_only=True, config={'temp_directory': ''}) as src:
        frames = [src.execute(f'''SELECT * FROM rqdata_a2.{table}
            WHERE order_book_id IN (SELECT unnest(?)) AND {field}>? AND {field}<=?''',
            [ids, start, end]).df() for table, field in [
            ('dividend', 'ex_dividend_date'), ('split', 'ex_dividend_date'),
            ('allotment', 'ex_right_date'), ('dividend_info', 'ex_dividend_date')]]
    if rq.sha(source_catalog) != source_hash:
        raise rq.InputBlocked('LOCAL_ACTION_SOURCE_CHANGED')
    try:
        actions = rq.normalize_actions(*frames, observed_at=observed_at)
    except rq.InputBlocked as exc:
        return {'corporate_action_coverage_complete': False, 'corporate_action_reason': str(exc),
                'corporate_action_source_sha256': source_hash}
    if 'captured_actions' in {r[0] for r in con.execute('SHOW TABLES').fetchall()}:
        saved = con.execute('''SELECT * FROM captured_actions
            WHERE order_book_id IN (SELECT unnest(?)) AND ex_date>? AND ex_date<=?''', [ids,start,end]).df()
        actions = pd.concat([actions, saved], ignore_index=True)
        if not actions.empty:
            terms = ['cash_per_share','share_multiplier','rights_per_share','rights_price']
            if (actions.groupby(['order_book_id','ex_date'])[terms].nunique() > 1).any().any():
                return {'corporate_action_coverage_complete': False, 'corporate_action_reason': 'LOCAL_AND_CAPTURED_ACTION_TERMS_CONFLICT'}
            actions = actions.drop_duplicates(['order_book_id','ex_date'])
    factors = con.execute('''SELECT order_book_id,ex_date FROM factors
        WHERE order_book_id IN (SELECT unnest(?)) AND ex_date>? AND ex_date<=?''', [ids, start, end]).df()
    actual = set(zip(actions.order_book_id, pd.to_datetime(actions.ex_date))) if not actions.empty else set()
    missing = set(zip(factors.order_book_id, pd.to_datetime(factors.ex_date))) - actual
    available = (actions.empty or all(pd.Timestamp(t).tzinfo is not None and pd.Timestamp(t)<=pd.Timestamp(observed_at)
        for t in actions.available_at))
    if actions.empty:
        con.execute('DROP TABLE IF EXISTS corporate_actions')
    else:
        con.register('normalized_local_actions', actions)
        con.execute('CREATE OR REPLACE TABLE corporate_actions AS SELECT * FROM normalized_local_actions')
    return dict(corporate_action_coverage_complete=not missing and available,
        corporate_action_coverage_start=str(start), corporate_action_coverage_end=str(end),
        corporate_action_symbols=sorted(ids), corporate_action_events=len(actions),
        corporate_action_factor_events=len(factors),
        corporate_action_missing=[dict(order_book_id=s, ex_date=str(d.date())) for s,d in sorted(missing)],
        corporate_action_source=str(source_catalog), corporate_action_source_sha256=source_hash,
        corporate_action_observed_at=str(observed_at),
        corporate_action_cohort='RQ planning cohort; not preopen PIT; later member must be covered explicitly')


def collect(provider, base_manifest, business_date, work, now=None):
    """All writes in a new caller-owned TEMP directory; no production mutation."""
    t = pd.Timestamp(now or rq.clock())
    receipt = ready_receipt(provider, business_date, t)
    work = Path(work).resolve()
    if not work.is_relative_to(Path(r'D:\TEMP').resolve()):
        raise rq.InputBlocked('CAPTURE_WORK_MUST_STAY_IN_TEMP')
    work.mkdir(parents=True, exist_ok=False)
    write_json(work / 'supplier_ready.json', receipt)
    if receipt['status'] != 'PASS':
        return receipt
    base = rq.DailyInput(base_manifest, {'enabled': False}, now=t)
    day = pd.Timestamp(business_date).date()
    previous = pd.Timestamp(base.meta['asof']).date()
    cal = json.loads((ROOT / 'config/ops/cn_exchange_calendar_2026.json').read_text(encoding='utf-8'))
    dates = pd.date_range(previous + pd.Timedelta(days=1), day)
    valid = [d for d in dates if d.weekday() < 5 and not any(r['start'] <= d.strftime('%Y%m%d') <= r['end'] for r in cal['closed_ranges'])]
    if len(valid) != 1 or valid[0].date() != day:
        raise rq.InputBlocked('INCREMENT_REQUIRES_IMMEDIATE_PREVIOUS_SESSION_VERSION')
    # Exact current universe; new listings get only today's row, never a fake history.
    identity = normalize(provider.all_instruments(type='CS', date=day))
    ids = sorted(set(identity.order_book_id))
    if len(ids) < 3000:
        raise rq.InputBlocked('INCREMENT_STOCK_POOL_TOO_SMALL')
    for obid in ids:
        rq.qmt_id(obid)
    frames, statuses, factors = [], [], []
    for i in range(0, len(ids), 200):
        batch = ids[i:i + 200]
        raw = normalize(provider.get_price(batch, start_date=day, end_date=day,
            frequency='1d', fields=FIELDS, adjust_type='none', skip_suspended=False, expect_df=True))
        stamp = pd.Timestamp(rq.clock()).isoformat()
        raw['source_version'] = 'RQDATA_CLOSE_' + day.strftime('%Y%m%d') + '_' + stamp
        frames.append(raw)
        raw.to_parquet(work / f'raw_{i // 200:03}.parquet', index=False)
        matrix = provider.is_suspended(batch, start_date=day, end_date=day)
        if matrix is None or matrix.empty:
            raise rq.InputBlocked('INCREMENT_STATUS_RESPONSE_EMPTY')
        s = matrix.rename_axis('date').reset_index().melt('date', var_name='order_book_id', value_name='status_suspended')
        s['status_available_at'] = pd.Timestamp(rq.clock()).isoformat()
        ticks = provider.get_tick_size(batch)
        s['price_tick'] = s.order_book_id.map(ticks)
        s['tick_available_at'] = pd.Timestamp(rq.clock()).isoformat()
        statuses.append(s)
        s.to_parquet(work / f'status_{i // 200:03}.parquet', index=False)
        f = normalize(provider.get_ex_factor(batch, start_date=day, end_date=day))
        if not f.empty:
            factors.append(f)
            f.to_parquet(work / f'factor_{i // 200:03}.parquet', index=False)
    bars = pd.concat(frames, ignore_index=True)
    status = pd.concat(statuses, ignore_index=True)
    quality = validate_day(bars, status, ids, day)
    # Supplier must still report the exact complete date after the full capture.
    after = ready_receipt(provider, day, rq.clock())
    if after['status'] != 'PASS':
        raise rq.InputBlocked('INCREMENT_SUPPLIER_COMPLETENESS_LOST')
    write_json(work / 'supplier_ready_after.json', after)
    if factors:
        changed = sorted(set(pd.concat(factors).order_book_id))
        with duckdb.connect(str(base.db), read_only=True, config={'temp_directory': ''}) as c:
            if 'membership_reference' in {r[0] for r in c.execute('SHOW TABLES').fetchall()}:
                cohort = {r[0] for r in c.execute("SELECT order_book_id FROM membership_reference WHERE index_code='000905'").fetchall()}
                capture_action_terms(provider, sorted(set(changed) & cohort), previous, day, work)
    return seal_capture(base, day, work, bars, status, identity, factors, receipt)


def seal_capture(base, day, work, bars, status, identity, factors, receipt):
    """Seal saved exact-day observations; permits offline recovery without refetch."""
    work = Path(work).resolve()
    day = pd.Timestamp(day).date()
    if not work.is_relative_to(Path(r'D:\TEMP').resolve()):
        raise rq.InputBlocked('CAPTURE_WORK_MUST_STAY_IN_TEMP')
    for name in ('supplier_ready.json', 'supplier_ready_after.json'):
        ready = json.loads((work / name).read_text(encoding='utf-8'))
        if ready.get('status') != 'PASS' or ready.get('business_date') != str(day):
            raise rq.InputBlocked('SAVED_CAPTURE_COMPLETENESS_UNVERIFIED')
    quality = validate_day(bars, status, set(identity.order_book_id), day)
    identity.to_parquet(work / 'identity_observed_today.parquet', index=False)
    dest = work / 'daily.duckdb'
    if dest.exists():
        raise rq.InputBlocked('SEAL_REQUIRES_NEW_OWNED_DB_PATH')
    shutil.copyfile(base.db, dest)
    if rq.sha(dest) != base.meta['database_sha256']:
        raise rq.InputBlocked('INCREMENT_BASE_CHANGED_DURING_CAPTURE')
    bars['date'] = pd.to_datetime(bars.date).dt.date
    status['date'] = pd.to_datetime(status.date).dt.date
    with duckdb.connect(str(dest), config={'temp_directory': ''}) as c:
        if c.execute('SELECT count(*) FROM raw_daily WHERE date>=?', [day]).fetchone()[0]:
            raise rq.InputBlocked('INCREMENT_EXISTING_OR_FUTURE_ROWS')
        c.register('new_bars', bars)
        c.execute('INSERT INTO raw_daily BY NAME SELECT * FROM new_bars')
        c.register('new_status', status)
        c.execute('CREATE TABLE IF NOT EXISTS execution_status AS SELECT * FROM new_status WHERE false')
        c.execute('INSERT INTO execution_status BY NAME SELECT * FROM new_status')
        c.execute('INSERT INTO suspension SELECT order_book_id,date,status_suspended FROM new_status WHERE status_suspended')
        c.register('current_identity', identity)
        # Retain old identity for held/delisted symbols; do not rewrite history.
        c.execute('CREATE OR REPLACE TABLE identity_observed_today AS SELECT * FROM current_identity')
        c.execute('INSERT INTO security_identity BY NAME SELECT * FROM current_identity n WHERE NOT EXISTS (SELECT 1 FROM security_identity o WHERE o.order_book_id=n.order_book_id)')
        if factors:
            f = pd.concat(factors, ignore_index=True)
            if f.duplicated(['order_book_id', 'ex_date']).any() or not pd.to_datetime(f.ex_date).dt.date.eq(day).all() or not np.isfinite(f.ex_cum_factor).all() or (f.ex_cum_factor <= 0).any():
                raise rq.InputBlocked('INCREMENT_FACTOR_INVALID')
            f['ex_date'] = pd.to_datetime(f.ex_date).dt.date
            f['source_version'] = 'RQDATA_CLOSE_' + day.strftime('%Y%m%d')
            c.register('new_factors', f)
            # New supplier field: preserve actual times only in the new owned
            # version; old rows stay NULL and are not assigned invented PIT.
            if 'rice_create_tm' in f:
                if not pd.api.types.is_integer_dtype(f.rice_create_tm):
                    raise rq.InputBlocked('FACTOR_RAW_CREATION_TYPE_CHANGED')
                c.execute('ALTER TABLE factor_raw_versions ADD COLUMN IF NOT EXISTS rice_create_tm BIGINT')
            c.execute('INSERT INTO factor_raw_versions BY NAME SELECT * FROM new_factors')
            c.execute('INSERT INTO factors SELECT order_book_id,ex_date,ex_cum_factor FROM new_factors')
        if (work / 'captured_actions.parquet').exists():
            captured = pd.read_parquet(work / 'captured_actions.parquet')
            c.register('new_captured_actions', captured)
            c.execute('CREATE TABLE IF NOT EXISTS captured_actions AS SELECT * FROM new_captured_actions WHERE false')
            c.execute('INSERT INTO captured_actions BY NAME SELECT * FROM new_captured_actions')
        tables = {r[0] for r in c.execute('SHOW TABLES').fetchall()}
        action_meta = bind_local_actions(c, day, rq.clock()) if 'membership_reference' in tables else {
            'corporate_action_coverage_complete': False, 'corporate_action_reason': 'ENGINEERING_COHORT_NOT_IN_INPUT'}
        c.execute('CHECKPOINT')
        total_rows, total_symbols = c.execute('SELECT count(*),count(DISTINCT order_book_id) FROM raw_daily').fetchone()
    ingested = pd.Timestamp(rq.clock()).isoformat()
    database_hash = rq.sha(dest)
    version = 'asof_' + day.strftime('%Y%m%d') + '_' + database_hash[:12]
    meta = dict(base.meta, schema='P0_RQ_IMMUTABLE_INPUT_V2', asof=str(day),
        business_date=str(day), availability=ingested, available_at=ingested,
        ingested_at=ingested, version=version, database='daily.duckdb',
        database_sha256=database_hash, quality_status='PASS', raw_adjustment='none',
        adjustment='none', unit=base.meta['volume_unit'],
        base_manifest_sha256=rq.sha(base.root / 'manifest.json'),
        supplier_complete_observed_at=receipt['available_at'],
        historical_availability_not_inferred=True,
        current_status_and_tick='exact-day observed; no historical tick reconstruction',
        factor_rice_create_tm='vendor raw integer preserved; never substituted for available_at or our observation time',
        adjustments=dict(base.meta['adjustments'], QMT_FRONT='BLOCKED_PENDING_ACTION_AND_NUMERIC_CONTRACT'),
        activated_in_production=False)
    # Frozen old preview remains old-date evidence; it is never an increment source.
    meta['historical_preview_asof'] = base.meta.get('historical_preview_asof', base.meta['asof'])
    meta['adjustments']['RET120_FROZEN_CLOSE_PRE_CLOSE'] = 'Original frozen cumulative close/prev_close research series; separate raw execution basis'
    meta['counts'] = dict(raw_rows=total_rows, symbols=total_symbols, appended_rows=len(bars), business_date_symbols=len(set(identity.order_book_id)))
    meta.update(action_meta)
    meta['incremental_' + day.strftime('%Y%m%d')] = 'PASS'
    write_json(work / 'source_metadata.json', meta)
    write_json(work / 'quality.json', quality)
    inventory = [{'path': p.name, 'sha256': rq.sha(p), 'bytes': p.stat().st_size}
                 for p in sorted(work.iterdir()) if p.is_file()]
    write_json(work / 'manifest.json', {'schema': 'P0_RQ_VERSION_MANIFEST_V1',
                                      'version': version, 'files': inventory})
    write_json(work / 'COMPLETE.json', {'manifest_sha256': rq.sha(work / 'manifest.json')})
    rq.DailyInput(work / 'manifest.json', {'enabled': False})
    return dict(quality, version=version, work=str(work), database_sha256=database_hash)


def publish(work, asset_root=ASSET_ROOT):
    work, root = Path(work).resolve(), Path(asset_root).resolve()
    if root != ASSET_ROOT.resolve():
        raise rq.InputBlocked('PUBLISH_OUTSIDE_AUTHORIZED_ASSET_ROOT')
    reader = rq.DailyInput(work / 'manifest.json', {'enabled': False})
    target = root / 'versions' / reader.meta['version']
    if not target.resolve().is_relative_to(root) or not rq.re.fullmatch(r'asof_\d{8}_[0-9a-f]{12}', reader.meta['version']):
        raise rq.InputBlocked('PUBLISH_INVALID_VERSION_PATH')
    if target.exists():
        existing = rq.DailyInput(target / 'manifest.json', {'enabled': False})
        if rq.sha(existing.db) != rq.sha(reader.db):
            raise rq.InputBlocked('PUBLISH_VERSION_COLLISION')
        return str(target)
    target.mkdir(parents=False, exist_ok=False)
    # COMPLETE is copied last. Interrupted writes remain visibly unsealed.
    for p in work.iterdir():
        if p.is_file() and p.name != 'COMPLETE.json':
            shutil.copyfile(p, target / p.name)
    shutil.copyfile(work / 'COMPLETE.json', target / 'COMPLETE.json')
    rq.DailyInput(target / 'manifest.json', {'enabled': False})
    return str(target)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--business-date', required=True)
    p.add_argument('--base-manifest', required=True)
    p.add_argument('--work-dir', required=True)
    p.add_argument('--publish', action='store_true')
    args = p.parse_args()
    try:
        # Before close, do not even initialize a network session.
        t = pd.Timestamp(rq.clock())
        if pd.Timestamp(args.business_date).date() != t.tz_convert(rq.CST).date():
            raise rq.InputBlocked('ONLY_ACTUAL_CURRENT_DAY_CAPTURE_ALLOWED')
        rq.require_trading_date(args.business_date)
        if t < t.normalize() + pd.Timedelta(hours=15, minutes=30):
            print(json.dumps({'status': 'PENDING', 'reason': 'COMPLETE_CLOSE_WINDOW_NOT_REACHED'}))
            return 3
        # Each manual invocation retains its own receipt; an earlier PENDING
        # attempt never needs deletion and cannot block the later ready capture.
        attempt = Path(args.work_dir) / ('capture_' + t.strftime('%Y%m%dT%H%M%S') + '_' + uuid.uuid4().hex[:8])
        result = collect(init_provider(), args.base_manifest, args.business_date, attempt)
        if args.publish and result['status'] == 'PASS':
            result['published'] = publish(result['work'])
        print(json.dumps(result))
        return 0 if result['status'] == 'PASS' else 3
    except Exception as exc:
        # Provider errors can contain connection details; never echo credentials.
        print(json.dumps({'status': 'FAIL', 'error_type': type(exc).__name__,
                          'reason': str(exc) if isinstance(exc, rq.InputBlocked) else 'CAPTURE_OR_VALIDATION_FAILED'}))
        return 3


if __name__ == '__main__':
    raise SystemExit(main())
