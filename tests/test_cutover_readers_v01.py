from __future__ import annotations
import contextlib,copy,datetime as dt,io,json,pathlib,tempfile,unittest
from unittest.mock import patch
from core import qmt_runtime as rt
from core.ops_runtime import RunStore
from research.research_store import ResearchStore
from research.strategy_portfolio import DEEP_ID,RET120_ID
from scripts.run_ret120_csi500_forward import ensure_ret120_tables
from scripts import run_capacity_overflow_audit_v01 as capacity
from scripts import run_g4_forward_reservation_v02 as g4
from scripts import audit_pilot_live_readiness_v01 as pilot
from scripts import run_daily_ops_orchestrator_v1 as orch
from scripts import run_daily_ops_report_v1 as report
ROOT=pathlib.Path(__file__).resolve().parents[1]

class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=pathlib.Path(self.tmp.name);self.day=dt.datetime.now().strftime('%Y%m%d')
        self.bind={'deep_db':str(self.root/'deep.duckdb'),'ret120_db':str(self.root/'ret.duckdb'),'portfolio_db':str(self.root/'ret.duckdb'),'start_date':self.day}
        for sid,key in ((DEEP_ID,'deep_db'),(RET120_ID,'ret120_db')):
            with ResearchStore(self.bind[key]) as store:
                if sid==RET120_ID:ensure_ret120_tables(store)
                store.save_state(sid,'FAKE',{'last_processed_date':self.day,'pending_signals':0,'status':'COLLECTING'})
                if sid==RET120_ID:
                    store.con.execute('CREATE TABLE p0_portfolio_equity(trade_date VARCHAR,equity DOUBLE,cash DOUBLE,market_value DOUBLE,gross_weight DOUBLE,proof_json VARCHAR)')
                    store.con.execute('INSERT INTO p0_portfolio_equity VALUES (?,100.,100.,0.,0.,?)',[self.day,'{}'])
        self.market=self.root/'market.duckdb';self.market.touch()
        self.p=patch.object(rt,'is_full',return_value=True);self.p.start();self.addCleanup(self.p.stop)
        self.q=patch.object(rt,'segment_bindings',return_value=self.bind);self.q.start();self.addCleanup(self.q.stop)
    def test_capacity_both_new_stores(self):
        c={'market_db':str(self.market),'strategies':{DEEP_ID:{'signal_table':'forward_signals'},RET120_ID:{'signal_table':'ret120_forward_signals'}}}
        out=io.StringIO()
        with contextlib.redirect_stdout(out):code=capacity.probe(c)
        value=json.loads(out.getvalue());self.assertEqual(code,0);self.assertEqual(value['strategies'][DEEP_ID]['database'],self.bind['deep_db']);self.assertEqual(value['strategies'][RET120_ID]['database'],self.bind['ret120_db'])
    def test_stale_core_not_success(self):
        with ResearchStore(self.bind['deep_db']) as store:store.save_state(DEEP_ID,'FAKE',{'last_processed_date':'20261015','pending_signals':0})
        c={'market_db':str(self.market),'strategies':{DEEP_ID:{'signal_table':'forward_signals'},RET120_ID:{'signal_table':'ret120_forward_signals'}}}
        out=io.StringIO()
        with contextlib.redirect_stdout(out):code=capacity.probe(c)
        self.assertEqual(code,3);self.assertFalse(json.loads(out.getvalue())['strategies'][DEEP_ID]['current'])
    def test_g4_inspect_uses_new_sources(self):
        out=io.StringIO()
        with contextlib.redirect_stdout(out):code=g4.inspect()
        value=json.loads(out.getvalue());self.assertEqual(code,0);self.assertEqual(len(value['sources']),2);self.assertTrue(all(s['current'] for s in value['sources']))
    def test_pilot_reads_new_curve_but_not_execution_ready(self):
        out=io.StringIO()
        with patch.object(pilot,'_read',return_value={}),contextlib.redirect_stdout(out):code=pilot.audit(self.root/'OLD_MUST_NOT_OPEN.duckdb')
        value=json.loads(out.getvalue());self.assertEqual(code,0);self.assertEqual(value['forward_states']['portfolio']['common_sessions'],1);self.assertFalse(value['forward_states']['portfolio']['legacy_returns_spliced']);self.assertFalse(value['ready']);self.assertFalse((self.root/'OLD_MUST_NOT_OPEN.duckdb').exists())
    def test_PAUSED_is_never_SUCCESS_or_GREEN(self):
        r=json.loads((ROOT/'config/ops/daily_ops_task_registry_v1.json').read_bytes());task=copy.deepcopy(r['tasks']['OPTIMUS_STATUS']);task['paused']=True;r['tasks']={'OPTIMUS_STATUS':task}
        r['ops_storage']={k:str(self.root/k) for k in r['ops_storage']}
        with patch.object(orch,'load_registry',return_value=(r,self.root/'unused.json')),patch.object(orch,'run_step',side_effect=AssertionError('BUSINESS_TASK_EXECUTED')):
            value=orch.execute_task('OPTIMUS_STATUS',str(ROOT),str(self.root),self.day,'fake')
        self.assertEqual(value['status'],'PAUSED')
        with patch.object(report,'load_registry',return_value=r):
            value=report.daily(self.day,str(ROOT),str(self.root))
        # daily returns an object in this repository; the artifact is authoritative for reporting.
        artifact=json.loads((pathlib.Path(r['ops_storage']['report_root'])/'daily'/self.day/'report.json').read_bytes())
        self.assertEqual(artifact['overall_status'],'YELLOW');self.assertEqual(artifact['tasks'][0]['task_status'],'PAUSED')
