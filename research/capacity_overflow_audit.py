from __future__ import annotations
import math, hashlib
from pathlib import Path
import pandas as pd
import numpy as np

CAPACITY_DEFAULT_TOKENS=("CAPACITY","GROSS","MAX_GROSS","SLOT","MAX_POSITION","MAX_POSITIONS","FULL","NO_ROOM","NO_CAPACITY")

def norm_date(x):
    if x is None or (isinstance(x,float) and math.isnan(x)): return None
    s=str(x).strip()[:10].replace('-','')
    return s if len(s)==8 and s.isdigit() else str(x).strip()

def is_capacity_block(status, reason, tokens=CAPACITY_DEFAULT_TOKENS):
    s=(str(status or '')+'|'+str(reason or '')).upper()
    return any(t.upper() in s for t in tokens) and not any(x in s for x in ('HYGIENE','ST','SUSPEND','LIMIT','UPSTOP','NEW_LIST','MISSING'))

def classify_signal(status, reason, actual_entry_date, entry_open, tokens=CAPACITY_DEFAULT_TOKENS):
    st=str(status or '').upper(); rs=str(reason or '').upper()
    if actual_entry_date not in (None,'') and str(actual_entry_date).lower()!='nan': return 'ENTERED'
    try:
        if entry_open is not None and np.isfinite(float(entry_open)) and float(entry_open)>0 and ('ENTER' in st or 'FILLED' in st): return 'ENTERED'
    except Exception: pass
    if st.startswith('EXCLUDED') or 'HYGIENE' in st or 'EXCLUDED' in rs: return 'EXCLUDED'
    if 'PENDING' in st: return 'PENDING'
    if is_capacity_block(st,rs,tokens): return 'BLOCKED_CAPACITY'
    if 'BLOCK' in st or 'REJECT' in st or 'INVALID' in st or 'CANCEL' in st: return 'BLOCKED_OTHER'
    return 'OTHER'

def next_session(sessions, signal_date):
    d=norm_date(signal_date)
    for x in sessions:
        if x>d: return x
    return None

def nth_subsequent_session(sessions, start_date, n):
    d=norm_date(start_date); fut=[x for x in sessions if x>d]
    return fut[n-1] if len(fut)>=n else None

def safe_return(entry, exit_):
    try:
        a=float(entry); b=float(exit_)
        if a>0 and b>0 and np.isfinite(a) and np.isfinite(b): return b/a-1.0
    except Exception: pass
    return np.nan

def sha256_file(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()

def aggregate_returns(df, value='gross_return'):
    if df.empty or value not in df.columns:
        return {'n':0,'avg_return':None,'median_return':None,'win_rate':None,'p10_return':None,'worst_return':None,'best_return':None}
    y=pd.to_numeric(df[value],errors='coerce').dropna()
    if y.empty:
        return {'n':0,'avg_return':None,'median_return':None,'win_rate':None,'p10_return':None,'worst_return':None,'best_return':None}
    return {'n':int(len(y)),'avg_return':float(y.mean()),'median_return':float(y.median()),'win_rate':float((y>0).mean()),'p10_return':float(y.quantile(.10)),'worst_return':float(y.min()),'best_return':float(y.max())}

def active_at_open(intervals: pd.DataFrame, date8: str):
    if intervals.empty: return intervals.copy()
    x=intervals.copy(); x['entry_date']=x['entry_date'].map(norm_date); x['exit_date']=x['exit_date'].map(norm_date)
    return x[(x['entry_date']<date8) & (x['exit_date'].isna() | (x['exit_date']>=date8))].copy()

def build_intervals(positions: pd.DataFrame, trades: pd.DataFrame, strategy_id: str, ledger='BASE20'):
    rows=[]
    if trades is not None and not trades.empty:
        t=trades[trades['strategy_id'].astype(str).eq(strategy_id)].copy()
        if 'ledger' in t.columns and ledger is not None: t=t[t['ledger'].astype(str).eq(ledger)]
        for r in t.itertuples(index=False):
            rows.append({'strategy_id':strategy_id,'symbol':str(r.symbol),'signal_date':norm_date(getattr(r,'signal_date',None)),'entry_date':norm_date(r.entry_date),'exit_date':norm_date(r.exit_date),'source':'COMPLETED_TRADE'})
    if positions is not None and not positions.empty:
        p=positions[positions['strategy_id'].astype(str).eq(strategy_id)].copy()
        if 'ledger' in p.columns and ledger is not None: p=p[p['ledger'].astype(str).eq(ledger)]
        for r in p.itertuples(index=False):
            rows.append({'strategy_id':strategy_id,'symbol':str(r.symbol),'signal_date':norm_date(getattr(r,'signal_date',None)),'entry_date':norm_date(r.entry_date),'exit_date':None,'source':'ACTIVE_POSITION'})
    return pd.DataFrame(rows,columns=['strategy_id','symbol','signal_date','entry_date','exit_date','source'])
