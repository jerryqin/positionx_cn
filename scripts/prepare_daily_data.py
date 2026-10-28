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
from core.rqdata_identity import capture_identity
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
    deadline = now.normalize() + pd.Timedelta(hours=9, minutes=20)
    identity = capture_identity(p, day, root/'identity', deadline=deadline)
    identity_binding = identity.attrs.get('identity_capture')
    if not isinstance(identity_binding, dict) or identity_binding.get('business_date') != day:
        raise ValueError('PREOPEN_IDENTITY_CAPTURE_BINDING_MISSING')
    identity = close.old.normalize(identity)
    if len(identity) < 3000 or identity.order_book_id.duplicated().any():
        raise ValueError('PREOPEN_IDENTITY_INCOMPLETE')
    if rq.clock().strftime('%H%M%S') >= '092000':
        raise ValueError('IDENTITY_CAPTURE_FINISHED_AFTER_0920')
    started = rq.clock().isoformat()
    capture = pd.Timestamp(started).strftime('%Y%m%dT%H%M%S+0800_preopen')
    frame = rq.membership_frame(p, started, capture)
    if getattr(p, 'source_version', None):
        frame['source_version'] = p.source_version
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
                    identity_sha256=rq.sha(dest/'identity.parquet'),
                    identity_capture=identity_binding,
                    identity_observed_range=identity_binding.get('observed_range'))
    manifests.mkdir(parents=True, exist_ok=True)
    local.write_json(manifests/(capture+'.json'), manifest)
    local.membership({'pit_root':str(manifests)}, day)
    return {'status':'GREEN_PIT', 'members':len(frame), 'identity':len(identity),
            'capture_id':capture, 'production':False, 'identity_capture':identity_binding}


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


def merge_benchmark(con, capture, previous=None):
    """Append the captured native index unchanged; unknown old provenance stays unknown."""
    import numpy as np
    fields = ['order_book_id', 'date', 'open', 'high', 'low', 'close', 'prev_close']
    frame = con.execute('SELECT * FROM delta.benchmark').df()
    days = pd.to_datetime(frame.date, errors='coerce')
    values = frame[fields[2:]].to_numpy(dtype=float)
    if (capture.get('source') != 'RQDATA' or frame.empty
            or frame.duplicated(['order_book_id', 'date']).any()
            or not frame.order_book_id.eq('000905.XSHG').all()
            or days.isna().any() or not days.eq(days.dt.normalize()).all()
            or sorted(days.dt.strftime('%Y%m%d')) != sorted(capture['dates'])
            or not np.isfinite(values).all() or (values <= 0).any()
            or (frame.high < frame[['open', 'close', 'low']].max(axis=1)).any()
            or (frame.low > frame[['open', 'close', 'high']].min(axis=1)).any()):
        raise ValueError('MERGE_BENCHMARK_EXACT_NATIVE_ROWS_INVALID')
    columns = dict(source='VARCHAR', price_unit='VARCHAR', observed_at='VARCHAR',
                   source_database_sha256='VARCHAR')
    con.execute('CREATE TABLE IF NOT EXISTS benchmark AS SELECT * FROM delta.benchmark WHERE false')
    existing = {r[0] for r in con.execute('DESCRIBE benchmark').fetchall()}
    for name, dtype in columns.items():
        if name not in existing:
            # No UPDATE: historical rows without provenance remain NULL.
            con.execute(f'ALTER TABLE benchmark ADD COLUMN {name} {dtype}')
    frame['source'] = 'RQDATA'
    frame['price_unit'] = 'INDEX_POINTS'
    observed_at = capture.get('benchmark_observed_at', capture['observed_at'])
    frame['observed_at'] = observed_at
    frame['source_database_sha256'] = capture['database_sha256']
    con.register('captured_benchmark', frame)
    if con.execute('SELECT count(*) FROM (SELECT order_book_id,date FROM benchmark GROUP BY 1,2 HAVING count(*)>1)').fetchone()[0]:
        raise ValueError('MERGE_BENCHMARK_DUPLICATE_KEY')
    equal = ' AND '.join(f'o.{name} IS NOT DISTINCT FROM n.{name}' for name in fields[2:]+list(columns))
    if con.execute('''SELECT count(*) FROM benchmark o JOIN captured_benchmark n
        USING(order_book_id,date) WHERE NOT ('''+equal+')').fetchone()[0]:
        raise ValueError('MERGE_BENCHMARK_CONFLICT')
    con.execute('''INSERT INTO benchmark BY NAME SELECT n.* FROM captured_benchmark n
        WHERE NOT EXISTS (SELECT 1 FROM benchmark o WHERE o.order_book_id=n.order_book_id AND o.date=n.date)''')
    previous = previous or {}
    bindings = dict(previous.get('date_bindings', {}))
    if not bindings and previous.get('source') == 'RQDATA' and previous.get('source_database_sha256'):
        bindings = {d: dict(source_database_sha256=previous['source_database_sha256'],
                            observed_at=previous['observed_at']) for d in previous['dates']}
    for day in capture['dates']:
        binding = dict(source_database_sha256=capture['database_sha256'], observed_at=observed_at)
        if day in bindings and bindings[day] != binding:
            raise ValueError('MERGE_BENCHMARK_DATE_BINDING_CONFLICT')
        bindings[day] = binding
    return dict(table='benchmark', source='RQDATA', symbol='000905.SH', order_book_id='000905.XSHG',
                dates=capture['dates'], price_unit='INDEX_POINTS', observed_at=observed_at,
                source_database_sha256=capture['database_sha256'], date_bindings=bindings, no_value_conversion=True)


