"""Focused offline checks for the three-strategy daily closeout wiring.

Every writable path is a TemporaryDirectory, all collection/report subprocesses
are mocked, and no task, provider SDK, credential, or external service is used.
"""
from contextlib import ExitStack
import csv
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

from core import p0_rq_input as rq
from scripts import run_data_only_daily as task
from scripts import run_postclose_daily as daily
from scripts import run_postclose_local_candidate as local


def core_source(root=Path('unused')):
    meta = dict(version='core-28', database_sha256='db-hash',
                qualification_scope='CORE_MODELS_V1', quality_status='CORE_MODELS_PASS_V1',
                engineering_only=True, full_p0_pass=False, execution_qualified=True,
                model_continuation_qualified=True)
    return SimpleNamespace(meta=meta, root=root, db=root / 'market.duckdb',
                           lineage=lambda basis: dict(version=meta['version'], basis=basis))


class InputSelection(unittest.TestCase):
    def test_default_reader_does_not_opt_into_partial_scope(self):
        with patch.object(rq, 'open_inputs', side_effect=rq.InputBlocked('RQ_EXACT_DATE_VERSION_MISSING:20261028')) as read:
            with self.assertRaises(rq.InputBlocked):
                daily.full_model_input('20261028')
        self.assertEqual(read.call_args_list[0].kwargs, {})
        self.assertEqual(read.call_count, 1)

    def test_core_opt_in_accepts_only_explicit_complete_model_contract(self):
        source = core_source()
        with patch.object(rq, 'open_inputs', side_effect=[rq.InputBlocked('RQ_EXACT_DATE_VERSION_MISSING:20261028'), source]) as read:
            self.assertIs(daily.full_model_input('20261028', allow_core_models=True), source)
        self.assertEqual(read.call_args_list[1].kwargs, {'scope': 'CORE_MODELS_V1'})
        for key, wrong in [('execution_qualified', False), ('model_continuation_qualified', False),
                           ('quality_status', 'DAILY_SIGNALS_PASS_V1'), ('full_p0_pass', True)]:
            bad = core_source(); bad.meta[key] = wrong
            with self.subTest(key=key), patch.object(rq, 'open_inputs', side_effect=[
                    rq.InputBlocked('RQ_EXACT_DATE_VERSION_MISSING:20261028'), bad]):
                with self.assertRaisesRegex(ValueError, 'CORE_MODELS_PUBLICATION_NOT_QUALIFIED'):
                    daily.full_model_input('20261028', allow_core_models=True)

    def test_corrupt_full_p0_never_falls_back(self):
        with patch.object(rq, 'open_inputs', side_effect=rq.InputBlocked('DATABASE_HASH_MISMATCH')) as read:
            with self.assertRaisesRegex(rq.InputBlocked, 'DATABASE_HASH_MISMATCH'):
                daily.full_model_input('20261028', allow_core_models=True)
        self.assertEqual(read.call_count, 1)

    def test_full_p0_is_still_preferred(self):
        source = core_source(); source.meta.update(quality_status='PASS', engineering_only=False)
        with patch.object(rq, 'open_inputs', return_value=source) as read:
            self.assertIs(daily.full_model_input('20261028', allow_core_models=True), source)
        self.assertEqual(read.call_count, 1)

    def test_signal_report_cannot_be_model_parent(self):
        self.assertFalse(daily.is_model_report(dict(mode='ISOLATED_DAILY_SIGNAL_RESEARCH', forward_acceptance=False)))
        self.assertTrue(daily.is_model_report(dict(mode='ACTUAL_MODEL_FORWARD', forward_acceptance=True)))
        self.assertFalse(daily.is_model_report(dict(mode='ISOLATED_LOCAL_MODEL_REPLAY', forward_acceptance=True)))

    def test_real_parent_freeze_after_open_remains_blocked(self):
        root = Path('unused').resolve()
        report = dict(business_date='20261027', mode='ISOLATED_LOCAL_MODEL_REPLAY', forward_acceptance=False,
                      strategies={'deep': {'status': 'MODEL_REPLAY_COMPLETE', 'forward_acceptance': False,
                                           'policy_hash': 'policy'}})
        hashes = {'deep/model.duckdb': 'ledger'}
        proof = dict(research_state='NOT_FORWARD', forward_qualified=False, next_session='20261028',
                     ledger_sha256='ledger', plan_sha256='plan', frozen_at='2026-10-28T09:30:00+08:00')
        with patch.object(daily, 'model_report', return_value=(root, report, hashes)), \
             patch.object(daily, 'verified_file'), patch.object(daily, 'policy', return_value=(None, 'policy')), \
             patch.object(local, 'read_json', return_value=proof), patch.object(rq, 'sha', return_value='plan'):
            with self.assertRaisesRegex(ValueError, 'PLAN_NOT_FROZEN_BEFORE_EXECUTION_EVENT'):
                daily.parent_profile(root, 'deep', {}, ['20261027', '20261028'], '20261028')

    def test_model_cursor_cannot_skip_a_session_or_settle_twice(self):
        for cursor in ('20261026', '20261028'):
            with self.subTest(cursor=cursor), self.assertRaises(ValueError):
                local.require_next_session({'last_processed_date': cursor}, ['20261026', '20261027', '20261028'], '20261028')


