"""Daily local Deep/RET120 research report from the exact published RQData day.

No collection, Broker, Scheduler or production-ledger writes. Historical model
sessions remain NOT_FORWARD. A preauthorized future model starts independently
from an empty account at its real eligible event; completed input identities reuse
their report.
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
CLOSEOUT_VERSION = 'THREE_STRATEGY_CLOSEOUT_V2_FUNNEL_MODEL'
FUNNEL_AMOUNT_BINDING = ROOT / 'reports/postclose_stock_20261027_resume/funnel_live/amount_unit_binding.json'


def full_model_input(day, *, allow_core_models=False):
    """Prefer exact full P0; an explicit opt-in permits only the core-model seal."""
    from core import p0_rq_input as rq
    try:
        source = rq.open_inputs(day)
    except rq.InputBlocked as exc:
        if not allow_core_models or not str(exc).startswith('RQ_EXACT_DATE_VERSION_MISSING:'):
            raise
        source = rq.open_inputs(day, scope='CORE_MODELS_V1')
        m = source.meta
        if (m.get('qualification_scope') != 'CORE_MODELS_V1'
                or m.get('quality_status') != 'CORE_MODELS_PASS_V1'
                or m.get('engineering_only') is not True or m.get('full_p0_pass') is not False
                or m.get('execution_qualified') is not True
                or m.get('model_continuation_qualified') is not True):
            raise ValueError('CORE_MODELS_PUBLICATION_NOT_QUALIFIED')
        return source
    if source.meta.get('quality_status') != 'PASS' or source.meta.get('engineering_only') is not False:
        raise ValueError('PUBLISHED_DAY_NOT_QUALIFIED')
    return source


def is_model_report(report):
    return ((report.get('mode') == 'ISOLATED_LOCAL_MODEL_REPLAY' and report.get('forward_acceptance') is False)
            or (report.get('mode') == 'ACTUAL_MODEL_FORWARD' and report.get('forward_acceptance') is True))


def actual_forward_window(day):
    """The authorization begins after the historical closeout day, at real time."""
    from core import p0_rq_input as rq
    now = rq.clock()
    return day > '20261028' and now.strftime('%Y%m%d') == day and '1530' <= now.strftime('%H%M') < '1715'


def actual_model_parent(day, kind):
    candidates = [r for r in daily_reports() if r[0] < day
                  and r[3].get('mode') == 'ACTUAL_MODEL_FORWARD'
                  and r[3].get('forward_acceptance') is True
                  and r[3].get('strategies', {}).get(kind, {}).get('status') == 'MODEL_REPLAY_COMPLETE']
    return candidates[-1][2] if candidates else None


def funnel_parent(day, dates, *, actual_model_forward=False):
    """Use only an adjacent, sealed Funnel model; a first actual account is empty."""
    import pandas as pd
    from core import p0_rq_input as rq
    candidates = [row for row in daily_reports() if row[0] < day
                  and row[3].get('funnel', {}).get('model', {}).get('status') == 'FUNNEL_MODEL_SESSION_COMPLETE'
                  and row[3]['funnel']['model'].get('forward_acceptance') is actual_model_forward]
    if not candidates:
        if actual_model_forward or day == '20261028':
            return None
        raise ValueError('ADJACENT_FUNNEL_MODEL_PARENT_MISSING')
    _, _, root, _ = candidates[-1]
    root, report, hashes = model_report(root)
    local.require_next_session({'last_processed_date': report['business_date']}, dates, day)
    model = report['funnel']['model']
    if model.get('forward_acceptance') is not actual_model_forward:
        raise ValueError('FUNNEL_PARENT_FORWARD_STATE_MISMATCH')
    for name in ('model.duckdb', 'plan.csv', 'plan_freeze.json', 'model_summary.json'):
        verified_file(root, 'funnel/' + name, hashes)
    proof = local.read_json(root / 'funnel/plan_freeze.json')
    if (proof.get('next_session') != day or proof.get('forward_qualified') is not actual_model_forward
            or proof.get('ledger_sha256') != hashes['funnel/model.duckdb']):
        raise ValueError('FUNNEL_PARENT_PLAN_BINDING_MISMATCH')
    event = pd.Timestamp(day).tz_localize(rq.CST).replace(hour=9, minute=30)
    local.require_plan_before_event(root / 'funnel/plan.csv', proof, event)
    return root / 'funnel'


def required_execution_symbols(day):
    """Only real adjacent model holdings and pending plans require execution ticks."""
    profile = local.read_json(ROOT / 'config/ops/p0_core_recovery_v01.json')
    dates = local.sessions(profile['calendar'], day)
    if len(dates) < 2:
        raise ValueError('PREVIOUS_APPROVED_SESSION_MISSING')
    symbols = set()
    for kind in ('deep', 'ret120'):
        parent = actual_model_parent(day, kind)
        if parent is None and actual_forward_window(day):
            continue  # Authorized independent empty account; historical plans are not inherited.
        parent = parent or usable_model_run(dates[-2], kind)
        checked = parent_profile(parent, kind, profile, dates, day)
        module_policy, _ = policy(kind)
        table = 'forward_signals' if kind == 'deep' else 'ret120_forward_signals'
        with local.read_db(checked['segment_db']) as con:
            symbols.update(r[0] for r in con.execute(f'''SELECT symbol FROM forward_positions
                WHERE strategy_id=? UNION SELECT symbol FROM {table}
                WHERE strategy_id=? AND status='PENDING_ENTRY' ''',
                [module_policy.strategy_id, module_policy.strategy_id]).fetchall())
    return sorted(symbols)


def daily_reports(day=None):
    """Discover immutable attempts by report identity, without a mutable pointer."""
    result = []
    for path in DAILY_ROOT.glob('*/report.json'):
        value = local.read_json(path)
        if (value.get('daily_entry') == 'run_postclose_daily.py'
                and (day is None or value.get('business_date') == day)):
            result.append((value.get('business_date', ''), value.get('generated_at', ''), path.parent, value))
    return sorted(result, key=lambda row: (row[0], row[1], row[2].name))


def usable_model_run(day, kind, source=None, pit=None, *, actual_forward=None):
    from core import p0_rq_input as rq
    for _, _, root, report in reversed(daily_reports(day)):
        if (not is_model_report(report)
                or report.get('boundary', {}).get('violations')
                or report.get('strategies', {}).get(kind, {}).get('status') != 'MODEL_REPLAY_COMPLETE'):
            continue
        if actual_forward is not None and report.get('forward_acceptance') is not actual_forward:
            continue
        if source is not None and (report.get('input', {}).get('version') != source.meta['version']
                or report.get('database_sha256') != source.meta['database_sha256']
                or report.get('manifest_sha256') != rq.sha(source.root / 'manifest.json')
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
    if not is_model_report(report) or report.get('boundary', {}).get('violations'):
        raise ValueError('MODEL_PARENT_MUST_BE_QUALIFIED_MODEL_SESSION')
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
    forward = report.get('mode') == 'ACTUAL_MODEL_FORWARD'
    if item.get('status') != 'MODEL_REPLAY_COMPLETE' or item.get('forward_acceptance') is not forward:
        raise ValueError('PARENT_STRATEGY_NOT_COMPLETE:' + kind)
    _, fingerprint = policy(kind)
    if item['policy_hash'] != fingerprint:
        raise ValueError('PARENT_STRATEGY_FINGERPRINT_CHANGED:' + kind)
    for name in ('model.duckdb', 'plan.csv', 'plan_freeze.json'):
        verified_file(root, kind + '/' + name, hashes)
    proof = local.read_json(root / kind / 'plan_freeze.json')
    if (proof.get('research_state') != ('ACTUAL_MODEL_FORWARD' if forward else 'NOT_FORWARD')
            or proof.get('forward_qualified') is not forward or proof.get('next_session') != day):
        raise ValueError('PARENT_PLAN_SESSION_OR_RESEARCH_STATE_MISMATCH:' + kind)
    if proof.get('ledger_sha256') != hashes[kind + '/model.duckdb']:
        raise ValueError('PARENT_PLAN_LEDGER_SEAL_MISMATCH:' + kind)
    event = pd.Timestamp(day).tz_localize(rq.CST).replace(hour=9, minute=30)
    local.require_plan_before_event(root / kind / 'plan.csv', proof, event)
    return dict(profile, segment_db=str(root / kind / 'model.duckdb'), actual_model_forward=forward)


def reuse_model(root, kind, out, *, reuse_operations=False):
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
    if reuse_operations and item.get('operations'):
        for name in ('operations.csv', 'entry_decisions.csv', 'position_actions.csv'):
            key = kind + '/' + name
            shutil.copyfile(verified_file(root, key, hashes), out / key)
            if rq.sha(out / key) != hashes[key]:
                raise ValueError('RECOVERY_OPERATIONS_COPY_MISMATCH:' + key)
    else:
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
    if result.get('mode') == 'ISOLATED_DAILY_SIGNAL_RESEARCH':
        return write_signal_report(path, result)
    lines = ['# PositionX-CN 三策略盘后日报', '',
             f"业务日：{result['business_date']}；生成：{result['generated_at']}。",
             f"数据：{result.get('input', {}).get('version', '未取得准确日期发布数据')}。",
             f"运行：{result['status']}；模型模式：{result.get('mode')}；研究状态：{result.get('research_state', 'NOT_FORWARD')}。真实账户、资金、持仓、成交未知，未查询。", '',
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
                  f"阶段状态：{json.dumps(item.get('calculation_stages', {}), ensure_ascii=False)}。",
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
    lines += [f"Funnel：{f.get('status', 'NOT_RUN')}；完整模型：{f.get('full_model_status', 'NOT_RUN')}。",
               f"候选 {f.get('candidate_count', '未计算')}；结构 {f.get('feature_count', '未计算')}；风险预览：{f.get('execution_preview', {}).get('status', 'NOT_RUN')}。明细：[summary.json](funnel/summary.json)。"]
    model = f.get('model', {})
    state = model.get('state', {})
    lines += [f"Funnel模型：{model.get('status', 'NOT_RUN')}；条件计划 {len(model.get('plans', []))}；当日模型进入 {len(model.get('entries', []))}；持仓 {len(model.get('positions', []))}；应退出计划 {len(model.get('exit_plans', []))}；退出 {len(model.get('exits', []))}；延期 {len(model.get('delayed', []))}。",
              f"Funnel现金 {state.get('cash', '未计算')}；权益 {state.get('equity', '未计算')}；结算 {json.dumps(model.get('settlement', {'status': 'NOT_RUN'}), ensure_ascii=False)}。",
              f"进入/未进入原因：{json.dumps(model.get('entry_decisions', []), ensure_ascii=False)}。阻断：{json.dumps(model.get('blocked', []), ensure_ascii=False)}。延期：{json.dumps(model.get('delayed', []), ensure_ascii=False)}。",
              f"Funnel模型明细：[model_summary.json](funnel/model_summary.json)；条件计划：[plan.csv](funnel/plan.csv)。未来价格、数量与成交继续保持未知；模型不是实盘账户。",
              '', '后续运行仍使用本入口与准确业务日，只有相邻研究日可接续；失败目录保留，再次运行会新建同日尝试目录，保留已完成策略的原计算。次日逐策略选择前一研究日最新可用尝试，不覆盖旧产物。',
              '本入口只读发布数据和真实PIT，所有新写入限制在本次reports目录，不采集数据、不连接Broker、不推进正式账本。']
    Path(path).write_text('\n'.join(lines) + '\n', encoding='utf-8')


def signal_report(root):
    """Validate an isolated signal report; it never qualifies as a model parent."""
    root = Path(root).resolve()
    if not root.is_relative_to((ROOT / 'reports').resolve()):
        raise ValueError('SIGNAL_REPORT_MUST_BE_REPOSITORY_REPORTS')
    hashes = inventory(root)
    report = local.read_json(verified_file(root, 'report.json', hashes))
    if (report.get('mode') != 'ISOLATED_DAILY_SIGNAL_RESEARCH'
            or report.get('qualification_scope') != 'DAILY_SIGNALS_V1'
            or report.get('forward_acceptance') is not False
            or report.get('model_continuation_qualified') is not False
            or report.get('execution_qualified') is not False
            or report.get('boundary', {}).get('violations')):
        raise ValueError('SIGNAL_REPORT_QUALIFICATION_INVALID')
    return root, report, hashes


def matching_signal_run(day, source, pit, manifest_sha, fingerprints, kind=None):
    for _, _, root, report in reversed(daily_reports(day)):
        if (report.get('mode') != 'ISOLATED_DAILY_SIGNAL_RESEARCH'
                or report.get('input', {}).get('version') != source.meta['version']
                or report.get('database_sha256') != source.meta['database_sha256']
                or report.get('manifest_sha256') != manifest_sha
                or any(report.get('pit', {}).get(k) != pit.get(k) for k in ('sha256', 'manifest_sha256'))):
            continue
        kinds = (kind,) if kind else ('deep', 'ret120')
        if any(report.get('strategies', {}).get(k, {}).get('status') != 'SIGNAL_RESEARCH_ONLY'
               or report['strategies'][k].get('policy_hash') != fingerprints[k] for k in kinds):
            continue
        root, report, hashes = signal_report(root)
        for k in kinds:
            for name in ('model.duckdb', 'signals.csv', 'signal_candidates.csv', 'summary.json'):
                verified_file(root, k + '/' + name, hashes)
            if k == 'ret120':
                verified_file(root, k + '/snapshot.csv', hashes)
        return root, report, hashes
    return None


def write_signal_report(path, result):
    lines = ['# PositionX-CN 盘后信号研究', '',
             f"业务日：{result['business_date']}；生成：{result['generated_at']}。",
             f"数据版本：{result.get('input', {}).get('version', '未取得合格输入')}。",
             f"结果：{result['status']}；NOT_FORWARD；DAILY_SIGNALS_V1。", '',
             '本次只计算日线信号。未运行模型入场、持仓接续、退出或结算；容量未评估。',
             '候选不是模型待入场计划；未来开盘价及数量未知。真实账户未查询。', '',
             '| 策略 | 原始候选 / Q5 | 冻结信号记录 | 信号日排除 |',
             '|---|---:|---:|---:|']
    details = []
    for kind, label in (('deep', 'Deep'), ('ret120', 'RET120')):
        item = result['strategies'].get(kind, {})
        summary = item.get('signal_summary')
        if not summary:
            lines.append(f"| {label} | 阻断：{item.get('error', '未运行')} | — | — |")
            continue
        excluded = summary['signal_status_counts'].get('EXCLUDED_SIGNAL_HYGIENE', 0)
        hygiene = str(excluded) if kind == 'ret120' else '冻结信号函数不做此项筛选'
        lines.append(f"| {label} | {summary['raw_candidate_count']} | {summary['new_signal_record_count']} | {hygiene} |")
        details += ['', f"{label}：[原始信号]({kind}/signals.csv)，"
                  f"[按冻结顺序排列的研究候选及原排除原因]({kind}/signal_candidates.csv)。",
                  f"策略指纹：{item['policy_hash']}。"]
    lines += details
    lines += ['', f"实际 PIT：{result.get('pit', {}).get('manifest', '未取得')}。",
              f"输入库：{result.get('published_database', '未取得')}。",
              '模型进入数、待入场计划、持仓、退出及收益均为未计算，不是零。',
              '该结果不能作为完整模型接续或正式 Forward 的父运行。Funnel 本次未运行。']
    if result.get('input_error'):
        lines += ['', '输入阻断：' + result['input_error']]
    Path(path).write_text('\n'.join(lines) + '\n', encoding='utf-8')


def run_signals(day, output=None):
    """Use the explicit research publication, real PIT, and frozen signal chain."""
    from core import p0_rq_input as rq
    out = local.output_path(Path(output or new_attempt(day)).absolute())
    out.mkdir(parents=True)
    guard = local.OfflineBoundary(out)
    guard.install()
    result = {'schema': 'POSITIONX_DAILY_POSTCLOSE_V1', 'daily_entry': 'run_postclose_daily.py',
              'business_date': day, 'generated_at': rq.clock().isoformat(),
              'mode': 'ISOLATED_DAILY_SIGNAL_RESEARCH', 'qualification_scope': 'DAILY_SIGNALS_V1',
              'forward_acceptance': False, 'model_continuation_qualified': False,
              'execution_qualified': False, 'output_directory': str(out),
              'real_account': {'status': 'UNKNOWN_NOT_QUERIED', 'cash': None, 'positions': None, 'fills': None},
              'entry_sha256': rq.sha(Path(__file__)), 'strategies': {},
              'funnel': {'status': 'NOT_RUN_DAILY_SIGNAL_SCOPE', 'exit_qualification': 'UNQUALIFIED'}}
    try:
        source = rq.open_inputs(day, scope='DAILY_SIGNALS_V1')
        if (source.meta.get('qualification_scope') != 'DAILY_SIGNALS_V1'
                or source.meta.get('quality_status') != 'DAILY_SIGNALS_PASS_V1'
                or source.meta.get('engineering_only') is not True
                or source.meta.get('full_p0_pass') is not False
                or source.meta.get('execution_qualified') is not False):
            raise ValueError('DAILY_SIGNALS_PUBLICATION_NOT_QUALIFIED')
        manifest_sha = rq.sha(source.root / 'manifest.json')
        result.update(input=source.lineage('back_ratio'), published_database=str(source.db),
                      database_sha256=source.meta['database_sha256'], manifest_sha256=manifest_sha)
        profiles = {'ret120': local.read_json(ROOT / 'config/ops/p0_core_recovery_v01.json'),
                    'deep': local.read_json(ROOT / 'config/ops/deep_forward_segment_v01.json')}
        dates = local.sessions(profiles['ret120']['calendar'], day)
        sets, result['pit'] = local.membership(profiles['ret120'], day)
        fingerprints = {}
        for kind in ('deep', 'ret120'):
            try:
                fingerprints[kind] = policy(kind)[1]
            except (Exception, SystemExit) as exc:
                result['strategies'][kind] = {'status': 'BLOCKED', 'error': str(exc),
                    'forward_acceptance': False, 'execution_qualified': False,
                    'model_continuation_qualified': False}
        existing = (matching_signal_run(day, source, result['pit'], manifest_sha, fingerprints)
                    if len(fingerprints) == 2 else None)
        if existing and output is None:
            out.rmdir()
            print('EXISTING_DAILY_SIGNAL_REPORT_NO_RECOMPUTE', flush=True)
            return existing[1]
        details = local.local_details(source, day)
        for kind in ('deep', 'ret120'):
            if kind not in fingerprints:
                continue
            try:
                existing = matching_signal_run(day, source, result['pit'], manifest_sha, fingerprints, kind)
                if existing:
                    old, report, hashes = existing
                    (out / kind).mkdir()
                    names = ['model.duckdb', 'signals.csv', 'signal_candidates.csv', 'summary.json']
                    if kind == 'ret120':
                        names.append('snapshot.csv')
                    for name in names:
                        key = kind + '/' + name
                        shutil.copyfile(verified_file(old, key, hashes), out / key)
                        if rq.sha(out / key) != hashes[key]:
                            raise ValueError('SIGNAL_REUSE_COPY_MISMATCH:' + key)
                    result['strategies'][kind] = dict(report['strategies'][kind])
                    result.setdefault('strategy_reused_from', {})[kind] = str(old)
                else:
                    result['strategies'][kind], _ = local.run_model(kind, source, profiles[kind], sets,
                        details, dates, day, out / kind, fresh_research=True, signals_only=True)
            except (Exception, SystemExit) as exc:
                result['strategies'][kind] = {'status': 'BLOCKED', 'error': str(exc),
                    'forward_acceptance': False, 'execution_qualified': False,
                    'model_continuation_qualified': False}
    except (Exception, SystemExit) as exc:
        result['input_error'] = str(exc)
    passed = sum(result['strategies'].get(k, {}).get('status') == 'SIGNAL_RESEARCH_ONLY'
                 for k in ('deep', 'ret120'))
    result['status'] = ('TWO_STRATEGY_SIGNAL_RESEARCH_COMPLETE' if passed == 2 and not result.get('input_error')
                        else 'SIGNAL_RESEARCH_PARTIAL' if passed else 'SIGNAL_RESEARCH_BLOCKED')
    result['boundary'] = {'violations': guard.violations, 'blocked_sdk_modules': list(guard.blocked)}
    if guard.violations:
        result['status'] = 'BOUNDARY_VALIDATION_FAILED'
    local.write_json(out / 'report.json', result)
    write_report(out / 'report.md', result)
    local.write_json(out / 'artifacts_manifest.json', {'schema': 'POSTCLOSE_LOCAL_ARTIFACTS_V1',
        'files': [{'path': p.relative_to(out).as_posix(), 'sha256': rq.sha(p)}
                  for p in sorted(out.rglob('*')) if p.is_file()]})
    return result


def run(day=None, output=None, previous_run=None, reuse_run=None, *, signals_only=False, allow_core_models=False):
    import pandas as pd
    from core import p0_rq_input as rq
    day = pd.Timestamp(day or rq.clock().date()).strftime('%Y%m%d')
    if signals_only:
        if previous_run or reuse_run:
            raise ValueError('SIGNALS_ONLY_CANNOT_USE_MODEL_PARENT_OR_ACCEPTED_RUN')
        return run_signals(day, output)
    source = full_model_input(day, allow_core_models=allow_core_models)
    def matches(report):
        return (report.get('status') == 'TWO_STRATEGY_REPORT_COMPLETE'
                and report.get('closeout_version') == CLOSEOUT_VERSION
                and report.get('funnel', {}).get('model', {}).get('status') == 'FUNNEL_MODEL_SESSION_COMPLETE'
                and report.get('input', {}).get('version') == source.meta['version']
                and report.get('database_sha256') == source.meta['database_sha256']
                and (not actual_forward_window(day) or previous_run or reuse_run
                     or report.get('forward_acceptance') is True)
                and report.get('manifest_sha256') == rq.sha(source.root / 'manifest.json'))
    if output is None:
        completed = [row for row in daily_reports(day) if matches(row[3])]
        if completed:
            _, existing, _ = model_report(completed[-1][2])
            print('EXISTING_DAILY_REPORT_NO_RECOMPUTE', flush=True)
            return existing
    out = Path(output or new_attempt(day)).absolute()
    if out.exists():
        _, existing, _ = model_report(out)
        if existing.get('daily_entry') != 'run_postclose_daily.py' or existing['business_date'] != day:
            raise ValueError('OUTPUT_ALREADY_EXISTS_NOT_THIS_DAILY_RUN')
        if not matches(existing):
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
              'research_state': 'NOT_FORWARD',
              'closeout_version': CLOSEOUT_VERSION,
              'output_directory': str(out),
              'real_account': {'status': 'UNKNOWN_NOT_QUERIED', 'cash': None, 'positions': None, 'fills': None},
              'entry_sha256': rq.sha(Path(__file__)),
              'strategies': {}, 'funnel': {'status': 'NOT_RUN_TWO_STRATEGY_DAILY_SCOPE', 'exit_qualification': 'UNQUALIFIED'}}
    try:
        result.update(input=source.lineage('back_ratio'), published_database=str(source.db),
                      database_sha256=source.meta['database_sha256'], manifest_sha256=rq.sha(source.root / 'manifest.json'),
                      qualification_scope=source.meta.get('qualification_scope', 'P0_COMPLETE'),
                      execution_qualified=True, model_continuation_qualified=True,
                      full_p0_pass=source.meta.get('full_p0_pass', source.meta.get('quality_status') == 'PASS'))
        ret_profile = local.read_json(ROOT / 'config/ops/p0_core_recovery_v01.json')
        deep_profile = local.read_json(ROOT / 'config/ops/deep_forward_segment_v01.json')
        dates = local.sessions(ret_profile['calendar'], day)
        sets, result['pit'] = local.membership(ret_profile, day)
        actual_forward = actual_forward_window(day) and not previous_run and not reuse_run
        result['forward_start'] = {'status': 'ACTUAL_ELIGIBLE_WINDOW' if actual_forward else 'NOT_OCCURRED_OR_HISTORICAL',
                                   'historical_holdings_inherited': False if actual_forward else None,
                                   'authorization': 'CODEX_CLOSEOUT_20261028_SECTION_B'}
        if actual_forward:
            result.update(mode='ACTUAL_MODEL_FORWARD', research_state='ACTUAL_MODEL_FORWARD', forward_acceptance=True)
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
                    if actual_forward:
                        profile = dict(profile, pit_root=ret_profile['pit_root'])
                    try:
                        same_day = usable_model_run(day, kind, source, result['pit'], actual_forward=actual_forward)
                    except LookupError:
                        same_day = None
                    if same_day is not None:
                        result['strategies'][kind] = reuse_model(same_day, kind, out, reuse_operations=True)
                        result.setdefault('strategy_reused_from', {})[kind] = str(same_day)
                        continue
                    forward_parent = actual_model_parent(day, kind) if actual_forward else None
                    fresh_forward = actual_forward and forward_parent is None
                    if not fresh_forward:
                        parent = (Path(previous_run).resolve() if previous_run
                                  else forward_parent or usable_model_run(dates[-2], kind))
                        result['parent_runs'][kind] = str(parent)
                        profile = parent_profile(parent, kind, profile, dates, day)
                        if actual_forward and not profile.get('actual_model_forward'):
                            raise ValueError('ACTUAL_FORWARD_CANNOT_INHERIT_HISTORICAL_REPLAY')
                    result['strategies'][kind], _ = local.run_model(kind, source, profile, sets, details, dates, day,
                        out / kind, fresh_research=fresh_forward, actual_forward_start=fresh_forward,
                        actual_model_forward=actual_forward)
                except (Exception, SystemExit) as exc:
                    result['strategies'][kind] = {'status': 'BLOCKED', 'error': str(exc), 'forward_acceptance': False}
        # Funnel is independent of model parent/exit eligibility and uses the same sealed input and real PIT.
        if not result.get('reused_run'):
            try:
                previous_funnel = funnel_parent(day, dates, actual_model_forward=actual_forward)
                result['funnel'] = local.run_funnel(source, sets, day, out / 'funnel',
                    amount_unit_binding=FUNNEL_AMOUNT_BINDING, pit_proof=result['pit'],
                    previous_run=previous_funnel, actual_model_forward=actual_forward)
            except (Exception, SystemExit) as exc:
                result['funnel'] = {'status': 'BLOCKED', 'error': str(exc),
                    'full_model_status': 'FUNNEL_MODEL_BLOCKED', 'model': {'status': 'BLOCKED', 'error': str(exc)},
                    'forward_acceptance': False}
        for kind in ('deep', 'ret120'):
            item = result['strategies'].get(kind, {})
            if item.get('status') == 'MODEL_REPLAY_COMPLETE' and not item.get('operations'):
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
    funnel_complete = result.get('funnel', {}).get('model', {}).get('status') == 'FUNNEL_MODEL_SESSION_COMPLETE'
    result['three_strategy_status'] = ('THREE_STRATEGY_MODELS_COMPLETE' if len(passed) == 2 and funnel_complete
                                       and not result.get('input_error') else 'PARTIAL' if passed or funnel_complete else 'BLOCKED')
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
    parser.add_argument('--signals-only', action='store_true', help='Explicit DAILY_SIGNALS_V1 research; no model entries, holdings, exits or settlement')
    parser.add_argument('--allow-core-models', action='store_true', help='Explicitly permit a qualified CORE_MODELS_V1 publication; never a signal-only seal')
    args = parser.parse_args()
    from core import p0_rq_input as rq
    if args.signals_only and args.allow_core_models:
        parser.error('--signals-only and --allow-core-models are separate qualifications')
    if args.latest_report and any((args.date, args.output, args.previous_run, args.reuse_run, args.signals_only, args.allow_core_models)):
        parser.error('--latest-report is read-only and cannot be combined with calculation arguments')
    before_close = (rq.clock().hour, rq.clock().minute) < (15, 30)
    if args.latest_report or (before_close and not any((args.date, args.output, args.previous_run, args.reuse_run, args.signals_only))):
        report_dir, result = latest_report()
        print(json.dumps({'status': 'LATEST_COMPLETED_REPORT_READ_ONLY',
                          'business_date': result['business_date'], 'data_version': result['input']['version'],
                          'research_state': result.get('research_state', 'NOT_FORWARD'), 'today_data_status': 'NOT_EVALUATED',
                          'report': str(report_dir / 'report.md')}, ensure_ascii=False))
        return 0
    result = run(args.date, args.output, args.previous_run, args.reuse_run,
                 signals_only=args.signals_only, allow_core_models=args.allow_core_models)
    report_dir = Path(args.output or result.get('output_directory') or DAILY_ROOT / result['business_date']).resolve()
    print(json.dumps({'status': result['status'], 'three_strategy_status': result.get('three_strategy_status'),
                      'report': str(report_dir / 'report.md')}, ensure_ascii=False))
    return 0 if (result['status'] == 'TWO_STRATEGY_SIGNAL_RESEARCH_COMPLETE'
                 or result.get('three_strategy_status') == 'THREE_STRATEGY_MODELS_COMPLETE') else 3


if __name__ == '__main__':
    raise SystemExit(main())
