
from __future__ import annotations
import hashlib, json, math
from pathlib import Path
import numpy as np
import pandas as pd


def _finite(x):
    try: return math.isfinite(float(x))
    except Exception: return False


def sha256_file(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):
            h.update(b)
    return h.hexdigest()


def synth_adjust(g: pd.DataFrame) -> pd.DataFrame:
    x=g.sort_values('trade_date').copy()
    for c in ['open','high','low','close','pre_close','suspend_flag']:
        if c in x.columns:
            x[c]=pd.to_numeric(x[c],errors='coerce')
    ratio=np.where((x['close']>0)&(x['pre_close']>0),x['close']/x['pre_close'],np.nan)
    ratio=pd.Series(ratio,index=x.index,dtype='float64').fillna(1.0)
    x['adj_close']=ratio.cumprod()
    scale=np.where(x['close']>0,x['adj_close']/x['close'],np.nan)
    x['adj_open']=x['open']*scale
    x['adj_high']=x['high']*scale if 'high' in x.columns else np.nan
    x['adj_low']=x['low']*scale if 'low' in x.columns else np.nan
    return x


def canonicalize_gate_rows(rows: pd.DataFrame) -> pd.DataFrame:
    if rows.empty: return rows.copy()
    keep=[]
    compare=['fire_state','actual_open','suggested_shares','suggested_notional','open_valid_low','open_valid_high','stop_raw','lane','symbol','signal_date','fire_date']
    for rid,g in rows.groupby('recommendation_id',sort=False):
        vals=[tuple(str(r.get(c,'')) for c in compare) for _,r in g.iterrows()]
        if len(set(vals))>1:
            raise RuntimeError(f'conflicting gate records for {rid}')
        keep.append(g.sort_values('_gate_run').iloc[-1])
    return pd.DataFrame(keep).reset_index(drop=True)


def valid_open(row) -> bool:
    if not (_finite(row.get('adj_open')) and float(row['adj_open'])>0): return False
    if 'suspend_flag' in row.index and _finite(row.get('suspend_flag')) and int(float(row['suspend_flag']))!=0: return False
    return True


def valid_close(row) -> bool:
    if not (_finite(row.get('adj_close')) and float(row['adj_close'])>0): return False
    if 'suspend_flag' in row.index and _finite(row.get('suspend_flag')) and int(float(row['suspend_flag']))!=0: return False
    return True


def event_context(event: pd.Series, bars: pd.DataFrame, sessions: list[pd.Timestamp]) -> dict:
    symbol=str(event['symbol'])
    signal_date=pd.Timestamp(str(event['signal_date'])[:10])
    fire_date=pd.Timestamp(str(event['fire_date'])[:10])
    g=bars[bars['symbol'].astype(str).eq(symbol)].copy()
    if g.empty: return {'ok':False,'reason':'NO_SYMBOL_BARS'}
    g=g.sort_values('trade_date').reset_index(drop=True)
    bydate={pd.Timestamp(r.trade_date):r for r in g.itertuples(index=False)}
    srow=bydate.get(signal_date); erow=bydate.get(fire_date)
    if srow is None: return {'ok':False,'reason':'NO_SIGNAL_DATE_BAR'}
    if erow is None: return {'ok':False,'reason':'NO_FIRE_DATE_BAR'}
    if not (_finite(srow.close) and float(srow.close)>0 and _finite(srow.adj_close) and float(srow.adj_close)>0):
        return {'ok':False,'reason':'BAD_SIGNAL_SCALE'}
    if not (_finite(erow.close) and float(erow.close)>0 and _finite(erow.adj_close) and float(erow.adj_close)>0):
        return {'ok':False,'reason':'BAD_ENTRY_SCALE'}
    stop_raw=float(event['stop_raw']); actual_open=float(event['actual_open'])
    stop_adj=stop_raw*(float(srow.adj_close)/float(srow.close))
    entry_adj=actual_open*(float(erow.adj_close)/float(erow.close))
    if not (_finite(stop_adj) and stop_adj>0 and _finite(entry_adj) and entry_adj>0):
        return {'ok':False,'reason':'BAD_ADJUSTED_LEVEL'}
    pos={d:i for i,d in enumerate(sessions)}
    if fire_date not in pos: return {'ok':False,'reason':'FIRE_DATE_NOT_IN_CALENDAR'}
    return {'ok':True,'symbol':symbol,'signal_date':signal_date,'fire_date':fire_date,
            'stop_adj':stop_adj,'entry_adj':entry_adj,'g':g,'bydate':bydate,'pos':pos}