def merge_factors(con, capture):
    """Append native event fields to the existing raw and calculation contracts."""
    import numpy as np
    frame = con.execute('SELECT * FROM delta.factors').df()
    keys = ['order_book_id', 'ex_date']
    if (frame.empty or len(frame) != capture['factor_events'] or frame.duplicated(keys).any()
            or not pd.to_datetime(frame.ex_date).dt.strftime('%Y%m%d').isin(capture['dates']).all()
            or not np.isfinite(frame.ex_cum_factor).all() or not frame.ex_cum_factor.gt(0).all()):
        raise ValueError('MERGE_FACTOR_EXACT_NATIVE_EVENTS_INVALID')
    if 'rice_create_tm' in frame and not pd.api.types.is_integer_dtype(frame.rice_create_tm):
        raise ValueError('FACTOR_RAW_CREATION_TYPE_CHANGED')
    if 'factor_raw_versions' not in {r[0] for r in con.execute('SHOW TABLES').fetchall()}:
        raise ValueError('MERGE_FACTOR_RAW_CONTRACT_TABLE_MISSING')
    if con.execute('''SELECT count(*) FROM factors o JOIN delta.factors n
            ON o.order_book_id=n.order_book_id AND o.ex_date=n.ex_date''').fetchone()[0]:
        raise ValueError('MERGE_FACTOR_DUPLICATE_OR_CONFLICT')
    # All vendor fields remain in the retained capture. Publish the columns in
    # the existing raw contract, with missing native times left NULL.
    if 'rice_create_tm_http_native' in frame:
        # HTTP exposes timezone-free text. Retain it verbatim without mapping
        # it into the older integer clock or assigning a timezone.
        con.execute('ALTER TABLE factor_raw_versions ADD COLUMN IF NOT EXISTS rice_create_tm_http_native VARCHAR')
    raw_columns = [r[0] for r in con.execute('DESCRIBE factor_raw_versions').fetchall()]
    columns = ','.join(name for name in raw_columns if name in frame.columns)
    con.execute('INSERT INTO factor_raw_versions BY NAME SELECT '+columns+' FROM delta.factors')
    con.execute('INSERT INTO factors SELECT order_book_id,ex_date,ex_cum_factor FROM delta.factors')


