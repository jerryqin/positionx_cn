from __future__ import annotations
import argparse, json, sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from research import capacity_overflow_audit as eng
CFG=ROOT/'config/research/capacity_overflow_audit_v0_1.json'

def cfg(): return json.loads(CFG.read_text(encoding='utf-8'))
def now8(): return datetime.now(timezone(timedelta(hours=8)))
def iso(): return now8().isoformat(timespec='seconds')
def choose_existing(paths):
    for p in paths:
        if Path(p).exists(): return Path(p)
    return None

def state_info(spec):
    p=choose_existing(spec['state_candidates'])
    if not p: return {'exists':False,'path':None,'state':{}}
    try: j=json.loads(p.read_text(encoding='utf-8'))
    except Exception as e: return {'exists':True,'path':str(p),'error':str(e),'state':{}}
    return {'exists':True,'path':str(p),'sha256':eng.sha256_file(p),'state':j}

def connect_ro(path):
    import duckdb
    return duckdb.connect(str(path),read_only=True)

def table_cols(con, table):
    try:return [r[1] for r in con.execute(f"PRAGMA table_info('{table}')").fetchall()]
    except Exception:return []

def probe(c):
    db=choose_existing(c['forward_db_candidates'])
    out={'status':'BLOCKED_CAPACITY_OVERFLOW_AUDIT_V0_1_PROBE','generated_at':iso(),'forward_db':str(db) if db else None,'market_db':c['market_db'],'strategies':{},'guardrails':c['guardrails']}
    if not db:
        out['error']='No forward research DuckDB found'; print(json.dumps(out,ensure_ascii=False,indent=2)); return 3
    con=connect_ro(db)
    try:
        tables={x[0] for x in con.execute('SHOW TABLES').fetchall()}; out['tables_present']=sorted(tables)
        for sid,spec in c['strategies'].items():
            tab=spec['signal_table']; rec={'signal_table':tab,'table_exists':tab in tables,'columns':table_cols(con,tab),'state':state_info(spec)}
            if tab in tables:
                try: rec['status_counts']={str(k):int(v) for k,v in con.execute(f'SELECT status,count(*) FROM {tab} WHERE strategy_id=? GROUP BY status ORDER BY status',[sid]).fetchall()}
                except Exception as e: rec['status_counts_error']=str(e)
            out['strategies'][sid]=rec
        required={'forward_positions','forward_trades'}|{v['signal_table'] for v in c['strategies'].values()}
        out['market_db_exists']=Path(c['market_db']).exists(); ok=required.issubset(tables) and out['market_db_exists']
        out['status']='PASS_CAPACITY_OVERFLOW_AUDIT_V0_1_PROBE' if ok else 'BLOCKED_CAPACITY_OVERFLOW_AUDIT_V0_1_PROBE'
    finally: con.close()
    print(json.dumps(out,ensure_ascii=False,indent=2,default=str)); return 0 if out['status'].startswith('PASS') else 3

def load_market(c, symbols, as_of):
    con=connect_ro(Path(c['market_db']))
    try:
        table=c['market_table']
        sessions=[eng.norm_date(r[0]) for r in con.execute(f'SELECT DISTINCT trade_date FROM {table} WHERE trade_date<=? ORDER BY trade_date',[as_of]).fetchall()]
        if symbols:
            con.register('audit_symbols',pd.DataFrame({'symbol':sorted(set(symbols))}))
            q=f'SELECT b.symbol,b.trade_date,b.open,b.close FROM {table} b JOIN audit_symbols s USING(symbol) WHERE b.trade_date<=? ORDER BY b.symbol,b.trade_date'
            bars=con.execute(q,[as_of]).df()
        else: bars=pd.DataFrame(columns=['symbol','trade_date','open','close'])
    finally: con.close()
    if not bars.empty: bars['trade_date']=bars['trade_date'].map(eng.norm_date)
    op={(str(r.symbol),str(r.trade_date)):float(r.open) for r in bars.itertuples(index=False) if pd.notna(r.open) and float(r.open)>0}
    cl={(str(r.symbol),str(r.trade_date)):float(r.close) for r in bars.itertuples(index=False) if pd.notna(r.close) and float(r.close)>0}
    return sessions,op,cl

