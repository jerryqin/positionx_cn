"""Pinned Full-QMT runtime for the existing interfaces. No retries or client control."""
from __future__ import annotations
import ctypes, datetime as dt, hashlib, importlib, importlib.util, json, os, pathlib, re, subprocess, sys, threading
ROOT=pathlib.Path(__file__).resolve().parents[1]
PROFILE=ROOT/'config/ops/full_qmt_runtime_v01.json'
_lock=threading.RLock()
_data=None
_dll_handles=[]

class QmtRuntimeError(RuntimeError):pass

def profile():
    if not PROFILE.exists():return {'enabled':False}
    p=json.loads(PROFILE.read_text(encoding='utf-8-sig'))
    if type(p.get('enabled')) is not bool:raise QmtRuntimeError('FULL_QMT_PROFILE_INVALID')
    return p

def is_full():
    p=profile();return p.get('backend')=='full' or p['enabled']

def _sha(p):return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()

def verify_full_endpoint(p):
    command='@(Get-NetTCPConnection -State Listen -LocalPort 58610 -ErrorAction Stop).OwningProcess | Sort-Object -Unique | ConvertTo-Json -Compress'
    out=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],capture_output=True,timeout=8,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if out.returncode:raise QmtRuntimeError('FULL_QMT_LISTENER_NOT_VERIFIABLE')
    pid=json.loads(out.stdout)
    if type(pid) is list and len(pid)==1:pid=pid[0]
    if type(pid) is not int:raise QmtRuntimeError('FULL_QMT_LISTENER_AMBIGUOUS')
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    k.OpenProcess.argtypes=[ctypes.c_ulong,ctypes.c_int,ctypes.c_ulong];k.OpenProcess.restype=ctypes.c_void_p
    k.QueryFullProcessImageNameW.argtypes=[ctypes.c_void_p,ctypes.c_ulong,ctypes.c_wchar_p,ctypes.POINTER(ctypes.c_ulong)]
    k.CloseHandle.argtypes=[ctypes.c_void_p]
    handle=k.OpenProcess(4096,False,pid)
    if not handle:raise QmtRuntimeError('FULL_QMT_PROCESS_IDENTITY_UNAVAILABLE')
    try:
        name=ctypes.create_unicode_buffer(32768);n=ctypes.c_ulong(len(name))
        if not k.QueryFullProcessImageNameW(handle,0,name,ctypes.byref(n)):raise QmtRuntimeError('FULL_QMT_PROCESS_IDENTITY_UNAVAILABLE')
        if pathlib.Path(name.value).resolve()!=pathlib.Path(p['client_exe']).resolve():raise QmtRuntimeError('MINI_OR_UNEXPECTED_ENDPOINT_OWNER')
    finally:k.CloseHandle(handle)
    return pid

def pin_sdk():
    p=profile()
    if not p['enabled']:
        if p.get('backend')=='full':raise QmtRuntimeError('FULL_QMT_DISABLED_NO_FALLBACK')
        return importlib.import_module('xtquant')
    with _lock:
        if pathlib.Path(sys.executable).resolve()!=pathlib.Path(p['python']).resolve():raise QmtRuntimeError('PYTHON_IDENTITY_MISMATCH')
        sdk=pathlib.Path(p['sdk_root']).resolve()
        if _sha(p['client_config'])!=p['client_config_sha256']:raise QmtRuntimeError('CLIENT_CONFIG_IDENTITY_MISMATCH')
        for item in p['sdk_files']:
            f=(sdk/item['path']).resolve()
            if not f.is_relative_to(sdk) or _sha(f)!=item['sha256']:raise QmtRuntimeError('SDK_IDENTITY_MISMATCH')
        for name,version in [('numpy','1.26.4'),('pandas','2.3.3')]:
            mod=importlib.import_module(name)
            if mod.__version__!=version or not pathlib.Path(mod.__file__).resolve().is_relative_to(pathlib.Path(p['numerical_site'])/name):raise QmtRuntimeError('NUMERICAL_IDENTITY_MISMATCH')
        existing=sys.modules.get('xtquant')
        if existing is not None:
            if pathlib.Path(existing.__file__).resolve()!=sdk/'__init__.py':raise QmtRuntimeError('PRELOADED_WRONG_XTQUANT')
            return existing
        _dll_handles.extend(os.add_dll_directory(x) for x in p['dll_roots'])
        spec=importlib.util.spec_from_file_location('xtquant',sdk/'__init__.py',submodule_search_locations=[str(sdk)])
        mod=importlib.util.module_from_spec(spec);sys.modules['xtquant']=mod
        try:spec.loader.exec_module(mod)
        except BaseException:sys.modules.pop('xtquant',None);raise
        return mod