def core_factor_coverage(con, capture, binding):
    """Bind exact daily request coverage, including legitimately empty events."""
    if capture.get('dates') != [capture.get('target_date')]:
        raise ValueError('CORE_FACTOR_COVERAGE_EXACT_DAY_REQUIRED')
    try:
        covered = con.execute('SELECT order_book_id FROM delta.factor_capture_coverage').df()
        identity = con.execute('SELECT order_book_id FROM delta.identity_observed_today').df()
    except Exception as exc:
        raise ValueError('CORE_FACTOR_CAPTURE_COVERAGE_MISSING') from exc
    if (identity.empty or identity.order_book_id.isna().any()
            or identity.order_book_id.duplicated().any()
            or covered.order_book_id.isna().any() or covered.order_book_id.duplicated().any()
            or set(covered.order_book_id) != set(identity.order_book_id)):
        raise ValueError('CORE_FACTOR_CAPTURE_COVERAGE_INCOMPLETE_OR_CONFLICT')
    day = str(pd.Timestamp(capture['target_date']).date())
    return dict(status='COMPLETE', source='RQDATA', dates=list(capture['dates']),
        business_date=day, start_date=day, end_date=day,
        order_book_ids=sorted(covered.order_book_id),
        capture_manifest_sha256=binding['capture_manifest_sha256'],
        increment_database_sha256=binding['increment_database_sha256'],
        semantics='Actual completed get_ex_factor request coverage; event absence alone is not coverage')