def capture(c,as_of):
    as8=eng.norm_date(as_of); db=choose_existing(c['forward_db_candidates'])
    if not db: raise RuntimeError('forward research DuckDB not found')
    if not Path(c['market_db']).exists(): raise RuntimeError('market DB not found')
    con=connect_ro(db)
    try:
        tables={x[0] for x in con.execute('SHOW TABLES').fetchall()}
        for t in ['forward_positions','forward_trades']+[s['signal_table'] for s in c['strategies'].values()]:
            if t not in tables: raise RuntimeError(f'missing required table {t}')
        positions=con.execute('SELECT * FROM forward_positions').df(); trades=con.execute('SELECT * FROM forward_trades').df(); signals=[]
        for sid,spec in c['strategies'].items():
            tab=spec['signal_table']; cols=table_cols(con,tab); needed=['strategy_id','symbol','signal_date','status','reason','actual_entry_date','entry_open']
            if any(x not in cols for x in needed): raise RuntimeError(f'{tab} schema missing one of {needed}; columns={cols}')
            val=spec['signal_value_column']; select=needed+([val] if val in cols else [])
            x=con.execute('SELECT '+','.join(select)+f' FROM {tab} WHERE strategy_id=? AND signal_date<=?',[sid,as8]).df()
            x['strategy_id']=sid; x['signal_value']=x[val] if val in x.columns else np.nan; x['signal_value_name']=val
            x['disposition']=[eng.classify_signal(r.status,r.reason,r.actual_entry_date,r.entry_open,c['capacity_reason_tokens']) for r in x.itertuples(index=False)]
            signals.append(x)
    finally: con.close()
    sig=pd.concat(signals,ignore_index=True) if signals else pd.DataFrame(); symbols=list(sig['symbol'].astype(str)) if not sig.empty else []
    intervals=[]
    for sid in c['strategies']:
        z=eng.build_intervals(positions,trades,sid,'BASE20'); intervals.append(z)
        if not z.empty: symbols += list(z['symbol'].astype(str))
    iv=pd.concat(intervals,ignore_index=True) if intervals else pd.DataFrame(); sessions,op,cl=load_market(c,symbols,as8)
    blocked=sig[sig['disposition'].eq('BLOCKED_CAPACITY')].copy() if not sig.empty else pd.DataFrame(); mark_rows=[]; inc_rows=[]; pressure=[]
    for sid,spec in c['strategies'].items():
        sb=blocked[blocked['strategy_id'].eq(sid)] if not blocked.empty else pd.DataFrame()
        for _,r in sb.iterrows():
            entry_date=eng.norm_date(r.get('actual_entry_date')) or eng.next_session(sessions,r['signal_date']); entry_open=r.get('entry_open')
            if not (pd.notna(entry_open) and float(entry_open)>0): entry_open=op.get((str(r['symbol']),entry_date),np.nan)
            pressure.append({'strategy_id':sid,'signal_date':eng.norm_date(r['signal_date']),'decision_entry_date':entry_date,'symbol':r['symbol'],'status':r['status'],'reason':r['reason'],'signal_value':r['signal_value']})
            for h in c['horizons_sessions']:
                td=eng.nth_subsequent_session(sessions,entry_date,int(h)) if entry_date else None; ep=float(entry_open) if pd.notna(entry_open) else np.nan; xp=cl.get((str(r['symbol']),td),np.nan) if td else np.nan
                mark_rows.append({'strategy_id':sid,'kind':'BLOCKED_CANDIDATE','signal_date':eng.norm_date(r['signal_date']),'decision_entry_date':entry_date,'symbol':r['symbol'],'horizon_sessions':h,'target_date':td,'entry_open':ep,'target_close':xp,'gross_return':eng.safe_return(ep,xp),'matured':bool(td and pd.notna(xp))})
            if entry_date:
                ai=eng.active_at_open(iv[iv['strategy_id'].eq(sid)],entry_date)
                for _,p in ai.iterrows():
                    iopen=op.get((str(p['symbol']),entry_date),np.nan)
                    for h in c['horizons_sessions']:
                        td=eng.nth_subsequent_session(sessions,entry_date,int(h)); xp=cl.get((str(p['symbol']),td),np.nan) if td else np.nan
                        inc_rows.append({'strategy_id':sid,'blocked_symbol':r['symbol'],'signal_date':eng.norm_date(r['signal_date']),'decision_entry_date':entry_date,'incumbent_symbol':p['symbol'],'incumbent_signal_date':p['signal_date'],'horizon_sessions':h,'entry_open':iopen,'target_date':td,'target_close':xp,'gross_return':eng.safe_return(iopen,xp),'matured':bool(td and pd.notna(xp))})
    marks=pd.DataFrame(mark_rows); inc=pd.DataFrame(inc_rows); pressure_df=pd.DataFrame(pressure); comp=[]
    for sid in c['strategies']:
        for h in c['horizons_sessions']:
            a=marks[(marks['strategy_id']==sid)&(marks['horizon_sessions']==h)] if not marks.empty else pd.DataFrame(); b=inc[(inc['strategy_id']==sid)&(inc['horizon_sessions']==h)] if not inc.empty else pd.DataFrame(); am=eng.aggregate_returns(a); bm=eng.aggregate_returns(b)
            comp.append({'strategy_id':sid,'horizon_sessions':h,'blocked_candidate_n':am['n'],'blocked_candidate_avg':am['avg_return'],'blocked_candidate_median':am['median_return'],'incumbent_observation_n':bm['n'],'incumbent_avg':bm['avg_return'],'incumbent_median':bm['median_return'],'avg_opportunity_delta':(am['avg_return']-bm['avg_return']) if am['avg_return'] is not None and bm['avg_return'] is not None else None})
    compdf=pd.DataFrame(comp); strategy_summary={}
    for sid,spec in c['strategies'].items():
        ss=sig[sig['strategy_id'].eq(sid)] if not sig.empty else pd.DataFrame(); st=state_info(spec)
        strategy_summary[sid]={'signals_total':int(len(ss)),'disposition_counts':{str(k):int(v) for k,v in ss['disposition'].value_counts().to_dict().items()} if not ss.empty else {},'capacity_blocked_signals':int((ss['disposition']=='BLOCKED_CAPACITY').sum()) if not ss.empty else 0,'observed_capacity_pressure_signal_dates':int(ss.loc[ss['disposition'].eq('BLOCKED_CAPACITY'),'signal_date'].astype(str).nunique()) if not ss.empty else 0,'current_forward_state':st,'capacity_rule':spec['capacity_rule'],'formal_hold_sessions':spec['formal_hold_sessions']}
    summary={'status':'PASS_CAPACITY_OVERFLOW_AUDIT_V0_1_CAPTURE','generated_at':iso(),'as_of':as8,'forward_db':str(db),'market_db':c['market_db'],'strategy_summary':strategy_summary,'interpretation_status':'OBSERVATIONAL_ONLY_NO_STRATEGY_CHANGE','capacity_day_semantics':'Only days with source-runner capacity-blocked signals are called observed capacity-pressure days; absence of a blocked signal is not proof capacity was available.','guardrails':c['guardrails']}
    dest=Path(c['output_root'])/as8/now8().strftime('%Y%m%dT%H%M%S+0800'); dest.mkdir(parents=True,exist_ok=False)
    sig.to_csv(dest/'signal_disposition.csv',index=False,encoding='utf-8-sig'); blocked.to_csv(dest/'capacity_blocked_signals.csv',index=False,encoding='utf-8-sig'); pressure_df.to_csv(dest/'pressure_events.csv',index=False,encoding='utf-8-sig'); marks.to_csv(dest/'blocked_candidate_markouts.csv',index=False,encoding='utf-8-sig'); inc.to_csv(dest/'incumbent_markouts.csv',index=False,encoding='utf-8-sig'); compdf.to_csv(dest/'blocked_vs_incumbent.csv',index=False,encoding='utf-8-sig'); (dest/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    lines=['# Capacity / Overflow Audit V0.1','',f'- As of: **{as8}**','- Mode: **observational only**','','## What this does','','Keeps Deep Oversold and RET120 Forward rules frozen. It only records signals the source Forward runner explicitly blocks for capacity/gross/slot reasons, then follows their H20/H60/H120 outcomes and compares them with positions already occupying capital at that open.','','## Current evidence','']
    for sid,v in strategy_summary.items(): lines += [f'### {sid}',f"- Capacity rule: `{v['capacity_rule']}`",f"- Signals observed: {v['signals_total']}",f"- Capacity-blocked signals: {v['capacity_blocked_signals']}",f"- Observed capacity-pressure signal dates: {v['observed_capacity_pressure_signal_dates']}",'']
    lines += ['## Important','', 'No replacement, rebalance, stop, ranking override, broker action or Forward mutation is performed. Positive opportunity delta is research evidence only.']
    (dest/'report.md').write_text('\n'.join(lines),encoding='utf-8'); print(json.dumps(summary,ensure_ascii=False,indent=2,default=str)); print('OUTPUT:',dest); return 0

def main():
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest='cmd',required=True); sub.add_parser('probe'); p=sub.add_parser('capture'); p.add_argument('--as-of',required=True); a=ap.parse_args(); c=cfg()
    try: rc=probe(c) if a.cmd=='probe' else capture(c,a.as_of)
    except Exception as e: print(json.dumps({'status':'FAIL_CLOSED_CAPACITY_OVERFLOW_AUDIT_V0_1','error':f'{type(e).__name__}: {e}'},ensure_ascii=False,indent=2)); rc=3
    raise SystemExit(rc)
if __name__=='__main__': main()
