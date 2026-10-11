
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
from datetime import datetime,timezone,timedelta
import pandas as pd
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from research import ret120_exit_policy_qualification as eng
CFG=ROOT/'config/research/ret120_exit_policy_qualification_v0_1.json'

def C(): return json.loads(CFG.read_text(encoding='utf-8'))
def now_cst(): return datetime.now(timezone(timedelta(hours=8)))
def iso_now(): return now_cst().isoformat(timespec='seconds')

def load_fire_rows(c):
    root=Path(c['paths']['fire_root']); rows=[]
    for s in root.glob('*/fire_gate/*/summary.json'):
        try:
            j=json.loads(s.read_text(encoding='utf-8'))
            if j.get('status')!='PASS_RET120_MANUAL_FIRE_SHADOW_V0_1_GATE': continue
            f=s.parent/'fire_gate.csv'
            if not f.exists(): continue
            x=pd.read_csv(f); x=x[x['fire_state'].eq('FIRE_MANUAL')].copy()
            if x.empty: continue
            x['_gate_run']=s.parent.name; x['_gate_summary']=str(s); x['_gate_summary_sha256']=eng.sha256_file(s)
            rows.append(x)
        except Exception: continue
    if not rows: return pd.DataFrame()
    return eng.canonicalize_gate_rows(pd.concat(rows,ignore_index=True))

def load_market(c,events,as_of):
    import duckdb
    if events.empty: return pd.DataFrame(),[]
    start=(pd.to_datetime(events['signal_date']).min()-pd.Timedelta(days=400)).strftime('%Y-%m-%d')
    syms=sorted(events['symbol'].astype(str).unique())
    con=duckdb.connect(c['paths']['market_db'],read_only=True)
    try:
        con.register('px_syms',pd.DataFrame({'symbol':syms}))
        table=c['paths']['market_table']
        cols=[r[0] for r in con.execute(f'DESCRIBE {table}').fetchall()]
        suspend_expr='CAST(b.suspend_flag AS INTEGER) suspend_flag' if 'suspend_flag' in cols else '0 AS suspend_flag'
        q=f'''SELECT b.symbol,CAST(b.trade_date AS DATE) trade_date,
                    CAST(b.open AS DOUBLE) open,CAST(b.high AS DOUBLE) high,CAST(b.low AS DOUBLE) low,
                    CAST(b.close AS DOUBLE) close,CAST(b.pre_close AS DOUBLE) pre_close,{suspend_expr}
             FROM {table} b JOIN px_syms s USING(symbol)
             WHERE CAST(b.trade_date AS DATE) BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
             ORDER BY b.symbol,b.trade_date'''
        bars=con.execute(q,[start,as_of]).df()
        sessions=[pd.Timestamp(x[0]) for x in con.execute(
            f'''SELECT DISTINCT CAST(trade_date AS DATE) d FROM {table}
                WHERE CAST(trade_date AS DATE)<=CAST(? AS DATE) ORDER BY d''',[as_of]).fetchall()]
    finally: con.close()
    if bars.empty: return bars,sessions
    bars['trade_date']=pd.to_datetime(bars['trade_date'])
    parts=[]
    for _,g in bars.groupby('symbol',sort=False): parts.append(eng.synth_adjust(g))
    return pd.concat(parts,ignore_index=True),sessions

def check(c):
    db=Path(c['paths']['market_db']); fr=Path(c['paths']['fire_root'])
    out={'status':'PASS_RET120_EXIT_POLICY_QUALIFICATION_V0_1_CHECK' if db.exists() and fr.exists() else 'BLOCKED_RET120_EXIT_POLICY_QUALIFICATION_V0_1_CHECK',
         'market_db_exists':db.exists(),'fire_root_exists':fr.exists(),'formal_forward':c['formal_forward'],
         'structural_invalidation':c['structural_invalidation'],'policies':c['policies'],'review_gate':c['review_gate'],'guardrails':c['guardrails']}
    print(json.dumps(out,ensure_ascii=False,indent=2)); return 0 if out['status'].startswith('PASS') else 3