def first_structural_exit(ctx: dict, sessions: list[pd.Timestamp], as_of: pd.Timestamp) -> dict:
    if not ctx.get('ok'): return {'status':'UNAVAILABLE','reason':ctx.get('reason')}
    i0=ctx['pos'][ctx['fire_date']]
    trigger=None
    for d in sessions[i0:]:
        if d>as_of: break
        r=ctx['bydate'].get(d)
        if r is None: continue
        if valid_close(pd.Series(r._asdict())) and float(r.adj_close) < float(ctx['stop_adj']):
            trigger=d; break
    if trigger is None:
        return {'status':'OPEN_NO_TRIGGER','trigger_date':None,'exit_date':None,'exit_adj':np.nan,'deferred_sessions':None}
    it=ctx['pos'].get(trigger)
    deferred=0
    for d in sessions[it+1:]:
        if d>as_of: break
        r=ctx['bydate'].get(d)
        if r is not None and valid_open(pd.Series(r._asdict())):
            return {'status':'CLOSED','trigger_date':trigger,'exit_date':d,'exit_adj':float(r.adj_open),'deferred_sessions':deferred}
        deferred+=1
    return {'status':'TRIGGERED_WAIT_EXIT','trigger_date':trigger,'exit_date':None,'exit_adj':np.nan,'deferred_sessions':deferred}


def fixed_horizon_exit(ctx: dict, sessions: list[pd.Timestamp], as_of: pd.Timestamp, horizon: int) -> dict:
    if not ctx.get('ok'): return {'status':'UNAVAILABLE','reason':ctx.get('reason')}
    i0=ctx['pos'][ctx['fire_date']]
    target_i=i0+int(horizon)
    if target_i>=len(sessions):
        return {'status':'WAIT_HORIZON','scheduled_date':None,'exit_date':None,'exit_adj':np.nan,'deferred_sessions':None}
    scheduled=sessions[target_i]
    if scheduled>as_of:
        return {'status':'WAIT_HORIZON','scheduled_date':scheduled,'exit_date':None,'exit_adj':np.nan,'deferred_sessions':None}
    deferred=0
    for d in sessions[target_i:]:
        if d>as_of: break
        r=ctx['bydate'].get(d)
        if r is not None and valid_close(pd.Series(r._asdict())):
            return {'status':'CLOSED','scheduled_date':scheduled,'exit_date':d,'exit_adj':float(r.adj_close),'deferred_sessions':deferred}
        deferred+=1
    return {'status':'WAIT_VALID_CLOSE','scheduled_date':scheduled,'exit_date':None,'exit_adj':np.nan,'deferred_sessions':deferred}


