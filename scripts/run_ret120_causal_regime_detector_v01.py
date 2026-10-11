
from __future__ import annotations
import argparse,hashlib,importlib.util,json,math,sys
from pathlib import Path
import numpy as np
import pandas as pd
import yaml

ROOT=Path(__file__).resolve().parents[1]
CFG=ROOT/'config/opportunity/ret120_causal_regime_detector_v0_1.yaml'

def C():return yaml.safe_load(CFG.read_text(encoding='utf-8'))
def sha(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()
def safe(v):
    if v is None or isinstance(v,(str,bool,int)):return v
    if isinstance(v,float):return v if math.isfinite(v) else None
    if isinstance(v,(np.integer,)):return int(v)
    if isinstance(v,(np.floating,)):return safe(float(v))
    if isinstance(v,pd.Timestamp):return v.strftime('%Y-%m-%d')
    return str(v)
def writej(p,x):
    Path(p).parent.mkdir(parents=True,exist_ok=True)
    Path(p).write_text(json.dumps(x,ensure_ascii=False,indent=2,default=safe),encoding='utf-8')
def scanner(c):
    p=Path(c['source']['exact_scanner_source'])
    sp=importlib.util.spec_from_file_location('px_regime_scanner',p)
    m=importlib.util.module_from_spec(sp);sys.modules[sp.name]=m;sp.loader.exec_module(m);return m
def load_json(p):return json.loads(Path(p).read_text(encoding='utf-8'))

def upstream(c):
    a=load_json(c['source']['index_ladder_summary'])
    b=load_json(c['source']['tail_holdout_summary'])
    checks={
      'ladder_status':a.get('status')==c['upstream_gates']['ladder_status'],
      'ladder_verdict':a.get('diagnostic_verdict')==c['upstream_gates']['ladder_verdict'],
      'holdout_status':b.get('status')==c['upstream_gates']['holdout_status'],
      'holdout_gate':b.get('holdout_gate',{}).get('status')==c['upstream_gates']['holdout_gate'],
    }
    return checks,all(checks.values())

def paths(c):
    ld=Path(c['source']['index_ladder_dir']);hd=Path(c['source']['tail_holdout_dir'])
    return {
      'CSI1800_DISC':ld/'csi1800_current_daily_bucket_rows.parquet',
      'TAIL_DISC':ld/'ex_csi1800_current_daily_bucket_rows.parquet',
      'CSI1800_H20_HOLD':hd/'csi1800_holdout_h20_daily_bucket_rows.parquet',
      'CSI1800_H60_HOLD':hd/'csi1800_holdout_h60_daily_bucket_rows.parquet',
      'TAIL_H20_HOLD':hd/'ex_csi1800_holdout_h20_daily_bucket_rows.parquet',
      'TAIL_H60_HOLD':hd/'ex_csi1800_holdout_h60_daily_bucket_rows.parquet',
    }

def check_inputs(c):
    rows=[];ok=True
    fixed=[
      ('scanner',c['source']['exact_scanner_source'],c['source']['expected_scanner_source_sha256']),
      ('full_market',c['source']['full_market_universe'],c['source']['expected_full_market_universe_sha256']),
      ('features',c['source']['canonical_features'],c['source']['expected_canonical_features_sha256']),
    ]
    for n,p,e in fixed:
        P=Path(p);a=sha(P) if P.exists() else None;g=P.exists() and a==e;ok&=g
        rows.append({'name':n,'path':str(P),'exists':P.exists(),'sha256':a,'expected':e,'pass':g})
    for n,P in paths(c).items():
        g=P.exists();ok&=g
        rows.append({'name':n,'path':str(P),'exists':g,'sha256':sha(P) if g else None,'pass':g})
    for n in ['index_ladder_summary','tail_holdout_summary']:
        P=Path(c['source'][n]);g=P.exists();ok&=g
        rows.append({'name':n,'path':str(P),'exists':g,'sha256':sha(P) if g else None,'pass':g})
    ug,uok=upstream(c) if ok else ({},False);ok&=uok
    return rows,ug,ok

def load_combined(c,universe):
    P=paths(c)
    if universe=='CSI1800':
        disc=P['CSI1800_DISC'];h20=P['CSI1800_H20_HOLD'];h60=P['CSI1800_H60_HOLD']
    else:
        disc=P['TAIL_DISC'];h20=P['TAIL_H20_HOLD'];h60=P['TAIL_H60_HOLD']
    d=pd.read_parquet(disc)
    d['signal_date']=pd.to_datetime(d['signal_date'])
    d=d[(d['signal_date']<=pd.Timestamp(c['window']['discovery_end'])) & d['horizon'].isin([20,60])].copy()
    d['segment']='DISCOVERY'
    hs=[]
    for h,p in [(20,h20),(60,h60)]:
        x=pd.read_parquet(p);x['signal_date']=pd.to_datetime(x['signal_date'])
        x=x[x['signal_date']>pd.Timestamp(c['window']['discovery_end'])].copy()
        x['horizon']=h;x['segment']='HOLDOUT';hs.append(x)
    z=pd.concat([d]+hs,ignore_index=True)
    z['bucket']=z['bucket'].astype(str)
    return z.sort_values(['horizon','signal_date','bucket']).reset_index(drop=True)

def calendar(c):
    u=pd.read_parquet(c['source']['full_market_universe'],columns=['trade_date'])
    return [pd.Timestamp(x) for x in sorted(pd.to_datetime(u['trade_date']).dropna().unique())]

def cohort_table(rows,sc,cal):
    pos={d:i for i,d in enumerate(cal)};out=[]
    for (h,d),g in rows.groupby(['horizon','signal_date'],sort=True):
        q={str(r.bucket):float(r.mean_excess) for r in g.itertuples() if pd.notna(r.mean_excess)}
        if not all(k in q for k in ['Q1','Q2','Q3','Q4','Q5']):continue
        i=pos.get(pd.Timestamp(d))
        if i is None or i+int(h)>=len(cal):continue
        out.append({'signal_date':pd.Timestamp(d),'exit_date':cal[i+int(h)],'horizon':int(h),
                    'Q1':q['Q1'],'Q2':q['Q2'],'Q3':q['Q3'],'Q4':q['Q4'],'Q5':q['Q5'],
                    'spread':q['Q5']-q['Q1'],'mono':sc.monotonicity_score(q)})
    return pd.DataFrame(out).sort_values(['horizon','exit_date','signal_date']).reset_index(drop=True)

def rolling_evidence(coh,sc,w):
    parts=[]
    for h,g in coh.groupby('horizon',sort=True):
        g=g.sort_values('exit_date').reset_index(drop=True).copy()
        for q in ['Q1','Q2','Q3','Q4','Q5']:g[f'roll_{q}']=g[q].rolling(w,min_periods=w).mean()
        g['roll_spread']=g['spread'].rolling(w,min_periods=w).mean()
        monos=[]
        for r in g.itertuples():
            q={k:getattr(r,f'roll_{k}') for k in ['Q1','Q2','Q3','Q4','Q5']}
            monos.append(sc.monotonicity_score(q) if all(pd.notna(v) for v in q.values()) else np.nan)
        g['roll_mono']=monos;g['evidence_count']=g['spread'].rolling(w,min_periods=w).count()
        parts.append(g[['exit_date','horizon','roll_spread','roll_mono','evidence_count']])
    return pd.concat(parts,ignore_index=True)

def state_daily(evid,cal,c):
    w=int(c['detector']['rolling_matured_cohorts']);base=pd.DataFrame({'state_date':cal});merged=base.copy()
    for h in [20,60]:
        x=evid[evid['horizon']==h].sort_values('exit_date')[['exit_date','roll_spread','roll_mono','evidence_count']]
        x=x.rename(columns={'roll_spread':f'spread{h}','roll_mono':f'mono{h}','evidence_count':f'n{h}'})
        merged=pd.merge_asof(merged.sort_values('state_date'),x.sort_values('exit_date'),left_on='state_date',right_on='exit_date',direction='backward')
        merged=merged.drop(columns=['exit_date'])
    def classify(r):
        vals=[r.spread20,r.mono20,r.spread60,r.mono60]
        if any(pd.isna(v) for v in vals) or r.n20<w or r.n60<w:return 'UNAVAILABLE'
        if all(v>0 for v in vals):return 'PERSISTENCE'
        if all(v<0 for v in vals):return 'REVERSAL'
        return 'TRANSITION'
    merged['state']=merged.apply(classify,axis=1);return merged

def eval_future(states,coh):
    rows=[]
    for h in [20,60]:
        c=coh[coh['horizon']==h][['signal_date','spread','mono','Q1','Q5']].copy()
        z=c.merge(states[['state_date','state','spread20','mono20','spread60','mono60']],left_on='signal_date',right_on='state_date',how='left')
        z=z[z['state'].isin(['PERSISTENCE','REVERSAL','TRANSITION'])]
        for st,g in z.groupby('state'):
            directional=None
            if st=='PERSISTENCE':directional=float((g['spread']>0).mean())
            elif st=='REVERSAL':directional=float((g['spread']<0).mean())
            rows.append({'horizon':h,'state':st,'n_signal_dates':len(g),'mean_future_spread':float(g['spread'].mean()),
                         'median_future_spread':float(g['spread'].median()),'mean_future_mono':float(g['mono'].mean()),
                         'directional_hit_rate':directional})
    return pd.DataFrame(rows)

def post_holdout_benchmark(c,cal):
    start=pd.Timestamp(c['window']['discovery_end']);end=pd.Timestamp(c['window']['market_data_through'])
    u=pd.read_parquet(c['source']['full_market_universe'],columns=['trade_date','symbol'])
    u['trade_date']=pd.to_datetime(u['trade_date']);u['symbol']=u['symbol'].astype(str);u=u[(u['trade_date']>start)&(u['trade_date']<=end)].copy()
    f=pd.read_parquet(c['source']['canonical_features'],columns=['trade_date','symbol','adj_open','adj_close'])
    f['trade_date']=pd.to_datetime(f['trade_date']);f['symbol']=f['symbol'].astype(str)
    pos={d:i for i,d in enumerate(cal)};maps=[]
    for d in sorted(u['trade_date'].unique()):
        d=pd.Timestamp(d);i=pos.get(d)
        if i is None:continue
        for h in [20,60]:
            if i+h<len(cal):maps.append({'signal_date':d,'horizon':h,'entry_date':cal[i+1],'exit_date':cal[i+h]})
    dm=pd.DataFrame(maps);out=[]
    for h in [20,60]:
        m=dm[dm['horizon']==h]
        if m.empty:continue
        z=u.rename(columns={'trade_date':'signal_date'}).merge(m,on='signal_date',how='inner')
        e=f[['trade_date','symbol','adj_open']].rename(columns={'trade_date':'entry_date','adj_open':'entry'})
        x=f[['trade_date','symbol','adj_close']].rename(columns={'trade_date':'exit_date','adj_close':'exit'})
        z=z.merge(e,on=['entry_date','symbol'],how='left').merge(x,on=['exit_date','symbol'],how='left')
        ep=pd.to_numeric(z['entry'],errors='coerce');xp=pd.to_numeric(z['exit'],errors='coerce')
        z['raw']=np.where((ep>0)&(xp>0),xp/ep-1.0,np.nan)
        med=z.groupby('signal_date')['raw'].median().reset_index(name='benchmark_median');med['horizon']=h;out.append(med)
    return pd.concat(out,ignore_index=True) if out else pd.DataFrame()

def q1_active_long(rows,states,bench):
    x=rows[(rows['segment']=='HOLDOUT') & rows['horizon'].isin([20,60])].copy()
    x=x.merge(states[['state_date','state']],left_on='signal_date',right_on='state_date',how='left')
    x=x.merge(bench,on=['signal_date','horizon'],how='left');x['raw_mean']=x['mean_excess']+x['benchmark_median']
    out=[]
    for h in [20,60]:
        for st in ['PERSISTENCE','REVERSAL','TRANSITION']:
            g=x[(x['horizon']==h)&(x['state']==st)]
            if g.empty:continue
            q1=g[g['bucket']=='Q1'];q5=g[g['bucket']=='Q5'];dates=sorted(set(q1['signal_date']) & set(q5['signal_date']))
            q1=q1[q1['signal_date'].isin(dates)];q5=q5[q5['signal_date'].isin(dates)]
            out.append({'horizon':h,'state':st,'n_signal_dates':len(dates),
                        'q1_mean_raw_return':float(q1['raw_mean'].mean()) if len(q1) else None,
                        'q1_mean_excess_vs_market_median':float(q1['mean_excess'].mean()) if len(q1) else None,
                        'q5_mean_raw_return':float(q5['raw_mean'].mean()) if len(q5) else None,
                        'q5_mean_excess_vs_market_median':float(q5['mean_excess'].mean()) if len(q5) else None,
                        'q1_minus_q5_raw':float(q1['raw_mean'].mean()-q5['raw_mean'].mean()) if len(q1) and len(q5) else None})
    return pd.DataFrame(out)

def diagnostics(evaldf):
    out={}
    for h in [20,60]:
        z=evaldf[evaldf['horizon']==h].set_index('state')
        p=z.loc['PERSISTENCE'] if 'PERSISTENCE' in z.index else None;r=z.loc['REVERSAL'] if 'REVERSAL' in z.index else None
        out[f'H{h}']={'persistence_mean_future_spread':safe(p['mean_future_spread']) if p is not None else None,
                      'reversal_mean_future_spread':safe(r['mean_future_spread']) if r is not None else None,
                      'persistence_directional_hit_rate':safe(p['directional_hit_rate']) if p is not None else None,
                      'reversal_directional_hit_rate':safe(r['directional_hit_rate']) if r is not None else None,
                      'state_separation':safe(float(p['mean_future_spread']-r['mean_future_spread'])) if p is not None and r is not None else None}
    return out

def check(c):
    rows,ug,ok=check_inputs(c)
    out={'status':'PASS_RET120_CAUSAL_REGIME_DETECTOR_V0_1_CHECK' if ok else 'BLOCKED_RET120_CAUSAL_REGIME_DETECTOR_V0_1_CHECK',
         'inputs':rows,'upstream_gates':ug,'detector':c['detector'],'evaluation':c['evaluation'],'safety':c['safety']}
    print(json.dumps(out,ensure_ascii=False,indent=2));return 0 if ok else 3

def run(c):
    rows,ug,ok=check_inputs(c)
    if not ok:raise RuntimeError('input/upstream gate failed')
    sc=scanner(c);cal=calendar(c);outdir=Path(c['output_dir']);outdir.mkdir(parents=True,exist_ok=True);bench=post_holdout_benchmark(c,cal)
    result={}
    for uni in ['CSI1800','EX_CSI1800']:
        print('[regime]',uni)
        dr=load_combined(c,uni);coh=cohort_table(dr,sc,cal);evid=rolling_evidence(coh,sc,int(c['detector']['rolling_matured_cohorts']))
        states=state_daily(evid,cal,c);ev=eval_future(states,coh);q1=q1_active_long(dr,states,bench)
        states.to_parquet(outdir/f'{uni.lower()}_daily_state.parquet',index=False)
        ev.to_csv(outdir/f'{uni.lower()}_state_future_diagnostic.csv',index=False)
        q1.to_csv(outdir/f'{uni.lower()}_q1_active_long_holdout_diagnostic.csv',index=False)
        latest=states[states['state']!='UNAVAILABLE'].iloc[-1].to_dict() if (states['state']!='UNAVAILABLE').any() else None
        dist=states[states['state']!='UNAVAILABLE']['state'].value_counts().to_dict()
        result[uni]={'latest_causal_state':{k:safe(v) for k,v in latest.items()} if latest else None,
                     'state_distribution_sessions':{k:int(v) for k,v in dist.items()},
                     'future_diagnostic':diagnostics(ev),'future_by_state':ev.to_dict(orient='records'),
                     'q1_active_long_post_discovery':q1.to_dict(orient='records'),
                     'artifacts':{'daily_state':str(outdir/f'{uni.lower()}_daily_state.parquet'),
                                  'future_diagnostic':str(outdir/f'{uni.lower()}_state_future_diagnostic.csv'),
                                  'q1_active_long':str(outdir/f'{uni.lower()}_q1_active_long_holdout_diagnostic.csv')}}
    summary={'status':'PASS_RET120_CAUSAL_REGIME_DETECTOR_V0_1_RUN','research_id':c['research_id'],
             'market_data_through':c['window']['market_data_through'],'detector':c['detector'],
             'important_semantics':['Regime is universe-specific: CSI1800 and EX_CSI1800 are classified separately.',
                                    'State at T close uses only outcomes fully matured by T close.',
                                    'Future diagnostics use later returns only to evaluate the causal state, never to construct that state.',
                                    'Q1 active-long diagnostics distinguish raw return from excess-vs-market-median return.'],
             'results':result,
             'interpretation_guardrail':'This is retrospective causal validation of a predeclared simple sign detector, not an independent forward qualification. The next step after a useful result is immutable forward state capture.',
             'safety':c['safety']}
    writej(outdir/'ret120_causal_regime_detector_v0_1_summary.json',summary)
    print(json.dumps(summary,ensure_ascii=False,indent=2,default=safe));print('OUTPUT:',outdir);return 0

def main():
    ap=argparse.ArgumentParser();ap.add_argument('command',choices=['check','run']);a=ap.parse_args();c=C()
    try:rc=check(c) if a.command=='check' else run(c)
    except Exception as e:
        print(json.dumps({'status':'FAIL_CLOSED_RET120_CAUSAL_REGIME_DETECTOR_V0_1','error':f'{type(e).__name__}: {e}'},ensure_ascii=False,indent=2));rc=3
    raise SystemExit(rc)
if __name__=='__main__':main()
