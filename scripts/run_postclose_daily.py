"""Daily local Deep/RET120 research report from the exact published RQData day.

No collection, Broker, Scheduler or production-ledger writes. Continued model
sessions remain NOT_FORWARD. Repeated completed dates return the existing report.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import run_postclose_local_candidate as local

DAILY_ROOT = ROOT / 'reports/postclose_daily'
ACCEPTED_RUN = ROOT / 'reports/rqdata_consumer_acceptance_20261028/TEMP/strategy_run'
ACCEPTED_VERSION = 'asof_20261027_5b3800ece8a8'
ACCEPTED_DATABASE_SHA = '5b3800ece8a8754140670309eefe3bd0d6fc08d9ac06c432d496982f03bc3a9f'


def daily_reports(day=None):
    """Discover immutable attempts by report identity, without a mutable pointer."""
    result = []
    for path in DAILY_ROOT.glob('*/report.json'):
        value = local.read_json(path)
        if (value.get('daily_entry') == 'run_postclose_daily.py'
                and (day is None or value.get('business_date') == day)):
            result.append((value.get('business_date', ''), value.get('generated_at', ''), path.parent, value))
    return sorted(result, key=lambda row: (row[0], row[1], row[2].name))


def usable_model_run(day, kind, source=None, pit=None):
    for _, _, root, report in reversed(daily_reports(day)):
        if (report.get('mode') != 'ISOLATED_LOCAL_MODEL_REPLAY'
                or report.get('forward_acceptance') is not False
                or report.get('boundary', {}).get('violations')
                or report.get('strategies', {}).get(kind, {}).get('status') != 'MODEL_REPLAY_COMPLETE'):
            continue
        if source is not None and (report.get('input', {}).get('version') != source.meta['version']
                or report.get('database_sha256') != source.meta['database_sha256']
                or any(report.get('pit', {}).get(k) != pit.get(k) for k in ('sha256', 'manifest_sha256'))):
            continue
        return root
    raise LookupError('EXACT_PREVIOUS_MODEL_DAY_MISSING:' + day + ':' + kind)


def new_attempt(day):
    base = DAILY_ROOT / day
    if not base.exists():
        return base
    number = 2
    while (path := DAILY_ROOT / f'{day}_attempt_{number:03d}').exists():
        number += 1
    return path


def inventory(root):
    rows = local.read_json(root / 'artifacts_manifest.json')['files']
    result = {r['path'].replace('\\', '/'): r['sha256'] for r in rows}
    if len(result) != len(rows):
        raise ValueError('DUPLICATE_MODEL_ARTIFACT_PATH')
    return result


def verified_file(root, name, hashes):
    from core import p0_rq_input as rq
    p = (root / name).resolve()
    if not p.is_relative_to(root) or name not in hashes or rq.sha(p) != hashes[name]:
        raise ValueError('MODEL_ARTIFACT_HASH_MISMATCH:' + name)
    return p


def model_report(root):
    root = Path(root).resolve()
    if not root.is_relative_to((ROOT / 'reports').resolve()):
        raise ValueError('MODEL_PARENT_MUST_BE_REPOSITORY_REPORTS')
    hashes = inventory(root)
    report = local.read_json(verified_file(root, 'report.json', hashes))
    if report.get('mode') != 'ISOLATED_LOCAL_MODEL_REPLAY' or report.get('forward_acceptance') is not False:
        raise ValueError('MODEL_PARENT_MUST_REMAIN_NOT_FORWARD')
    return root, report, hashes


def policy(kind):
    from scripts import run_deep_oversold_csi500_forward as deep
    from scripts import run_ret120_csi500_forward as ret
    module = deep if kind == 'deep' else ret
    artifacts = ret.verify_frozen_prerequisites() if kind == 'ret120' else None
    fingerprint = module.POLICY.fingerprint(artifacts) if artifacts else module.POLICY.fingerprint()
    return module.POLICY, fingerprint


def parent_profile(root, kind, profile, dates, day):
    """Validate one sleeve independently and require its real seal before S1 open."""
    import pandas as pd
    from core import p0_rq_input as rq
    root, report, hashes = model_report(root)
    local.require_next_session({'last_processed_date': report['business_date']}, dates, day)
    item = report['strategies'].get(kind, {})
    if item.get('status') != 'MODEL_REPLAY_COMPLETE' or item.get('forward_acceptance') is not False:
        raise ValueError('PARENT_STRATEGY_NOT_COMPLETE:' + kind)
    _, fingerprint = policy(kind)
    if item['policy_hash'] != fingerprint:
        raise ValueError('PARENT_STRATEGY_FINGERPRINT_CHANGED:' + kind)
    for name in ('model.duckdb', 'plan.csv', 'plan_freeze.json'):
        verified_file(root, kind + '/' + name, hashes)
    proof = local.read_json(root / kind / 'plan_freeze.json')
    if proof.get('research_state') != 'NOT_FORWARD' or proof.get('forward_qualified') is not False or proof.get('next_session') != day:
        raise ValueError('PARENT_PLAN_SESSION_OR_RESEARCH_STATE_MISMATCH:' + kind)
    if proof.get('ledger_sha256') != hashes[kind + '/model.duckdb']:
        raise ValueError('PARENT_PLAN_LEDGER_SEAL_MISMATCH:' + kind)
    event = pd.Timestamp(day).tz_localize(rq.CST).replace(hour=9, minute=30)
    local.require_plan_before_event(root / kind / 'plan.csv', proof, event)
    return dict(profile, segment_db=str(root / kind / 'model.duckdb'))


def reuse_model(root, kind, out):
    """Carry a completed same-input sleeve into a new recovery report unchanged."""
    from core import p0_rq_input as rq
    root, report, hashes = model_report(root)
    item = dict(report['strategies'][kind])
    _, fingerprint = policy(kind)
    if item.get('status') != 'MODEL_REPLAY_COMPLETE' or item.get('policy_hash') != fingerprint:
        raise ValueError('RECOVERY_MODEL_IDENTITY_MISMATCH:' + kind)
    (out / kind).mkdir()
    for name in ('model.duckdb', 'plan.csv', 'plan_freeze.json', 'signals.csv',
                 'positions.csv', 'exits.csv', 'state.json', 'summary.json'):
        key = kind + '/' + name
        shutil.copyfile(verified_file(root, key, hashes), out / key)
        if rq.sha(out / key) != hashes[key]:
            raise ValueError('RECOVERY_MODEL_COPY_MISMATCH:' + key)
    item.pop('operations', None)
    item.pop('operations_error', None)
    return item


def reuse_accepted(source, sets_pit, day, out, reuse_root):
    """Reuse exact accepted values and seals; never rerun the accepted 27th."""
    from core import p0_rq_input as rq
    root, report, hashes = model_report(reuse_root)
    if (day != '20261027' or report['business_date'] != day
            or source.meta['version'] != ACCEPTED_VERSION
            or source.meta['database_sha256'] != ACCEPTED_DATABASE_SHA
            or report['input']['version'] != source.meta['version']
            or report['stock_acceptance']['database_sha256'] != source.meta['database_sha256']
            or report['stock_acceptance']['manifest_sha256'] != rq.sha(source.root / 'manifest.json')):
        raise ValueError('ACCEPTED_RUN_INPUT_IDENTITY_MISMATCH')
    if any(report['pit'].get(k) != sets_pit.get(k) for k in ('sha256', 'manifest_sha256')):
        raise ValueError('ACCEPTED_RUN_ACTUAL_PIT_MISMATCH')
    for kind in ('deep', 'ret120'):
        _, fingerprint = policy(kind)
        item = report['strategies'][kind]
        if item['status'] != 'MODEL_REPLAY_COMPLETE' or item['policy_hash'] != fingerprint:
            raise ValueError('ACCEPTED_RUN_STRATEGY_IDENTITY_MISMATCH:' + kind)
        (out / kind).mkdir()
        for name in ('model.duckdb', 'plan.csv', 'plan_freeze.json', 'signals.csv',
                     'positions.csv', 'exits.csv', 'state.json', 'summary.json'):
            key = kind + '/' + name
            shutil.copyfile(verified_file(root, key, hashes), out / key)
            if rq.sha(out / key) != hashes[key]:
                raise ValueError('ACCEPTED_ARTIFACT_COPY_MISMATCH:' + key)
    funnel = report['strategies'].get('funnel', {})
    (out / 'funnel').mkdir()
    for name in ('candidates.parquet', 'features.parquet', 'summary.json'):
        key = 'funnel/' + name
        shutil.copyfile(verified_file(root, key, hashes), out / key)
    return {k: report['strategies'][k] for k in ('deep', 'ret120')}, {
        'status': 'REUSED_CANDIDATES_AND_STRUCTURE_ONLY', 'source_run': str(root),
        'candidate_count': funnel.get('candidate_count'), 'setup_counts': funnel.get('setup_counts'),
        'risk_preview': 'NOT_RUN', 'exit_qualification': 'UNQUALIFIED',
        'unknown_special_type_count': len(funnel.get('security_status_qualification', {}).get('unknown_special_type', [])),
        'forward_acceptance': False}


def pending_operations(kind, pending, active_positions, policy_obj):
    """Display frozen ordering/capacity conditions without predicting execution."""
    rows = sorted(pending, key=lambda r: (r['symbol'], r['signal_date']))
    slots = max(0, policy_obj.max_positions - active_positions) if kind == 'ret120' else None
    result = []
    for rank, row in enumerate(rows, 1):
        if kind == 'ret120':
            condition = 'WITHIN_CURRENT_SLOT_LIMIT_IF_EARLIER_CANDIDATES_FILL' if rank <= slots else 'WAIT_EARLIER_FILLABILITY_AND_SLOT_LIMIT'
        else:
            condition = 'ALL_FILLABLE_SCALE_TO_GROSS_AND_CASH_THEN_MIN_LOT'
        result.append(dict(row, frozen_order=rank, capacity_condition=condition,
                           action='MODEL_NEXT_SESSION_OPEN_ENTRY', research_state='NOT_FORWARD',
                           entry_decision='PENDING_FUTURE_OPEN_AND_EXACT_STATUS',
                           future_open_price=None, future_entry_units=None,
                           target_weight_ceiling=policy_obj.target_weight,
                           current_available_slots=slots))
    return result


def operational_summary(kind, root, day, dates, source=None, members=None, reused=False, q5_count=None):
    import pandas as pd
    p, _ = policy(kind)
    table = 'forward_signals' if kind == 'deep' else 'ret120_forward_signals'
    with local.read_db(root / kind / 'model.duckdb') as con:
        def query(sql, args):
            return con.execute(sql, args).df().where(lambda f: pd.notna(f), None)
        today = query(f'SELECT * FROM {table} WHERE strategy_id=? AND signal_date=? ORDER BY symbol', [p.strategy_id, day])
        decisions = query(f'SELECT * FROM {table} WHERE strategy_id=? AND actual_entry_date=? ORDER BY symbol,signal_date', [p.strategy_id, day])
        pending = query(f"SELECT * FROM {table} WHERE strategy_id=? AND status='PENDING_ENTRY' ORDER BY symbol,signal_date", [p.strategy_id])
        positions = query("SELECT * FROM forward_positions WHERE strategy_id=? AND ledger='BASE20' ORDER BY symbol", [p.strategy_id])
        exits = query("SELECT * FROM forward_trades WHERE strategy_id=? AND ledger='BASE20' AND exit_date=? ORDER BY symbol", [p.strategy_id, day])
        equity = query("SELECT * FROM forward_equity WHERE strategy_id=? AND ledger='BASE20' AND trade_date=?", [p.strategy_id, day])
    operations = pending_operations(kind, pending.to_dict('records'), len(positions), p)
    pd.DataFrame(operations, columns=list(operations[0]) if operations else ['symbol', 'entry_decision', 'future_open_price', 'future_entry_units']).to_csv(root / kind / 'operations.csv', index=False, encoding='utf-8-sig')
    decisions.to_csv(root / kind / 'entry_decisions.csv', index=False, encoding='utf-8-sig')
    indexes = {d: i for i, d in enumerate(dates)}
    due = [r['symbol'] for r in positions.to_dict('records')
           if r['signal_date'] not in indexes or indexes[day] - indexes[r['signal_date']] >= p.exit_offset_sessions_from_signal]
    calendar = ROOT / 'config/ops/cn_exchange_calendar_2026.json'
    all_dates = local.sessions(calendar, local.read_json(calendar)['valid_to'])
    all_indexes = {d: i for i, d in enumerate(all_dates)}
    next_day = [d for d in all_dates if d > day]
    position_actions = []
    for row in positions.to_dict('records'):
        offset = all_indexes.get(row['signal_date'], len(all_dates)) + p.exit_offset_sessions_from_signal
        position_actions.append(dict(row, planned_exit_session=all_dates[offset] if offset < len(all_dates) else None,
                                     due_at_this_close=row['symbol'] in due,
                                     model_action='DUE_EXIT_UNRESOLVED' if row['symbol'] in due else 'HOLD_UNTIL_FROZEN_EXIT_SESSION'))
    pd.DataFrame(position_actions, columns=list(position_actions[0]) if position_actions else ['symbol', 'planned_exit_session', 'due_at_this_close', 'model_action']).to_csv(root / kind / 'position_actions.csv', index=False, encoding='utf-8-sig')
    raw_count = len(today)
    not_new = []
    if kind == 'ret120':
        if q5_count is None:
            raise ValueError('RET120_ACTUAL_SNAPSHOT_Q5_COUNT_MISSING')
        raw_count = q5_count
    if kind == 'deep' and not reused:
        from scripts import run_deep_oversold_csi500_forward as deep
        histories = source.histories(sorted(members), day, 120, 'front', 120)
        raw = {s for s, f in histories.items()
               if (value := deep.ret20_at_close(f, day)) is not None and value <= p.ret20_threshold}
        raw_count = len(raw)
        not_new = [{'symbol': s, 'reason': 'EXISTING_MODEL_POSITION_OR_PENDING_PLAN'}
                   for s in sorted(raw - set(today.symbol))]
    return {'raw_candidate_count': raw_count, 'new_signal_record_count': len(today),
            'raw_candidates_not_new_signals': not_new,
            'new_pending_plan_count': int(today.status.eq('PENDING_ENTRY').sum()),
            'actual_model_entries_today': int(decisions.status.eq('ENTERED').sum()),
            'entry_decision_counts': {str(k): int(v) for k, v in decisions.status.value_counts().items()},
            'entry_decisions': decisions[['symbol', 'signal_date', 'status', 'reason']].to_dict('records'),
            'pending_plan_count': len(pending), 'next_execution_session': next_day[0] if next_day else None,
            'current_positions': len(positions), 'due_unresolved_positions': due,
            'exits_today': len(exits), 'settlement': equity.to_dict('records'),
            'capacity': {'target_weight_ceiling': p.target_weight,
                         'maximum_gross': p.max_total_weight if kind == 'deep' else p.max_target_gross,
                         'max_positions': None if kind == 'deep' else p.max_positions,
                         'ordering': 'symbol_asc; filter exact S1 fillability before capacity',
                         'allocation': 'ALL_FILLABLE_PROPORTIONAL_SCALE_THEN_MIN_LOT' if kind == 'deep' else 'FIRST_AVAILABLE_SLOTS_THEN_PROPORTIONAL_SCALE_FRACTIONAL_RESEARCH'},
            'execution_status': 'RESEARCH_MODEL_ONLY_NOT_REAL_ACCOUNT'}


def write_report(path, result):
    lines = ['# PositionX-CN 两策略盘后日报', '',
             f"业务日：{result['business_date']}；生成：{result['generated_at']}。",
             f"数据：{result.get('input', {}).get('version', '未取得准确日期发布数据')}。",
             f"运行：{result['status']}；研究状态：NOT_FORWARD。真实账户、资金、持仓、成交未知，未查询。", '',
             '原始信号、待入场计划和已进入模型分开计数；待入场数量和成交条件须等下一交易日真实行情，不能视为订单或成交。', '',
             '| 策略 | 当日原始候选 | 新增待入场 | 当日模型实际进入 | 现有模型持仓 | 当日退出 |',
             '|---|---:|---:|---:|---:|---:|']
    for kind, label in (('deep', 'Deep'), ('ret120', 'RET120')):
        item = result['strategies'].get(kind, {})
        op = item.get('operations')
        if not op:
            lines.append(f"| {label} | 阻断：{item.get('error', item.get('operations_error', '未完成'))} | — | — | — | — |")
            continue
        lines.append(f"| {label} | {op['raw_candidate_count']} | {op['new_pending_plan_count']} | {op['actual_model_entries_today']} | {op['current_positions']} | {op['exits_today']} |")
    lines += ['', '## 模型操作清单与结算', '']
    for kind, label in (('deep', 'Deep'), ('ret120', 'RET120')):
        item = result['strategies'].get(kind, {})
        if 'operations' not in item:
            continue
        op = item['operations']
        st = item['state']
        lines += [f"### {label}", '',
                  f"待入场 {op['pending_plan_count']} 条；下一执行观察日 {op['next_execution_session']}。完整逐证券清单：[operations.csv]({kind}/operations.csv)。",
                  f"信号：[signals.csv]({kind}/signals.csv)；已完成入场判定及原因：[entry_decisions.csv]({kind}/entry_decisions.csv)；持仓和计划退出日：[position_actions.csv]({kind}/position_actions.csv)；当日退出：[exits.csv]({kind}/exits.csv)。",
                  ('Deep对所有S1可成交候选按1%目标、30%总仓及现金统一缩放，再向下取最小整手；候选数不等于已入场数，也没有30名硬截断。' if kind == 'deep' else 'RET120在S1可成交筛选后按证券代码排序，最多30个持仓；1%目标、30%总仓与现金共同约束。当前无持仓时前30名仅为有条件容量候选，后续候选仍可能因前序不可成交而递补；碎股仅为研究单位。'),
                  f"当日模型实际进入 {op['actual_model_entries_today']}；当日入场判定状态计数：{json.dumps(op['entry_decision_counts'], ensure_ascii=False)}。逐证券未进入原因见 entry_decisions.csv；待执行的计划未填未来开盘价或数量。",
                  f"原始候选中因已有模型持仓/待计划未新发信号：{len(op['raw_candidates_not_new_signals'])}；逐项原因见 report.json。",
                  f"模型应退出未解决：{', '.join(op['due_unresolved_positions']) or '无'}；已退出 {op['exits_today']}。",
                  f"模型日终权益 {st['base_equity']:.2f}，现金 {st['base_cash']:.2f}；该研究段累计收益 {st['total_return_base']:.4%}。",
                  f"策略指纹 {item['policy_hash']}；计划实际封版 {item['plan_freeze']['frozen_at']}。", '']
    lines += ['## 数据与任务影响', '',
              f"已发布数据库：{result.get('published_database', '未绑定')}。",
              f"实际PIT：{result.get('pit', {}).get('manifest', '未取得')}。",
              '封版时间表示该版本的本地发布可用时间，未改写原始采集时间；历史重算及后续研究模型接续均不计入正式Forward。']
    if result.get('reused_run'):
        lines.append(f"本日直接复用已验收计算：{result['reused_run']}；未重复计算。")
    for name, item in result['strategies'].items():
        if item.get('error'):
            lines.append(f"- {name}：{item['error']}")
    if result.get('input_error'):
        lines.append('- 准确日期输入：' + result['input_error'])
    f = result.get('funnel', {})
    lines += [f"Funnel：{f.get('status', 'NOT_RUN')}；本轮仅保留候选与结构，未运行风险预览，退出未资格化。",
              '', '后续运行仍使用本入口与准确业务日，只有相邻研究日可接续；失败目录保留，再次运行会新建同日尝试目录，保留已完成策略的原计算。次日逐策略选择前一研究日最新可用尝试，不覆盖旧产物。',
              '本入口只读发布数据和真实PIT，所有新写入限制在本次reports目录，不采集数据、不连接Broker、不推进正式账本。']
    Path(path).write_text('\n'.join(lines) + '\n', encoding='utf-8')


def run(day=None, output=None, previous_run=None, reuse_run=None):
    import pandas as pd
    from core import p0_rq_input as rq
    day = pd.Timestamp(day or rq.clock().date()).strftime('%Y%m%d')
    if output is None:
        completed = [row for row in daily_reports(day) if row[3].get('status') == 'TWO_STRATEGY_REPORT_COMPLETE']
        if completed:
            _, existing, _ = model_report(completed[-1][2])
            print('EXISTING_DAILY_REPORT_NO_RECOMPUTE', flush=True)
            return existing
    out = Path(output or new_attempt(day)).absolute()
    if out.exists():
        _, existing, _ = model_report(out)
        if existing.get('daily_entry') != 'run_postclose_daily.py' or existing['business_date'] != day:
            raise ValueError('OUTPUT_ALREADY_EXISTS_NOT_THIS_DAILY_RUN')
        if existing.get('status') != 'TWO_STRATEGY_REPORT_COMPLETE':
            raise ValueError('FAILED_OUTPUT_PRESERVED; retry without --output or choose a new --output')
        print('EXISTING_DAILY_REPORT_NO_RECOMPUTE', flush=True)
        return existing
    out = local.output_path(out)
    # The validated reports parent may be new on first use; create the empty
    # output before narrowing the audit boundary to files beneath that output.
    out.mkdir(parents=True)
    guard = local.OfflineBoundary(out)
    guard.install()
    result = {'schema': 'POSITIONX_DAILY_POSTCLOSE_V1', 'daily_entry': 'run_postclose_daily.py',
              'business_date': day, 'generated_at': rq.clock().isoformat(),
              'mode': 'ISOLATED_LOCAL_MODEL_REPLAY', 'forward_acceptance': False,
              'output_directory': str(out),
              'real_account': {'status': 'UNKNOWN_NOT_QUERIED', 'cash': None, 'positions': None, 'fills': None},
              'entry_sha256': rq.sha(Path(__file__)),
              'strategies': {}, 'funnel': {'status': 'NOT_RUN_TWO_STRATEGY_DAILY_SCOPE', 'exit_qualification': 'UNQUALIFIED'}}
    try:
        source = rq.open_inputs(day)
        if source.meta.get('quality_status') != 'PASS' or source.meta.get('engineering_only') is not False:
            raise ValueError('PUBLISHED_DAY_NOT_QUALIFIED')
        result.update(input=source.lineage('back_ratio'), published_database=str(source.db),
                      database_sha256=source.meta['database_sha256'], manifest_sha256=rq.sha(source.root / 'manifest.json'))
        ret_profile = local.read_json(ROOT / 'config/ops/p0_core_recovery_v01.json')
        deep_profile = local.read_json(ROOT / 'config/ops/deep_forward_segment_v01.json')
        dates = local.sessions(ret_profile['calendar'], day)
        sets, result['pit'] = local.membership(ret_profile, day)
        if day == '20261027' or reuse_run:
            if previous_run:
                raise ValueError('REUSE_AND_PREVIOUS_RUN_ARE_MUTUALLY_EXCLUSIVE')
            result['strategies'], result['funnel'] = reuse_accepted(source, result['pit'], day, out, reuse_run or ACCEPTED_RUN)
            result['reused_run'] = str(Path(reuse_run or ACCEPTED_RUN).resolve())
        else:
            result['parent_runs'] = {}
            details = local.local_details(source, day)
            for kind, profile in (('deep', deep_profile), ('ret120', ret_profile)):
                try:
                    try:
                        same_day = usable_model_run(day, kind, source, result['pit'])
                    except LookupError:
                        same_day = None
                    if same_day is not None:
                        result['strategies'][kind] = reuse_model(same_day, kind, out)
                        result.setdefault('strategy_reused_from', {})[kind] = str(same_day)
                        continue
                    parent = Path(previous_run).resolve() if previous_run else usable_model_run(dates[-2], kind)
                    result['parent_runs'][kind] = str(parent)
                    profile = parent_profile(parent, kind, profile, dates, day)
                    result['strategies'][kind], _ = local.run_model(kind, source, profile, sets, details, dates, day, out / kind)
                except (Exception, SystemExit) as exc:
                    result['strategies'][kind] = {'status': 'BLOCKED', 'error': str(exc), 'forward_acceptance': False}
        for kind in ('deep', 'ret120'):
            item = result['strategies'].get(kind, {})
            if item.get('status') == 'MODEL_REPLAY_COMPLETE':
                try:
                    item['operations'] = operational_summary(kind, out, day, dates, source=source,
                                    members=sets['000905.SH'], reused=bool(result.get('reused_run')),
                                    q5_count=item.get('snapshot', {}).get('q5_count'))
                except (Exception, SystemExit) as exc:
                    item['operations_error'] = str(exc)
    except (Exception, SystemExit) as exc:
        result['input_error'] = str(exc)
    passed = [k for k in ('deep', 'ret120') if result['strategies'].get(k, {}).get('operations')]
    result['status'] = 'TWO_STRATEGY_REPORT_COMPLETE' if len(passed) == 2 and not result.get('input_error') else 'PARTIAL' if passed else 'BLOCKED'
    result['boundary'] = {'violations': guard.violations, 'blocked_sdk_modules': list(guard.blocked)}
    if guard.violations:
        result['status'] = 'BOUNDARY_VALIDATION_FAILED'
    local.write_json(out / 'report.json', result)
    write_report(out / 'report.md', result)
    local.write_json(out / 'artifacts_manifest.json', {'schema': 'POSTCLOSE_LOCAL_ARTIFACTS_V1',
                     'files': [{'path': p.relative_to(out).as_posix(), 'sha256': rq.sha(p)}
                               for p in sorted(out.rglob('*')) if p.is_file()]})
    return result


def latest_report():
    """Read the last completed daily report; this says nothing about today's data."""
    candidates = [row for row in daily_reports() if row[3].get('status') == 'TWO_STRATEGY_REPORT_COMPLETE']
    if not candidates:
        raise ValueError('NO_COMPLETED_DAILY_REPORT; first run requires an exact --date')
    _, _, root, _ = candidates[-1]
    _, result, _ = model_report(root)
    return root, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', help='Exact calculation day; without arguments show latest report before close, calculate exact today after close')
    parser.add_argument('--output', help='New directory under repository reports; default reports/postclose_daily/YYYYMMDD')
    parser.add_argument('--previous-run', help='Adjacent NOT_FORWARD model day; default prior daily report directory')
    parser.add_argument('--reuse-run', help='Exact accepted 20261027 run; no strategy recomputation')
    parser.add_argument('--latest-report', action='store_true', help='Read the latest completed report only; never declares today ready')
    args = parser.parse_args()
    from core import p0_rq_input as rq
    if args.latest_report and any((args.date, args.output, args.previous_run, args.reuse_run)):
        parser.error('--latest-report is read-only and cannot be combined with calculation arguments')
    before_close = (rq.clock().hour, rq.clock().minute) < (15, 30)
    if args.latest_report or (before_close and not any((args.date, args.output, args.previous_run, args.reuse_run))):
        report_dir, result = latest_report()
        print(json.dumps({'status': 'LATEST_COMPLETED_REPORT_READ_ONLY',
                          'business_date': result['business_date'], 'data_version': result['input']['version'],
                          'research_state': 'NOT_FORWARD', 'today_data_status': 'NOT_EVALUATED',
                          'report': str(report_dir / 'report.md')}, ensure_ascii=False))
        return 0
    result = run(args.date, args.output, args.previous_run, args.reuse_run)
    report_dir = Path(args.output or result.get('output_directory') or DAILY_ROOT / result['business_date']).resolve()
    print(json.dumps({'status': result['status'], 'report': str(report_dir / 'report.md')}, ensure_ascii=False))
    return 0 if result['status'] == 'TWO_STRATEGY_REPORT_COMPLETE' else 3


if __name__ == '__main__':
    raise SystemExit(main())