class ActualModelTiming(unittest.TestCase):
    def test_no_historical_promotion_and_exact_real_window(self):
        for now, day, expected in [
                ('2026-10-28T16:10:00+08:00', '20261028', False),
                ('2026-10-29T15:29:59+08:00', '20261029', False),
                ('2026-10-29T15:30:00+08:00', '20261029', True),
                ('2026-10-29T17:14:59+08:00', '20261029', True),
                ('2026-10-29T17:15:00+08:00', '20261029', False),
                ('2026-10-30T16:10:00+08:00', '20261029', False)]:
            with self.subTest(now=now, day=day), patch.object(rq, 'clock', return_value=datetime.fromisoformat(now)):
                self.assertEqual(daily.actual_forward_window(day), expected)
                if expected:
                    local.require_actual_model_window(day)
                else:
                    with self.assertRaisesRegex(ValueError, 'OUTSIDE_AUTHORIZED_REAL_WINDOW'):
                        local.require_actual_model_window(day)

    def test_future_start_requires_no_inherited_historical_parent(self):
        with patch.object(daily, 'actual_forward_window', return_value=True), \
             patch.object(daily, 'actual_model_parent', return_value=None), \
             patch.object(local, 'read_json', return_value={'calendar': 'unused'}), \
             patch.object(local, 'sessions', return_value=['20261028', '20261029']), \
             patch.object(daily, 'usable_model_run', side_effect=AssertionError('historical parent must not be inherited')):
            self.assertEqual(daily.required_execution_symbols('20261029'), [])

    def test_actual_seed_is_independent_empty_and_not_formal_ledger(self):
        from research.research_store import ResearchStore
        policy = SimpleNamespace(strategy_id='fake', fingerprint=lambda: 'policy')
        module = SimpleNamespace(POLICY=policy, initial_state=lambda p: {'base_cash': 1_000_000.0})
        with TemporaryDirectory() as name:
            root = Path(name).resolve(); (root / 'reports').mkdir()
            dest = root / 'reports/model.duckdb'
            with patch.object(local, 'ROOT', root), patch.object(rq, 'clock', return_value=datetime.fromisoformat('2026-10-29T16:10:00+08:00')):
                state, proof = local.seed_research_ledger(dest, module, None, ['20261028', '20261029'],
                                                         '20261029', actual_forward_start=True)
            self.assertEqual(proof['mode'], 'EMPTY_ACTUAL_MODEL_FORWARD_START')
            self.assertEqual(proof['inherited_positions'], [])
            self.assertEqual(proof['inherited_plans'], [])
            self.assertTrue(state['actual_model_forward'])
            with ResearchStore(dest) as store:
                self.assertEqual(store.con.execute('SELECT count(*) FROM forward_positions').fetchone()[0], 0)
                self.assertEqual(store.con.execute('SELECT count(*) FROM forward_sessions').fetchone()[0], 0)


