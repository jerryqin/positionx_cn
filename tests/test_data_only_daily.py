"""No network, Scheduler or production writes: fake reader and isolated paths only."""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from scripts import run_data_only_daily as daily


class DataOnlyTests(unittest.TestCase):
    def test_nonretryable_source_failure_does_not_query_again(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)
            (root/'20261027_preopen_last_error.json').write_text('{"nonretryable":true}',encoding='utf-8')
            with patch.object(daily,'OPS',root),patch.object(daily.prep,'preopen',side_effect=AssertionError('NETWORK')):
                with self.assertRaisesRegex(ValueError,'NO_RETRY'):daily.run('preopen','20261027')

    def test_same_published_version_reuses_qualified_receipt(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);manifest=root/'manifest.json';manifest.write_text('{}',encoding='utf-8')
            result=dict(date='20261027',status='GREEN',version='v1',manifest_sha256=daily.rq.sha(manifest))
            (root/'20261027_postclose.json').write_text(json.dumps(result),encoding='utf-8')
            reader=SimpleNamespace(meta={'version':'v1'},root=root)
            report={'status':'TWO_STRATEGY_REPORT_COMPLETE','strategies':{k:{'operations':{'pending':1}} for k in ('deep','ret120')}}
            with patch.object(daily,'OPS',root),patch.object(daily,'result_files'),patch.object(daily.rq,'open_inputs',return_value=reader),patch.object(daily.close,'run',side_effect=AssertionError('DOWNLOAD')),patch.object(daily,'model_report',return_value=(root,report,True)) as consume:
                self.assertTrue(daily.run('postclose','20261027')['idempotent_reuse'])
                consume.assert_called_once_with(reader,'20261027')

    def test_missed_preopen_refuses_capture(self):
        import pandas as pd
        with tempfile.TemporaryDirectory() as t,patch.object(daily,'OPS',Path(t)),patch.object(daily.rq,'clock',return_value=pd.Timestamp('2026-10-27T18:00:00+08:00')),patch.object(daily.prep,'preopen',side_effect=AssertionError('CAPTURE')):
            with self.assertRaisesRegex(ValueError,'MISSED_PREOPEN'):daily.run('preopen','20261027')


if __name__=='__main__':unittest.main()