def merge_candidate(stock_output, live=False, output=None, *, scope='P0_COMPLETE'):
    """Bind accepted old history and exact captured deltas without a production pointer."""
    import duckdb
    src = output_root(stock_output)
    m = local.read_json(src/'manifest.json')
    if scope not in ('P0_COMPLETE', 'DAILY_SIGNALS_V1', 'CORE_MODELS_V1'):
        raise ValueError('MERGE_UNKNOWN_QUALIFICATION_SCOPE')
    research_only = scope == 'DAILY_SIGNALS_V1'
    if m.get('qualification_scope', 'P0_COMPLETE') != scope:
        raise ValueError('MERGE_CAPTURE_QUALIFICATION_SCOPE_MISMATCH')
    if research_only and live:
        raise ValueError('DAILY_RESEARCH_MERGE_CANNOT_FETCH_EXECUTION_INPUTS')
    close.verified_existing(src, m['target_date'], m['base_manifest'], scope=scope)
    dest = output_root(output) if output is not None else src/('merged_live' if live else 'merged')
    binding = dict(capture_manifest_sha256=rq.sha(src/'manifest.json'),
                   increment_database_sha256=m['database_sha256'], base_manifest_sha256=m['base_manifest_sha256'])
    if (dest/'COMPLETE.json').exists():
        reader = rq.DailyInput(dest/'manifest.json', rq.profile(), scope=scope)
        if reader.meta.get('merge_binding') != binding:
            raise ValueError('MERGE_EXISTING_BINDING_MISMATCH_USE_NEW_OUTPUT')
        return reader
    dest.mkdir(parents=True, exist_ok=False)
    base_scope=local.read_json(Path(m['base_manifest']).parent/'source_metadata.json').get('qualification_scope','P0_COMPLETE')
    base = rq.DailyInput(m['base_manifest'], rq.profile(),scope=base_scope)
    db = dest/'daily.duckdb'
    shutil.copyfile(base.db, db)
    with duckdb.connect(str(db), config={'temp_directory':''}) as c:
        c.execute("ATTACH '"+str(src/'increment.duckdb').replace("'","''")+"' AS delta (READ_ONLY)")
        c.execute('BEGIN TRANSACTION')
        if scope=='CORE_MODELS_V1':
            factor_coverage = core_factor_coverage(c, m, binding)
            for name,dtype in [('rule_price_tick','DOUBLE'),('tick_rule_evidence','VARCHAR'),('tick_source','VARCHAR')]:
                c.execute('ALTER TABLE execution_status ADD COLUMN IF NOT EXISTS '+name+' '+dtype)
        for name in ('raw_daily','execution_status'):
            if c.execute('SELECT count(*) FROM '+name+' WHERE date>=?', [pd.Timestamp(m['dates'][0]).date()]).fetchone()[0]:
                raise ValueError('MERGE_DUPLICATE_OR_FUTURE_ROWS')
            c.execute('INSERT INTO '+name+' BY NAME SELECT * FROM delta.'+name)
        c.execute('INSERT INTO suspension SELECT order_book_id,date,status_suspended FROM delta.execution_status WHERE status_suspended')
        c.execute('CREATE OR REPLACE TABLE identity_observed_today AS SELECT * FROM delta.identity_observed_today')
        c.execute('INSERT INTO security_identity BY NAME SELECT * FROM delta.identity_observed_today n WHERE NOT EXISTS (SELECT 1 FROM security_identity o WHERE o.order_book_id=n.order_book_id)')
        if m['factor_events']:
            merge_factors(c, m)
        actions = src/'deep_actions/captured_actions.parquet'
        if actions.exists():
            frame = pd.read_parquet(actions)
            c.register('captured_frame', frame)
            c.execute('CREATE TABLE IF NOT EXISTS captured_actions AS SELECT * FROM captured_frame WHERE false')
            c.execute('INSERT INTO captured_actions SELECT * FROM captured_frame n WHERE NOT EXISTS (SELECT 1 FROM captured_actions o WHERE o.order_book_id=n.order_book_id AND o.ex_date=n.ex_date)')
        action_meta = close.old.bind_local_actions(c, pd.Timestamp(m['target_date']).date(), rq.clock())
        if research_only:
            coverage = m.get('factor_capture_coverage', {})
            if (coverage.get('status') != 'COMPLETE'
                    or not set(action_meta.get('corporate_action_symbols', [])).issubset(
                        set(coverage.get('order_book_ids', [])))):
                action_meta.update(corporate_action_coverage_complete=False,
                    corporate_action_reason='EXACT_DAY_FACTOR_EVENT_COVERAGE_UNKNOWN')
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
        benchmark_meta = merge_benchmark(c, m, base.meta.get('benchmark'))
        total_rows, total_symbols = c.execute('SELECT count(*),count(DISTINCT order_book_id) FROM raw_daily').fetchone()
        appended_rows = c.execute('SELECT count(*) FROM delta.raw_daily').fetchone()[0]
        current_symbols = c.execute('SELECT count(DISTINCT order_book_id) FROM raw_daily WHERE date=?',
                                    [pd.Timestamp(m['target_date']).date()]).fetchone()[0]
        c.execute('COMMIT')
        c.execute('CHECKPOINT')
    if rq.sha(src/'increment.duckdb') != m['database_sha256'] or rq.sha(base.root/'manifest.json') != m['base_manifest_sha256']:
        raise ValueError('MERGE_SOURCE_CHANGED')
    sealed_at = rq.clock().isoformat()
    meta = dict(base.meta, asof=str(pd.Timestamp(m['target_date']).date()),
                business_date=str(pd.Timestamp(m['target_date']).date()), availability=sealed_at,
                ingested_at=sealed_at, available_at=sealed_at,
                version_time_semantics='actual merged-version seal time; not supplier or historical availability',
                counts=dict(raw_rows=total_rows, symbols=total_symbols, appended_rows=appended_rows,
                            business_date_symbols=current_symbols),
                base_version_metadata={**{k: base.meta.get(k) for k in
                                          ('version', 'asof', 'ingested_at', 'available_at', 'availability',
                                           'counts', 'base_manifest_sha256')},
                                       'manifest_sha256': m['base_manifest_sha256']},
                base_manifest_sha256=m['base_manifest_sha256'],
                version='asof_'+m['target_date']+'_'+rq.sha(db)[:12], database='daily.duckdb',
                database_sha256=rq.sha(db), activated_in_production=False,
                late_historical_increment_dates=m['dates'], merge_binding=binding,
                benchmark=benchmark_meta, identity_source_target_date=m['target_date'],
                identity_observed_no_later_than=m['observed_at'],
                identity_time_semantics='capture completion upper bound; no historical observation time inferred',
                **action_meta)
    meta['supplier_complete_observed_at'] = m['after']['available_at']
    if research_only:
        meta.update(qualification_scope='DAILY_SIGNALS_V1', quality_status='DAILY_SIGNALS_PASS_V1',
            engineering_only=True, full_p0_pass=False, execution_qualified=False,
            model_continuation_qualified=False,
            admission_version='DAILY_SIGNALS_V1',
            admission_reason='Price/trade/status signal inputs qualified independently; quote increments and complete P0 execution remain unqualified',
            signal_input_evidence=m.get('saved_input_evidence', {}),
            factor_capture_coverage=m.get('factor_capture_coverage', {'status':'UNKNOWN'}),
            missing_execution_inputs=['price_tick', 'tick_effective_date_proof'],
            supplier_final_completeness=m['after']['status'])
    elif scope=='CORE_MODELS_V1':
        meta.update(qualification_scope='CORE_MODELS_V1',quality_status='CORE_MODELS_PASS_V1',
            engineering_only=True,full_p0_pass=False,execution_qualified=True,
            model_continuation_qualified=True,
            factor_capture_coverage=factor_coverage,
            execution_qualified_symbols=m['execution_qualified_symbols'],
            execution_evidence_contract='RQ_OR_EXCHANGE_RULE_V1')
    if m.get('identity_capture'):
        meta['identity_capture'] = m['identity_capture']
        meta['identity_observed_range'] = m['identity_capture'].get('observed_range')
        meta['identity_time_semantics'] = 'actual per-batch identity observation range; not an atomic snapshot; separate from version seal time'
    for name,receipt in [('supplier_ready.json',m['before']),('supplier_ready_after.json',m['after'])]:
        local.write_json(dest/name,receipt)
    local.write_json(dest/'source_metadata.json', meta)
    local.write_json(dest/'manifest.json', dict(version=meta['version'],files=[
        dict(path=p.name,sha256=rq.sha(p)) for p in (db,dest/'source_metadata.json',dest/'supplier_ready.json',dest/'supplier_ready_after.json')]))
    local.write_json(dest/'COMPLETE.json', dict(manifest_sha256=rq.sha(dest/'manifest.json')))
    try:
        return rq.DailyInput(dest/'manifest.json', rq.profile(), scope=scope)
    except BaseException:
        (dest/'COMPLETE.json').unlink()
        raise


