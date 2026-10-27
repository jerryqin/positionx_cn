"""Bounded RQ close increment into an immutable repository candidate, never production.

No Broker/QMT, scheduler, ledger, active pointer, source switch or historical PIT.
Existing qualified history is read only. A verified retry performs no downloads.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
import sys
import time
import os

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import duckdb
import pandas as pd
from core import p0_rq_input as rq
from scripts import collect_p0_rq_close_v01 as old
from scripts import run_postclose_local_candidate as local

SOURCE_STEP = {'method': 'not_started'}
_WRITE_GUARDS = []


def bounded_provider():
    import rqdatac
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        kw = dict(auto_load_plugins=False, connect_timeout=5, timeout=8)
        if os.environ.get('RQDATAC_CONF') or os.environ.get('RQDATAC2_CONF'):
            rqdatac.init(**kw)
        elif os.environ.get('RQDATA_LICENSE'):
            rqdatac.init(username='license', password=os.environ['RQDATA_LICENSE'], **kw)
        else:
            raise rq.InputBlocked('RQDATA_AUTH_MISSING')
    return rqdatac


def fetch(provider, method, *args, **kwargs):
    """Retain safe operation/date/batch context, never credentials or raw errors."""
    SOURCE_STEP.clear()
    SOURCE_STEP.update(method=method, start=str(kwargs.get('start_date', kwargs.get('date', ''))),
                       end=str(kwargs.get('end_date', kwargs.get('date', ''))),
                       batch_size=len(args[0]) if args and isinstance(args[0], list) else 1)
    started = time.monotonic()
    try:
        return getattr(provider, method)(*args, **kwargs)
    finally:
        print(json.dumps(dict(SOURCE_STEP, seconds=round(time.monotonic()-started, 3))), flush=True)


def verified_existing(out, day, base):
    """A failed attempt is retained; only a complete verified candidate is reused."""
    marker = out / 'manifest.json'
    if not out.exists():
        return None
    if not marker.is_file():
        raise ValueError('INCOMPLETE_ATTEMPT_RETAINED_USE_NEW_OUTPUT')
    m = local.read_json(marker)
    if (m.get('target_date') != day or m.get('status') != 'PASS_CANDIDATE_NOT_PRODUCTION'
            or m.get('base_manifest_sha256') != rq.sha(base)
            or rq.sha(out / 'increment.duckdb') != m.get('database_sha256')):
        raise ValueError('EXISTING_CANDIDATE_BINDING_FAILED')
    return m


def run(day, base_manifest, output, provider=None, batch_size=100):
    try:
        return _run(day, base_manifest, output, provider, batch_size)
    finally:
        for guard in _WRITE_GUARDS:
            guard[0] = False


def _run(day, base_manifest, output, provider=None, batch_size=100):
    out = Path(output).absolute()
    if out.resolve() != out or not out.is_relative_to(ROOT / 'reports'):
        raise ValueError('OUTPUT_MUST_STAY_IN_REPOSITORY_REPORTS')
    base_manifest = Path(base_manifest).resolve()
    existing = verified_existing(out, day, base_manifest) if (out/'manifest.json').exists() else None
    if existing:
        return dict(existing, reused_without_download=True)
    rq.require_trading_date(day)
    base = rq.DailyInput(base_manifest, rq.profile())
    dates = [d for d in local.sessions(ROOT / 'config/ops/cn_exchange_calendar_2026.json', day)
             if base.meta['asof'].replace('-', '') < d <= day]
    if not dates or len(dates) > 5:
        raise ValueError('REQUIRES_ONE_TO_FIVE_MISSING_SESSIONS')
    # Only writes under this new candidate. Native DuckDB is also given an exact path.
    out.mkdir(parents=True, exist_ok=True)
    active = [True]
    _WRITE_GUARDS.append(active)
    def audit(event, args):
        if not active[0]: return
        if event == 'open':
            path, mode, flags = args
            if ((mode and any(x in mode for x in 'wax+'))
                    or flags & (1 | 2 | 0x100 | 0x200 | 0x400)):
                if not isinstance(path, int) and not Path(path).resolve().is_relative_to(out):
                    raise RuntimeError('CANDIDATE_WRITE_OUTSIDE_OUTPUT')
    sys.addaudithook(audit)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        p = provider or bounded_provider()
        before = old.ready_receipt(p, day, rq.clock())
    if before['status'] != 'PASS':
        raise ValueError('SUPPLIER_NOT_READY')
    db = out / 'increment.duckdb'
    def save(name, frame, append=False):
        if frame.empty:
            return
        with duckdb.connect(str(db), config={'temp_directory':''}) as c:
            c.register('new_frame', frame)
            if append and name in {r[0] for r in c.execute('SHOW TABLES').fetchall()}:
                keys = ['order_book_id'] + (['date'] if name in ('raw_daily','execution_status') else ['ex_date'] if name=='factors' else [])
                match = ' AND '.join('o.'+k+'=n.'+k for k in keys)
                c.execute('INSERT INTO '+name+' BY NAME SELECT n.* FROM new_frame n WHERE NOT EXISTS (SELECT 1 FROM '+name+' o WHERE '+match+')')
            else:
                c.execute('CREATE TABLE '+name+' AS SELECT * FROM new_frame')
    def cached(name):
        if not db.exists(): return pd.DataFrame()
        with local.read_db(db) as c:
            return c.execute('SELECT * FROM '+name).df() if name in {r[0] for r in c.execute('SHOW TABLES').fetchall()} else pd.DataFrame()
    binding = out / 'capture.json'
    expected_binding = dict(target_date=day, base_manifest_sha256=rq.sha(base_manifest))
    if db.exists() and not binding.exists():
        raise ValueError('UNBOUND_PARTIAL_DATABASE')
    if binding.exists() and local.read_json(binding) != expected_binding:
        raise ValueError('RESUME_BINDING_FAILED')
    if not binding.exists(): local.write_json(binding, expected_binding)
    identity = cached('identity_observed_today')
    if identity.empty:
        identity = old.normalize(fetch(p, 'all_instruments', type='CS', date=day))
        save('identity_observed_today', identity)
    ids = sorted(set(identity.order_book_id))
    if len(ids) < 3000 or identity.order_book_id.duplicated().any():
        raise ValueError('CURRENT_IDENTITY_INCOMPLETE')
    for s in ids:
        rq.qmt_id(s)
    done_raw = cached('raw_daily')
    done_status = cached('execution_status')
    raw_ids = set()
    if len(done_raw):
        for obid,g in done_raw.groupby('order_book_id'):
            listed = pd.Timestamp(identity.loc[identity.order_book_id.eq(obid),'listed_date'].iloc[0])
            required = {d for d in dates if pd.Timestamp(d)>=listed}
            if required.issubset(set(pd.to_datetime(g.date).dt.strftime('%Y%m%d'))):raw_ids.add(obid)
    status_ids = set(done_status.order_book_id) if len(done_status) else set()
    factor_done = cached('factor_capture_coverage')
    factor_ids = set(factor_done.order_book_id) if len(factor_done) else set()
    for offset in range(0, len(ids), batch_size):
        batch = ids[offset:offset + batch_size]
        missing = sorted(set(batch)-raw_ids)
        if missing:
            b = old.normalize(fetch(p, 'get_price', missing, start_date=dates[0], end_date=day,
            frequency='1d', fields=old.FIELDS, adjust_type='none',
            skip_suspended=False, expect_df=True))
            b['source_version'] = 'RQDATA_LATE_OBSERVED_' + rq.clock().isoformat()
            save('raw_daily', b, append=True)
        missing = sorted(set(batch)-status_ids)
        if missing:
            s = fetch(p, 'is_suspended', missing, start_date=dates[0], end_date=day)
            s = s.rename_axis('date').reset_index().melt('date', var_name='order_book_id', value_name='status_suspended')
            s['status_available_at'] = rq.clock().isoformat()
            s['price_tick'] = s.order_book_id.map(fetch(p, 'get_tick_size', missing))
            s['tick_available_at'] = rq.clock().isoformat()
            save('execution_status', s, append=True)
        missing = sorted(set(batch)-factor_ids)
        if missing:
            f = old.normalize(fetch(p, 'get_ex_factor', missing, start_date=dates[0], end_date=day))
            save('factors', f, append=True)
            save('factor_capture_coverage', pd.DataFrame({'order_book_id':missing}), append=True)
    bars, states, factor_frame = cached('raw_daily'), cached('execution_status'), cached('factors')
    factors = [factor_frame] if len(factor_frame) else []
    quality = {}
    for date in dates:
        d = pd.Timestamp(date).date()
        expected = set(identity.loc[pd.to_datetime(identity.listed_date).dt.date.le(d), 'order_book_id'])
        b = bars.loc[pd.to_datetime(bars.date).dt.date.eq(d)]
        s = states.loc[pd.to_datetime(states.date).dt.date.eq(d) & states.order_book_id.isin(expected)]
        quality[date] = old.validate_day(b, s, expected, d)
    reference_dir = ROOT/'reports'/('postclose_increment_'+day)
    ref = local.read_json(reference_dir/'references.json') if (reference_dir/'references.json').exists() else None
    if ref and rq.sha(reference_dir/'references.duckdb') == ref['sha256']:
        with local.read_db(reference_dir/'references.duckdb') as c:
            membership = pd.concat([c.execute('SELECT * FROM members_'+code).df() for code in ('000300','000905','000852')])
            benchmark = c.execute('SELECT * FROM benchmark').df()
    else:
        members = []
        for code in ('000300.XSHG', '000905.XSHG', '000852.XSHG'):
            symbols, created = fetch(p, 'index_components', code, date=day, return_create_tm=True)
            members.extend(dict(index_code=rq.qmt_id(code), symbol=rq.qmt_id(s),
                effective_date=str(pd.Timestamp(day).date()), observed_at=rq.clock().isoformat(),
                provider_created_at=str(created), preopen_evidence=False) for s in symbols)
        membership = pd.DataFrame(members)
        benchmark = old.normalize(fetch(p, 'get_price', '000905.XSHG', start_date=dates[0], end_date=day,
            frequency='1d', fields=['open','high','low','close','prev_close'], adjust_type='none', expect_df=True))
    sets = {k: set(g.symbol) for k, g in membership.groupby('index_code')}
    if sorted(map(len, sets.values())) != [300, 500, 1000] or len(set.union(*sets.values())) != 1800:
        raise ValueError('INDEX_COMPONENT_COUNT_OR_OVERLAP_FAILED')
    if (len(benchmark) != len(dates) or benchmark.date.duplicated().any()
            or not set(pd.to_datetime(benchmark.date).dt.strftime('%Y%m%d')).issuperset(dates)
            or not benchmark.close.gt(0).all()):
        raise ValueError('EXACT_BENCHMARK_DATES_INVALID')
    after = old.ready_receipt(p, day, rq.clock())
    if after['status'] != 'PASS':
        raise ValueError('SUPPLIER_COMPLETENESS_LOST')
    save('postclose_membership', membership)
    save('benchmark', benchmark)
    result = dict(schema='POSTCLOSE_CANDIDATE_V1', target_date=day,
        status='PASS_CANDIDATE_NOT_PRODUCTION', observed_at=rq.clock().isoformat(),
        source='RQDATA', base_manifest=str(base_manifest), base_manifest_sha256=rq.sha(base_manifest),
        database_sha256=rq.sha(db), dates=dates, quality=quality,
        membership_counts={k:len(v) for k,v in sets.items()},
        before=before, after=after, factor_events=sum(len(f) for f in factors),
        historical_observation_times_not_inferred=True, preopen_pit=False,
        corporate_action_terms_qualified=False, volume_unit=base.meta['volume_unit'],
        production_activated=False, scheduler_installed=False)
    # Completion manifest is last; failed captures never replace a complete candidate.
    local.write_json(out / 'manifest.json', result)
    return result


if __name__ == '__main__':
    a = argparse.ArgumentParser(description=__doc__)
    a.add_argument('--date', required=True)
    a.add_argument('--base-manifest', required=True)
    a.add_argument('--output', required=True)
    a.add_argument('--batch-size', type=int, default=100)
    args = a.parse_args()
    try:
        if not 1 <= args.batch_size <= 200: raise ValueError('BATCH_SIZE_OUT_OF_RANGE')
        r = run(args.date, args.base_manifest, args.output, batch_size=args.batch_size)
        print(json.dumps(r, ensure_ascii=False, default=str))
    except Exception as e:
        print(json.dumps({'status':'BLOCKED', 'source':'RQDATA', 'target_date':args.date,
                          'source_step':SOURCE_STEP, 'error_type':type(e).__name__,
                          'reason':str(e) if isinstance(e, (ValueError, rq.InputBlocked))
                          else 'SOURCE_CALL_FAILED_NO_PRODUCTION_MUTATION'}))
        raise SystemExit(3)