class DailyFlow(unittest.TestCase):
    def mocked_report(self, day, now):
        temp = TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve(); out = root / 'new-report'
        source = core_source(root)
        guard = SimpleNamespace(violations=[], blocked=(), install=lambda: None)
        stack = ExitStack(); self.addCleanup(stack.close)
        replacements = [
            (rq, 'clock', Mock(return_value=datetime.fromisoformat(now))),
            (rq, 'sha', Mock(return_value='sha')),
            (daily, 'full_model_input', Mock(return_value=source)),
            (daily, 'daily_reports', Mock(return_value=[])),
            (local, 'output_path', lambda p: p), (local, 'OfflineBoundary', Mock(return_value=guard)),
            (local, 'read_json', Mock(return_value={'calendar': 'unused', 'pit_root': 'actual-preopen'})),
            (local, 'sessions', Mock(return_value=['20261027' if day=='20261028' else '20261028', day])),
            (local, 'membership', Mock(return_value=({'000905.SH': {'000001.SZ'}}, {'sha256': 'pit', 'manifest_sha256': 'manifest'}))),
            (local, 'local_details', Mock(return_value={})),
            (daily, 'usable_model_run', Mock(side_effect=lambda d, *a, **k: (_ for _ in ()).throw(LookupError()) if d==day else root / 'historical')),
            (daily, 'actual_model_parent', Mock(return_value=None)),
            (daily, 'parent_profile', Mock(return_value={})),
            (daily, 'operational_summary', Mock(return_value={'actual_model_entries_today': 0})),
            (daily, 'write_report', Mock()),
            (local, 'run_funnel', Mock(return_value={'status': 'STRUCTURE_RESEARCH_COMPLETE', 'full_model_status': 'FUNNEL_FULL_MODEL_BLOCKED_EXIT_POLICY'}))]
        for owner, key, value in replacements:
            stack.enter_context(patch.object(owner, key, value))
        model = stack.enter_context(patch.object(local, 'run_model', return_value=({'status': 'MODEL_REPLAY_COMPLETE'}, None)))
        result = daily.run(day, out, allow_core_models=True)
        return result, model

    def test_historical_full_models_run_and_funnel_exit_does_not_block(self):
        result, model = self.mocked_report('20261028', '2026-10-28T20:30:00+08:00')
        self.assertEqual(result['status'], 'TWO_STRATEGY_REPORT_COMPLETE')
        self.assertFalse(result['forward_acceptance'])
        self.assertEqual(model.call_count, 2)
        self.assertTrue(all(c.kwargs['actual_forward_start'] is False for c in model.call_args_list))
        self.assertEqual(result['funnel']['full_model_status'], 'FUNNEL_FULL_MODEL_BLOCKED_EXIT_POLICY')

    def test_natural_eligible_day_starts_both_empty_accounts(self):
        result, model = self.mocked_report('20261029', '2026-10-29T16:10:00+08:00')
        self.assertEqual(result['status'], 'TWO_STRATEGY_REPORT_COMPLETE')
        self.assertEqual(result['mode'], 'ACTUAL_MODEL_FORWARD')
        self.assertTrue(result['forward_acceptance'])
        for call in model.call_args_list:
            self.assertTrue(call.kwargs['fresh_research'])
            self.assertTrue(call.kwargs['actual_forward_start'])
            self.assertTrue(call.kwargs['actual_model_forward'])
        self.assertEqual(result['parent_runs'], {})

    def test_published_core_consumed_after_cutoff_without_collection(self):
        with TemporaryDirectory() as name:
            root = Path(name); source = core_source(root)
            report = {'status': 'TWO_STRATEGY_REPORT_COMPLETE', 'strategies': {
                k: {'operations': {'actual_model_entries_today': 0}} for k in ('deep', 'ret120')},
                'funnel': {'full_model_status': 'FUNNEL_FULL_MODEL_BLOCKED_EXIT_POLICY'}}
            with patch.object(task, 'OPS', root / 'ops'), \
                 patch.object(rq, 'clock', return_value=datetime.fromisoformat('2026-10-28T20:30:00+08:00')), \
                 patch.object(rq, 'require_trading_date'), patch.object(rq, 'sha', return_value='sha'), \
                 patch.object(daily, 'full_model_input', return_value=source) as reader, \
                 patch.object(task, 'model_report', return_value=(root, report, False)), \
                 patch.object(task, 'result_files'), \
                 patch.object(task.close, 'run', side_effect=AssertionError('no redundant acquisition')) as collect:
                result = task.run('postclose', '20261028')
            self.assertEqual(result['status'], 'GREEN')
            reader.assert_called_once_with('20261028', allow_core_models=True)
            collect.assert_not_called()

    def test_funnel_preview_uses_same_source_pit_and_unit_proof(self):
        from scripts import qualify_postclose_funnel as qualify
        with TemporaryDirectory() as name:
            out = Path(name); source = core_source(out); pit = {'sha256': 'actual-pit'}
            with patch.object(qualify, 'run', return_value={'status': 'PASS_STRUCTURE_ADV20_INPUTS',
                    'full_model_status': 'FUNNEL_FULL_MODEL_BLOCKED_EXIT_POLICY'}) as run:
                result = local.run_funnel(source, {'000905.SH': {'000001.SZ'}}, '20261028', out,
                                         amount_unit_binding='CNY-proof', pit_proof=pit)
            self.assertIs(run.call_args.kwargs['source'], source)
            self.assertIs(run.call_args.kwargs['membership_proof'], pit)
            self.assertTrue(run.call_args.kwargs['execution_preview'])
            self.assertEqual(result['full_model_status'], 'FUNNEL_FULL_MODEL_BLOCKED_EXIT_POLICY')