def seal(day, stock_output, output):
    """Replay the existing publication contract into a new reports/TEMP asset root."""
    from scripts.postclose_data_admission_v1 import audit_stock_asset
    root = output_root(output)
    if root.exists():
        raise ValueError('SEAL_OUTPUT_EXISTS_USE_NEW_ATTEMPT')
    capture = local.read_json(output_root(stock_output)/'manifest.json')
    if capture['target_date'] != day:
        raise ValueError('SEAL_EXACT_CAPTURE_DATE_REQUIRED')
    reader = merge_candidate(stock_output, output=root/'TEMP/published')
    try:
        audit = audit_stock_asset(reader, day)
        consumer = rq.DailyInput(reader.root/'manifest.json', rq.profile())
        rows = consumer.benchmark_history('000905.SH', capture['dates'])
        result = dict(status='PASS_ISOLATED_PUBLICATION', date=day, production=False,
                      published=str(consumer.root), stock_audit=audit,
                      publication_mode='ISOLATED_SEALED_COPY_NO_PRODUCTION_ACTIVATION',
                      benchmark_readback=rows.to_dict('records'),
                      manifest=str(consumer.root/'manifest.json'),
                      manifest_sha256=rq.sha(consumer.root/'manifest.json'))
        local.write_json(root/'publication.json', result)
        return result
    except BaseException:
        (reader.root/'COMPLETE.json').unlink()
        raise


