"""Only fake providers and temporary directories; no real service or production writes."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock
import tempfile
import unittest
import pandas as pd
from scripts import prepare_daily_data as d
from core import p0_rq_input as rq


class DailyDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root/'reports/day28'
        self.patch = patch.object(d, 'ROOT', self.root)
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.calls = []
        def components(code, date, return_create_tm):
            self.calls.append(code)
            start,n = {'000300.XSHG':(0,300),'000905.XSHG':(300,500),'000852.XSHG':(800,1000)}[code]
            return [f'{600000+i:06}.XSHG' for i in range(start,start+n)], pd.Timestamp('2026-10-27T18:00:00+08:00')
        self.provider = SimpleNamespace(index_components=components,
            all_instruments=lambda **kw:pd.DataFrame({'order_book_id':[f'{600000+i:06}.XSHG' for i in range(3000)]}))

    def test_preopen_28_validates_with_existing_consumer_and_reuses(self):
        with patch.object(rq, 'clock', return_value=pd.Timestamp('2026-10-28T08:20:01+08:00')):
            first = d.preopen('20261028', self.out, self.provider)
            self.assertEqual(first['members'],1800)
            again = d.preopen('20261028', self.out, self.provider)
            self.assertTrue(again['reused'])
        self.assertEqual(len(self.calls),3)

    def test_late_or_future_capture_never_calls_provider(self):
        for now in ('2026-10-28T09:20:00+08:00','2026-10-27T08:20:00+08:00'):
            with patch.object(rq, 'clock', return_value=pd.Timestamp(now)):
                with self.assertRaisesRegex(ValueError,'BEFORE_0920'):
                    d.preopen('20261028', self.out, self.provider)
        self.assertEqual(self.calls,[])

    def test_future_provider_timestamp_rejected(self):
        bad = SimpleNamespace(index_components=lambda *a,**kw:(['600000.XSHG'],pd.Timestamp('2026-10-29')))
        with patch.object(rq,'clock',return_value=pd.Timestamp('2026-10-28T08:20:01+08:00')):
            with self.assertRaises(rq.InputBlocked):d.preopen('20261028',self.out,bad)
        self.assertFalse((self.out/'pit/manifests').exists())

    def test_failure_writes_red_receipt_and_nonzero(self):
        with patch('sys.argv',['prepare_daily_data','--phase','preopen','--date','20261028','--output',str(self.out)]), \
             patch.object(d,'preopen',side_effect=TimeoutError('SECRET_MUST_NOT_APPEAR')):
            self.assertEqual(d.main(),3)
        text=(self.out/'preopen_20261028_result.json').read_text(encoding='utf-8')
        self.assertIn('RED',text)
        self.assertNotIn('SECRET',text)


if __name__ == '__main__':unittest.main()