class TimeoutAndFunnelStatus(unittest.TestCase):
    """Only the bounded child runtime and four-column Funnel receipt additions."""

    def test_new_calculation_timeout_is_bounded_by_cap_and_real_deadline(self):
        class ChildNotStarted(Exception):
            pass
        for day, now, expected in [
                ('20261028', '2026-10-28T20:30:00+08:00', 600),
                ('20261029', '2026-10-29T16:00:00+08:00', 600),
                ('20261029', '2026-10-29T17:14:00+08:00', 60),
                ('20261029', '2026-10-29T17:14:59.500000+08:00', 0.5)]:
            with self.subTest(day=day, now=now), \
                 patch.object(rq, 'clock', return_value=datetime.fromisoformat(now)), \
                 patch.object(daily, 'daily_reports', return_value=[]), \
                 patch.object(task.subprocess, 'run', side_effect=ChildNotStarted) as child:
                with self.assertRaises(ChildNotStarted):
                    task.model_report(core_source(), day)
                self.assertEqual(child.call_args.kwargs['timeout'], expected)

    def test_deadline_does_not_launch_new_historical_calculation(self):
        for now in ('2026-10-29T17:15:00+08:00', '2026-10-29T20:30:00+08:00'):
            with self.subTest(now=now), \
                 patch.object(rq, 'clock', return_value=datetime.fromisoformat(now)), \
                 patch.object(daily, 'daily_reports', return_value=[]), \
                 patch.object(task.subprocess, 'run') as child:
                with self.assertRaisesRegex(ValueError, 'MODEL_REPORT_FINAL_DEADLINE_REACHED_NO_NEW_CALCULATION'):
                    task.model_report(core_source(), '20261029')
                child.assert_not_called()

    def test_complete_report_can_be_reused_after_deadline(self):
        root = Path('unused').resolve(); source = core_source(root)
        report = dict(status='TWO_STRATEGY_REPORT_COMPLETE', closeout_version=daily.CLOSEOUT_VERSION,
                      business_date='20261029', input={'version': source.meta['version']},
                      database_sha256=source.meta['database_sha256'], manifest_sha256='manifest',
                      mode='ACTUAL_MODEL_FORWARD', forward_acceptance=True)
        with patch.object(rq, 'clock', return_value=datetime.fromisoformat('2026-10-29T20:30:00+08:00')), \
             patch.object(rq, 'sha', return_value='manifest'), \
             patch.object(daily, 'daily_reports', return_value=[('20261029', 'time', root, report)]), \
             patch.object(daily, 'model_report', return_value=(root, report, {})), \
             patch.object(task.subprocess, 'run') as child:
            actual_root, actual_report, reused = task.model_report(source, '20261029')
        self.assertTrue(reused)
        self.assertEqual(actual_root, root)
        self.assertIs(actual_report, report)
        child.assert_not_called()

    def test_four_column_csv_shows_funnel_structure_risk_and_exit_block(self):
        with TemporaryDirectory() as name:
            root = Path(name)
            result = dict(date='20261028', version='core-28', status='GREEN',
                          strategies={'deep': 'PASS', 'ret120': 'PASS'},
                          funnel={'status': 'STRUCTURE_RESEARCH_COMPLETE',
                                  'structure_input_status': 'PASS_STRUCTURE_ADV20_INPUTS',
                                  'execution_preview': {'status': 'PARTIAL_EXECUTION_PREVIEW'},
                                  'full_model_status': 'FUNNEL_FULL_MODEL_BLOCKED_EXIT_POLICY'})
            with patch.object(task, 'ROOT', root):
                task.result_files(result)
            with (root / 'DATA_STATUS.csv').open(encoding='utf-8-sig', newline='') as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(rows[0], ['target_date', 'strategy', 'status', 'version'])
            self.assertTrue(all(len(row)==4 for row in rows))
            self.assertEqual(rows[-1], ['20261028', 'funnel',
                'structure=PASS_STRUCTURE_ADV20_INPUTS;risk=PARTIAL_EXECUTION_PREVIEW;full=FUNNEL_FULL_MODEL_BLOCKED_EXIT_POLICY',
                'core-28'])


if __name__ == '__main__':
    unittest.main()
