
from __future__ import annotations
import argparse, hashlib, importlib.util, json, math, re, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pandas as pd
import yaml

ROOT=Path(__file__).resolve().parents[1]
CFG=ROOT/'config/forward/ret120_universe_prior_forward_v1_0.yaml'

def cfg(): return yaml.safe_load(CFG.read_text(encoding='utf-8'))
def now_cst(): return datetime.now(timezone(timedelta(hours=8)))
def iso_now(): return now_cst().isoformat(timespec='seconds')

def sha256_file(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()

def sha256_json(obj):
    return hashlib.sha256(json.dumps(obj,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()

def normalize_symbol(x):
    s=str(x).strip().upper()
    if re.fullmatch(r'\d{6}\.(SH|SZ)',s): return s
    m=re.fullmatch(r'(SH|SZ)(\d{6})',s)
    if m: return f'{m.group(2)}.{m.group(1)}'
    if re.fullmatch(r'\d{6}',s): return s+('.SH' if s.startswith(('6','9')) else '.SZ')
    return s

def norm_date(v):
    if v is None: return None
    if isinstance(v,pd.Timestamp):
        return v.strftime('%Y%m%d')
    if hasattr(v,'to_pydatetime'):
        try: return pd.Timestamp(v).strftime('%Y%m%d')
        except Exception: pass
    s=str(v).strip()
    if re.fullmatch(r'\d{8}',s):
        return s
    if re.fullmatch(r'\d{13}',s):
        try: return pd.to_datetime(int(s),unit='ms').strftime('%Y%m%d')
        except Exception: return None
    if re.fullmatch(r'\d{10}',s):
        try: return pd.to_datetime(int(s),unit='s').strftime('%Y%m%d')
        except Exception: return None
    try:
        return pd.to_datetime(v).strftime('%Y%m%d')
    except Exception:
        return None

def load_scanner(c):
    p=Path(c['paths']['exact_scanner_source']); exp=c['hashes']['exact_scanner_source']
    if not p.exists(): raise RuntimeError(f'missing exact scanner: {p}')
    act=sha256_file(p)
    if act!=exp: raise RuntimeError(f'scanner hash mismatch actual={act} expected={exp}')
    sp=importlib.util.spec_from_file_location('px_ret120_forward_scanner',p)
    m=importlib.util.module_from_spec(sp); sys.modules[sp.name]=m; sp.loader.exec_module(m)
    if not hasattr(m,'assign_quintiles'): raise RuntimeError('assign_quintiles missing')
    return m

def find_preopen_membership(c,signal_date):
    base=Path(c['paths']['pit_root'])/'raw'/signal_date.replace('-','')/'preopen'
    cand=sorted(base.glob('*/index_membership_qmt.parquet'),key=lambda p:p.parent.name) if base.exists() else []
    if not cand: raise RuntimeError(f'no exact-date preopen membership for {signal_date}')
    return cand[-1]

def load_index_membership(c,signal_date):
    p=find_preopen_membership(c,signal_date); x=pd.read_parquet(p)
    req={'index_code','snapshot_date','symbol'}
    if not req.issubset(x.columns): raise RuntimeError(f'membership schema missing {sorted(req-set(x.columns))}')
    x=x.copy(); x['symbol']=x['symbol'].map(normalize_symbol); x=x[~x['symbol'].astype(str).str.startswith('__ERROR__')]
    x['snapshot_date']=x['snapshot_date'].astype(str).str.replace('-','',regex=False)
    x=x[x['snapshot_date'].eq(signal_date.replace('-',''))]
    idx={}; counts=c['membership']['expected_counts']; codes=c['membership']['index_codes']
    for tier in ['CSI300','CSI500','CSI1000']:
        s=set(x.loc[x['index_code'].eq(codes[tier]),'symbol'].astype(str))
        if len(s)!=int(counts[tier]): raise RuntimeError(f'{tier} count invalid: {len(s)} expected {counts[tier]}')
        idx[tier]=s
    union=set().union(*idx.values())
    if len(union)!=sum(len(v) for v in idx.values()): raise RuntimeError('CSI300/500/1000 membership overlap detected')
    return idx,p,sha256_file(p)

def qmt_full_market(c):
    try: from xtquant import xtdata
    except Exception as e: raise RuntimeError(f'xtquant import failed: {e}')
    syms=set(); errors=[]
    for sector in c['full_market']['qmt_sector_candidates']:
        try:
            for x in (xtdata.get_stock_list_in_sector(sector) or []): syms.add(normalize_symbol(x))
        except Exception as e: errors.append(f'{sector}: {e}')
    suffix=tuple(c['full_market']['allowed_suffixes']); excludes=tuple(c['full_market']['exclude_prefixes'])
    syms={s for s in syms if s.endswith(suffix) and not s.startswith(excludes) and re.fullmatch(r'\d{6}\.(SH|SZ)',s)}
    if len(syms)<int(c['full_market']['min_symbol_count']): raise RuntimeError(f'QMT full market too small: {len(syms)} errors={errors}')
    return sorted(syms),errors

def series_for(raw,field,symbol):
    if not isinstance(raw,dict): return None
    obj=raw.get(field)
    if obj is not None:
        try:
            if hasattr(obj,'index') and symbol in obj.index: return obj.loc[symbol]
            if hasattr(obj,'columns') and symbol in obj.columns: return obj[symbol]
        except Exception: pass
    obj2=raw.get(symbol)
    if obj2 is not None:
        try:
            if hasattr(obj2,'columns') and field in obj2.columns: return obj2[field]
            if isinstance(obj2,dict) and field in obj2: return obj2[field]
            if isinstance(obj2,pd.Series) and field=='close': return obj2
        except Exception: pass
    return None

def series_rows(series,symbol):
    if series is None: return []
    try:
        items=series.dropna().items()
    except Exception:
        try: items=series.items()
        except Exception: return []
    out=[]
    for k,v in items:
        d=norm_date(k)
        try: val=float(v)
        except Exception: continue
        if d and math.isfinite(val): out.append({'trade_date':pd.Timestamp(d),'symbol':symbol,'close':val})
    return out

def local_back_ratio_close(symbols,signal_date,c,batch_size=500):
    try: from xtquant import xtdata
    except Exception as e: raise RuntimeError(f'xtquant import failed: {e}')
    start=(pd.Timestamp(signal_date)-pd.Timedelta(days=int(c['qmt_history']['lookback_calendar_days']))).strftime('%Y%m%d')
    end=signal_date.replace('-',''); rows=[]
    for i in range(0,len(symbols),batch_size):
        batch=symbols[i:i+batch_size]
        try:
            raw=xtdata.get_local_data(field_list=['close'],stock_list=batch,period='1d',start_time=start,end_time=end,count=-1,dividend_type='back_ratio',fill_data=False)
        except TypeError:
            raw=xtdata.get_local_data(['close'],batch,'1d',start,end,-1,'back_ratio',False)
        for sym in batch:
            rows.extend(series_rows(series_for(raw,'close',sym),sym))
    if not rows: return pd.DataFrame(columns=['trade_date','symbol','close'])
    x=pd.DataFrame(rows)
    return x.drop_duplicates(['symbol','trade_date'],keep='last').sort_values(['symbol','trade_date'],kind='stable').reset_index(drop=True)

def compute_ret120(close_df,signal_date):
    d=pd.Timestamp(signal_date); rows=[]
    for sym,g in close_df.groupby('symbol',sort=False):
        g=g.sort_values('trade_date'); g=g[g['trade_date']<=d]
        if g.empty or pd.Timestamp(g.iloc[-1]['trade_date'])!=d: continue
        v=pd.to_numeric(g['close'],errors='coerce').dropna()
        if len(v)<121: continue
        c=float(v.iloc[-1]); p=float(v.iloc[-121])
        if math.isfinite(c) and math.isfinite(p) and c>0 and p>0: rows.append({'symbol':sym,'ret120':c/p-1.0})
    return pd.DataFrame(rows)

def completeness(close_df,ret,all_symbols,signal_date):
    d=pd.Timestamp(signal_date)
    exact_t=set(close_df.loc[close_df['trade_date'].eq(d),'symbol']) if len(close_df) else set()
    valid_ret=set(ret['symbol']) if len(ret) else set()
    return {
        'requested_symbols':len(all_symbols),
        'symbols_with_any_local_close':int(close_df['symbol'].nunique()) if len(close_df) else 0,
        'symbols_with_exact_t_bar':len(exact_t),
        'symbols_with_valid_ret120':len(valid_ret),
        'incomplete_symbols':sorted(set(all_symbols)-valid_ret),
    }

def refresh_history(symbols,signal_date,c):
    if not symbols: return {'requested':0,'chunks':0,'errors':[],'chunk_results':[]}
    try: from xtquant import xtdata
    except Exception as e: raise RuntimeError(f'xtquant import failed: {e}')
    start=(pd.Timestamp(signal_date)-pd.Timedelta(days=int(c['qmt_history']['lookback_calendar_days']))).strftime('%Y%m%d')
    end=signal_date.replace('-',''); chunk=int(c['qmt_history']['refresh_chunk_size']); errors=[]; chunks=0; results=[]
    total=(len(symbols)+chunk-1)//chunk
    state_dir=Path(c['paths']['output_root'])/'_refresh_state'/signal_date.replace('-','')
    state_dir.mkdir(parents=True,exist_ok=True)
    state_file=state_dir/'refresh_state.json'
    for i in range(0,len(symbols),chunk):
        batch=symbols[i:i+chunk]; chunks+=1; t0=time.monotonic(); last=[0.0]
        print(f'[QMT REFRESH START] chunk {chunks}/{total} symbols={len(batch)} {start}->{end}',flush=True)
        def cb(*args,**kwargs):
            if not bool(c['qmt_history'].get('progress_callback',True)): return
            now=time.monotonic(); minsec=float(c['qmt_history'].get('progress_print_min_seconds',5))
            if now-last[0] < minsec: return
            last[0]=now
            payload=args[0] if args else kwargs
            txt=str(payload)
            if len(txt)>220: txt=txt[:217]+'...'
            print(f'[QMT REFRESH PROGRESS] chunk {chunks}/{total} {txt}',flush=True)
        err=None
        try:
            try:
                xtdata.download_history_data2(batch,'1d',start,end,callback=cb,incrementally=True)
            except TypeError:
                xtdata.download_history_data2(batch,'1d',start,end,cb,True)
        except Exception as e:
            err=f'{type(e).__name__}: {e}'; errors.append(f'chunk {chunks}: {err}')
        # Verify immediately from local cache so progress is measurable and resumable.
        local=local_back_ratio_close(batch,signal_date,c,batch_size=max(1,len(batch)))
        r=compute_ret120(local,signal_date)
        elapsed=round(time.monotonic()-t0,1)
        row={'chunk':chunks,'total_chunks':total,'symbols':len(batch),'valid_ret120_after':len(r),'elapsed_seconds':elapsed,'error':err}
        results.append(row)
        print(f'[QMT REFRESH DONE] chunk {chunks}/{total} valid_ret120={len(r)}/{len(batch)} elapsed={elapsed:.1f}s error={err}',flush=True)
        state={'signal_date':signal_date,'updated_at':iso_now(),'requested':len(symbols),'chunk_size':chunk,'completed_chunks':chunks,'total_chunks':total,'results':results,'errors':errors}
        state_file.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
    return {'requested':len(symbols),'chunks':chunks,'errors':errors,'chunk_results':results,'state_file':str(state_file)}

def tier_coverage(scanner,tier_sets,ret,c,fail=True):
    out={}; frames=[]
    for tier,syms in tier_sets.items():
        z=pd.DataFrame({'symbol':sorted(syms)}).merge(ret,on='symbol',how='left')
        z['bucket']=scanner.assign_quintiles(z['ret120']).to_numpy(); valid=int(z['bucket'].notna().sum())
        need=max(int(c['signal']['min_valid_absolute'][tier]),int(round(len(z)*float(c['signal']['min_valid_fraction']))))
        out[tier]={'total':len(z),'valid_bucket':valid,'need':need,'pass':valid>=need}
        z['universe_tier']=tier; frames.append(z)
        if fail and valid<need: raise RuntimeError(f'{tier} feature coverage invalid: valid={valid} need={need} total={len(z)}')
    return pd.concat(frames,ignore_index=True),out

def load_prior_module():
    p=ROOT/'research/ret120_universe_prior.py'; sp=importlib.util.spec_from_file_location('px_ret120_prior_rules',p)
    m=importlib.util.module_from_spec(sp); sys.modules[sp.name]=m; sp.loader.exec_module(m); return m

def apply_rules(panel):
    rules=load_prior_module(); rec=[]
    for r in panel.itertuples(index=False):
        b=r._asdict(); q=b.get('bucket')
        if pd.isna(q): prior={'momentum_candidate':False,'momentum_confidence':None,'momentum_penalty':False,'mean_reversion_candidate':False,'preferred_horizon':None,'action_semantics':'NO_BUCKET_NO_PRIOR'}
        else: prior=rules.classify_ret120_prior(str(b['universe_tier']),str(q))
        b.update(prior); rec.append(b)
    return pd.DataFrame(rec)

def build_context(c,signal_date):
    scanner=load_scanner(c); idx,mpath,mhash=load_index_membership(c,signal_date); full,sector_errors=qmt_full_market(c)
    fullset=set(full); csi1800=set().union(idx['CSI300'],idx['CSI500'],idx['CSI1000']); tail=fullset-csi1800
    tier_sets={'CSI300':idx['CSI300'],'CSI500':idx['CSI500'],'CSI1000':idx['CSI1000'],'EX_CSI1800':tail}
    return scanner,idx,mpath,mhash,full,fullset,csi1800,tail,tier_sets,sector_errors

def probe(c,signal_date):
    scanner,idx,mpath,mhash,full,fullset,csi1800,tail,tier_sets,sector_errors=build_context(c,signal_date)
    close=local_back_ratio_close(full,signal_date,c); ret=compute_ret120(close,signal_date)
    _,coverage=tier_coverage(scanner,tier_sets,ret,c,fail=False)
    comp=completeness(close,ret,full,signal_date); comp['incomplete_symbols_sample']=comp['incomplete_symbols'][:30]; comp.pop('incomplete_symbols',None)
    out={'status':'RET120_UNIVERSE_PRIOR_FORWARD_V1_0_1_PROBE','signal_date':signal_date,'membership_path':str(mpath),'membership_sha256':mhash,'full_market_count':len(fullset),'csi1800_union':len(csi1800),'tail_count':len(tail),'local_cache':comp,'tier_coverage':coverage,'sector_errors':sector_errors,'mutation':False,'qmt_download_called':False}
    print(json.dumps(out,ensure_ascii=False,indent=2)); return 0

def refresh(c,signal_date):
    scanner,idx,mpath,mhash,full,fullset,csi1800,tail,tier_sets,sector_errors=build_context(c,signal_date)
    close_pre=local_back_ratio_close(full,signal_date,c); ret_pre=compute_ret120(close_pre,signal_date)
    pre_comp=completeness(close_pre,ret_pre,full,signal_date)
    _,pre_cov=tier_coverage(scanner,tier_sets,ret_pre,c,fail=False)
    incomplete=pre_comp['incomplete_symbols']
    print(json.dumps({'status':'RET120_UNIVERSE_PRIOR_FORWARD_V1_0_2_REFRESH_START','signal_date':signal_date,'requested_symbols':len(full),'already_valid_ret120':pre_comp['symbols_with_valid_ret120'],'incomplete_symbols':len(incomplete),'tier_coverage':pre_cov,'refresh_chunk_size':int(c['qmt_history']['refresh_chunk_size'])},ensure_ascii=False,indent=2),flush=True)
    ref=refresh_history(incomplete,signal_date,c)
    close=local_back_ratio_close(full,signal_date,c); ret=compute_ret120(close,signal_date)
    post_comp=completeness(close,ret,full,signal_date); _,coverage=tier_coverage(scanner,tier_sets,ret,c,fail=False)
    passed=all(v['pass'] for v in coverage.values()) and not ref['errors']
    out={'status':'PASS_RET120_UNIVERSE_PRIOR_FORWARD_V1_0_2_REFRESH' if passed else 'PARTIAL_RET120_UNIVERSE_PRIOR_FORWARD_V1_0_2_REFRESH','signal_date':signal_date,'pre_valid_ret120':pre_comp['symbols_with_valid_ret120'],'post_valid_ret120':post_comp['symbols_with_valid_ret120'],'remaining_incomplete':len(post_comp['incomplete_symbols']),'remaining_incomplete_sample':post_comp['incomplete_symbols'][:30],'tier_coverage':coverage,'refresh':ref,'mutation':'QMT_LOCAL_CACHE_ONLY','broker':False,'orders':False}
    print(json.dumps(out,ensure_ascii=False,indent=2),flush=True); return 0 if passed else 3

def capture(c,signal_date):
    scanner,idx,mpath,mhash,full,fullset,csi1800,tail,tier_sets,sector_errors=build_context(c,signal_date)
    close=local_back_ratio_close(full,signal_date,c); ret=compute_ret120(close,signal_date)
    pre_comp=completeness(close,ret,full,signal_date)
    _,pre_cov=tier_coverage(scanner,tier_sets,ret,c,fail=False)
    bad={k:v for k,v in pre_cov.items() if not v['pass']}
    if bad:
        raise RuntimeError(f'coverage not ready for immutable capture: {bad}. Run: python scripts\run_ret120_universe_prior_forward_v10.py refresh --signal-date {signal_date}')
    panel0,coverage=tier_coverage(scanner,tier_sets,ret,c,fail=True)
    post_comp=pre_comp
    refresh={'requested':0,'chunks':0,'errors':[],'note':'capture_auto_refresh_disabled_v1_0_2'}
    panel=apply_rules(panel0); observed=iso_now(); panel['signal_date']=signal_date; panel['observed_at']=observed; panel['signal_representation']=c['signal']['representation']

    capture_id=now_cst().strftime('%Y%m%dT%H%M%S+0800'); date_dir=Path(c['paths']['output_root'])/signal_date.replace('-','')
    if date_dir.exists() and any(date_dir.iterdir()): raise RuntimeError(f'immutable same-day capture already exists: {date_dir}')
    dest=date_dir/capture_id; dest.mkdir(parents=True,exist_ok=False)
    close_path=dest/'qmt_back_ratio_close_raw.parquet'; ret_path=dest/'ret120_signal_snapshot.parquet'; panel_path=dest/'ret120_universe_prior_snapshot.parquet'
    close.to_parquet(close_path,index=False); ret.to_parquet(ret_path,index=False); panel.to_parquet(panel_path,index=False)

    def comp_public(x):
        y=dict(x); y['incomplete_symbols_count']=len(y.pop('incomplete_symbols',[])); return y
    manifest={'schema_version':'RET120_UNIVERSE_PRIOR_FORWARD_V1_0_2','forward_id':c['forward_id'],'capture_id':capture_id,'signal_date':signal_date,'observed_at':observed,'status':'CAPTURED_RESEARCH_ONLY','membership':{'path':str(mpath),'sha256':mhash,'counts':{k:len(v) for k,v in idx.items()},'csi1800_union':len(csi1800)},'full_market':{'symbol_count':len(fullset),'tail_count':len(tail),'sector_errors':sector_errors},'acquisition_audit':{'pre_refresh':comp_public(pre_comp),'pre_refresh_tier_coverage':pre_cov,'refresh':refresh,'post_refresh':comp_public(post_comp)},'signal':{'representation':c['signal']['representation'],'ret120_formula':c['signal']['formula'],'raw_close_path':str(close_path),'raw_close_sha256':sha256_file(close_path),'ret120_path':str(ret_path),'ret120_sha256':sha256_file(ret_path),'panel_path':str(panel_path),'panel_sha256':sha256_file(panel_path),'coverage':coverage},'flag_counts':{'momentum_candidate':int(panel['momentum_candidate'].sum()),'momentum_penalty':int(panel['momentum_penalty'].sum()),'mean_reversion_candidate':int(panel['mean_reversion_candidate'].sum())},'guardrails':c['guardrails']}
    manifest['manifest_payload_sha256']=sha256_json(manifest); (dest/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print(json.dumps(manifest,ensure_ascii=False,indent=2)); print('OUTPUT:',dest); return 0

def check(c):
    p=Path(c['paths']['exact_scanner_source']); act=sha256_file(p) if p.exists() else None
    out={'status':'PASS_RET120_UNIVERSE_PRIOR_FORWARD_V1_0_2_CHECK','scanner_exists':p.exists(),'scanner_sha256':act,'expected_scanner_sha256':c['hashes']['exact_scanner_source'],'pit_root_exists':Path(c['paths']['pit_root']).exists(),'qmt_history_policy':c['qmt_history'],'rules':c['interpretation_rules'],'guardrails':c['guardrails']}
    if not p.exists() or act!=c['hashes']['exact_scanner_source'] or not out['pit_root_exists']: out['status']='BLOCKED_RET120_UNIVERSE_PRIOR_FORWARD_V1_0_1_CHECK'
    print(json.dumps(out,ensure_ascii=False,indent=2)); return 0 if out['status'].startswith('PASS') else 3

def status(c):
    root=Path(c['paths']['output_root']); caps=[]
    for p in sorted(root.glob('*/*/manifest.json')) if root.exists() else []:
        try:
            j=json.loads(p.read_text(encoding='utf-8')); caps.append({'signal_date':j.get('signal_date'),'capture_id':j.get('capture_id'),'status':j.get('status'),**j.get('flag_counts',{}),'manifest':str(p)})
        except Exception: pass
    print(json.dumps({'status':'RET120_UNIVERSE_PRIOR_FORWARD_V1_0_2_STATUS','count':len(caps),'captures':caps},ensure_ascii=False,indent=2)); return 0

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('command',choices=['check','probe','refresh','capture','status']); ap.add_argument('--signal-date'); a=ap.parse_args(); c=cfg()
    try:
        if a.command=='check': rc=check(c)
        elif a.command=='status': rc=status(c)
        else:
            if not a.signal_date: raise RuntimeError('--signal-date YYYY-MM-DD is required')
            pd.Timestamp(a.signal_date)
            rc=probe(c,a.signal_date) if a.command=='probe' else (refresh(c,a.signal_date) if a.command=='refresh' else capture(c,a.signal_date))
    except Exception as e:
        print(json.dumps({'status':'FAIL_CLOSED_RET120_UNIVERSE_PRIOR_FORWARD_V1_0_2','error':f'{type(e).__name__}: {e}'},ensure_ascii=False,indent=2)); rc=3
    raise SystemExit(rc)
if __name__=='__main__': main()
