from __future__ import annotations
import datetime as dt, importlib.util, sys, types, unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from core.ops_runtime import qmt_is_trading_day,qmt_trading_date_to_yyyymmdd

def load_module(name,rel):
    spec=importlib.util.spec_from_file_location(name,ROOT/rel);mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod
orch=load_module('daily_ops_orch_hotfix_test','scripts/run_daily_ops_orchestrator_v1.py')
watch=load_module('daily_ops_watch_hotfix_test','scripts/run_daily_ops_watchdog_v1.py')

class ShadowOpsDay1HotfixTests(unittest.TestCase):
    def test_qmt_epoch_milliseconds_maps_to_local_trade_date(self):
        tz=dt.datetime.now().astimezone().tzinfo
        epoch_ms=int(dt.datetime(2026,10,14,12,0,tzinfo=tz).timestamp()*1000)
        self.assertEqual(qmt_trading_date_to_yyyymmdd(epoch_ms),'20261014')
    def test_qmt_date_formats_supported(self):
        self.assertEqual(qmt_trading_date_to_yyyymmdd('2026-10-14'),'20261014')
        self.assertEqual(qmt_trading_date_to_yyyymmdd(20261014),'20261014')
    def test_qmt_is_trading_day_with_epoch_milliseconds(self):
        tz=dt.datetime.now().astimezone().tzinfo
        epoch_ms=int(dt.datetime(2026,10,14,12,0,tzinfo=tz).timestamp()*1000)
        fake_xtdata=types.SimpleNamespace(connect=lambda:None,get_trading_dates=lambda *a,**k:[epoch_ms])
        fake_xtquant=types.ModuleType('xtquant');fake_xtquant.xtdata=fake_xtdata
        with patch.dict(sys.modules,{'xtquant':fake_xtquant}):
            self.assertTrue(qmt_is_trading_day('20261014','SH'))
    def test_business_fail_and_waiting_do_not_count_as_success(self):
        for st in ('FAIL_OPTIMUS_X','WAITING_OPTIMUS_X','BLOCKED_X','PARTIAL_X'):
            self.assertFalse(orch.business_status_ok({'status':st}),st)
    def test_business_pass_and_no_status_remain_success(self):
        self.assertTrue(orch.business_status_ok({'status':'PASS_OPTIMUS_X'}))
        self.assertTrue(orch.business_status_ok({'ready':True}))
        self.assertTrue(orch.business_status_ok(None))
    def test_watchdog_rejects_false_nontrading_skip_on_trading_day(self):
        reg={'tasks':{'PIT_PREOPEN':{'calendar_scope':'TRADING_DAY'}}}
        rows=[{'task_id':'PIT_PREOPEN','status':'SKIPPED_NON_TRADING_DAY'}]
        bad=watch.classify_findings(rows,reg,trading_day=True)
        self.assertEqual(bad[0]['reason'],'SKIPPED_NON_TRADING_DAY_ON_TRADING_DAY')
    def test_watchdog_allows_nontrading_skip_only_when_calendar_confirms(self):
        reg={'tasks':{'PIT_PREOPEN':{'calendar_scope':'TRADING_DAY'}}}
        rows=[{'task_id':'PIT_PREOPEN','status':'SKIPPED_NON_TRADING_DAY'}]
        self.assertEqual(watch.classify_findings(rows,reg,trading_day=False),[])
        self.assertTrue(watch.classify_findings(rows,reg,trading_day=None,calendar_error='calendar unavailable'))

if __name__=='__main__':unittest.main()