def evaluate_policy(event: pd.Series, policy: dict, ctx: dict, sessions: list[pd.Timestamp], as_of: pd.Timestamp) -> dict:
    base={
      'recommendation_id':event['recommendation_id'],'signal_date':str(event['signal_date'])[:10],
      'fire_date':str(event['fire_date'])[:10],'symbol':event['symbol'],'lane':event['lane'],
      'universe_tier':event.get('universe_tier',''),'policy_id':policy['policy_id'],'policy_type':policy['type'],
      'winner_candidate':bool(policy.get('winner_candidate',False)),
      'entry_adj':ctx.get('entry_adj',np.nan),'stop_adj':ctx.get('stop_adj',np.nan),
    }
    if not ctx.get('ok'):
        return {**base,'status':'UNAVAILABLE','exit_reason':ctx.get('reason'),'trigger_date':None,'scheduled_exit_date':None,'exit_date':None,'exit_adj':np.nan}
    typ=policy['type']; structural=first_structural_exit(ctx,sessions,as_of)
    if typ=='STRUCTURAL_ONLY':
        chosen=structural; reason='STRUCTURAL' if chosen.get('status')=='CLOSED' else chosen.get('status')
        scheduled=None
    else:
        fixed=fixed_horizon_exit(ctx,sessions,as_of,int(policy['horizon_sessions']))
        scheduled=fixed.get('scheduled_date')
        if typ=='FIXED_HORIZON':
            chosen=fixed; reason='FIXED_HORIZON' if fixed.get('status')=='CLOSED' else fixed.get('status')
        elif typ=='HYBRID':
            trig=structural.get('trigger_date')
            # Structural wins only when trigger is strictly before the scheduled horizon date.
            if trig is not None and scheduled is not None and pd.Timestamp(trig)<pd.Timestamp(scheduled):
                chosen=structural; reason='STRUCTURAL' if structural.get('status')=='CLOSED' else structural.get('status')
            elif trig is not None and scheduled is None:
                chosen=structural; reason='STRUCTURAL' if structural.get('status')=='CLOSED' else structural.get('status')
            else:
                chosen=fixed; reason='FIXED_HORIZON' if fixed.get('status')=='CLOSED' else fixed.get('status')
        else:
            raise RuntimeError(f'unknown policy type {typ}')
    exit_adj=chosen.get('exit_adj',np.nan)
    gross=(float(exit_adj)/float(ctx['entry_adj'])-1.0) if _finite(exit_adj) and float(exit_adj)>0 else np.nan
    exit_date=chosen.get('exit_date')
    holding=None
    if exit_date is not None:
        holding=int(ctx['pos'][pd.Timestamp(exit_date)]-ctx['pos'][ctx['fire_date']])
    out={**base,
      'status':'CLOSED' if _finite(gross) else chosen.get('status','OPEN'),
      'exit_reason':reason,
      'trigger_date':str(structural.get('trigger_date').date()) if structural.get('trigger_date') is not None else None,
      'scheduled_exit_date':str(pd.Timestamp(scheduled).date()) if scheduled is not None else None,
      'exit_date':str(pd.Timestamp(exit_date).date()) if exit_date is not None else None,
      'exit_adj':exit_adj,'holding_sessions':holding,
      'structural_exit':bool(reason=='STRUCTURAL' and _finite(gross)),
      'deferred_sessions':chosen.get('deferred_sessions'),
      'gross_return':gross,
    }
    for bps in [0,20,50]:
        out[f'net_return_{bps}bp']=(1.0+gross)*(1.0-bps/10000.0)-1.0 if _finite(gross) else np.nan
    return out


def metrics(df: pd.DataFrame) -> dict:
    z=df[df['status'].eq('CLOSED')].copy()
    if z.empty:
        return {'completed_n':0,'avg_gross_return':None,'median_gross_return':None,'win_rate_gross':None,
                'avg_net20':None,'avg_net50':None,'p10_net20':None,'worst_net20':None,'best_net20':None,
                'avg_holding_sessions':None,'structural_exit_fraction':None}
    g=pd.to_numeric(z['gross_return'],errors='coerce').dropna(); n20=pd.to_numeric(z['net_return_20bp'],errors='coerce').dropna()
    return {
      'completed_n':int(len(g)),
      'avg_gross_return':float(g.mean()),'median_gross_return':float(g.median()),'win_rate_gross':float((g>0).mean()),
      'avg_net20':float(n20.mean()),'avg_net50':float(pd.to_numeric(z['net_return_50bp'],errors='coerce').mean()),
      'p10_net20':float(n20.quantile(.10)),'worst_net20':float(n20.min()),'best_net20':float(n20.max()),
      'avg_holding_sessions':float(pd.to_numeric(z['holding_sessions'],errors='coerce').mean()),
      'structural_exit_fraction':float(z['structural_exit'].fillna(False).mean())
    }
