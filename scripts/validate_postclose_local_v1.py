"""One-shot local asset inspection and explicitly synthetic next-session validation.

No data polling, SDK, production writes or Forward promotion. Chinese text lives
in this UTF-8 file, not in a PowerShell stdin pipeline.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
from scripts import run_postclose_local_candidate as local

ACCEPTED = ROOT / 'reports/postclose_local_20261023_20261026_verified'
FOUNDATION = Path('D:/positionx_data/incoming/eva/20260925_foundation_assets/data/research/px_assets/benchmark_index_daily.csv')
RESEARCH_DB = ROOT / 'data/research/positionx_research.duckdb'
MARKET_DB = Path('D:/positionx_data/catalog/market_data.duckdb')


class SettlementSample:
    """Constant, invented quotes for testing actual saved plans; never market data."""
    synthetic_sample = True

    def __init__(self, day, dates, suspended=(), up_locked=(), missing=()):
        self.day, self.dates = day, dates
        self.suspended, self.up_locked, self.missing = set(suspended), set(up_locked), set(missing)

    def lineage(self, adjustment):
        return {'source': 'SYNTHETIC_SAMPLE_NOT_MARKET', 'asof': self.day,
                'adjustment': adjustment, 'raw_quote': 10.0,
                'ret120_research_factor': 0.37, 'volume_unit': 'SAMPLE_ONLY'}

    def histories(self, symbols, day, bars, adjustment, min_bars, **kwargs):
        import pandas as pd
        if day != self.day:
            raise ValueError('SAMPLE_EXACT_DATE_MISMATCH')
        result = {}
        index = self.dates[-max(min_bars, 121):]
        for symbol in symbols:
            if symbol in self.missing:
                continue
            factor = 1.0 if adjustment == 'front' else 0.37
            frame = pd.DataFrame(index=index)
            for field in ('open', 'high', 'low', 'close'):
                frame[field] = 10.0 * factor
                frame['raw_' + field] = 10.0
            frame['research_factor'], frame['factor_valid'] = factor, True
            frame['volume'], frame['num_trades'], frame['amount'] = 1000.0, 10.0, 10000.0
            frame['prev_close'], frame['limit_up'], frame['limit_down'] = 10.0, 11.0, 9.0
            frame['status_suspended'], frame['is_suspended'], frame['price_tick'] = False, False, .01
            frame['bar_available_at'] = frame['status_available_at'] = '2026-10-23T16:00:00+08:00'
            frame['tick_available_at'] = pd.Timestamp(day).strftime('%Y-%m-%d') + 'T00:00:00+08:00'
            if symbol in self.suspended:
                frame.loc[day, ['status_suspended', 'is_suspended']] = True
            if symbol in self.up_locked:
                frame.loc[day, 'limit_up'] = 10.0
            frame.attrs.update(source='SYNTHETIC_SAMPLE_NOT_MARKET', version='OFFLINE_FIXTURE_V1')
            result[symbol] = frame
        return result


def sample_details(symbols, suspended=()):
    return {s: {'InstrumentName': 'SAMPLE_ONLY', 'OpenDate': '20000101',
                'InstrumentStatus': int(s in suspended), 'MinLimitOrderVolume': 100,
                'UpStopPrice': 11.0, 'PriceTick': .01} for s in symbols}


def inspect_assets(parent, target, out):
    import pandas as pd
    from core import p0_rq_input as rq
    old = local.read_json(parent / 'report.json')
    sealed = rq.open_inputs(old['business_date'])
    for adjustment in ('front', 'back_ratio'):
        sealed.require_contract(adjustment)
    with local.read_db(RESEARCH_DB) as con:
        registry = con.execute("SELECT source_path,source_sha256,status FROM asset_registry WHERE asset_id='FOUNDATION_BENCHMARK_INDEX_DAILY'").fetchone()
        observed = con.execute("SELECT dividend_type,source_system,min(trade_date),max(trade_date),count(*) FROM daily_bars WHERE symbol='000905.SH' GROUP BY 1,2").fetchall()
    index = pd.read_csv(FOUNDATION)
    index = index.loc[index.symbol.eq('000905.SH')]
    foundation_hash = rq.sha(FOUNDATION)
    with local.read_db(MARKET_DB) as con:
        latest = str(con.execute('SELECT max(trade_date) FROM market.daily_bars_v1').fetchone()[0])
    with local.read_db(sealed.db) as con:
        stock_range = con.execute('SELECT min(date),max(date),count(DISTINCT order_book_id) FROM raw_daily').fetchone()
        stock_index_count = con.execute("SELECT count(*) FROM raw_daily WHERE order_book_id='000905.XSHG'").fetchone()[0]
    prior_path = parent / 'funnel/universe_aware_prior.csv'
    prior = pd.read_csv(prior_path)
    candidates = prior.loc[prior.momentum_candidate | prior.mean_reversion_candidate, 'symbol']
    dependency = local.funnel_market_dependencies(candidates, old['business_date'])
    pd.DataFrame(dependency.pop('symbols')).to_csv(out / 'funnel_symbol_gaps.csv', index=False, encoding='utf-8-sig')
    # One exact-date selection only; absence is final for this invocation.
    try:
        next_input = rq.open_inputs(target)
        if next_input is None or next_input.meta.get('quality_status') != 'PASS' or next_input.meta.get('engineering_only') is not False:
            raise ValueError('TARGET_ASSET_NOT_QUALIFIED')
        next_error = None
    except (rq.InputBlocked, ValueError) as e:
        next_input, next_error = None, str(e)
    pit_root = Path(local.read_json(ROOT / 'config/ops/p0_core_recovery_v01.json')['pit_root'])
    pit_available = [str(p) for p in pit_root.glob(target + 'T*_preopen.json')]
    result = {
        'checked_at': rq.clock().isoformat(), 'target_session': target,
        'target_input': {'available': next_input is not None, 'error': next_error,
                         'preopen_manifest_paths': pit_available, 'availability_checks': 1},
        'csi500': {
            'required_dates_minimum': [old['business_date'], target],
            'required_fields': ['trade_date', 'symbol', 'close'],
            'history_fields': ['open', 'high', 'low', 'close', 'volume', 'amount'],
            'price_unit': 'INDEX_POINTS_NOT_STOCK_PRICE',
            'volume_amount_unit': 'NOT_DECLARED_IN_FOUNDATION_METADATA; NOT_USED_FOR_CLOSE_RETURN',
            'adjustment': 'NATIVE_INDEX_SERIES; NEVER_ALIAS_OR_SPLICE_DIFFERENT_STOCK_ADJUSTMENTS',
            'foundation': {'path': str(FOUNDATION), 'sha256': foundation_hash,
                           'registry_status': registry[2], 'hash_matches_registry': foundation_hash == registry[1],
                           'rows': len(index), 'first_date': str(index.trade_date.min()), 'last_date': str(index.trade_date.max()),
                           'target_date_rows': int(index.trade_date.astype(str).isin([old['business_date'], target]).sum())},
            'research_observed_series': [dict(zip(['adjustment', 'source', 'first_date', 'last_date', 'rows'], row)) for row in observed],
            'sealed_stock_asset_index_rows': stock_index_count,
            'reusable_scope': 'Foundation historical research through 20260924; separate observed QMT-labelled samples through 20261015. No automatic splice.',
            'gap': 'Exact 20261023 and 20261026 closes and bound source/adjustment evidence are absent.',
        },
        'funnel': {
            'audit_cohort_signal_date': old['business_date'], 'dependency': dependency,
            'catalog_last_date': latest, 'required_history_start_23': '2026-02-05',
            'required_history_start_26': '2026-02-08', 'minimum_history_rows': 65,
            'missing_catalog_sessions_23': ['20261016', '20261019', '20261020', '20261021', '20261022', '20261023'],
            'additional_session_26': target,
            'units': {'price': 'RAW_RMB_PER_SHARE_EXPECTED; retained QMT vendor values',
                      'amount': 'RMB_TURNOVER_REQUIRED_FOR_NAV_AND_ADV20_SIZING; raw catalog values retained',
                      'volume': 'RAW_VENDOR_UNIT_RETAINED; STRUCTURE_ENGINE_USES_AMOUNT_NOT_VOLUME'},
            'adjustment': 'RAW_NONE; frozen cumulative(close/pre_close) synth_adjust, then map levels back to raw price',
            'rq_reuse': 'Sealed raw OHLC/prev_close/total_turnover exist through 20261023. Existing accepted contracts cover Deep/RET120, not a Funnel amount-unit substitution.',
            'minimum_addition': 'Exact-date raw fields for each requested candidate, >=65 retained daily bars in the 260-calendar-day window under frozen semantics; row counts are not tradability proof. Require source, amount-unit and no-adjustment proof; explicit retained-source binding before using RQ as a replacement.',
            'no_history_symbols': dependency['candidate_count'] - dependency['with_any_history'],
            'short_history_symbols': dependency['with_any_history'] - dependency['history_65_count'],
            'exit_qualification': 'UNQUALIFIED_NO_POSITIONS_OR_RETURNS',
        },
        'sealed_stocks': {'path': str(sealed.db), 'sha256': rq.sha(sealed.db),
                          'first_date': str(stock_range[0]), 'last_date': str(stock_range[1]), 'symbols': stock_range[2],
                          'raw_adjustment': sealed.meta['raw_adjustment'], 'volume_unit': sealed.meta['volume_unit'],
                          'deep_contract': 'QMT_FRONT_AFFINE', 'ret120_contract': 'RET120_FROZEN_CLOSE_PRE_CLOSE',
                          'reusable_scope': 'Accepted 20261023 model signal/plans and historical raw stock observations; no target-26 quotes or PIT are inferred.'},
    }
    local.write_json(out / 'data_gaps.json', result)
    return result, next_input


def settlement_samples(parent, target, out):
    import pandas as pd
    from core import p0_rq_input as rq
    deep_profile = local.read_json(ROOT / 'config/ops/deep_forward_segment_v01.json')
    ret_profile = local.read_json(ROOT / 'config/ops/p0_core_recovery_v01.json')
    dates = local.sessions(ret_profile['calendar'], target)
    deep_profile, ret_profile, _ = local.previous_profiles(parent, target, dates, deep_profile, ret_profile)
    result = {'data_kind': 'SYNTHETIC_SAMPLE_NOT_MARKET', 'forward_acceptance': False,
              'target_session': target, 'market_price_observed': False, 'strategies': {}}
    sets = {'000905.SH': set()}
    source = SettlementSample(target, dates)
    for kind, profile in (('deep', deep_profile), ('ret120', ret_profile)):
        original_plan = pd.read_csv(parent / kind / 'plan.csv')
        symbols = sorted(original_plan.symbol)
        summary, curve = local.run_model(kind, source, profile, sets, sample_details(symbols), dates, target,
                                         out / kind, settlement_only=True)
        plan = pd.read_csv(out / kind / 'plan.csv')
        positions = pd.read_csv(out / kind / 'positions.csv')
        notional = float(positions.entry_notional.sum())
        expected = 37 if kind == 'deep' else 30
        assert len(positions) == expected
        assert notional <= 300000.0 + 1e-7
        assert (positions.entry_notional <= 10000.0 + 1e-7).all()
        assert summary['state']['base_cash'] >= 0 and summary['state']['shadow_cash'] >= 0
        assert positions.data_kind.eq('SYNTHETIC_SAMPLE_NOT_MARKET').all()
        if kind == 'deep':
            assert ((positions.shares % 100) == 0).all()
        else:
            assert list(positions.symbol) == symbols[:30]
            assert any(abs(v - round(v)) > 1e-7 for v in positions.shares)
        with local.read_db(out / kind / 'model.duckdb') as con:
            assert con.execute('SELECT count(*) FROM forward_sessions').fetchone()[0] == 0
            assert con.execute("SELECT count(*) FROM forward_trades WHERE exit_date=?", [target]).fetchone()[0] == 0
            no_slot = 0 if kind == 'deep' else con.execute("SELECT count(*) FROM ret120_forward_signals WHERE status='NO_SLOT'").fetchone()[0]
        checks = {}
        for test_day in (target, '20261028'):
            try:
                local.require_next_session(summary['state'], local.sessions(ret_profile['calendar'], test_day), test_day)
            except ValueError as e:
                checks[test_day] = str(e)
            else:
                raise AssertionError('Duplicate or gap was accepted')
        result['strategies'][kind] = {'status': summary['status'], 'prior_plan_count': len(original_plan),
            'entered_positions': len(positions), 'no_slot': no_slot, 'remaining_plan_count': len(plan),
            'entry_notional': notional, 'base_entry_cost': float(positions.entry_fee.sum()),
            'base_cash': summary['state']['base_cash'], 'sample_equity': summary['state']['base_equity'],
            'sample_pnl': summary['state']['base_equity'] - 1000000.0, 'exits': 0,
            'cursor': summary['state']['last_processed_date'], 'duplicate_gap_checks': checks}
    # Original historical plan was created after the next actual opening event.
    old = local.read_json(parent / 'report.json')
    proof = {'frozen_at': old['generated_at'], 'plan_sha256': rq.sha(parent / 'deep/plan.csv')}
    try:
        local.require_plan_before_event(parent / 'deep/plan.csv', proof, '2026-10-26T09:30:00+08:00')
    except ValueError as e:
        result['historical_forward_gate'] = str(e)
    else:
        raise AssertionError('Late historical plan passed Forward event gate')
    local.write_json(out / 'settlement_samples.json', result)
    return result


def write_v1_report(path, result):
    old = result['accepted_result']
    inventory = result['assets']
    dependency = inventory['funnel']['dependency']
    lines = ['# PositionX-CN Postclose Local V1 日报', '',
             '核验时间：' + result['verified_at'],
             '研究状态：PARTIAL_RESEARCH_CANDIDATE / NOT_FORWARD。原 2026-10-23 结果继续有效，不追认历史 Forward。', '',
             '真实账户：UNKNOWN_NOT_QUERIED；现金、持仓和成交均为未知，未填零。', '',
             '已接受的 10 月 23 日模型结果：', '',
             '| 策略 | 模型计划 | 模型持仓 | 模型退出 | 模型权益/收益 |',
             '|---|---:|---:|---:|---|']
    for kind in ('deep', 'ret120'):
        state = old['strategies'][kind]['state']
        lines.append(f"| {kind} | {state['pending_signals']} | {state['active_positions']} | {state['completed_trades']} | 权益 {state['base_equity']:,.2f}；模型收益 {state['total_return_base']:.2%} |")
    lines += ['', '50/50 模型组合权益为 1,000,000，首个模型日收益为 0；不是实际账户净值。', '',
              '数据缺口与可复用资产：', '',
              '- CSI500：FOUNDATION_BENCHMARK_INDEX_DAILY 的来源文件哈希与 READY 注册记录一致，1633 条 CSI500 日线覆盖 2020-01-02 至 2026-09-24；另有旧研究库 front 标签样本到 10 月 15 日、back_ratio 标签样本到 10 月 13 日。各序列单独保留，未拼接或换复权。',
              '- 本次基准比较最少需要 10 月 23 日和 10 月 26 日的精确 close、证券身份和来源/口径证据；价格单位为指数点位。历史日线可保留 OHLC、volume、amount，但成交量/金额单位未在 foundation 元数据中明确，不能用作流动性或成交判断。',
              f"- Funnel：原 market.daily_bars_v1 到 {inventory['funnel']['catalog_last_date']}。10 月 23 日的 {dependency['candidate_count']} 个 prior 候选，目标日有效行 {dependency['exact_target_count']}；{dependency['with_any_history']} 个在窗口内有历史，{dependency['history_65_count']} 个达到 65 行。逐证券明细见 funnel_symbol_gaps.csv。",
              '- Funnel 要求目标日精确 raw OHLC、pre_close、amount、volume，260 个自然日窗口内至少 65 条按冻结口径留存的日线；行数不等同于可成交证明。金额须与人民币 NAV/ADV20 口径一致。冻结结构复权为累计 close/pre_close，价格水平再映回原始价；不使用 RQ_PRE/RQ_POST 别名代替。',
              '- 已有封版股票资产覆盖 2025-09-15 至 2026-10-23、5225 只证券；Deep affine front 与 RET120 冻结 close/prev_close 兼容证据有效。原始成交量单位保持 RQ_VENDOR_ORIGINAL_NOT_CONVERTED。此证据没有自动授权将 RQ total_turnover 替换 Funnel 的 amount。',
              '- Funnel 最小补充为各候选目标日数据及不足的 65 观测暖机段、金额单位/原始价格来源绑定。已有 RQ 原始字段可作为显式核对的本地材料，单位和来源绑定完成前结构/风险预览保持阻断。', '',
              '下一交易日与离线接续：', '',
              '- 2026-10-23 的下一交易日为 2026-10-26，使用既有批准交易日历。',
              '- 目标盘后数据检查：' + str(inventory['target_input']['error']),
              '- 实行情结算状态：' + json.dumps(result['actual_next_session'], ensure_ascii=False),
              '- 本次可用性检查只执行一次；缺失则完成独立样本工作并结束，没有等待或循环轮询。', '',
              '| 合成结算样本（绝非 10 月 26 日实行情） | 原计划 | 样本入场 | 无仓位槽 | 样本成本 | 样本现金 | 样本退出 |',
              '|---|---:|---:|---:|---:|---:|---:|']
    for kind, item in result['samples']['strategies'].items():
        lines.append(f"| {kind} | {item['prior_plan_count']} | {item['entered_positions']} | {item['no_slot']} | {item['base_entry_cost']:.2f} | {item['base_cash']:.2f} | {item['exits']} |")
    lines += ['', '样本报价是人为设置的原始价 10 元，RET120 研究因子 0.37；仅用于代码验证，样本持仓/盈亏均不得计入模型实行情业绩。样本成本保留 20/50bp 往返定义，各边收取一半。',
              '原排序、1% 单票与30%入场名义总仓位限制保持；Deep 向下取整手，RET120 保留碎股研究单位。结算先入场再处理退出；S1 无应到期退出，S20/S120 与停牌延期由独立假数据测试验证。',
              '两个样本账本由 10 月 23 日推进至 26 日；同日重入与跳到 28 日均被拒绝，未写 forward_sessions 成功记录。样本只结算旧计划，不创造 26 日信号或盘前 PIT。', '',
              'Funnel 独立研究：动量候选360、均值回归候选676，覆盖为300/300、500/500、999/1000、3380/3425。结构/风险数据仍有缺口；退出未资格化，不生成 Funnel 模型持仓或收益，也不阻断 Deep/RET120。', '',
              '未来正式前向信号：必须在后续执行事件之前按实际时间冻结计划与哈希；本入口全部保持 NOT_FORWARD。旧23日计划在26日开盘后才生成，事件时序门槛明确拒绝其追认。', '',
              '报告乱码修复：', '',
              '- 旧 report.md 第52至59行已经含字节0x3f问号，不是 UTF-8 解码故障。原因是上轮 PowerShell stdin 文本管道把中文替换后再传入 Python；Python 的 -X utf8 不能修复上游已丢失字符。',
              '- 本报告通过UTF-8源码模板与显式UTF-8文件写入生成，未用中文PowerShell管道；保留原报告不覆盖。',
              '- 原报告 SHA256：`' + result['old_report']['sha256'] + '`。', '',
              '连续运行的最小阻断：', '',
              '1. 10月26日精确封版股票数据：有效开盘/收盘、状态、真实成交观测、涨跌停、tick、整手元数据及冻结复权兼容证据；新T-close信号另需实际同日PIT，不能事后补造。',
              '2. CSI500 的23日/26日精确基准日线及来源口径绑定；仅影响基准比较，不作为研究账户查询前提。',
              '3. Funnel 各候选目标日与足够暖机数据、人民币金额口径绑定；退出资格化仍独立未完成。', '',
              '全部原始来源与旧报告哈希核对：' + str(result['source_hashes_unchanged']),
              '运行边界违规：' + json.dumps(result['boundary_violations'], ensure_ascii=False),
              '生产数据库、Scheduler、冻结配置、历史验收状态均未修改；未新增外部查询、调用真实SDK或Git提交。']
    text = '\n'.join(lines) + '\n'
    if '\ufffd' in text or '????' in text:
        raise ValueError('REPORT_TEXT_ENCODING_CORRUPTED')
    Path(path).write_text(text, encoding='utf-8')


def run(parent, target, output):
    from core import p0_rq_input as rq
    if target != '20261026':
        raise ValueError('THIS_ONE_SHOT_VALIDATION_IS_20261023_TO_20261026_ONLY')
    out = local.output_path(output)
    guard = local.OfflineBoundary(out)
    guard.install()
    out.mkdir(parents=True)
    parent = Path(parent).resolve()
    original_files = [parent / 'report.md', parent / 'report.json', parent / 'verification.json',
                      parent / 'deep/model.duckdb', parent / 'ret120/model.duckdb',
                      parent / 'deep/plan.csv', parent / 'ret120/plan.csv', FOUNDATION, RESEARCH_DB, MARKET_DB]
    proofs = {str(p): rq.sha(p) for p in original_files}
    for p in (Path(__file__), ROOT / 'scripts/run_postclose_local_candidate.py'):
        proofs[str(p)] = rq.sha(p)
    assets, next_input = inspect_assets(parent, target, out)
    proofs[assets['sealed_stocks']['path']] = assets['sealed_stocks']['sha256']
    temp = out / 'TEMP'
    temp.mkdir()
    samples = settlement_samples(parent, target, temp)
    actual = {'status': 'BLOCKED_MISSING_EXACT_TARGET_INPUT', 'models_advanced': False}
    if next_input is not None:
        # This branch is allowed only for an already sealed exact-date local asset.
        rp = local.read_json(ROOT / 'config/ops/p0_core_recovery_v01.json')
        dp = local.read_json(ROOT / 'config/ops/deep_forward_segment_v01.json')
        dates = local.sessions(rp['calendar'], target)
        dp, rp, _ = local.previous_profiles(parent, target, dates, dp, rp)
        try:
            sets, pit_proof = local.membership(rp, target)
            details = local.local_details(next_input, target)
        except (Exception, SystemExit) as e:
            actual = {'status': 'BLOCKED_EXACT_TARGET_CONTEXT', 'models_advanced': False, 'error': str(e)}
        else:
            actual_dir = out / 'actual'
            actual_dir.mkdir()
            actual = {'status': 'ISOLATED_REAL_DATA_MODEL_REPLAY_NOT_FORWARD', 'models_advanced': False,
                      'pit': pit_proof, 'strategies': {}}
            for kind, profile in (('deep', dp), ('ret120', rp)):
                try:
                    actual['strategies'][kind], _ = local.run_model(kind, next_input, profile, sets, details, dates, target, actual_dir / kind)
                    actual['models_advanced'] = True
                except (Exception, SystemExit) as e:
                    actual['strategies'][kind] = {'status': 'BLOCKED', 'error': str(e)}
                    actual['status'] = 'PARTIAL_REAL_DATA_MODEL_REPLAY_NOT_FORWARD'
    result = {'schema': 'POSITIONX_POSTCLOSE_LOCAL_V1', 'status': 'PARTIAL_RESEARCH_CANDIDATE',
              'verified_at': rq.clock().isoformat(), 'forward_acceptance': False,
              'accepted_result': local.read_json(parent / 'report.json'), 'assets': assets,
              'samples': samples, 'actual_next_session': actual,
              'old_report': {'path': str(parent / 'report.md'), 'sha256': proofs[str(parent / 'report.md')]},
              'source_files': [{'path': p, 'sha256': digest, 'unchanged': rq.sha(p) == digest} for p, digest in proofs.items()],
              'boundary_violations': guard.violations}
    result['source_hashes_unchanged'] = all(p['unchanged'] for p in result['source_files'])
    if not result['source_hashes_unchanged'] or guard.violations:
        raise ValueError('ISOLATION_VERIFICATION_FAILED')
    local.write_json(out / 'report.json', result)
    write_v1_report(out / 'report.md', result)
    print(json.dumps({'status': result['status'], 'report': str(out / 'report.md'),
                      'actual_next_session': actual['status']}, ensure_ascii=False))
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--parent', default=str(ACCEPTED))
    p.add_argument('--date', default='20261026')
    p.add_argument('--output', required=True)
    a = p.parse_args()
    run(a.parent, a.date, a.output)


if __name__ == '__main__':
    main()
