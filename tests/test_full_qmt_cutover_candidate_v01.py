from __future__ import annotations
import copy,datetime as dt,hashlib,json,pathlib,sys,tempfile,types,unittest
from unittest.mock import patch
from core import broker as br
from core import qmt_runtime as rt
from scripts import deep_forward_segment_v01 as ds
from research.forward_deep_oversold import DeepOversoldForwardPolicy
from research.research_store import ResearchStore
ROOT=pathlib.Path(__file__).resolve().parents[1]

class FakeTrader:
    def __init__(self,*args):self.rows=[];self.rc=0;self.sub=0;self.stop_bad=False;self.stopped=0
    def register_callback(self,c):pass
    def start(self):pass
    def connect(self):return self.rc
    def subscribe(self,a):return self.sub
    def stop(self):
        self.stopped+=1
        if self.stop_bad:raise RuntimeError('fake stop')
    def query_stock_positions(self,a):return self.rows
    query_stock_orders=query_stock_positions
    query_stock_trades=query_stock_positions
    def query_stock_asset(self,a):return types.SimpleNamespace(account_id='FAKE_ACCOUNT',total_asset=100.,cash=100.,market_value=0.,frozen_cash=0.)
    def order_stock(self,*a):raise AssertionError('WRITE_CALLED')
    def cancel_order_stock(self,*a):raise AssertionError('WRITE_CALLED')

class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.cfg={'enabled':True,'broker_userdata':'FAKE_USERDATA'}
        self.p=patch.object(br,'qmt_profile',return_value=self.cfg);self.p.start();self.addCleanup(self.p.stop)
        self.b=br.MiniQmtBroker('IGNORED','FAKE_ACCOUNT');self.b.trader=FakeTrader();self.b.account=object();self.b.connected=True
    def test_none_not_empty(self):
        for fn in (self.b.query_positions,self.b.query_orders,self.b.query_trades):
            self.b.trader.rows=None
            with self.assertRaisesRegex(br.BrokerError,'RETURNED_NONE'):fn()
    def test_empty_schema_not_covered(self):
        for name in ('positions','orders','trades'):
            self.assertEqual(getattr(self.b,'query_'+name)(),[])
            self.assertEqual(self.b.read_coverage[name],'EMPTY_SCHEMA_NOT_COVERED')
    def test_wrong_container_rejected(self):
        self.b.trader.rows=()
        with self.assertRaisesRegex(br.BrokerError,'WRONG_TYPE'):self.b.query_positions()
    def test_wrong_account_rejected(self):
        self.b.trader.rows=[types.SimpleNamespace(account_id='OTHER')]
        with self.assertRaisesRegex(br.BrokerError,'BINDING'):self.b.query_positions()
    def test_missing_field_rejected(self):
        self.b.trader.rows=[types.SimpleNamespace(account_id='FAKE_ACCOUNT')]
        with self.assertRaisesRegex(br.BrokerError,'MISSING_FIELD'):self.b.query_orders()
    def test_position_sample_mapping(self):
        self.b.trader.rows=[types.SimpleNamespace(account_id='FAKE_ACCOUNT',stock_code='000001.SZ',volume=100,can_use_volume=100,market_value=1000.,open_price=10.)]
        row=self.b.query_positions()[0];self.assertEqual(row.volume,100);self.assertEqual(row.avg_price,10.)
    def test_asset_mapping(self):self.assertEqual(self.b.query_account().total_asset,100.)
    def test_asset_missing_frozen_cash_is_not_zero(self):
        self.b.trader.query_stock_asset=lambda a:types.SimpleNamespace(account_id='FAKE_ACCOUNT',total_asset=100.,cash=100.,market_value=0.)
        with self.assertRaisesRegex(br.BrokerError,'MISSING_FIELD'):self.b.query_account()
    def test_callback_known_fields_only_no_account_or_unknown_getter(self):
        class Row:
            account_id='FAKE_ACCOUNT';stock_code='000001.SZ';order_id=1
            @property
            def secret(self):raise AssertionError('UNKNOWN_GETTER_ACCESSED')
        tm=types.ModuleType('xtquant.xttrader');tm.XtQuantTraderCallback=object
        events=[]
        with patch.dict(sys.modules,{'xtquant.xttrader':tm}):
            cb=br._CallbackProxy.build(lambda k,p:events.append((k,p)),'FAKE_ACCOUNT')
            cb.on_stock_order(Row())
            self.assertEqual(events,[('ORDER',{'order_id':1,'stock_code':'000001.SZ'})])
            cb.on_stock_order(types.SimpleNamespace(account_id='OTHER'))
        self.assertEqual(events[-1],('CAPTURE_ERROR',{'error':'CALLBACK_FACT_CAPTURE_FAILED'}))
    def test_disconnect_callback_blocks_queries_without_reconnect(self):
        tm=types.ModuleType('xtquant.xttrader');tm.XtQuantTrader=FakeTrader;tm.XtQuantTraderCallback=object
        ty=types.ModuleType('xtquant.xttype');ty.StockAccount=lambda *a:object()
        self.b.connected=False;self.b.trader=None
        with patch.dict(sys.modules,{'xtquant.xttrader':tm,'xtquant.xttype':ty}),patch.object(br,'pin_sdk'),patch.object(br,'verify_full_endpoint'):
            self.b.connect();self.b.callback.on_disconnected()
            self.assertFalse(self.b.connected)
            with self.assertRaises(br.BrokerError):self.b.query_positions()
            with self.assertRaisesRegex(br.BrokerError,'RETRY_NOT_ALLOWED'):self.b.connect()
    def test_write_methods_denied(self):
        with self.assertRaisesRegex(br.BrokerError,'SUBMIT_FORBIDDEN'):self.b.submit_limit('000001.SZ','BUY',100,1.,'')
        with self.assertRaisesRegex(br.BrokerError,'CANCEL_FORBIDDEN'):self.b.cancel(1)
    def test_stop_failure_not_hidden(self):
        self.b.trader.stop_bad=True
        with self.assertRaisesRegex(br.BrokerError,'STOP_FAILED'):self.b.disconnect()
        self.assertFalse(self.b.connected)
    def test_primary_and_cleanup_separate_no_retry(self):
        trader=FakeTrader();trader.rc=-1;trader.stop_bad=True
        pkg=types.ModuleType('xtquant');tm=types.ModuleType('xtquant.xttrader');ty=types.ModuleType('xtquant.xttype')
        tm.XtQuantTrader=lambda *a:trader;ty.StockAccount=lambda *a:object()
        self.b.connected=False;self.b.trader=None
        with patch.dict(sys.modules,{'xtquant':pkg,'xtquant.xttrader':tm,'xtquant.xttype':ty}),patch.object(br,'pin_sdk'),patch.object(br,'verify_full_endpoint'),patch.object(br._CallbackProxy,'build',return_value=object()):
            with self.assertRaisesRegex(br.BrokerError,'rc=-1'):self.b.connect()
            self.assertEqual(self.b.last_cleanup_error,'STOP_FAILED_AFTER_PRIMARY_ERROR')
            with self.assertRaisesRegex(br.BrokerError,'RETRY_NOT_ALLOWED'):self.b.connect()

