"""Manual isolated preopen/postclose data entry. No task installation or production writes."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import shutil

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pandas as pd
from core import p0_rq_input as rq
from scripts import collect_postclose_candidate as close
from scripts import run_postclose_local_candidate as local


def output_root(value):
    p = Path(value).absolute()
    if p.resolve() != p or not p.is_relative_to(ROOT/'reports') or p == ROOT/'reports':
        raise ValueError('ISOLATED_REPORTS_OUTPUT_REQUIRED')
    return p


def preopen(day, output, provider=None):
    root = output_root(output)
    now = pd.Timestamp(rq.clock())
    rq.require_trading_date(day)
    if now.strftime('%Y%m%d') != day or now.strftime('%H%M%S') >= '092000':
        raise ValueError('ACTUAL_SAME_DAY_BEFORE_0920_REQUIRED')
    manifests = root/'pit/manifests'
    if list(manifests.glob(day+'T*_preopen.json')):
        local.membership({'pit_root':str(manifests)}, day)
        return {'status':'GREEN_PIT', 'reused':True, 'production':False}
    p = provider or close.bounded_provider()
    started = rq.clock().isoformat()
    capture = pd.Timestamp(started).strftime('%Y%m%dT%H%M%S+0800_preopen')
    frame = rq.membership_frame(p, started, capture)
    identity = close.old.normalize(p.all_instruments(type='CS', date=day))
    if len(identity) < 3000 or identity.order_book_id.duplicated().any():
        raise ValueError('PREOPEN_IDENTITY_INCOMPLETE')
    finished = rq.clock().isoformat()
    if pd.Timestamp(finished).strftime('%H%M%S') >= '092000':
        raise ValueError('CAPTURE_FINISHED_AFTER_0920')
    dest = root/'pit/raw'/day/'preopen'/capture
    dest.mkdir(parents=True, exist_ok=False)
    path = dest/'index_membership_rqdata.parquet'
    frame.to_parquet(path, index=False)
    identity.to_parquet(dest/'identity.parquet', index=False)
    items = [dict(dataset='index_membership_'+code, source='RQDATA_COMPONENTS_PROVIDER',
                  rows=int(frame.index_code.eq(code).sum()), source_max_date=day,
                  health='FRESH', file_path=str(path), sha256=rq.sha(path))
             for code in ('000300.SH','000905.SH','000852.SH')]
    manifest = dict(phase='preopen', status='SUCCESS', observed_at=started,
                    finished_at=finished, items=items, capture_id=capture,
                    identity_sha256=rq.sha(dest/'identity.parquet'))
    manifests.mkdir(parents=True, exist_ok=True)
    local.write_json(manifests/(capture+'.json'), manifest)
    local.membership({'pit_root':str(manifests)}, day)
    return {'status':'GREEN_PIT', 'members':len(frame), 'identity':len(identity),
            'capture_id':capture, 'production':False}


def postclose(day, output, base_manifest, pit_output, provider=None):
    root = output_root(output)
    stock = close.run(day, base_manifest, root/'stock', provider=provider)
    pit, reasons = False, []
    try:
        local.membership({'pit_root':str(output_root(pit_output)/'pit/manifests')}, day)
        pit = True
    except (Exception, SystemExit):
        reasons.append('EXACT_DAY_PREOPEN_PIT_NOT_QUALIFIED')
    # Candidate rows alone cannot promote unverified strategy representations.
    return dict(status='RED', stock=stock['status'], pit=pit,
                strategies={k:'BLOCKED_PENDING_CONSUMER_QUALIFICATION' for k in ('deep','ret120','funnel')},
                reasons=reasons+['MERGED_STRATEGY_INPUT_SEAL_NOT_YET_QUALIFIED'], production=False)


def merge_candidate(stock_output, live=False):
    """Bind accepted old history and exact captured deltas without a production pointer."""
    import duckdb
    src = output_root(stock_output)
    m = local.read_json(src/'manifest.json')
    close.verified_existing(src, m['target_date'], m['base_manifest'])
    dest = src/('merged_live' if live else 'merged')
    if (dest/'COMPLETE.json').exists():
        return rq.DailyInput(dest/'manifest.json', rq.profile())
    dest.mkdir(exist_ok=False)
    base = rq.DailyInput(m['base_manifest'], rq.profile())
    db = dest/'daily.duckdb'
    shutil.copyfile(base.db, db)
    with duckdb.connect(str(db), config={'temp_directory':''}) as c:
        c.execute("ATTACH '"+str(src/'increment.duckdb').replace("'","''")+"' AS delta (READ_ONLY)")
        for name in ('raw_daily','execution_status'):
            if c.execute('SELECT count(*) FROM '+name+' WHERE date>=?', [pd.Timestamp(m['dates'][0]).date()]).fetchone()[0]:
                raise ValueError('MERGE_DUPLICATE_OR_FUTURE_ROWS')
            c.execute('INSERT INTO '+name+' BY NAME SELECT * FROM delta.'+name)
        c.execute('INSERT INTO suspension SELECT order_book_id,date,status_suspended FROM delta.execution_status WHERE status_suspended')
        c.execute('CREATE OR REPLACE TABLE identity_observed_today AS SELECT * FROM delta.identity_observed_today')
        c.execute('INSERT INTO security_identity BY NAME SELECT * FROM delta.identity_observed_today n WHERE NOT EXISTS (SELECT 1 FROM security_identity o WHERE o.order_book_id=n.order_book_id)')
        if m['factor_events']:
            c.execute('INSERT INTO factors SELECT order_book_id,ex_date,ex_cum_factor FROM delta.factors')
        actions = src/'deep_actions/captured_actions.parquet'
        if live and actions.exists():
            frame = pd.read_parquet(actions)
            c.register('captured_frame', frame)
            c.execute('CREATE TABLE IF NOT EXISTS captured_actions AS SELECT * FROM captured_frame WHERE false')
            c.execute('INSERT INTO captured_actions SELECT * FROM captured_frame n WHERE NOT EXISTS (SELECT 1 FROM captured_actions o WHERE o.order_book_id=n.order_book_id AND o.ex_date=n.ex_date)')
        action_meta = close.old.bind_local_actions(c, pd.Timestamp(m['target_date']).date(), rq.clock())
        if live and action_meta.get('corporate_action_missing'):
            if rq.clock().strftime('%H%M')>='1715':
                raise ValueError('ACTION_GAP_AFTER_FINAL_DATA_DEADLINE')
            provider=close.bounded_provider()
            missing=action_meta['corporate_action_missing']
            for event_day in sorted({x['ex_date'] for x in missing}):
                work=src/'deep_actions'/event_day
                work.mkdir(parents=True,exist_ok=True)
                ids=sorted({x['order_book_id'] for x in missing if x['ex_date']==event_day})
                close.old.capture_action_terms(provider,ids,pd.Timestamp(event_day).date()-pd.Timedelta(days=1),event_day,work)
                path=work/'captured_actions.parquet'
                if path.exists():
                    c.register('new_actions',pd.read_parquet(path))
                    c.execute('CREATE TABLE IF NOT EXISTS captured_actions AS SELECT * FROM new_actions WHERE false')
                    c.execute('INSERT INTO captured_actions SELECT * FROM new_actions n WHERE NOT EXISTS (SELECT 1 FROM captured_actions o WHERE o.order_book_id=n.order_book_id AND o.ex_date=n.ex_date)')
            action_meta=close.old.bind_local_actions(c,pd.Timestamp(m['target_date']).date(),rq.clock())
        c.execute('CHECKPOINT')
    meta = dict(base.meta, asof=str(pd.Timestamp(m['target_date']).date()),
                business_date=str(pd.Timestamp(m['target_date']).date()), availability=rq.clock().isoformat(),
                version='asof_'+m['target_date']+'_'+rq.sha(db)[:12], database='daily.duckdb',
                database_sha256=rq.sha(db), activated_in_production=False,
                late_historical_increment_dates=m['dates'], **action_meta)
    meta['supplier_complete_observed_at'] = m['after']['available_at']
    for name,receipt in [('supplier_ready.json',m['before']),('supplier_ready_after.json',m['after'])]:
        local.write_json(dest/name,receipt)
    local.write_json(dest/'source_metadata.json', meta)
    local.write_json(dest/'manifest.json', dict(version=meta['version'],files=[
        dict(path=p.name,sha256=rq.sha(p)) for p in (db,dest/'source_metadata.json',dest/'supplier_ready.json',dest/'supplier_ready_after.json')]))
    local.write_json(dest/'COMPLETE.json', dict(manifest_sha256=rq.sha(dest/'manifest.json')))
    return rq.DailyInput(dest/'manifest.json', rq.profile())


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--phase', required=True, choices=['preopen','postclose'])
    p.add_argument('--date', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--base-manifest')
    p.add_argument('--pit-output')
    a = p.parse_args()
    root = output_root(a.output)
    receipt = root/(a.phase+'_'+a.date+'_result.json')
    if receipt.exists():
        raise ValueError('RESULT_EXISTS_PRESERVED_USE_NEW_ATTEMPT_OUTPUT')
    try:
        r = preopen(a.date, root) if a.phase == 'preopen' else postclose(a.date, root, a.base_manifest, a.pit_output)
    except (Exception, SystemExit) as e:
        r = dict(status='RED', source='RQDATA', date=a.date, error_type=type(e).__name__,
                 source_step=close.SOURCE_STEP, production=False)
    root.mkdir(parents=True, exist_ok=True)
    local.write_json(receipt, r)
    print(json.dumps(r, default=str))
    return 0 if r['status'].startswith('GREEN') else 3


if __name__ == '__main__':
    raise SystemExit(main())
