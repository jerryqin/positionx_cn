"""Direct daily-entry checks; fake inputs only, temporary writes, no SDK/network."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import run_postclose_daily as daily
from scripts import run_postclose_local_candidate as local


class DailyOperationsTests(unittest.TestCase):
    def test_ret_capacity_is_conditional_and_never_fills_future_units(self):
        p = SimpleNamespace(max_positions=30, target_weight=.01)
        pending = [{'symbol': f'{i:06d}.SZ', 'signal_date': '20261027'} for i in range(32, 0, -1)]
        rows = daily.pending_operations('ret120', pending, 2, p)
        self.assertEqual(rows[0]['symbol'], '000001.SZ')
        self.assertEqual(rows[27]['capacity_condition'], 'WITHIN_CURRENT_SLOT_LIMIT_IF_EARLIER_CANDIDATES_FILL')
        self.assertEqual(rows[28]['capacity_condition'], 'WAIT_EARLIER_FILLABILITY_AND_SLOT_LIMIT')
        self.assertTrue(all(r['entry_decision'] == 'PENDING_FUTURE_OPEN_AND_EXACT_STATUS' for r in rows))
        self.assertTrue(all(r['future_entry_units'] is None and r['future_open_price'] is None for r in rows))

    def test_deep_has_proportional_capacity_not_thirty_name_cutoff(self):
        rows = daily.pending_operations('deep', [
            {'symbol': f'{i:06d}.SZ', 'signal_date': '20261027'} for i in range(67)],
            0, SimpleNamespace(target_weight=.01))
        self.assertEqual(len(rows), 67)
        self.assertTrue(all(r['current_available_slots'] is None for r in rows))
        self.assertEqual(len({r['capacity_condition'] for r in rows}), 1)

    def test_reuse_rejects_other_database_before_copy_or_calculation(self):
        source = SimpleNamespace(meta={'version': daily.ACCEPTED_VERSION, 'database_sha256': 'OTHER'})
        report = {'business_date': '20261027'}
        with patch.object(daily, 'model_report', return_value=(Path('FAKE'), report, {})), \
                patch.object(daily.shutil, 'copyfile') as copy, \
                patch.object(daily, 'policy') as policy:
            with self.assertRaisesRegex(ValueError, 'ACCEPTED_RUN_INPUT_IDENTITY_MISMATCH'):
                daily.reuse_accepted(source, {}, '20261027', Path('NEVER_CREATED'), Path('FAKE'))
        copy.assert_not_called()
        policy.assert_not_called()

    def test_parent_rejects_repeat_and_gap_before_any_model_write(self):
        report = {'business_date': '20261027'}
        with patch.object(daily, 'model_report', return_value=(Path('FAKE'), report, {})):
            for day in ('20261027', '20261029'):
                with self.assertRaisesRegex(ValueError, 'MODEL_LEDGER_GAP_OR_ALREADY_PROCESSED'):
                    daily.parent_profile(Path('FAKE'), 'deep', {}, ['20261026', '20261027', '20261028', '20261029'], day)


class DailyOrchestrationTests(unittest.TestCase):
    def test_failed_default_attempt_recovers_without_overwrite_and_becomes_parent(self):
        import hashlib
        from core import p0_rq_input as rq
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / 'source'
            source_root.mkdir()
            (source_root / 'manifest.json').write_text('{}', encoding='utf-8')
            source = SimpleNamespace(root=source_root, db=source_root / 'daily.duckdb',
                meta={'quality_status': 'PASS', 'engineering_only': False,
                      'database_sha256': 'FAKE', 'version': 'FAKE'},
                lineage=lambda adjustment: {'version': 'FAKE', 'source': 'RQDATA'})
            reports = root / 'reports/postclose_daily'
            prior = reports / '20261027'
            prior.mkdir(parents=True)
            local.write_json(prior / 'report.json', {'daily_entry': 'run_postclose_daily.py',
                'business_date': '20261027', 'mode': 'ISOLATED_LOCAL_MODEL_REPLAY',
                'forward_acceptance': False, 'generated_at': '2026-10-27T16:00:00+08:00',
                'strategies': {kind: {'status': 'MODEL_REPLAY_COMPLETE'} for kind in ('deep', 'ret120')}})
            profile = {'calendar': 'FAKE'}
            original_read = local.read_json
            def read(path):
                return profile if Path(path).name in ('p0_core_recovery_v01.json', 'deep_forward_segment_v01.json') else original_read(path)
            with patch.object(daily, 'DAILY_ROOT', reports), \
                    patch.object(daily, 'ROOT', root), \
                    patch.object(local, 'output_path', side_effect=lambda path: Path(path)), \
                    patch.object(local.OfflineBoundary, 'install'), \
                    patch.object(local, 'read_json', side_effect=read), \
                    patch.object(local, 'sessions', return_value=['20261027', '20261028']), \
                    patch.object(local, 'membership', return_value=({'000905.SH': set()}, {'sha256': 'PIT', 'manifest_sha256': 'PIT_MANIFEST'})), \
                    patch.object(local, 'local_details', return_value={}), \
                    patch.object(daily, 'parent_profile', return_value=profile), \
                    patch.object(local, 'run_model', side_effect=lambda *args: ({'status': 'MODEL_REPLAY_COMPLETE', 'forward_acceptance': False}, None)), \
                    patch.object(daily, 'operational_summary', return_value={'pending_plan_count': 1}), \
                    patch.object(daily, 'write_report'), \
                    patch.object(rq, 'open_inputs', side_effect=[ValueError('RQ_EXACT_DATE_VERSION_MISSING'), source]):
                first = daily.run('20261028')
                failed_path = reports / '20261028/report.json'
                failed_sha = hashlib.sha256(failed_path.read_bytes()).hexdigest()
                second = daily.run('20261028')
                latest_path, latest = daily.latest_report()
                self.assertEqual(first['status'], 'BLOCKED')
                self.assertEqual(second['status'], 'TWO_STRATEGY_REPORT_COMPLETE')
                self.assertEqual(latest_path.name, '20261028_attempt_002')
                self.assertEqual(latest['business_date'], '20261028')
                self.assertEqual(hashlib.sha256(failed_path.read_bytes()).hexdigest(), failed_sha)
                for kind in ('deep', 'ret120'):
                    self.assertEqual(daily.usable_model_run('20261028', kind), latest_path)

    def test_raw_q5_comes_from_real_snapshot_count_instead_of_new_record_count(self):
        from research.research_store import ResearchStore
        from scripts import run_ret120_csi500_forward as ret
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'ret120').mkdir()
            with ResearchStore(root / 'ret120/model.duckdb') as store:
                ret.ensure_ret120_tables(store)
                store.con.execute('''INSERT INTO ret120_forward_signals(strategy_id,symbol,signal_date,ret120,quintile,status)
                    VALUES (?,?,?,?,?,?)''', [ret.POLICY.strategy_id, '000001.SZ', '20261028', .2, 'Q5', 'PENDING_ENTRY'])
            with patch.object(daily, 'policy', return_value=(ret.POLICY, 'FAKE')):
                result = daily.operational_summary('ret120', root, '20261028', ['20261027', '20261028'], q5_count=100)
            self.assertEqual(result['raw_candidate_count'], 100)
            self.assertEqual(result['new_signal_record_count'], 1)
            self.assertEqual(result['actual_model_entries_today'], 0)

    def test_latest_report_shows_completed_prior_day_without_claiming_today(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for day, status in (('20261027', 'TWO_STRATEGY_REPORT_COMPLETE'), ('20261028', 'BLOCKED')):
                (root / day).mkdir()
                local.write_json(root / day / 'report.json', {'daily_entry': 'run_postclose_daily.py',
                                  'business_date': day, 'status': status})
            result = {'business_date': '20261027', 'status': 'TWO_STRATEGY_REPORT_COMPLETE'}
            with patch.object(daily, 'DAILY_ROOT', root), \
                    patch.object(daily, 'model_report', return_value=(root / '20261027', result, {})) as read:
                chosen, value = daily.latest_report()
            self.assertEqual(chosen.name, '20261027')
            self.assertEqual(value['business_date'], '20261027')
            read.assert_called_once_with(root / '20261027')

    def test_core_failure_is_independent_and_all_new_writes_are_in_output(self):
        from core import p0_rq_input as rq
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / 'source'
            source_root.mkdir()
            (source_root / 'manifest.json').write_text('{}', encoding='utf-8')
            source = SimpleNamespace(root=source_root, db=source_root / 'daily.duckdb',
                                     meta={'quality_status': 'PASS', 'engineering_only': False,
                                           'database_sha256': 'FAKE'},
                                     lineage=lambda adjustment: {'version': 'FAKE', 'source': 'RQDATA'})
            out = root / 'output'
            profile = {'calendar': 'UNUSED'}
            for stage in ('model', 'report'):
                def parent(parent_root, kind, *args):
                    if kind == 'deep' and stage == 'model':
                        raise ValueError('DEEP_EXACT_STATUS_MISSING')
                    return profile
                def operations(kind, *args, **kwargs):
                    if kind == 'deep':
                        raise ValueError('DEEP_RAW_COUNT_READ_FAILED')
                    return {'pending_plan_count': 1}
                def available(day, kind, source=None, pit=None):
                    if source is not None:
                        raise LookupError('NO_SAME_DAY_ATTEMPT')
                    return Path('FAKE_PARENT')
                with self.subTest(stage=stage), \
                        patch.object(local, 'output_path', return_value=out / stage), \
                        patch.object(local.OfflineBoundary, 'install'), \
                        patch.object(rq, 'open_inputs', return_value=source) as opener, \
                        patch.object(local, 'read_json', return_value=profile), \
                        patch.object(local, 'sessions', return_value=['20261027', '20261028']), \
                        patch.object(local, 'membership', return_value=({'000905.SH': set()}, {'manifest': 'FAKE_PIT'})), \
                        patch.object(local, 'local_details', return_value={}), \
                        patch.object(daily, 'parent_profile', side_effect=parent), \
                        patch.object(daily, 'usable_model_run', side_effect=available), \
                        patch.object(local, 'run_model', side_effect=lambda *args: ({'status': 'MODEL_REPLAY_COMPLETE'}, None)) as model, \
                        patch.object(daily, 'operational_summary', side_effect=operations), \
                        patch.object(daily, 'write_report'):
                    result = daily.run('20261028', out / stage)
                opener.assert_called_once_with('20261028')
                self.assertEqual(model.call_args.args[0], 'ret120')
                self.assertEqual(result['status'], 'PARTIAL')
                expected_key = 'error' if stage == 'model' else 'operations_error'
                expected = 'DEEP_EXACT_STATUS_MISSING' if stage == 'model' else 'DEEP_RAW_COUNT_READ_FAILED'
                self.assertEqual(result['strategies']['deep'][expected_key], expected)
                self.assertEqual(result['strategies']['ret120']['operations']['pending_plan_count'], 1)
                self.assertFalse(result['forward_acceptance'])
                self.assertIsNone(result['real_account']['positions'])
            self.assertEqual(sorted(p.name for p in root.iterdir()), ['output', 'source'])


if __name__ == '__main__':
    unittest.main()