class FakeClient:
    def __init__(self):self.connected=True;self.ok=True;self.connects=0
    def set_remote_addr(self,*a):pass
    def reset(self):pass
    def connect_ex(self):self.connects+=1;return self.ok,''
    def is_connected(self):return self.connected
    def get_data_dir(self):return str(ROOT/'runtime_cache')
    def get_app_dir(self):return str(ROOT)
class DataTests(unittest.TestCase):
    def setUp(self):
        self.client=FakeClient();self.flags=[];self.downloads=[]
        self.raw=types.SimpleNamespace(RPCClient=lambda *a:self.client,get_market_data_ori=lambda *a,**kw:self.flags.append((a,kw)) or {},get_local_data=lambda *a,**kw:{},get_market_data=lambda *a,**kw:{},download_history_data2=lambda *a:self.downloads.append(a))
        self.cfg={'client_config':'FAKE_CLIENT_CONFIG','sdk_root':str(ROOT),'cache_root':str(ROOT/'runtime_cache'),'client_root':str(ROOT),'allow_history_updates':False,'allow_metadata_updates':False}
        self.d=rt.FullQmtData(self.raw,self.cfg);self.p=patch.object(rt,'verify_full_endpoint',return_value=1);self.p.start();self.addCleanup(self.p.stop)
    def test_local_only_flag(self):
        self.d.connect();self.raw.get_market_data_ori([],[],'1d','','',125,'front',False,True)
        self.assertIs(self.flags[-1][0][8],False)
    def test_no_reconnect_after_loss(self):
        self.d.connect();self.client.connected=False
        with self.assertRaisesRegex(rt.QmtRuntimeError,'NO_RECONNECT'):self.d.connect()
        self.assertEqual(self.client.connects,1)
    def test_no_retry_after_failed_connect(self):
        self.client.ok=False
        with self.assertRaisesRegex(rt.QmtRuntimeError,'CONNECT_FAILED'):self.d.connect()
        with self.assertRaisesRegex(rt.QmtRuntimeError,'RETRY_DENIED'):self.d.connect()
        self.assertEqual(self.client.connects,1)
    def test_None_history_fails(self):
        self.raw.get_local_data=lambda **kw:None
        with self.assertRaisesRegex(rt.QmtRuntimeError,'RETURNED_NONE'):self.d.get_local_data()
    def test_updates_default_denied(self):
        with self.assertRaisesRegex(rt.QmtRuntimeError,'NOT_AUTHORIZED'):self.d.download_history_data2(['000001.SZ'],'1d')
        self.assertEqual(self.downloads,[])
    def test_unknown_API_denied(self):
        for name in ('order_stock','cancel_order_stock','reconnect','add_sector'):
            with self.assertRaises(AttributeError):getattr(self.d,name)
    def test_bounded_refresh_five_arg_signature(self):
        self.cfg.update(allow_history_updates=True,history_update={'mode':'bounded_range_acknowledged','start':'20260915','end':'20261019','not_before':'2020-01-01T00:00:00+08:00','max_symbols':2})
        self.d.download_history_data2(['000905.SH'],'1d','20250101','20261019',None,True)
        self.assertEqual(len(self.downloads[0]),5);self.assertEqual(self.downloads[0][2],'20260915')
    def test_approved_end_is_a_cap_not_a_future_download(self):
        class Clock(dt.datetime):
            @classmethod
            def now(cls,tz=None):return cls(2026,10,22,16,0,tzinfo=tz)
        self.cfg.update(allow_history_updates=True,history_update={'mode':'bounded_range_acknowledged','start':'20260915','end':'20261023','not_before':'2020-01-01T00:00:00+08:00','max_symbols':2})
        with patch.object(rt.dt,'datetime',Clock):self.d.download_history_data2(['000905.SH'],'1d','','')
        self.assertEqual(self.downloads[0][3],'20261022')
    def test_update_date_scope_rejected(self):
        self.cfg.update(allow_history_updates=True,history_update={'mode':'bounded_range_acknowledged','start':'20260915','end':'20261019','not_before':'2020-01-01T00:00:00+08:00','max_symbols':2})
        with self.assertRaisesRegex(rt.QmtRuntimeError,'UPDATE_DATE'):self.d.download_history_data2(['000905.SH'],'1d','','20261020')

