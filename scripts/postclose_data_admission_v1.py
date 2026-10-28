"""Read-only daily data admission and Forward-start decision. No acquisition or ledger writes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
from scripts import run_postclose_local_candidate as local

MARKET = Path('D:/positionx_data/catalog/market_data.duckdb')
RESEARCH = ROOT / 'data/research/positionx_research.duckdb'
LEGACY = Path('D:/TEMP/positionx_qmt_primary_replacement_20261025/run_20261025T162032_65afdb6e')
PRIOR = ROOT / 'reports/postclose_local_20261023_20261026_verified'


def state(ready, reasons=(), **evidence):
    return {'ready': bool(ready), 'status': 'READY' if ready else 'BLOCKED',
            'reasons': list(reasons), **evidence}


def compute_permissions(gates):
    core = gates['STOCK_DATA_READY']['ready'] and gates['PIT_READY']['ready']
    inputs = gates['STOCK_DATA_READY'].get('strategy_inputs', {})
    return {'deep_independent_computation': core and inputs.get('deep', {}).get('ready', False),
            'ret120_independent_computation': core and inputs.get('ret120', {}).get('ready', False),
            'benchmark_comparison': core and gates['BENCHMARK_READY']['ready'],
            'funnel_structure_computation': core and gates['FUNNEL_STRUCTURE_READY']['ready'],
            'formal_ledger_advance': False,
            'explanation': 'Data readiness alone cannot authorize a Forward start or ledger advance.'}


def audit_stock_records(bars, statuses, identity, day, meta, now, *, require_tick=True):
    """The existing producer quality gate plus exact-day identity/time bindings."""
    import pandas as pd
    from scripts.collect_p0_rq_close_v01 import validate_day
    target = pd.Timestamp(day).date()
    if meta.get('asof') != target.isoformat() or meta.get('business_date') != target.isoformat():
        raise ValueError('STOCK_EXACT_DATE_BINDING_MISSING')
    if meta.get('raw_adjustment') != 'none' or not meta.get('volume_unit'):
        raise ValueError('RAW_PRICE_OR_VOLUME_UNIT_BINDING_MISSING')
    if identity.empty or identity.order_book_id.duplicated().any():
        raise ValueError('IDENTITY_EMPTY_OR_DUPLICATE')
    for name in ('symbol', 'listed_date', 'special_type', 'round_lot'):
        if name not in identity or identity[name].isna().any():
            raise ValueError('IDENTITY_FIELD_MISSING:' + name)
    if (identity.round_lot <= 0).any() or (identity.round_lot % 1 != 0).any():
        raise ValueError('MINIMUM_LOT_INVALID')
    result = validate_day(bars, statuses, set(identity.order_book_id), target, require_tick=require_tick)
    for name in (('status_available_at', 'tick_available_at') if require_tick else ('status_available_at',)):
        times = pd.to_datetime(statuses[name], utc=True, errors='coerce')
        if times.isna().any() or (times > pd.Timestamp(now)).any() or not times.dt.tz_convert('Asia/Shanghai').dt.date.eq(target).all():
            raise ValueError('STATUS_OR_TICK_EXACT_DAY_TIME_INVALID')
    return {**result, 'raw_price_basis': 'NONE', 'volume_unit_retained': meta['volume_unit'],
            'amount_field_retained': 'total_turnover', 'no_unit_conversion': True,
            'identity_rows': len(identity), 'zero_trade_rows_are_not_fills': True}


def audit_stock_asset(reader, day):
    import pandas as pd
    from core import p0_rq_input as rq
    if (reader.meta.get('qualification_scope') == 'DAILY_SIGNALS_V1'
            or reader.meta.get('quality_status') != 'PASS' or reader.meta.get('engineering_only') is not False):
        raise ValueError('STOCK_ASSET_NOT_QUALIFIED')
    with local.read_db(reader.db) as con:
        bars = con.execute('SELECT * FROM raw_daily WHERE date=?', [pd.Timestamp(day).date()]).df()
        statuses = con.execute('SELECT * FROM execution_status WHERE date=?', [pd.Timestamp(day).date()]).df()
        identity = con.execute('SELECT * FROM identity_observed_today').df()
    quality = audit_stock_records(bars, statuses, identity, day, reader.meta, rq.clock())
    receipts = []
    for name in ('supplier_ready.json', 'supplier_ready_after.json'):
        r = local.read_json(reader.root / name)
        available = pd.Timestamp(r.get('available_at'))
        if (r.get('status') != 'PASS' or r.get('business_date') != pd.Timestamp(day).date().isoformat()
                or available.tzinfo is None or available > pd.Timestamp(rq.clock())
                or available.tz_convert(rq.CST).strftime('%Y%m%d') != day
                or available.tz_convert(rq.CST).strftime('%H%M') < '1530'):
            raise ValueError('SUPPLIER_COMPLETENESS_RECEIPT_INVALID')
        receipts.append({'path': str(reader.root / name), 'sha256': rq.sha(reader.root / name)})
    contracts = {}
    for adjustment in ('front', 'back_ratio'):
        try:
            reader.require_contract(adjustment)
            contracts[adjustment] = 'EXISTING_CONTRACT_VERIFIED'
        except Exception as e:
            contracts[adjustment] = str(e)
    return {'date': day, 'version': reader.meta['version'], 'database': str(reader.db),
            'database_sha256': rq.sha(reader.db), 'manifest_sha256': rq.sha(reader.root / 'manifest.json'),
            'quality': quality, 'strategy_contracts': contracts, 'supplier_receipts': receipts,
            'availability': reader.meta['availability']}


def audit_daily_research_asset(reader, day):
    """Exact observed daily inputs only; this never grants P0 execution admission."""
    import pandas as pd
    from core import p0_rq_input as rq
    meta = reader.meta
    if (meta.get('qualification_scope') != 'DAILY_SIGNALS_V1'
            or meta.get('quality_status') != 'DAILY_SIGNALS_PASS_V1'
            or meta.get('engineering_only') is not True or meta.get('full_p0_pass') is not False
            or meta.get('execution_qualified') is not False):
        raise ValueError('DAILY_SIGNAL_RESEARCH_SCOPE_REQUIRED')
    with local.read_db(reader.db) as con:
        bars = con.execute('SELECT * FROM raw_daily WHERE date=?', [pd.Timestamp(day).date()]).df()
        statuses = con.execute('SELECT * FROM execution_status WHERE date=?', [pd.Timestamp(day).date()]).df()
        identity = con.execute('SELECT * FROM identity_observed_today').df()
    quality = audit_stock_records(bars, statuses, identity, day, meta, rq.clock(), require_tick=False)
    return dict(date=day, version=meta['version'], database=str(reader.db),
        qualification_scope='DAILY_SIGNALS_V1', full_p0_pass=False, execution_qualified=False,
        quality=quality, database_sha256=rq.sha(reader.db),
        manifest_sha256=rq.sha(reader.root/'manifest.json'),
        availability=meta['availability'],
        admission_reason='Daily signal features use observed prices/trades/status, not quote increments; execution remains separately blocked')


def audit_core_model_asset(reader, day, *, execution_symbols=None):
    """Model input admission for its explicit execution set, never whole-pool P0."""
    import pandas as pd
    from core import p0_rq_input as rq
    meta=reader.meta
    if (meta.get('qualification_scope')!='CORE_MODELS_V1'
            or meta.get('quality_status')!='CORE_MODELS_PASS_V1'
            or meta.get('engineering_only') is not True or meta.get('full_p0_pass') is not False
            or meta.get('execution_qualified') is not True
            or meta.get('model_continuation_qualified') is not True):
        raise ValueError('CORE_MODEL_INPUT_SCOPE_REQUIRED')
    required=set(execution_symbols if execution_symbols is not None else meta.get('execution_qualified_symbols',[]))
    if not required.issubset(set(meta.get('execution_qualified_symbols',[]))):
        raise ValueError('ACTUAL_EXECUTION_SET_OUTSIDE_QUALIFIED_MODEL_INPUTS')
    with local.read_db(reader.db) as con:
        bars=con.execute('SELECT * FROM raw_daily WHERE date=?',[pd.Timestamp(day).date()]).df()
        status=con.execute('SELECT * FROM execution_status WHERE date=?',[pd.Timestamp(day).date()]).df()
        identity=con.execute('SELECT * FROM identity_observed_today').df()
    quality=audit_stock_records(bars,status,identity,day,meta,rq.clock(),require_tick=False)
    required_native={rq.rq_id(s) for s in required}
    subset=bars.loc[bars.order_book_id.isin(required_native)].merge(
        status,on=['order_book_id','date'],validate='one_to_one')
    if set(subset.order_book_id)!=required_native:
        raise ValueError('CORE_MODEL_EXECUTION_SET_ROWS_MISSING')
    tick_sources={}
    for row in subset.to_dict('records'):
        row.update(research_factor=1.0,bar_available_at=meta['availability'])
        for field in ('open','high','low','close'):row['raw_'+field]=row[field]
        frame=pd.DataFrame([row],index=[day])
        frame.attrs.update(reader.lineage('none'))
        result=rq.execution_bar(frame,day)
        tick_sources[rq.qmt_id(row['order_book_id'])]=result.get('price_tick_source','RQDATA_OBSERVED')
    quality.update(status='CORE_MODELS_PASS_V1',qualification_scope='CORE_MODELS_V1',
                   execution_qualified=True,raw_tick_unknown_rows=quality.pop('unknown_tick_rows',0),
                   effective_execution_tick_unknown=0)
    return dict(date=day,version=meta['version'],qualification_scope='CORE_MODELS_V1',
        full_p0_pass=False,execution_qualified=True,quality=quality,
        execution_symbols=sorted(required),execution_count=len(required),tick_sources=tick_sources,
        database_sha256=rq.sha(reader.db),manifest_sha256=rq.sha(reader.root/'manifest.json'))


def forward_start_decision(gates, plan, expected, now, plan_path=None):
    """Admission only. No historical holdings, dates or acceptance results are rewritten."""
    import pandas as pd
    errors = []
    strategy = expected.get('strategy', 'deep')
    permission = 'deep_independent_computation' if strategy == 'deep' else 'ret120_independent_computation'
    if strategy not in ('deep', 'ret120') or not compute_permissions(gates)[permission]:
        errors.append('STOCK_DATA_READY_AND_PIT_READY_REQUIRED')
    if not plan:
        errors.append('NO_ACTUAL_QUALIFIED_FROZEN_PLAN')
    else:
        if (plan.get('provenance_mode') != 'ACTUAL_QUALIFIED_POSTCLOSE'
                or any(x in str(plan.get('research_state', '')) for x in ('NOT_FORWARD', 'SYNTHETIC', 'REPLAY'))):
            errors.append('NOT_FORWARD_OR_SYNTHETIC_PLAN_IS_PERMANENTLY_INELIGIBLE')
        if plan.get('inherited_positions') != [] or plan.get('parent_replay') is not None:
            errors.append('HISTORICAL_OR_SAMPLE_HOLDINGS_CANNOT_BE_INHERITED')
        for key in ('signal_date', 'next_session', 'input_version', 'input_manifest_sha256', 'strategy_fingerprint', 'plan_sha256'):
            if not expected.get(key) or plan.get(key) != expected[key]:
                errors.append('FROZEN_BINDING_MISMATCH:' + key)
        from core import p0_rq_input as rq
        if plan_path is None or not Path(plan_path).is_file() or rq.sha(plan_path) != plan.get('plan_sha256'):
            errors.append('FROZEN_PLAN_FILE_HASH_UNVERIFIABLE')
        else:
            try:
                rows = pd.read_csv(plan_path, dtype={'signal_date': str})
                if (not {'signal_date', 'data_kind'}.issubset(rows.columns)
                        or not rows.signal_date.eq(expected['signal_date']).all()
                        or not rows.data_kind.eq('ACTUAL_QUALIFIED_POSTCLOSE').all()):
                    errors.append('PLAN_CONTENT_IS_NOT_ACTUAL_TARGET_SIGNAL')
            except (KeyError, ValueError, OSError):
                errors.append('PLAN_CONTENT_UNVERIFIABLE')
        try:
            frozen, generated, data_ready, event, clock = [pd.Timestamp(x) for x in (
                plan['frozen_at'], plan['generated_at'], expected['data_available_at'], expected['next_event_at'], now)]
            if any(t.tzinfo is None for t in (frozen, generated, data_ready, event, clock)):
                raise ValueError('TIMEZONE_REQUIRED')
            if not data_ready <= generated <= frozen <= clock < event:
                errors.append('DATA_GENERATION_FREEZE_EVENT_ORDER_INVALID_OR_EVENT_PASSED')
            if frozen.tz_convert('Asia/Shanghai').strftime('%Y%m%d') != plan['signal_date']:
                errors.append('FREEZE_NOT_ON_ACTUAL_SIGNAL_SESSION')
        except (KeyError, ValueError, TypeError):
            errors.append('ACTUAL_FREEZE_OR_EVENT_TIME_UNVERIFIABLE')
    return {'status': 'ELIGIBLE_FOR_NEW_ISOLATED_FORWARD_CANDIDATE' if not errors else 'BLOCKED_FORWARD_START',
            'eligible': not errors, 'reasons': errors, 'first_forward_signal_date': plan.get('signal_date') if plan and not errors else None,
            'ledger_action': 'NOT_CREATED_OR_ADVANCED', 'initial_positions_required': [],
            'inheritance_allowed': False, 'formal_forward_acceptance': False}


def legacy_funnel_material(day):
    """Inspect existing offline prices, never execute their old acquisition/replay scripts."""
    import pandas as pd
    from core import p0_rq_input as rq
    manifest = pd.read_csv(LEGACY / 'SHA256SUMS.csv')
    mapping = dict(zip(manifest.iloc[:, 0].str.replace('\\', '/', regex=False), manifest.iloc[:, 2]))
    names = ['replay/qmt_market.duckdb', 'evidence/units_and_times.json', 'ISOLATED_REPLAY.json']
    proofs = []
    for name in names:
        p = LEGACY / name
        if rq.sha(p) != mapping.get(name):
            raise ValueError('LEGACY_OFFLINE_ASSET_HASH_MISMATCH:' + name)
        proofs.append({'path': str(p), 'sha256': mapping[name]})
    prior = pd.read_csv(PRIOR / 'funnel/universe_aware_prior.csv')
    candidates = prior.loc[prior.momentum_candidate | prior.mean_reversion_candidate, ['symbol']]
    with local.read_db(LEGACY / names[0]) as con:
        con.register('wanted', candidates)
        coverage = con.execute("""SELECT symbol,count(*) n,max(trade_date) last_date
            FROM market.daily_bars_v1 JOIN wanted USING(symbol)
            WHERE trade_date BETWEEN DATE '2026-02-05' AND DATE '2026-10-23' GROUP BY 1""").df()
        last = str(con.execute('SELECT max(trade_date) FROM market.daily_bars_v1').fetchone()[0])
        target_rows = con.execute('SELECT count(*) FROM market.daily_bars_v1 WHERE trade_date=?', [pd.Timestamp(day).date()]).fetchone()[0]
    missing = coverage.loc[coverage.last_date.ne(pd.Timestamp('2026-10-23'))]
    return {'path': str(LEGACY / names[0]), 'last_date': last, 'target_rows': target_rows,
            'diagnostic_cohort_date': '20261023', 'cohort_is_not_target_date_membership': True,
            'known_cohort': len(candidates), 'known_cohort_history_65': int(coverage.n.ge(65).sum()),
            'known_cohort_exact_23': int(coverage.last_date.eq(pd.Timestamp('2026-10-23')).sum()),
            'missing_exact_23': missing.assign(last_date=missing.last_date.astype(str)).to_dict('records'),
            'units': local.read_json(LEGACY / names[1]), 'proofs': proofs,
            'reuse_scope': 'Bounded historical raw-price/amount research only; no source switch, new PIT or Forward promotion.'}


def run(day, output):
    import pandas as pd
    from core import p0_rq_input as rq
    out = local.output_path(output)
    guard = local.OfflineBoundary(out)
    guard.install()
    out.mkdir(parents=True)
    temp = out / 'TEMP'
    temp.mkdir()
    local.sessions(ROOT / 'config/ops/cn_exchange_calendar_2026.json', day)
    profile = local.read_json(rq.PROFILE)
    paths = sorted(Path(profile['asset_root']).glob('versions/*/source_metadata.json'))
    versions = [(local.read_json(p), p) for p in paths]
    exact = [p for m, p in versions if m.get('asof') == pd.Timestamp(day).date().isoformat()]
    # No nearest-date selection for the target; older data are inspected under a separate history key.
    candidates = sorted((m['asof'], str(p)) for m, p in versions if m.get('quality_status') == 'PASS' and m['asof'] < pd.Timestamp(day).date().isoformat())
    last_meta = Path(candidates[-1][1]) if candidates else None
    history, latest_error = None, None
    try:
        if last_meta is not None:
            historical_reader = rq.DailyInput(last_meta.parent / 'manifest.json', profile)
            history = audit_stock_asset(historical_reader, pd.Timestamp(historical_reader.meta['asof']).strftime('%Y%m%d'))
    except Exception as e:
        latest_error = str(e)
    stock = state(False, ['EXACT_TARGET_STOCK_SEAL_MISSING'], last_qualified_date=history['date'] if history else None,
                  selected_target_version=None, reusable_history=history, history_error=latest_error)
    if exact:
        try:
            current = rq.open_inputs(day)
            audit = audit_stock_asset(current, day)
            stock = state(True, selected_target_version=audit['version'], audit=audit, reusable_history=history,
                          last_qualified_date=day)
        except Exception as e:
            stock = state(False, [str(e)], selected_target_version=None, reusable_history=history)
    rp = local.read_json(ROOT / 'config/ops/p0_core_recovery_v01.json')
    manifests = sorted(Path(rp['pit_root']).glob('*_preopen.json'))
    target_pit = [p for p in manifests if p.name.startswith(day + 'T')]
    pit = state(False, ['TRUE_EXACT_DAY_PREOPEN_MEMBERSHIP_MISSING'], last_qualified_date=None,
                target_manifest=None, closing_identity_is_not_preopen_membership=True)
    last_pit_proof = None
    historical_pit = [p for p in manifests if p.name[:8] < day]
    if historical_pit:
        latest_day = historical_pit[-1].name[:8]
        try:
            sets, last_pit_proof = local.membership(rp, latest_day)
            pit['last_qualified_date'] = latest_day
            pit['last_capture'] = last_pit_proof
            pit['counts'] = {k: len(v) for k, v in sets.items()}
        except (Exception, SystemExit) as e:
            pit['last_capture_error'] = str(e)
    if target_pit:
        try:
            sets, proof = local.membership(rp, day)
            pit = state(True,
                        target_manifest=proof, counts={k: len(v) for k, v in sets.items()}, last_qualified_date=day)
        except (Exception, SystemExit) as e:
            pit = state(False, [str(e)], target_manifest=None)
    if stock['ready'] and pit['ready']:
        members = sorted(sets['000905.SH'])
        strategy_inputs = {}
        for kind, adjustment, bars, minimum in (('deep', 'front', 120, 120), ('ret120', 'back_ratio', 320, 121)):
            try:
                frames = current.histories(members, day, bars, adjustment, minimum, allow_incomplete=(kind == 'ret120'))
                if kind == 'ret120':
                    frames = rq.feature_histories(frames)
                count = sum(len(f) >= minimum and day in f.index for f in frames.values())
                need = 500 if kind == 'deep' else 400
                strategy_inputs[kind] = state(count >= need, [] if count >= need else ['FROZEN_HISTORY_COVERAGE_FAILED'], valid=count, required=need)
            except Exception as e:
                strategy_inputs[kind] = state(False, [str(e)])
        stock['strategy_inputs'] = strategy_inputs
    with local.read_db(RESEARCH) as con:
        observed = con.execute("SELECT dividend_type,source_system,max(trade_date),count(*) FROM daily_bars WHERE symbol='000905.SH' GROUP BY 1,2").fetchall()
        reg = con.execute("SELECT source_path,source_sha256,status,asof_date FROM asset_registry WHERE asset_id='FOUNDATION_BENCHMARK_INDEX_DAILY'").fetchone()
    foundation_ok = bool(reg and reg[2] == 'READY' and rq.sha(reg[0]) == reg[1])
    foundation = pd.read_csv(reg[0]) if foundation_ok else pd.DataFrame()
    foundation = foundation.loc[foundation.symbol.eq('000905.SH')] if len(foundation) else foundation
    benchmark = state(False, ['NO_ACCEPTED_EXACT_TARGET_BENCHMARK_BINDING'], observed_series=observed,
                      foundation_hash_verified=foundation_ok,
                      foundation_last_date=str(foundation.trade_date.max()) if len(foundation) else None,
                      foundation_exact_target_rows=int(foundation.trade_date.astype(str).eq(day).sum()) if len(foundation) else 0,
                      registry_asof=reg[3] if reg else None,
                      last_observed_research_date=max((x[2] for x in observed), default=None),
                      native_index_unit='INDEX_POINTS', index_is_not_stock_or_market_median=True)
    try:
        material = legacy_funnel_material(day)
        structure = state(False, ['EXACT_TARGET_RAW_FIELDS_AND_TARGET_COHORT_MISSING'],
                          last_local_material_date=material['last_date'], offline_material=material,
                          old_conditional_membership_not_accepted=True, legacy_RQ_amount_substitution_not_assumed=True)
    except Exception as e:
        structure = state(False, [str(e)])
    with local.read_db(MARKET) as con:
        structure['primary_catalog_last_date'] = str(con.execute('SELECT max(trade_date) FROM market.daily_bars_v1').fetchone()[0])
    gates = {'STOCK_DATA_READY': stock, 'PIT_READY': pit, 'BENCHMARK_READY': benchmark, 'FUNNEL_STRUCTURE_READY': structure}
    decision = forward_start_decision(gates, None, {}, rq.clock())
    dates = local.sessions(ROOT / 'config/ops/cn_exchange_calendar_2026.json', local.read_json(rp['calendar'])['valid_to'])
    possible = [d for d in dates if d > day]
    decision.update(earliest_possible_later_signal_date=possible[0] if possible else None,
                    earliest_is_not_promised=True, missed_PIT_must_not_be_reconstructed=True,
                    historical_23_plan_and_26_samples='PERMANENT_NOT_FORWARD')
    proof_paths = [ROOT / 'scripts/collect_p0_rq_close_v01.py', ROOT / 'scripts/daily_pit_intake_v01.py',
                   ROOT / 'scripts/update_a_share_daily_v1.py', ROOT / 'core/p0_rq_input.py',
                   RESEARCH, MARKET, Path(__file__),
                   PRIOR / 'report.md', ROOT / 'reports/postclose_local_v1_20261026_final/report.md']
    if history:
        proof_paths += [Path(history['database']), last_meta, last_meta.parent / 'manifest.json']
    if reg:
        proof_paths.append(Path(reg[0]))
    if last_pit_proof:
        proof_paths += [Path(last_pit_proof['path']), Path(last_pit_proof['manifest'])]
    proofs = [{'path': str(p), 'sha256': rq.sha(p)} for p in proof_paths]
    proofs += structure.get('offline_material', {}).get('proofs', [])
    result = {'schema': 'POSTCLOSE_DATA_ADMISSION_V1', 'target_date': day, 'checked_at': rq.clock().isoformat(),
              'status': 'BLOCKED_DATA_SUPPLY', 'mode': 'READ_ONLY_DATA_ADMISSION_NO_LEDGER_WRITES',
              'gates': gates, 'permissions': compute_permissions(gates), 'forward_start': decision,
              'future_RQ_channel': 'UNVERIFIED_NOT_CALLED_NOT_ASSUMED_AVAILABLE',
              'availability_polling': False, 'prior_settlement_tests_rerun': False,
              'source_files': proofs, 'boundary_violations': guard.violations}
    for p in proofs:
        p['unchanged_after_check'] = rq.sha(p['path']) == p['sha256']
    if guard.violations or not all(p['unchanged_after_check'] for p in proofs):
        raise ValueError('OFFLINE_BOUNDARY_OR_SOURCE_INTEGRITY_FAILED')
    local.write_json(temp / 'data_gates.json', result)
    local.write_json(temp / 'forward_start.json', decision)
    local.write_json(temp / 'forward_seed_contract.json', {
        'action': 'NOT_CREATED', 'requires': ['qualified same-session stock and actual PIT', 'actual generated plan frozen before next event'],
        'initial_positions': [], 'initial_trades': [], 'inherits_from_replay_or_sample': False,
        'required_bindings': ['actual generated_at', 'actual frozen_at', 'plan SHA256', 'input version and manifest SHA256',
                              'frozen strategy fingerprint', 'next trading session', 'next event timestamp'],
        'ledger_path': None, 'production_apply': False})
    write_summary(out / 'result.md', result)
    print(json.dumps({'status': result['status'], 'gates': {k: v['ready'] for k, v in gates.items()},
                      'formal_ledger': 'NOT_CREATED_OR_ADVANCED', 'output': str(out)}, ensure_ascii=False))
    return result


def write_summary(path, r):
    lines = ['# Postclose Local V1 数据供给与准入实查', '',
             '目标交易日：' + r['target_date'] + '；检查时间：' + r['checked_at'],
             '本次结果：BLOCKED_DATA_SUPPLY。未创建或推进正式模型/Forward账本。', '',
             '| 门槛 | 实查状态 | 直接原因 |', '|---|---|---|']
    for name, gate in r['gates'].items():
        lines.append(f"| {name} | {gate['status']} | {'; '.join(gate['reasons'])} |")
    lines += ['', '股票消费和部分封版可离线；每日新增获取仍依赖原RQ或已取消推进的QMT路径。RQ未来供数可用性未核实，本轮未调用或重启。',
              '23日封版股票及真实盘前成员可作历史输入；不当作26日材料。单纯缺基准不阻断核心计算，Funnel缺口也独立；本日核心阻断来自股票和PIT缺失。',
              '发现已有TEMP真实行情材料到23日，价格raw/none、amount=CNY，成交量原样为手；旧单位证据有77行/10只证券的有限交叉核对，不外推所有数据单位验真。',
              '该材料对旧23日1036候选均有65条日线，1035只具精确23日数据；301139.SZ只到2026-08-28。旧条件成员、历史回放和样本收益不被提升为Forward。',
              '原目录仍到10月15日。复用上述TEMP材料不等于恢复其QMT获取程序，也不证明26日更新可持续。', '',
              '真正Forward日期：尚未产生。26日真实同日PIT缺失，不能事后补造。最早只可能从后续条件齐备的交易日开始，27日不是承诺。',
              '准入必须验证实际合格输入→生成→实际冻结→下一交易日事件的时序，核验计划/输入/策略哈希，并从空模型持仓开始；23日事后计划及26日合成样本永久NOT_FORWARD。', '',
              '详细来源与更新入口、最小字段/质量合同、独立供数申请见 docs/POSTCLOSE_DATA_SUPPLY_CONTRACT_V1.md。',
              '直接证据在TEMP/data_gates.json、TEMP/forward_start.json、TEMP/forward_seed_contract.json。',
              '没有重跑41项测试或合成结算；仅验证本轮新增的数据合同和准入边界。没有真实SDK、外部查询、生产写入、Scheduler更改或Git提交。']
    Path(path).write_text('\n'.join(lines) + '\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', default='20261026')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    run(args.date, args.output)
