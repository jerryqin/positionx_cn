"""Offline Funnel integration checks: temporary artifacts and mocked consumers only."""
from contextlib import ExitStack
import csv
import json
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from core import p0_rq_input as rq
from scripts import run_postclose_daily as daily
from scripts import run_postclose_local_candidate as local
from scripts import run_data_only_daily as task


class FunnelDailyComplete(unittest.TestCase):
    def test_actual_first_account_does_not_inherit_historical_funnel(self):
        old = {'funnel': {'model': {'status': 'FUNNEL_MODEL_SESSION_COMPLETE', 'forward_acceptance': False}}}
        with patch.object(daily, 'daily_reports', return_value=[('20261028', 'time', Path('old'), old)]):
            self.assertIsNone(daily.funnel_parent('20261029', ['20261028', '20261029'], actual_model_forward=True))

    def parent_check(self, *, frozen='2026-10-29T16:00:00+08:00', parent_day='20261029'):
        root = Path('unused').resolve()
        report = {'business_date': parent_day, 'funnel': {'model': {
            'status': 'FUNNEL_MODEL_SESSION_COMPLETE', 'forward_acceptance': True}}}
        proof = dict(next_session='20261030', forward_qualified=True, ledger_sha256='ledger',
                     frozen_at=frozen, plan_sha256='plan')
        stack = ExitStack(); self.addCleanup(stack.close)
        stack.enter_context(patch.object(daily, 'daily_reports', return_value=[(parent_day, 'time', root, report)]))
        stack.enter_context(patch.object(daily, 'model_report', return_value=(root, report, {'funnel/model.duckdb': 'ledger'})))
        stack.enter_context(patch.object(daily, 'verified_file'))
        stack.enter_context(patch.object(local, 'read_json', return_value=proof))
        stack.enter_context(patch.object(rq, 'sha', return_value='plan'))
        return root

    def test_actual_funnel_parent_is_adjacent_and_pre_event_sealed(self):
        root = self.parent_check()
        self.assertEqual(daily.funnel_parent('20261030', ['20261029', '20261030'], actual_model_forward=True), root / 'funnel')

    def test_late_funnel_plan_is_not_accepted(self):
        self.parent_check(frozen='2026-10-30T09:30:00+08:00')
        with self.assertRaisesRegex(ValueError, 'PLAN_NOT_FROZEN_BEFORE_EXECUTION_EVENT'):
            daily.funnel_parent('20261030', ['20261029', '20261030'], actual_model_forward=True)

    def test_funnel_parent_cannot_skip_a_model_day(self):
        self.parent_check(parent_day='20261028')
        with self.assertRaises(ValueError):
            daily.funnel_parent('20261030', ['20261028', '20261029', '20261030'], actual_model_forward=True)

    def test_reuse_keeps_actual_core_operations_and_artifacts(self):
        with TemporaryDirectory() as name:
            root = Path(name).resolve(); old = root / 'old'; out = root / 'new'; (old / 'deep').mkdir(parents=True); out.mkdir()
            names = ('model.duckdb', 'plan.csv', 'plan_freeze.json', 'signals.csv', 'positions.csv',
                     'exits.csv', 'state.json', 'summary.json', 'operations.csv', 'entry_decisions.csv', 'position_actions.csv')
            for file in names:
                (old / 'deep' / file).write_text(file, encoding='utf-8')
            hashes = {'deep/' + file: rq.sha(old / 'deep' / file) for file in names}
            operations = {'actual_model_entries_today': 26}
            report = {'strategies': {'deep': dict(status='MODEL_REPLAY_COMPLETE', policy_hash='policy', operations=operations)}}
            with patch.object(daily, 'model_report', return_value=(old, report, hashes)), \
                 patch.object(daily, 'policy', return_value=(None, 'policy')):
                result = daily.reuse_model(old, 'deep', out, reuse_operations=True)
            self.assertEqual(result['operations'], operations)
            for file in names:
                self.assertEqual((old / 'deep' / file).read_bytes(), (out / 'deep' / file).read_bytes())

    def test_funnel_upgrade_does_not_recalculate_existing_core_results(self):
        with TemporaryDirectory() as name, ExitStack() as stack:
            root = Path(name).resolve(); output = root / 'new'
            source = SimpleNamespace(root=root, db=root / 'source.duckdb',
                meta=dict(version='exact', database_sha256='db', qualification_scope='CORE_MODELS_V1', full_p0_pass=False),
                lineage=lambda basis: {'version': 'exact'})
            guard = SimpleNamespace(violations=[], blocked=(), install=lambda: None)
            patches = [(daily, 'full_model_input', Mock(return_value=source)),
                       (daily, 'daily_reports', Mock(return_value=[])),
                       (rq, 'clock', Mock(return_value=datetime.fromisoformat('2026-10-28T21:00:00+08:00'))),
                       (rq, 'sha', Mock(return_value='hash')), (local, 'output_path', lambda p:p),
                       (local, 'OfflineBoundary', Mock(return_value=guard)),
                       (local, 'read_json', Mock(return_value={'calendar': 'calendar'})),
                       (local, 'sessions', Mock(return_value=['20261027', '20261028'])),
                       (local, 'membership', Mock(return_value=({'000905.SH': {'000001.SZ'}}, {'sha256': 'pit'}))),
                       (local, 'local_details', Mock(return_value={})),
                       (daily, 'usable_model_run', Mock(return_value=root / 'accepted-v1')),
                       (daily, 'reuse_model', Mock(return_value={'status': 'MODEL_REPLAY_COMPLETE', 'operations': {'entries': 26}})),
                       (daily, 'write_report', Mock())]
            for owner, attr, value in patches:
                stack.enter_context(patch.object(owner, attr, value))
            calculate = stack.enter_context(patch.object(local, 'run_model', side_effect=AssertionError('no core replay')))
            operations = stack.enter_context(patch.object(daily, 'operational_summary', side_effect=AssertionError('no core recalculation')))
            funnel = stack.enter_context(patch.object(local, 'run_funnel', return_value={'model': {
                'status': 'FUNNEL_MODEL_SESSION_COMPLETE', 'settlement': {'status': 'CALCULATED'}}}))
            result = daily.run('20261028', output, allow_core_models=True)
            self.assertEqual(result['three_strategy_status'], 'THREE_STRATEGY_MODELS_COMPLETE')
            calculate.assert_not_called(); operations.assert_not_called()
            self.assertIsNone(funnel.call_args.kwargs['previous_run'])
            self.assertFalse(funnel.call_args.kwargs['actual_model_forward'])

    def test_csv_reports_new_funnel_model_status(self):
        with TemporaryDirectory() as name:
            root = Path(name)
            with patch.object(task, 'ROOT', root):
                task.result_files({'date': '20261028', 'version': 'exact', 'funnel': {
                    'status': 'STRUCTURE_RESEARCH_COMPLETE', 'execution_preview': {'status': 'PASS'},
                    'full_model_status': 'OLD_EXIT_POLICY_BLOCK', 'model': {'status': 'FUNNEL_MODEL_SESSION_COMPLETE'}}})
            with (root / 'DATA_STATUS.csv').open(encoding='utf-8-sig', newline='') as f:
                rows = list(csv.reader(f))
            self.assertEqual(len(rows[-1]), 4)
            self.assertIn('full=FUNNEL_MODEL_SESSION_COMPLETE', rows[-1][2])
            self.assertNotIn('OLD_EXIT_POLICY_BLOCK', rows[-1][2])

    def test_funnel_failure_keeps_core_details_from_nonzero_child(self):
        with TemporaryDirectory() as name:
            root = Path(name).resolve(); report_root = root / 'reports/postclose_daily/new'
            source = SimpleNamespace(root=root, meta={'version': 'exact', 'database_sha256': 'db'})
            report = dict(status='TWO_STRATEGY_REPORT_COMPLETE', three_strategy_status='PARTIAL',
                          business_date='20261028', closeout_version=daily.CLOSEOUT_VERSION,
                          input={'version': 'exact'}, database_sha256='db', manifest_sha256='hash',
                          strategies={'deep': {'operations': {'entries': 26}}},
                          funnel={'model': {'status': 'BLOCKED', 'error': 'specific contract'}})
            child = SimpleNamespace(returncode=3, stdout=json.dumps({'report': str(report_root/'report.md')}))
            with patch.object(task, 'ROOT', root), patch.object(daily, 'daily_reports', return_value=[]), \
                 patch.object(rq, 'sha', return_value='hash'), \
                 patch.object(rq, 'clock', return_value=datetime.fromisoformat('2026-10-28T21:00:00+08:00')), \
                 patch.object(daily, 'model_report', return_value=(report_root, report, {})), \
                 patch.object(task.subprocess, 'run', return_value=child):
                _, result, reused = task.model_report(source, '20261028')
            self.assertFalse(reused)
            self.assertEqual(result['strategies']['deep']['operations']['entries'], 26)
            self.assertEqual(result['funnel']['model']['error'], 'specific contract')


if __name__ == '__main__':
    unittest.main()