class DisabledTests(unittest.TestCase):
    def test_prior_subprocess_has_no_unpinned_SDK_import(self):
        import ast
        tree=ast.parse((ROOT/'scripts/run_ret120_universe_prior_forward_v10.py').read_text(encoding='utf-8'))
        imports=[n for n in ast.walk(tree) if isinstance(n,ast.ImportFrom) and n.module=='xtquant']
        self.assertEqual(imports,[])
        calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='get_xtdata']
        self.assertEqual(len(calls),3)

    def test_disabled_full_never_loads_legacy_sdk(self):
        with patch.object(rt,'profile',return_value={'enabled':False,'backend':'full'}):
            with self.assertRaisesRegex(rt.QmtRuntimeError,'NO_FALLBACK'):rt.get_xtdata()
    def test_native_reconnect_config_disabled(self):
        self.assertIn('reconnectSecond=0',(ROOT/'config/ops/full_qmt_xtdata_client_v01.ini').read_text())

class DeepTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=pathlib.Path(self.tmp.name)
        self.p={'version':'deep-independent-v0.1','enabled':True,'start_date':'20261022','segment_root':str(self.root/'new'),'segment_db':str(self.root/'new/deep.duckdb'),'output_dir':str(self.root/'new/forward'),'backup_root':str(self.root/'new/backups'),'calendar':str(ROOT/'config/ops/cn_exchange_calendar_2026.json')}
    def test_no_automatic_initialization(self):
        with self.assertRaisesRegex(ValueError,'NOT_AUTHORIZED'):ds.initialize(self.p,now=dt.datetime(2026,10,20))
        self.assertFalse(pathlib.Path(self.p['segment_db']).exists())
    def test_new_state_no_prior_signals_or_fake_sessions(self):
        old=self.root/'old.bin';old.write_bytes(b'old positions and pending signals');before=hashlib.sha256(old.read_bytes()).hexdigest()
        ds.initialize(self.p,True,dt.datetime(2026,10,20))
        with ResearchStore(self.p['segment_db']) as store:
            state=store.load_state(DeepOversoldForwardPolicy().strategy_id)
            self.assertEqual(state['last_processed_date'],'20261021');self.assertEqual(state['base_cash'],1000000.)
            for table in ('forward_sessions','forward_positions','forward_signals','forward_equity'):
                self.assertEqual(store.con.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
            ds.require_state(self.p,state,dt.datetime(2026,10,22),con=store.con)
        self.assertEqual(hashlib.sha256(old.read_bytes()).hexdigest(),before)
    def test_initialized_segment_not_reinitialized(self):
        ds.initialize(self.p,True,dt.datetime(2026,10,20))
        with self.assertRaisesRegex(ValueError,'NOT_EMPTY'):ds.initialize(self.p,True,dt.datetime(2026,10,20))
    def test_past_start_rejected(self):
        with self.assertRaisesRegex(ValueError,'FUTURE'):ds.initialize(self.p,True,dt.datetime(2026,10,22))
    def test_disabled_rejected(self):
        self.p['enabled']=False
        with self.assertRaisesRegex(ValueError,'PROFILE'):ds.initialize(self.p,True,dt.datetime(2026,10,20))
    def test_external_db_path_rejected(self):
        self.p['segment_db']=str(self.root/'outside.duckdb')
        with self.assertRaisesRegex(ValueError,'PATH'):ds.initialize(self.p,True,dt.datetime(2026,10,20))

class AShareResumeTests(unittest.TestCase):
    def setUp(self):
        from scripts import update_a_share_daily_v1 as mod
        self.m=mod
        class Clock(dt.datetime):
            @classmethod
            def now(cls):return cls(2026,10,22,15,40)
        self.cfg={'enabled':True,'a_share_resume':{'enabled':True,'start_date':'20261022','last_validated_session':'20261015','missing_sessions':['20261016','20261019','20261020','20261021'],'authorization_ref':'FAKE_APPROVAL'}}
        self.stack=__import__('contextlib').ExitStack();self.addCleanup(self.stack.close)
        for obj,name,value in [(mod,'datetime',Clock),(mod,'get_latest_trade_date',lambda *a:'20261022'),(mod,'get_sessions_between',lambda *a:['20261016','20261019','20261020','20261021','20261022']),(mod,'load_state',lambda:{'last_processed_date':'20261015','sessions':[]})]:
            self.stack.enter_context(patch.object(obj,name,value))
        self.stack.enter_context(patch.object(rt,'profile',return_value=self.cfg))
    def test_default_gap_protection_stays(self):
        self.cfg['a_share_resume']['enabled']=False
        with self.assertRaisesRegex(RuntimeError,'MISSED_SESSION'):self.m.resolve_plan(object())
    def test_explicit_future_resume_only_captures_today(self):
        p=self.m.resolve_plan(object());self.assertEqual(p['target_date'],'20261022');self.assertFalse(p['resume_gap']['missing_dates_backfilled']);self.assertEqual(len(p['resume_gap']['missing_sessions']),4)
    def test_wrong_gap_or_date_refused(self):
        self.cfg['a_share_resume']['missing_sessions']=[]
        with self.assertRaisesRegex(RuntimeError,'MISSED_SESSION'):self.m.resolve_plan(object())
        with self.assertRaisesRegex(RuntimeError,'MISSED_SESSION'):self.m.resolve_plan(object(),'20261019')
    def test_unapproved_resume_refused(self):
        self.cfg['a_share_resume']['authorization_ref']='UNAPPROVED'
        with self.assertRaisesRegex(RuntimeError,'MISSED_SESSION'):self.m.resolve_plan(object())
    def test_state_retains_old_sessions_and_gap_without_fabricating_dates(self):
        old={'trade_date':'20261015','proof':'OLD_UNCHANGED'}
        p=self.m.resolve_plan(object());state={'last_processed_date':'20261015','sessions':[old],'acknowledged_session_gaps':[p['resume_gap']]}
        seen=[]
        with patch.object(self.m,'json_write_atomic',side_effect=lambda path,value:seen.append(copy.deepcopy(value))):
            self.m.commit_state(state,'20261022',{'observed_at':'2026-10-22T15:40:00+08:00','rows':{'daily_bars':2,'current_universe':2}},{'daily_rows':3,'latest_date':'2026-10-22'})
        self.assertEqual(seen[0]['sessions'][0],old);self.assertEqual([s['trade_date'] for s in seen[0]['sessions']],['20261015','20261022']);self.assertEqual(len(seen[0]['acknowledged_session_gaps']),1)