class FullQmtData:
    READS={'get_trading_dates','get_trading_calendar','get_holidays','get_stock_list_in_sector','get_sector_list','get_instrument_type','get_instrument_detail','get_index_weight','get_local_data','get_market_data','get_market_data_ex','get_market_data_ex_ori','get_full_tick','get_divid_factors'}
    METADATA={'download_sector_data','download_index_weight'}
    def __init__(self,raw,p):self.raw=raw;self.p=p;self.client=None;self._attempted=False
    def connect(self):
        with _lock:
            if self.client is not None:
                if not self.client.is_connected():raise QmtRuntimeError('CONNECTION_LOST_NO_RECONNECT')
                return True
            if self._attempted:raise QmtRuntimeError("CONNECTION_RETRY_DENIED")
            self._attempted=True
            verify_full_endpoint(self.p)
            c=self.raw.RPCClient('client_xtdata',self.p['client_config'])
            c.set_remote_addr('127.0.0.1',58610);c.reset()
            ok,error=c.connect_ex()
            if ok is not True:raise QmtRuntimeError('FULL_QMT_MARKET_CONNECT_FAILED')
            if pathlib.Path(c.get_data_dir()).resolve()!=pathlib.Path(self.p['cache_root']).resolve():raise QmtRuntimeError('CACHE_ROOT_MISMATCH')
            if not pathlib.Path(c.get_app_dir()).resolve().is_relative_to(pathlib.Path(self.p['client_root']).resolve()):raise QmtRuntimeError('APPLICATION_ROOT_MISMATCH')
            self.client=c
            def connected():
                if not c.is_connected():raise QmtRuntimeError('CONNECTION_LOST_NO_RECONNECT')
                return c
            self.raw.CLIENT=c;self.raw.get_client=connected;self.raw.data_dir=str(pathlib.Path(self.p["cache_root"]).resolve())
            # The native local-only flag remains mandatory for every history reader.
            for name in ('get_market_data_ori','get_market_data_ex_ori','_get_market_data_ex_ori_221207'):
                original=getattr(self.raw,name,None)
                if original is None:continue
                def local(*args,_fn=original,**kwargs):
                    argv=list(args)
                    if len(argv)>8:argv[8]=False;kwargs.pop('enable_read_from_server',None)
                    else:kwargs['enable_read_from_server']=False
                    return _fn(*argv,**kwargs)
                setattr(self.raw,name,local)
            return True
    def _update_guard(self,metadata=False):
        flag='allow_metadata_updates' if metadata else 'allow_history_updates'
        if self.p.get(flag) is not True:raise QmtRuntimeError('CACHE_UPDATE_NOT_AUTHORIZED')
        if not metadata:
            u=self.p.get('history_update',{})
            if u.get('mode')!='bounded_range_acknowledged' or not re.fullmatch(r'\d{8}',u.get('start','')) or not re.fullmatch(r'\d{8}',u.get('end','')):raise QmtRuntimeError('BOUNDED_REFRESH_PROFILE_REQUIRED')
            if dt.datetime.now().astimezone()<dt.datetime.fromisoformat(u['not_before']):raise QmtRuntimeError('REFRESH_TOO_EARLY')
        self.connect()
    def download_history_data2(self,stock_list,period,start_time='',end_time='',callback=None,incrementally=None):
        self._update_guard();u=self.p['history_update']
        if period!='1d' or not stock_list or len(stock_list)>u['max_symbols'] or any(not re.fullmatch(r'\d{6}\.(SH|SZ)',s) for s in stock_list):raise QmtRuntimeError('HISTORY_UPDATE_SCOPE')
        today=dt.datetime.now().strftime('%Y%m%d')
        end=end_time or min(u['end'],today)
        if end>u['end'] or end>today or start_time and start_time>u['end']:raise QmtRuntimeError('HISTORY_UPDATE_DATE')
        if end==today and dt.datetime.now().strftime('%H%M')<'1530':raise QmtRuntimeError('DAILY_FINALITY_NOT_REACHED')
        from core.ops_runtime import local_cn_exchange_is_trading_day
        if local_cn_exchange_is_trading_day(end) is not True:raise QmtRuntimeError('UPDATE_END_NOT_KNOWN_TRADING_DAY')
        start=max(start_time or u['start'],u['start'])
        # Full SDK has FIVE arguments; bounded refresh is explicitly declared, not claimed incremental.
        return self.raw.download_history_data2(stock_list,period,start,end,callback)
    def download_history_data(self,stock_code,period,start_time='',end_time='',callback=None):
        return self.download_history_data2([stock_code],period,start_time,end_time,callback)
    def __getattr__(self,name):
        if name in self.READS:
            fn=getattr(self.raw,name)
            def read(*args,**kwargs):
                self.connect();value=fn(*args,**kwargs)
                if value is None and name in {'get_local_data','get_market_data','get_market_data_ex'}:raise QmtRuntimeError('HISTORY_RETURNED_NONE')
                return value
            return read
        if name in self.METADATA:
            fn=getattr(self.raw,name)
            def update(*args,**kwargs):self._update_guard(metadata=True);return fn(*args,**kwargs)
            return update
        raise AttributeError(name)

def get_xtdata():
    global _data
    p=profile();pin_sdk()
    if not p['enabled']:
        from xtquant import xtdata
        return xtdata
    with _lock:
        if _data is None:
            raw=importlib.import_module('xtquant.xtdata')
            if pathlib.Path(raw.__file__).resolve()!=pathlib.Path(p['sdk_root'],'xtdata.py').resolve():raise QmtRuntimeError('XTDATA_ORIGIN_MISMATCH')
            _data=FullQmtData(raw,p)
        return _data

def segment_bindings():
    # Read-only consumers select each independent store; never copy or merge old returns.
    deep=json.loads((ROOT/'config/ops/deep_forward_segment_v01.json').read_text(encoding='utf-8-sig'))
    ret=json.loads((ROOT/'config/ops/p0_core_recovery_v01.json').read_text(encoding='utf-8-sig'))
    if deep.get('enabled') is not True or ret.get('enabled') is not True:raise QmtRuntimeError('CORE_SEGMENTS_DISABLED')
    if deep['start_date']!=ret['start_date'] or pathlib.Path(ret['deep_db']).resolve()!=pathlib.Path(deep['segment_db']).resolve():raise QmtRuntimeError('CORE_SEGMENT_BINDING_CONFLICT')
    return {'deep_db':deep['segment_db'],'ret120_db':ret['segment_db'],'portfolio_db':ret['segment_db'],'start_date':ret['start_date']}
