"""Signal-scope checks: synthetic frames, fake publication, temporary files only."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd

from core import p0_rq_input as rq
from scripts import run_deep_oversold_csi500_forward as deep
from scripts import run_postclose_daily as daily
from scripts import run_postclose_local_candidate as local
from scripts import run_ret120_csi500_forward as ret


class FrozenSignalOnlyTests(unittest.TestCase):
    def test_ret_uses_real_snapshot_and_hygiene_without_execution_or_financial_exports(self):
        dates = pd.bdate_range(end='2026-10-28', periods=121).strftime('%Y%m%d').tolist()
        members = [f'{i:06d}.SZ' for i in range(1, 501)]
        histories = {s: pd.DataFrame({'close': [1.] * 120 + [1. + i / 1000]}, index=dates)
                     for i, s in enumerate(members)}
        details = {s: {'InstrumentName': 'NORMAL', 'OpenDate': '20100101',
                       'InstrumentStatus': 0, 'MinLimitOrderVolume': 100} for s in members}
        details[members[-1]]['InstrumentName'] = '*ST'
        source = SimpleNamespace(meta={'qualification_scope': 'DAILY_SIGNALS_V1'},
            histories=lambda *args, **kwargs: histories,
            lineage=lambda adjustment: {'source': 'RQDATA', 'version': 'FAKE', 'adjustment': adjustment})
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            out = root / 'reports' / 'ret120'
            out.parent.mkdir()
            stack.enter_context(patch.object(local, 'ROOT', root))
            stack.enter_context(patch.object(rq, 'enabled', return_value=True))
            stack.enter_context(patch.object(ret, 'verify_frozen_prerequisites', return_value={'fixture': 'HASH'}))
            forbidden = []
            for owner, name in ((ret, 'process_entries'), (ret, 'process_exits_and_mark'),
                                (ret, 'state_counts'), (rq, 'execution_bar'),
                                (local, 'model_exports'), (local, 'plan_freeze')):
                forbidden.append(stack.enter_context(patch.object(owner, name, side_effect=AssertionError(name))))
            snapshot_call = stack.enter_context(patch.object(ret, 'build_daily_snapshot', wraps=ret.build_daily_snapshot))
            detect_call = stack.enter_context(patch.object(ret, 'detect_signals', wraps=ret.detect_signals))
            result, curve = local.run_model('ret120', source, {}, {'000905.SH': set(members)},
                details, dates, dates[-1], out, fresh_research=True, signals_only=True)
            self.assertEqual(result['snapshot'], {'universe_count': 500, 'valid_feature_count': 500, 'q5_count': 100})
            self.assertEqual(result['signals'], {'q5': 100, 'pending': 99, 'duplicate': 0, 'excluded': 1})
            self.assertEqual(result['signal_summary']['raw_candidate_count'], 100)
            self.assertIsNone(result['signal_summary']['actual_model_entries_today'])
            self.assertEqual(result['signal_summary']['settlement']['status'], 'NOT_RUN')
            self.assertNotIn('state', result)
            self.assertIsNone(curve)
            self.assertFalse(result['model_continuation_qualified'])
            self.assertEqual(sorted(p.name for p in out.iterdir()),
                ['model.duckdb', 'signal_candidates.csv', 'signals.csv', 'snapshot.csv', 'summary.json'])
            rows = pd.read_csv(out / 'signal_candidates.csv')
            self.assertTrue(rows.future_entry_units.isna().all())
            self.assertTrue(rows.future_open_price.isna().all())
            self.assertTrue((~rows.capacity_evaluated).all())
            self.assertEqual(rows.iloc[-1].reason, 'SIGNAL_KNOWN_ST')
            with local.read_db(out / 'model.duckdb') as con:
                self.assertEqual(con.execute('SELECT count(*) FROM forward_equity').fetchone()[0], 0)
                state = json.loads(con.execute('SELECT state_json FROM forward_state').fetchone()[0])
                self.assertEqual(state['last_processed_date'], dates[-2])
                self.assertEqual(con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='postclose_model_sessions'").fetchone()[0], 0)
            snapshot_call.assert_called_once()
            detect_call.assert_called_once()
            for call in forbidden:
                call.assert_not_called()

    def test_deep_uses_frozen_ret20_and_does_not_require_tick_on_empty_research_seed(self):
        dates = pd.bdate_range(end='2026-10-28', periods=120).strftime('%Y%m%d').tolist()
        symbol = '000001.SZ'
        source = SimpleNamespace(meta={'qualification_scope': 'DAILY_SIGNALS_V1'},
            histories=lambda *args, **kwargs: {symbol: pd.DataFrame({'close': [1.] * 119 + [.7]}, index=dates)},
            lineage=lambda adjustment: {'source': 'RQDATA', 'version': 'FAKE', 'adjustment': adjustment})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / 'reports' / 'deep'
            out.parent.mkdir()
            with patch.object(local, 'ROOT', root), \
                    patch.object(deep, 'process_entries', side_effect=AssertionError('entry')) as entries, \
                    patch.object(deep, 'process_exits_and_mark', side_effect=AssertionError('exit')) as exits, \
                    patch.object(rq, 'execution_bar', side_effect=AssertionError('tick')) as tick, \
                    patch.object(deep, 'detect_signals', wraps=deep.detect_signals) as detect:
                result, curve = local.run_model('deep', source, {}, {'000905.SH': {symbol}},
                    {symbol: {'InstrumentStatus': 0}}, dates, dates[-1], out,
                    fresh_research=True, signals_only=True)
            self.assertEqual(result['signals']['new'], 1)
            self.assertEqual(result['signal_summary']['raw_candidate_count'], 1)
            self.assertEqual(result['signal_summary']['capacity_status'], 'NOT_EVALUATED')
            self.assertIsNone(curve)
            self.assertFalse((out / 'plan.csv').exists())
            with self.assertRaisesRegex(ValueError, 'SIGNAL_RESEARCH_LEDGER_CANNOT_BE_MODEL_PARENT'):
                local.copy_ledger(out / 'model.duckdb', out / 'NEVER.duckdb',
                    deep.POLICY.strategy_id, deep.POLICY.fingerprint(), dates, dates[-1])
            self.assertFalse((out / 'NEVER.duckdb').exists())
            detect.assert_called_once()
            entries.assert_not_called()
            exits.assert_not_called()
            tick.assert_not_called()

    def test_signal_input_cannot_run_full_or_inherit_a_model_ledger(self):
        source = SimpleNamespace(meta={'qualification_scope': 'DAILY_SIGNALS_V1'})
        with self.assertRaisesRegex(ValueError, 'SIGNALS_ONLY_REQUIRES_EMPTY_FRESH_RESEARCH_SEED'):
            local.run_model('deep', source, {}, {}, {}, [], '20261028', Path('NEVER'), signals_only=True)
        with self.assertRaisesRegex(ValueError, 'DAILY_SIGNALS_INPUT_CANNOT_RUN_MODEL_SESSION'):
            local.run_model('deep', source, {}, {}, {}, [], '20261028', Path('NEVER'), fresh_research=True)
        with self.assertRaisesRegex(ValueError, 'SIGNALS_ONLY_CANNOT_USE_MODEL_PARENT'):
            daily.run('20261028', previous_run='NEVER', signals_only=True)


class SignalDailyEntryTests(unittest.TestCase):
    def fixture(self, stack, root, *, deep_blocked=False):
        source_root = root / 'source'
        source_root.mkdir()
        (source_root / 'manifest.json').write_text('{}', encoding='utf-8')
        source = SimpleNamespace(root=source_root, db=source_root / 'daily.duckdb',
            meta={'version': 'FAKE_V1', 'database_sha256': 'DB1', 'qualification_scope': 'DAILY_SIGNALS_V1',
                  'quality_status': 'DAILY_SIGNALS_PASS_V1', 'engineering_only': True,
                  'full_p0_pass': False, 'execution_qualified': False},
            lineage=lambda adjustment: {'source': 'RQDATA', 'version': source.meta['version']})
        pit = {'sha256': 'PIT', 'manifest_sha256': 'PIT_MANIFEST', 'manifest': 'REAL_PIT_FIXTURE'}
        original_read = local.read_json
        def read(path):
            return {'calendar': 'FIXTURE'} if Path(path).name in (
                'p0_core_recovery_v01.json', 'deep_forward_segment_v01.json') else original_read(path)
        def calculate(kind, source, profile, sets, details, dates, day, out, **kwargs):
            self.assertEqual(kwargs, {'fresh_research': True, 'signals_only': True})
            if kind == 'deep' and deep_blocked:
                raise ValueError('DEEP_FRONT_AFFINE_INPUT_MISSING')
            out.mkdir()
            item = {'status': 'SIGNAL_RESEARCH_ONLY', 'policy_hash': kind + '_FINGERPRINT',
                    'forward_acceptance': False, 'model_continuation_qualified': False,
                    'execution_qualified': False,
                    'signal_summary': {'raw_candidate_count': 1, 'new_signal_record_count': 1,
                        'signal_status_counts': {}, 'capacity_status': 'NOT_EVALUATED',
                        'actual_model_entries_today': None}}
            for name in ('model.duckdb', 'signals.csv', 'signal_candidates.csv', 'snapshot.csv'):
                (out / name).write_text('fixture', encoding='utf-8')
            local.write_json(out / 'summary.json', item)
            return item, None
        for owner, name, value in ((daily, 'ROOT', root), (daily, 'DAILY_ROOT', root / 'reports/postclose_daily')):
            stack.enter_context(patch.object(owner, name, value))
        stack.enter_context(patch.object(local.OfflineBoundary, 'install'))
        stack.enter_context(patch.object(local, 'output_path', side_effect=lambda p: Path(p)))
        stack.enter_context(patch.object(local, 'read_json', side_effect=read))
        stack.enter_context(patch.object(local, 'sessions', return_value=['20261027', '20261028']))
        stack.enter_context(patch.object(local, 'membership', side_effect=lambda *args: ({'000905.SH': set()}, dict(pit))))
        stack.enter_context(patch.object(local, 'local_details', return_value={}))
        stack.enter_context(patch.object(daily, 'policy', side_effect=lambda k: (None, k + '_FINGERPRINT')))
        opener = stack.enter_context(patch.object(rq, 'open_inputs', return_value=source))
        model = stack.enter_context(patch.object(local, 'run_model', side_effect=calculate))
        return source, pit, opener, model

    def test_same_input_reuses_report_and_changed_version_or_pit_recalculates(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            source, pit, opener, model = self.fixture(stack, root)
            first = daily.run('20261028', signals_only=True)
            first_path = Path(first['output_directory']) / 'report.json'
            original_hash = rq.sha(first_path)
            second = daily.run('20261028', signals_only=True)
            self.assertEqual(model.call_count, 2)
            self.assertEqual(first, second)
            self.assertEqual(first['status'], 'TWO_STRATEGY_SIGNAL_RESEARCH_COMPLETE')
            with self.assertRaisesRegex(LookupError, 'EXACT_PREVIOUS_MODEL_DAY_MISSING'):
                daily.usable_model_run('20261028', 'ret120')
            with self.assertRaisesRegex(ValueError, 'MODEL_PARENT_MUST_REMAIN_NOT_FORWARD'):
                daily.model_report(Path(first['output_directory']))
            source.meta.update(version='FAKE_V2', database_sha256='DB2')
            daily.run('20261028', signals_only=True)
            self.assertEqual(model.call_count, 4)
            pit['manifest_sha256'] = 'PIT_CHANGED'
            daily.run('20261028', signals_only=True)
            self.assertEqual(model.call_count, 6)
            self.assertEqual(rq.sha(first_path), original_hash)
            self.assertTrue(all(call.kwargs == {'scope': 'DAILY_SIGNALS_V1'} for call in opener.call_args_list))

    def test_deep_block_does_not_block_ret_and_repeat_preserves_successful_signal_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            _, _, _, model = self.fixture(stack, root, deep_blocked=True)
            first = daily.run('20261028', signals_only=True)
            first_path = Path(first['output_directory']) / 'report.json'
            before = rq.sha(first_path)
            second = daily.run('20261028', signals_only=True)
            self.assertEqual(first['status'], 'SIGNAL_RESEARCH_PARTIAL')
            self.assertEqual(second['strategies']['deep']['error'], 'DEEP_FRONT_AFFINE_INPUT_MISSING')
            self.assertEqual(second['strategies']['ret120']['status'], 'SIGNAL_RESEARCH_ONLY')
            self.assertEqual([c.args[0] for c in model.call_args_list], ['deep', 'ret120', 'deep'])
            self.assertEqual(rq.sha(first_path), before)
            self.assertIn('ret120', second['strategy_reused_from'])
            self.assertIsNone(second['strategies']['ret120']['signal_summary']['actual_model_entries_today'])
            self.assertNotIn('operations', second['strategies']['ret120'])

    def test_unqualified_publication_fails_before_strategy_calculation(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            source, _, _, model = self.fixture(stack, Path(tmp))
            source.meta['execution_qualified'] = True
            result = daily.run('20261028', signals_only=True)
            self.assertEqual(result['status'], 'SIGNAL_RESEARCH_BLOCKED')
            self.assertEqual(result['input_error'], 'DAILY_SIGNALS_PUBLICATION_NOT_QUALIFIED')
            model.assert_not_called()


if __name__ == '__main__':
    unittest.main()
