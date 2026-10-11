
from __future__ import annotations
import math
import numpy as np
import pandas as pd

def synth_adjust(g: pd.DataFrame) -> pd.DataFrame:
    x=g.sort_values('trade_date').copy()
    for c in ['open','high','low','close','pre_close','amount']:
        if c in x.columns: x[c]=pd.to_numeric(x[c],errors='coerce')
    ratio=np.where((x['close']>0)&(x['pre_close']>0),x['close']/x['pre_close'],np.nan)
    ratio=pd.Series(ratio,index=x.index,dtype='float64').fillna(1.0)
    x['adj_close']=ratio.cumprod()
    scale=x['adj_close']/x['close']
    x['adj_open']=x['open']*scale
    x['adj_high']=x['high']*scale
    x['adj_low']=x['low']*scale
    return x

def _finite(v):
    try:return math.isfinite(float(v))
    except:return False

def breakout_retest_features(g: pd.DataFrame, p: dict) -> dict:
    x=synth_adjust(g).reset_index(drop=True)
    if len(x) < max(65, p['lookback']+p['max_retest_sessions']+2):
        return {'history_ok':False,'setup_state':'NO_HISTORY'}
    t=len(x)-1
    close=float(x.loc[t,'adj_close']); raw_close=float(x.loc[t,'close'])
    ma20=x['adj_close'].rolling(20,min_periods=20).mean()
    ma20_t=float(ma20.iloc[t]) if _finite(ma20.iloc[t]) else np.nan
    ma20_5=float(ma20.iloc[t-5]) if t>=5 and _finite(ma20.iloc[t-5]) else np.nan
    trend_recovered=bool(_finite(ma20_t) and _finite(ma20_5) and close>ma20_t and ma20_t>ma20_5)
    adv20=float(pd.to_numeric(x['amount'],errors='coerce').iloc[max(0,t-19):t+1].mean()) if 'amount' in x.columns else np.nan
    prior20=float(x['adj_close'].iloc[t-p['lookback']:t].max())
    trigger=prior20*(1.0+p['breakout_buffer'])
    breakout_distance=(trigger/close-1.0) if close>0 else np.nan
    found=None
    j0=max(p['lookback'],t-p['max_retest_sessions'])
    for j in range(j0,t):
        hist=x['adj_close'].iloc[j-p['lookback']:j]
        if len(hist)<p['lookback']: continue
        level=float(hist.max())
        prior_vol=pd.to_numeric(x['amount'],errors='coerce').iloc[j-p['volume_lookback']:j]
        vol_base=float(prior_vol.mean()) if len(prior_vol) else np.nan
        vol_j=float(x.loc[j,'amount']) if _finite(x.loc[j,'amount']) else np.nan
        vol_ratio=(vol_j/vol_base) if _finite(vol_j) and _finite(vol_base) and vol_base>0 else np.nan
        breakout=bool(float(x.loc[j,'adj_close']) >= level*(1+p['breakout_buffer']) and _finite(vol_ratio) and vol_ratio>=p['min_volume_ratio'])
        if not breakout: continue
        between=x['adj_close'].iloc[j+1:t+1]
        not_invalid=bool((between >= level*(1-p['invalidation'])).all())
        retest=bool(float(x.loc[t,'adj_low']) <= level*(1+p['retest_upper']) and float(x.loc[t,'adj_close']) >= level*(1-p['retest_tolerance']))
        if not_invalid and retest: found=(j,level,vol_ratio)
    if found is None:
        return {'history_ok':True,'setup_state':'WATCH','trend_recovered':trend_recovered,
          'adj_close':close,'raw_close':raw_close,'ma20':ma20_t,
          'ma20_slope5':(ma20_t/ma20_5-1 if _finite(ma20_5) and ma20_5!=0 else np.nan),
          'adv20_amount':adv20,'breakout_distance_pct':breakout_distance,
          'breakout_level_raw':np.nan,'stop_raw':np.nan,'open_valid_low':np.nan,'open_valid_high':np.nan,'risk_distance_pct':np.nan}
    j,level,vol_ratio=found
    raw_scale=raw_close/close if close>0 else np.nan
    level_raw=level*raw_scale; stop_raw=level_raw*(1-p['invalidation'])
    risk_distance=(raw_close-stop_raw)/raw_close if raw_close>0 else np.nan
    return {'history_ok':True,'setup_state':'CONFIRMED_RETEST_T','trend_recovered':trend_recovered,
      'adj_close':close,'raw_close':raw_close,'ma20':ma20_t,
      'ma20_slope5':(ma20_t/ma20_5-1 if _finite(ma20_5) and ma20_5!=0 else np.nan),
      'adv20_amount':adv20,'breakout_distance_pct':breakout_distance,
      'breakout_session':str(pd.Timestamp(x.loc[j,'trade_date']).date()),'breakout_volume_ratio':vol_ratio,
      'breakout_level_raw':level_raw,'stop_raw':stop_raw,
      'open_valid_low':level_raw*(1-p['retest_tolerance']),'open_valid_high':level_raw*(1+p['retest_upper']),
      'risk_distance_pct':risk_distance}


