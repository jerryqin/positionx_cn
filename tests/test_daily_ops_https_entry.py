"""Only temporary files and fake consumers; no network, SDK or Scheduler calls."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd
from scripts import run_data_only_daily as daily


class HTTPSDailyEntryTests(unittest.TestCase):
    def test_before_close_is_not_ready_without_a_provider_or_capture(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(daily,'OPS',Path(tmp)), \
                patch.object(daily.rq,'clock',return_value=pd.Timestamp('2026-10-28T10:30:00+08:00')), \
                patch.object(daily.rq,'open_inputs',side_effect=daily.rq.InputBlocked('RQ_EXACT_DATE_VERSION_MISSING:2026-10-28')), \
                patch.object(daily.close,'run',side_effect=AssertionError('NETWORK')) as collect, \
                patch.object(daily.close,'bounded_provider',side_effect=AssertionError('AUTH')) as provider:
            result=daily.run('postclose','20261028')
        self.assertEqual(result['status'],'NOT_READY')
        self.assertTrue(result['published_previous_version_retained'])
        collect.assert_not_called()
        provider.assert_not_called()

    def test_corrupt_published_version_does_not_trigger_a_replacement_capture(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(daily,'OPS',Path(tmp)), \
                patch.object(daily.rq,'open_inputs',side_effect=daily.rq.InputBlocked('RQ_FILE_HASH_MISMATCH')), \
                patch.object(daily.close,'run',side_effect=AssertionError('NETWORK')) as collect:
            with self.assertRaisesRegex(daily.rq.InputBlocked,'FILE_HASH_MISMATCH'):
                daily.run('postclose','20261028')
        collect.assert_not_called()

    def test_base_uses_latest_seal_instead_of_path_sort_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for name,seal in [('asof_20261027_ffffffffff00','2026-10-28T08:00:00+08:00'),
                              ('asof_20261027_000000000001','2026-10-28T09:00:00+08:00')]:
                folder=root/'versions'/name
                folder.mkdir(parents=True)
                daily.local.write_json(folder/'source_metadata.json',dict(asof='2026-10-27',availability=seal,
                    quality_status='PASS',engineering_only=False,source='RQDATA'))
            with patch.object(daily.rq,'profile',return_value={'asset_root':str(root)}), \
                    patch.object(daily.rq,'clock',return_value=pd.Timestamp('2026-10-28T10:30:00+08:00')):
                selected=daily.latest_base('20261028')
            self.assertEqual(selected.parent.name,'asof_20261027_000000000001')

    def test_contract_block_persists_across_dates_but_is_phase_specific(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            daily.local.write_json(root/'20261027_preopen_last_error.json',dict(date='20261027',phase='preopen',
                nonretryable=True,transport=daily.TRANSPORT,source_block_scope='preopen',error='NATIVE_CREATE_TIME_MISSING'))
            with patch.object(daily,'OPS',root):
                with self.assertRaisesRegex(ValueError,'NO_RETRY'):
                    daily.require_source_allowed('preopen','20261028')
                daily.require_source_allowed('postclose','20261028')
            daily.local.write_json(root/'20261027_preopen_last_error.json',dict(date='20261027',phase='preopen',
                nonretryable=True,transport=daily.TRANSPORT,source_block_scope='all',error='HTTPS_AUTH_REJECTED'))
            with patch.object(daily,'OPS',root):
                with self.assertRaisesRegex(ValueError,'NO_RETRY'):
                    daily.require_source_allowed('postclose','20261028')

    def test_actual_core_report_controls_success_and_funnel_does_not_block(self):
        for complete in (True,False):
            with self.subTest(complete=complete),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                (root/'manifest.json').write_text('{}',encoding='utf-8')
                reader=SimpleNamespace(meta={'version':'new'},root=root)
                # An old GREEN input-check receipt must not skip the model report.
                daily.local.write_json(root/'20261027_postclose.json',dict(status='GREEN',version='new'))
                report={'status':'TWO_STRATEGY_REPORT_COMPLETE' if complete else 'PARTIAL',
                    'strategies':{'deep':{'operations':{'pending_plan_count':67}},
                        'ret120':{'operations':{'pending_plan_count':100}} if complete else {'error':'EXACT_PIT_MISSING'}},
                    'funnel':{'status':'BLOCKED_STRUCTURE_ONLY'}}
                with patch.object(daily,'OPS',root),patch.object(daily,'result_files'), \
                        patch.object(daily.rq,'open_inputs',return_value=reader), \
                        patch.object(daily,'model_report',return_value=(root,report,False)) as consume, \
                        patch.object(daily.close,'run',side_effect=AssertionError('NETWORK')):
                    result=daily.run('postclose','20261027')
                consume.assert_called_once_with(reader,'20261027')
                self.assertEqual(result['status'],'GREEN' if complete else 'PARTIAL')
                self.assertFalse(result['forward_or_trading_authorized'])

    def test_safe_https_error_is_preserved_and_not_ready_remains_retryable(self):
        for name,expected,permanent in [('HTTPSContractError','RED',True),('HTTPSNotReady','NOT_READY',False)]:
            error_type=type(name,(RuntimeError,),{'__module__':'core.rqdata_https'})
            with self.subTest(name=name),tempfile.TemporaryDirectory() as tmp, \
                    patch.object(daily,'OPS',Path(tmp)),patch.object(daily,'result_files'), \
                    patch.object(daily.rq,'clock',return_value=pd.Timestamp('2026-10-28T16:10:00+08:00')), \
                    patch.object(daily,'run',side_effect=error_type('SAFE_EXACT_REASON')), \
                    patch.object(daily.msvcrt,'locking'),patch('sys.argv',['entry','--phase','postclose']), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(daily.main(),3)
                error=daily.local.read_json(Path(tmp)/'20261028_postclose_last_error.json')
                self.assertEqual(error['status'],expected)
                self.assertEqual(error['error'],'SAFE_EXACT_REASON')
                self.assertEqual(error['nonretryable'],permanent)

    def test_report_child_must_bind_exact_published_input_and_exit_status(self):
        from scripts import run_postclose_daily as report_entry
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            source=root/'source'
            source.mkdir()
            (source/'manifest.json').write_text('{}',encoding='utf-8')
            reader=SimpleNamespace(root=source,meta={'version':'EXACT','database_sha256':'DB'})
            output=root/'reports/postclose_daily/20261028'
            report=dict(business_date='20261028',status='TWO_STRATEGY_REPORT_COMPLETE',
                        input={'version':'EXACT'},database_sha256='DB',
                        manifest_sha256=daily.rq.sha(source/'manifest.json'),forward_acceptance=False)
            for wrong_version,code,expected in [(True,0,'INPUT_IDENTITY_MISMATCH'),(False,3,'EXIT_STATUS_CONFLICT')]:
                with self.subTest(wrong_version=wrong_version,code=code):
                    returned=dict(report,input={'version':'OTHER'} if wrong_version else {'version':'EXACT'})
                    child=SimpleNamespace(returncode=code,stdout=json.dumps({'report':str(output/'report.md')}))
                    with patch.object(daily,'ROOT',root), \
                            patch.object(report_entry,'daily_reports',return_value=[]), \
                            patch.object(report_entry,'model_report',return_value=(output,returned,{})), \
                            patch.object(daily.subprocess,'run',return_value=child) as execute:
                        with self.assertRaisesRegex(ValueError,expected):
                            daily.model_report(reader,'20261028')
                    self.assertEqual(execute.call_args.kwargs['timeout'],240)


if __name__=='__main__':unittest.main()