def evaluate(c,as_of):
    events=load_fire_rows(c)
    if events.empty:
        out={'status':'COLLECTING_NO_FIRE_EVENTS','as_of':as_of,'formal_signal_start':c['formal_forward']['formal_signal_start']}
        print(json.dumps(out,ensure_ascii=False,indent=2)); return 0
    bars,sessions=load_market(c,events,as_of)
    if bars.empty or not sessions: raise RuntimeError('market bars/calendar unavailable')
    ao=pd.Timestamp(as_of); formal_start=pd.Timestamp(c['formal_forward']['formal_signal_start'])
    rows=[]
    for _,ev in events.iterrows():
        ctx=eng.event_context(ev,bars,sessions)
        lane=str(ev['lane']); policies=c['policies'].get(lane,[])
        for p in policies:
            r=eng.evaluate_policy(ev,p,ctx,sessions,ao)
            r['sample_class']='FORMAL_FORWARD' if pd.Timestamp(str(ev['signal_date'])[:10])>=formal_start else 'REHEARSAL_EXCLUDED'
            rows.append(r)
    out=pd.DataFrame(rows)
    metrics=[]
    for (sample,lane,pid),g in out.groupby(['sample_class','lane','policy_id'],dropna=False):
        m=eng.metrics(g); metrics.append({'sample_class':sample,'lane':lane,'policy_id':pid,**m})
    met=pd.DataFrame(metrics)
    formal=out[out['sample_class'].eq('FORMAL_FORWARD')]
    first_formal=pd.to_datetime(formal['fire_date']).min() if not formal.empty else pd.NaT
    months=(ao.year-first_formal.year)*12+(ao.month-first_formal.month) if pd.notna(first_formal) else 0
    winner_ids={p['policy_id'] for lane in c['policies'].values() for p in lane if p.get('winner_candidate')}
    fm=met[(met['sample_class'].eq('FORMAL_FORWARD')) & (met['policy_id'].isin(winner_ids))]
    min_n=int(fm['completed_n'].min()) if len(fm) else 0
    review_eligible=bool(months>=int(c['review_gate']['calendar_months_from_first_formal_fire_min']) and min_n>=int(c['review_gate']['formal_completed_per_winner_policy_min']))
    summary={
      'status':'PASS_RET120_EXIT_POLICY_QUALIFICATION_V0_1_UPDATE','generated_at':iso_now(),'as_of':as_of,
      'fire_events':int(len(events)),'formal_fire_events':int(events[pd.to_datetime(events['signal_date'])>=formal_start].shape[0]),
      'rehearsal_fire_events':int(events[pd.to_datetime(events['signal_date'])<formal_start].shape[0]),
      'formal_months_from_first_fire':int(months),'min_completed_across_winner_candidates':min_n,
      'review_eligible':review_eligible,'automatic_winner_selection':False,
      'formal_signal_start':c['formal_forward']['formal_signal_start'],
      'interpretation':["9/30 baseline/rehearsal is visible but excluded from formal verdict.",
                        "Structural invalidation is a CLOSE below the frozen adjusted stop, executed next tradable OPEN.",
                        "Loss of MA20 alone is not a V0.1 exit rule.",
                        "No winner is auto-selected even after review gate; compare pre-registered policies without post-result retuning."],
      'guardrails':c['guardrails']
    }
    rid=now_cst().strftime('%Y%m%dT%H%M%S+0800'); dest=Path(c['paths']['output_root'])/'updates'/as_of.replace('-','')/rid
    dest.mkdir(parents=True,exist_ok=False)
    out.to_csv(dest/'exit_policy_events.csv',index=False,encoding='utf-8-sig')
    met.to_csv(dest/'exit_policy_metrics.csv',index=False,encoding='utf-8-sig')
    (dest/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    # concise markdown
    lines=['# RET120 Exit Policy Qualification V0.1','',f'- As of: **{as_of}**',f'- Formal signal start: **{c["formal_forward"]["formal_signal_start"]}**',
           f'- Formal FIRE events: **{summary["formal_fire_events"]}**',f'- Rehearsal FIRE events: **{summary["rehearsal_fire_events"]}**',
           f'- Review eligible: **{review_eligible}**','',
           '## Structural invalidation','',
           'First post-entry adjusted CLOSE strictly below the frozen adjusted stop. Exit at the next tradable OPEN; suspension/missing open is deferred. Intraday wick alone does not invalidate.',
           '', '## Important', '',
           'MA20 loss alone is a warning, not a V0.1 exit. No policy is automatically selected as winner.', '',
           '## Files','','- exit_policy_events.csv','- exit_policy_metrics.csv','- summary.json']
    (dest/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2,default=str)); print('OUTPUT:',dest); return 0

def status(c):
    events=load_fire_rows(c); formal_start=pd.Timestamp(c['formal_forward']['formal_signal_start'])
    out={'status':'RET120_EXIT_POLICY_QUALIFICATION_V0_1_STATUS','fire_events':int(len(events)),
         'formal_fire_events':int((pd.to_datetime(events['signal_date'])>=formal_start).sum()) if not events.empty else 0,
         'rehearsal_fire_events':int((pd.to_datetime(events['signal_date'])<formal_start).sum()) if not events.empty else 0,
         'formal_signal_start':c['formal_forward']['formal_signal_start']}
    print(json.dumps(out,ensure_ascii=False,indent=2)); return 0

def main():
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest='command',required=True)
    sub.add_parser('check'); sub.add_parser('status'); p=sub.add_parser('update'); p.add_argument('--as-of',required=True)
    a=ap.parse_args(); c=C()
    try:
        if a.command=='check': rc=check(c)
        elif a.command=='status': rc=status(c)
        else: pd.Timestamp(a.as_of); rc=evaluate(c,a.as_of)
    except Exception as e:
        print(json.dumps({'status':'FAIL_CLOSED_RET120_EXIT_POLICY_QUALIFICATION_V0_1','error':f'{type(e).__name__}: {e}'},ensure_ascii=False,indent=2)); rc=3
    raise SystemExit(rc)
if __name__=='__main__': main()