def entry_condition_state(raw_close: float, low: float, high: float) -> dict:
    """Diagnostic only. Does not change V0.1 ranking/selection."""
    if not all(_finite(v) for v in [raw_close, low, high]) or raw_close <= 0 or low <= 0 or high <= 0 or low > high:
        return {'entry_condition_state':'NO_ENTRY_ZONE',
                'close_in_open_zone':False,
                'close_vs_open_zone_pct':np.nan}
    if raw_close < low:
        return {'entry_condition_state':'BELOW_ZONE_WAIT_RECOVERY',
                'close_in_open_zone':False,
                'close_vs_open_zone_pct':raw_close/low-1.0}
    if raw_close > high:
        return {'entry_condition_state':'ABOVE_ZONE_WAIT_PULLBACK',
                'close_in_open_zone':False,
                'close_vs_open_zone_pct':raw_close/high-1.0}
    return {'entry_condition_state':'IN_ZONE_AT_T_CLOSE',
            'close_in_open_zone':True,
            'close_vs_open_zone_pct':0.0}


def preview_size(entry: float, stop: float, adv20_amount: float, nav: float, lane: str, risk: dict) -> dict:
    if not all(_finite(v) for v in [entry,stop,nav]) or entry<=0 or stop<=0 or entry<=stop or nav<=0:
        return {'preview_shares':0,'preview_weight':0.0,'preview_notional':0.0,'risk_budget_amount':0.0}
    lot=int(risk['board_lot'])
    if lane=='MOMENTUM': rf=float(risk['momentum_risk_fraction_nav']); mw=float(risk['momentum_max_weight'])
    else: rf=float(risk['mean_reversion_risk_fraction_nav']); mw=float(risk['mean_reversion_max_weight'])
    risk_budget=nav*rf; per_share=entry-stop
    by_risk=math.floor((risk_budget/per_share)/lot)*lot
    by_weight=math.floor(((nav*mw)/entry)/lot)*lot
    by_liq=math.floor(((adv20_amount*float(risk['adv20_participation_max']))/entry)/lot)*lot if _finite(adv20_amount) and adv20_amount>0 else 0
    shares=max(0,min(by_risk,by_weight,by_liq)); notional=shares*entry
    return {'preview_shares':shares,'preview_notional':notional,'preview_weight':notional/nav if nav>0 else 0.0,'risk_budget_amount':risk_budget}

def rank_panel(df: pd.DataFrame, lane: str) -> pd.DataFrame:
    x=df.copy();x['_setup_rank']=np.where(x['setup_state'].eq('CONFIRMED_RETEST_T'),0,1)
    x['_risk']=pd.to_numeric(x['risk_distance_pct'],errors='coerce').fillna(999.0)
    x['_adv']=-pd.to_numeric(x['adv20_amount'],errors='coerce').fillna(0.0)
    if lane=='MOMENTUM':
        x['_conf']=x['momentum_confidence'].map({'STRONG':0,'MEDIUM':1}).fillna(9)
        x=x.sort_values(['_setup_rank','_conf','_risk','_adv','symbol'],kind='stable')
    else:
        x['_trend_rank']=np.where(x['trend_recovered'].astype('boolean').fillna(False).astype(bool),0,1)
        x=x.sort_values(['_setup_rank','_trend_rank','_risk','_adv','symbol'],kind='stable')
    x['lane_rank']=range(1,len(x)+1)
    return x.drop(columns=[c for c in x.columns if c.startswith('_')])
