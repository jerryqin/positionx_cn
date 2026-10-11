
from __future__ import annotations
import argparse, hashlib, importlib.util, json, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
CFG=ROOT/'config/execution/ret120_execution_funnel_v0_1.json'
def C(): return json.loads(CFG.read_text(encoding='utf-8'))
def now_cst(): return datetime.now(timezone(timedelta(hours=8)))
def iso_now(): return now_cst().isoformat(timespec='seconds')
def sha256_file(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()
def load_engine():
    p=ROOT/'research/ret120_execution_funnel.py';sp=importlib.util.spec_from_file_location('px_ret120_execution_funnel',p)
    m=importlib.util.module_from_spec(sp);sys.modules[sp.name]=m;sp.loader.exec_module(m);return m
def snapshot_for(c,signal_date):
    d=Path(c['paths']['snapshot_root'])/signal_date.replace('-','');hits=sorted(d.glob('*/ret120_universe_prior_snapshot.parquet')) if d.exists() else []
    if not hits: raise RuntimeError(f'no universe-prior snapshot for {signal_date}: {d}')
    return hits[-1]
def load_market(c,symbols,signal_date):
    try: import duckdb
    except Exception as e: raise RuntimeError(f'duckdb unavailable in venv: {e}')
    con=duckdb.connect(c['paths']['market_catalog'],read_only=True)
    try:
        cand=pd.DataFrame({'symbol':sorted(set(symbols))});con.register('px_cand',cand)
        start=(pd.Timestamp(signal_date)-pd.Timedelta(days=260)).strftime('%Y-%m-%d')
        sql=f'''SELECT b.symbol,b.trade_date,b.open,b.high,b.low,b.close,b.volume,b.amount,b.pre_close
                FROM {c['paths']['market_table']} b JOIN px_cand c USING(symbol)
                WHERE b.trade_date BETWEEN ? AND ? ORDER BY b.symbol,b.trade_date'''
        bars=con.execute(sql,[start,signal_date]).df()
        names=pd.DataFrame(columns=['symbol','name','board'])
        d8=signal_date.replace('-','')
        # Exact signal-date metadata only. No today/current fallback for a historical signal date.
        meta_queries=[
            ('''SELECT symbol, any_value(name) name, any_value(board) board
                FROM market.universe_status_forward_v1
                WHERE replace(CAST(trade_date AS VARCHAR),'-','')=?
                GROUP BY symbol''',[d8]),
            ('''SELECT symbol, any_value(name) name, any_value(board) board
                FROM market.current_universe_v1
                WHERE replace(CAST(trade_date AS VARCHAR),'-','')=?
                GROUP BY symbol''',[d8]),
            ('''SELECT symbol, any_value(name) name, any_value(board) board
                FROM market.security_master_v1
                WHERE replace(CAST(as_of AS VARCHAR),'-','')=?
                GROUP BY symbol''',[d8]),
        ]
        for q,args in meta_queries:
            try:
                z=con.execute(q,args).df()
                if not z.empty:
                    names=z
                    break
            except Exception:
                pass
        return bars,names
    finally: con.close()
def check(c):
    out={'status':'PASS_RET120_EXECUTION_FUNNEL_V0_1_2_CHECK','catalog_exists':Path(c['paths']['market_catalog']).exists(),
         'snapshot_root_exists':Path(c['paths']['snapshot_root']).exists(),'structure_proxy':c['structure_proxy'],
         'selection_caps':c['selection_caps'],'risk_preview':c['risk_preview'],'guardrails':c['guardrails']}
    if not out['catalog_exists'] or not out['snapshot_root_exists']: out['status']='BLOCKED_RET120_EXECUTION_FUNNEL_V0_1_2_CHECK'
    print(json.dumps(out,ensure_ascii=False,indent=2));return 0 if out['status'].startswith('PASS') else 3
def run(c,signal_date,nav):
    eng=load_engine();snap=snapshot_for(c,signal_date);s=pd.read_parquet(snap)
    need={'symbol','universe_tier','ret120','bucket','momentum_candidate','momentum_confidence','mean_reversion_candidate'}
    if not need.issubset(s.columns): raise RuntimeError(f'snapshot missing columns: {sorted(need-set(s.columns))}')
    mom=s['momentum_candidate'].astype('boolean').fillna(False).astype(bool)
    mr=s['mean_reversion_candidate'].astype('boolean').fillna(False).astype(bool)
    cand=s[mom|mr].copy()
    cand['lane']=cand.apply(lambda r:'MOMENTUM' if bool(r['momentum_candidate']) else 'MEAN_REVERSION',axis=1)
    bars,names=load_market(c,cand['symbol'].tolist(),signal_date)
    if bars.empty: raise RuntimeError('no market bars for candidates')
    last=bars.groupby('symbol')['trade_date'].max().map(lambda v:str(pd.Timestamp(v).date()))
    stale=sorted(set(cand['symbol'])-set(last[last.eq(signal_date)].index))
    feats=[];p=c['structure_proxy']
    for sym,g in bars.groupby('symbol',sort=False):
        if str(pd.Timestamp(g['trade_date'].max()).date())!=signal_date: continue
        f=eng.breakout_retest_features(g,p);f['symbol']=sym;feats.append(f)
    fdf=pd.DataFrame(feats);x=cand.merge(fdf,on='symbol',how='left')
    if not names.empty:x=x.merge(names.drop_duplicates('symbol'),on='symbol',how='left')
    else:x['name']=None;x['board']=None
    x['nav']=float(nav);x['ready']=False
    m=x['lane'].eq('MOMENTUM') & x['setup_state'].eq('CONFIRMED_RETEST_T')
    trend_ok=x['trend_recovered'].astype('boolean').fillna(False).astype(bool)
    r=x['lane'].eq('MEAN_REVERSION') & x['setup_state'].eq('CONFIRMED_RETEST_T') & trend_ok
    x.loc[m|r,'ready']=True
    diag=[]
    for z in x.itertuples(index=False):
        diag.append(eng.entry_condition_state(
            getattr(z,'raw_close',float('nan')),
            getattr(z,'open_valid_low',float('nan')),
            getattr(z,'open_valid_high',float('nan'))))
    x=pd.concat([x.reset_index(drop=True),pd.DataFrame(diag)],axis=1)
    sizes=[]
    for z in x.itertuples(index=False):
        if bool(getattr(z,'ready',False)):
            sizes.append(eng.preview_size(getattr(z,'raw_close'),getattr(z,'stop_raw'),getattr(z,'adv20_amount'),float(nav),getattr(z,'lane'),c['risk_preview']))
        else:sizes.append({'preview_shares':0,'preview_notional':0.0,'preview_weight':0.0,'risk_budget_amount':0.0})
    x=pd.concat([x.reset_index(drop=True),pd.DataFrame(sizes)],axis=1)
    x['executable_preview']=x['ready'] & x['preview_shares'].ge(int(c['risk_preview']['board_lot']));x['status']='POOL';outputs=[]
    for lane in ['MOMENTUM','MEAN_REVERSION']:
        y=eng.rank_panel(x[x['lane'].eq(lane)],lane).copy();ready=y[y['executable_preview']].copy()
        cap_prepare=c['selection_caps']['prepare_manual_momentum'] if lane=='MOMENTUM' else c['selection_caps']['prepare_manual_mean_reversion']
        prepare_syms=set(ready.head(cap_prepare)['symbol']);ready_syms=set(ready.head(c['selection_caps']['ready_per_lane'])['symbol'])-prepare_syms
        remain=y[~y['symbol'].isin(prepare_syms|ready_syms)];watch_syms=set(remain.head(c['selection_caps']['watch_per_lane'])['symbol'])
        y.loc[y['symbol'].isin(watch_syms),'status']='WATCH';y.loc[y['symbol'].isin(ready_syms),'status']='READY';y.loc[y['symbol'].isin(prepare_syms),'status']='PREPARE_MANUAL';outputs.append(y)
    out=pd.concat(outputs,ignore_index=True)
    rid=now_cst().strftime('%Y%m%dT%H%M%S+0800');dest=Path(c['paths']['output_root'])/signal_date.replace('-','')/rid
    cols=[q for q in ['status','lane_rank','symbol','name','board','lane','universe_tier','ret120','bucket','momentum_confidence','setup_state','trend_recovered','raw_close','breakout_level_raw','stop_raw','open_valid_low','open_valid_high','entry_condition_state','close_in_open_zone','close_vs_open_zone_pct','risk_distance_pct','adv20_amount','preview_shares','preview_weight','preview_notional'] if q in out.columns]
    action=out[out['status'].isin(['PREPARE_MANUAL','READY','WATCH'])][cols].copy()
    prepare=out[out['status'].eq('PREPARE_MANUAL')][cols].copy()
    summary={'status':'PASS_RET120_EXECUTION_FUNNEL_V0_1_2_RUN','generated_at':iso_now(),'signal_date':signal_date,'nav':float(nav),
      'source_snapshot':str(snap),'source_snapshot_sha256':sha256_file(snap),
      'candidate_counts':{'momentum':int((cand['lane']=='MOMENTUM').sum()),'mean_reversion':int((cand['lane']=='MEAN_REVERSION').sum())},
      'stale_candidates_count':len(stale),'stale_candidates_sample':stale[:30],
      'output_counts':out.groupby(['lane','status']).size().unstack(fill_value=0).to_dict(orient='index'),
      'prepare_entry_condition_counts':out[out['status'].eq('PREPARE_MANUAL')].groupby(['lane','entry_condition_state']).size().unstack(fill_value=0).to_dict(orient='index'),
      'metadata_enrichment':{
        'exact_signal_date_only':True,
        'name_non_null':int(out['name'].notna().sum()) if 'name' in out.columns else 0,
        'board_non_null':int(out['board'].notna().sum()) if 'board' in out.columns else 0
      },
      'structure_proxy':c['structure_proxy'],'ranking':c['ranking'],'next_open_policy':c['next_open_policy'],'risk_preview':c['risk_preview'],
      'important_interpretation':['PREPARE_MANUAL is not an order and not a qualified strategy signal.',
        'At next-session open, execute only after manual approval and only if actual open remains inside open_valid_low/open_valid_high; then recalculate shares from actual price.',
        'RET120 magnitude is not used to rank within Q5 or Q1.',
        'Mean-reversion requires confirmed retest plus close>MA20 and rising MA20; this is forward-only evidence collection, not a rescued historical filter.',
        'No LevelHold implementation is cloned in V0.1 because its exact source semantics were not located; add only after source-level reconciliation.',
        'V0.1.1 does not change ranking or selection after observing V0.1 results; it only adds exact-date name/board enrichment and entry-condition diagnostics.',
        'ABOVE_ZONE_WAIT_PULLBACK is not a rejection: PREPARE_MANUAL remains conditional on the next-session open returning to the frozen valid zone.',
        'V0.1.2 is a serialization/robustness hotfix only; selection, ranking, and risk semantics are unchanged from V0.1.1.'],
      'artifacts':{'full':str(dest/'execution_funnel_full.parquet'),'action_sheet':str(dest/'action_sheet.csv'),'prepare_manual':str(dest/'prepare_manual.csv')},'guardrails':c['guardrails']}
    # Validate JSON serializability before creating/writing the run directory.
    summary_text=json.dumps(summary,ensure_ascii=False,indent=2,default=str)
    dest.mkdir(parents=True,exist_ok=False)
    out.to_parquet(dest/'execution_funnel_full.parquet',index=False)
    action.to_csv(dest/'action_sheet.csv',index=False,encoding='utf-8-sig')
    prepare.to_csv(dest/'prepare_manual.csv',index=False,encoding='utf-8-sig')
    (dest/'summary.json').write_text(summary_text,encoding='utf-8')
    print(summary_text);print('OUTPUT:',dest);return 0
def main():
    ap=argparse.ArgumentParser();ap.add_argument('command',choices=['check','run']);ap.add_argument('--signal-date');ap.add_argument('--nav',type=float)
    a=ap.parse_args();c=C()
    try:
        if a.command=='check':rc=check(c)
        else:
            if not a.signal_date or not a.nav: raise RuntimeError('run requires --signal-date YYYY-MM-DD --nav <RMB>')
            pd.Timestamp(a.signal_date);rc=run(c,a.signal_date,float(a.nav))
    except Exception as e:
        print(json.dumps({'status':'FAIL_CLOSED_RET120_EXECUTION_FUNNEL_V0_1_2','error':f'{type(e).__name__}: {e}'},ensure_ascii=False,indent=2));rc=3
    raise SystemExit(rc)
if __name__=='__main__':main()