def qualify_core_models(source_manifest, output, execution_symbols):
    """Create a new model-input version; retain all native missing tick evidence."""
    import duckdb
    import pandas as pd
    from scripts.postclose_data_admission_v1 import audit_core_model_asset
    source=rq.DailyInput(source_manifest,rq.profile(),scope='DAILY_SIGNALS_V1')
    day=pd.Timestamp(source.meta['asof']).strftime('%Y%m%d')
    out=output_root(output)
    symbols=sorted(set(execution_symbols))
    binding=dict(parent_manifest_sha256=rq.sha(source.root/'manifest.json'),execution_symbols=symbols,
                 qualification_scope='CORE_MODELS_V1')
    if (out/'COMPLETE.json').exists():
        reader=rq.DailyInput(out/'manifest.json',rq.profile(),scope='CORE_MODELS_V1')
        if reader.meta.get('qualification_binding')!=binding:raise ValueError('CORE_QUALIFICATION_BINDING_CHANGED')
        audit_core_model_asset(reader,day,execution_symbols=symbols)
        return reader
    out.mkdir(parents=True,exist_ok=False)
    db=out/'daily.duckdb';shutil.copyfile(source.db,db)
    with duckdb.connect(str(db),config={'temp_directory':''}) as con:
        identity=con.execute('SELECT * FROM identity_observed_today').df()
        status=con.execute('SELECT * FROM execution_status WHERE date=?',[pd.Timestamp(day).date()]).df()
        derived,proofs=close.apply_execution_rules(status,identity,symbols,day)
        con.execute('BEGIN TRANSACTION')
        for name,dtype in [('rule_price_tick','DOUBLE'),('tick_rule_evidence','VARCHAR'),('tick_source','VARCHAR')]:
            con.execute('ALTER TABLE execution_status ADD COLUMN IF NOT EXISTS '+name+' '+dtype)
        con.register('derived',derived)
        con.execute('''UPDATE execution_status e SET rule_price_tick=n.rule_price_tick,
            tick_rule_evidence=n.tick_rule_evidence,tick_source=n.tick_source FROM derived n
            WHERE e.order_book_id=n.order_book_id AND e.date=n.date''')
        con.execute('COMMIT');con.execute('CHECKPOINT')
    now=rq.clock().isoformat()
    meta=dict(source.meta,qualification_scope='CORE_MODELS_V1',quality_status='CORE_MODELS_PASS_V1',
        engineering_only=True,full_p0_pass=False,execution_qualified=True,model_continuation_qualified=True,
        execution_qualified_symbols=symbols,execution_rule_bindings=proofs,
        execution_evidence_contract='RQ_OR_EXCHANGE_RULE_V1',qualification_binding=binding,
        qualification_parent=dict(manifest=str(source.root/'manifest.json'),manifest_sha256=binding['parent_manifest_sha256'],
            version=source.meta['version'],qualification_scope='DAILY_SIGNALS_V1',availability=source.meta['availability']),
        availability=now,ingested_at=now,available_at=now,database_sha256=rq.sha(db),
        version='asof_'+day+'_'+rq.sha(db)[:12],activated_in_production=False,
        admission_reason='Explicit actual model execution set validated against versioned exchange rules; native quote-unit NULLs preserved')
    for name in ('supplier_ready.json','supplier_ready_after.json'):
        shutil.copyfile(source.root/name,out/name)
    local.write_json(out/'source_metadata.json',meta)
    local.write_json(out/'manifest.json',dict(version=meta['version'],files=[dict(path=p.name,sha256=rq.sha(p))
        for p in (db,out/'source_metadata.json',out/'supplier_ready.json',out/'supplier_ready_after.json')]))
    local.write_json(out/'COMPLETE.json',dict(manifest_sha256=rq.sha(out/'manifest.json')))
    try:
        reader=rq.DailyInput(out/'manifest.json',rq.profile(),scope='CORE_MODELS_V1')
        audit_core_model_asset(reader,day,execution_symbols=symbols)
        return reader
    except BaseException:
        (out/'COMPLETE.json').unlink()
        raise


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--phase', required=True, choices=['preopen','postclose','seal'])
    p.add_argument('--date', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--base-manifest')
    p.add_argument('--pit-output')
    p.add_argument('--stock-output', help='Existing complete capture; seal never queries a provider')
    a = p.parse_args()
    root = output_root(a.output)
    receipt = root/(a.phase+'_'+a.date+'_result.json')
    if receipt.exists():
        raise ValueError('RESULT_EXISTS_PRESERVED_USE_NEW_ATTEMPT_OUTPUT')
    try:
        if a.phase == 'seal':
            local.OfflineBoundary(root).install()
            r = seal(a.date, a.stock_output, root)
        else:
            r = preopen(a.date, root) if a.phase == 'preopen' else postclose(a.date, root, a.base_manifest, a.pit_output)
    except (Exception, SystemExit) as e:
        r = dict(status='RED', source='RQDATA', date=a.date, error_type=type(e).__name__,
                 source_step=close.SOURCE_STEP, production=False)
    root.mkdir(parents=True, exist_ok=True)
    local.write_json(receipt, r)
    print(json.dumps(r, default=str))
    return 0 if r['status'].startswith('GREEN') or r['status']=='PASS_ISOLATED_PUBLICATION' else 3


if __name__ == '__main__':
    raise SystemExit(main())
