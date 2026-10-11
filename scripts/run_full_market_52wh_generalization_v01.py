from __future__ import annotations
import argparse, hashlib, json, math
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT=Path(__file__).resolve().parents[1]
CFG=ROOT/'config/opportunity/full_market_52wh_generalization_v0_1.yaml'

def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding='utf-8'))

def sha256_file(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):
            h.update(b)
    return h.hexdigest()

def qpath(p):
    return str(Path(p)).replace('\\','/').replace("'","''")

def safe(v):
    if v is None or isinstance(v,(str,int,bool)): return v
    if isinstance(v,float): return v if math.isfinite(v) else None
    if isinstance(v,np.integer): return int(v)
    if isinstance(v,np.floating): return safe(float(v))
    if isinstance(v,pd.Timestamp): return v.strftime('%Y-%m-%d')
    if hasattr(v,'item'):
        try:return safe(v.item())
        except Exception:pass
    return str(v)

def json_write(path,obj):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    Path(path).write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=safe),encoding='utf-8')

def trim_mean_5(x):
    a=pd.Series(x).dropna().astype(float).sort_values().to_numpy()
    if len(a)==0:return np.nan
    k=int(math.floor(len(a)*0.05))
    if 2*k>=len(a):return float(np.mean(a))
    return float(np.mean(a[k:len(a)-k]))

def year_gate(n_positive,n_valid,min_positive):
    return int(n_valid)>0 and int(n_positive)>=min(int(min_positive),int(n_valid))

def bool_mask(series):
    """Null-safe boolean mask: SQL NULL / pandas NA is always False."""
    return pd.Series(series, index=getattr(series, "index", None)).fillna(False).astype(bool)

def decision_from_horizons(h60,h120,cfg):
    p60=bool(h60['horizon_pass']); p120=bool(h120['horizon_pass'])
    if p60 and p120:return 'STRONG_52WH_CANDIDATE'
    if p60 != p120:return 'PROMISING_HORIZON_CANDIDATE'
    both_trim_nonpos=(not h60['pos_adjusted_trim5_positive']) and (not h120['pos_adjusted_trim5_positive'])
    both_de_spread_nonpos=(not h60['pos_neg_date_equal_spread_positive']) and (not h120['pos_neg_date_equal_spread_positive'])
    if both_trim_nonpos or both_de_spread_nonpos:return 'FAST_REJECT'
    return 'INCONCLUSIVE'

def validate_inputs(cfg):
    u=Path(cfg['source']['full_market_universe'])
    c=Path(cfg['source']['market_catalog'])
    s=Path(cfg['source']['trading_status'])
    rows=[]
    for p,expected,label in [
        (u,cfg['source']['expected_universe_sha256'],'full_market_universe'),
        (s,cfg['source']['expected_trading_status_sha256'],'trading_status'),
    ]:
        actual=sha256_file(p) if p.exists() else None
        rows.append({'name':label,'path':str(p),'exists':p.exists(),'sha256':actual,
                     'expected_sha256':expected,'sha_match':actual==expected})
    rows.append({'name':'market_catalog','path':str(c),'exists':c.exists()})
    return rows, all(r['exists'] and r.get('sha_match',True) for r in rows)

def market_schema(cfg):
    import duckdb
    con=duckdb.connect(cfg['source']['market_catalog'],read_only=True)
    try: cols=[r[0] for r in con.execute(f"DESCRIBE {cfg['source']['market_table']}").fetchall()]
    finally: con.close()
    req=['symbol','trade_date','open','high','low','close','pre_close','suspend_flag']
    return cols,[c for c in req if c not in cols]

def status_schema(cfg):
    import duckdb
    p=qpath(cfg['source']['trading_status']); con=duckdb.connect()
    try: cols=[r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{p}')").fetchall()]
    finally: con.close()
    req=['trade_date','symbol','is_suspended','can_buy_at_open','st_status','st_known','new_listing_special']
    return cols,[c for c in req if c not in cols]

def build_calendar(cfg,outdir):
    import duckdb
    path=outdir/'calendar.parquet'
    if path.exists(): return path
    up=qpath(cfg['source']['full_market_universe']); dest=qpath(path); con=duckdb.connect()
    try:
        con.execute(f"""
        COPY (
          SELECT trade_date,row_number() OVER(ORDER BY trade_date)-1 AS session_idx
          FROM (SELECT DISTINCT CAST(trade_date AS DATE) trade_date FROM read_parquet('{up}'))
          ORDER BY trade_date
        ) TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
    finally: con.close()
    return path

def build_features(cfg,outdir):
    import duckdb
    path=outdir/'features_52wh_v0_1.parquet'
    if path.exists(): return path
    up=qpath(cfg['source']['full_market_universe']); dest=qpath(path)
    table=cfg['source']['market_table']; start=cfg['window']['feature_start']; end=cfg['window']['latest_market_session']
    con=duckdb.connect(cfg['source']['market_catalog'],read_only=True)
    try:
        con.execute('PRAGMA threads=8')
        con.execute(f"""
        COPY (
          WITH syms AS (SELECT DISTINCT symbol FROM read_parquet('{up}')),
          b AS (
            SELECT CAST(b.trade_date AS DATE) trade_date,b.symbol,
                   CAST(b.open AS DOUBLE) open_px,CAST(b.close AS DOUBLE) close_px,
                   CAST(b.pre_close AS DOUBLE) pre_close_px,CAST(b.suspend_flag AS INTEGER) suspend_flag,
                   CASE WHEN b.open>0 AND b.close>0 AND b.pre_close>0
                        THEN CAST(b.close AS DOUBLE)/CAST(b.pre_close AS DOUBLE) END daily_factor
            FROM {table} b JOIN syms s USING(symbol)
            WHERE CAST(b.trade_date AS DATE) BETWEEN CAST('{start}' AS DATE) AND CAST('{end}' AS DATE)
          ),
          a AS (
            SELECT *,CASE WHEN daily_factor>0 THEN
              exp(sum(ln(daily_factor)) OVER(PARTITION BY symbol ORDER BY trade_date
                  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)) END adj_close
            FROM b
          ),
          f AS (
            SELECT trade_date,symbol,open_px,close_px,pre_close_px,suspend_flag,adj_close,
                   CASE WHEN close_px>0 THEN adj_close*open_px/close_px END adj_open
            FROM a
          )
          SELECT *,
            count(adj_close) OVER(PARTITION BY symbol ORDER BY trade_date ROWS BETWEEN 250 PRECEDING AND 1 PRECEDING) prev_n_250,
            max(adj_close) OVER(PARTITION BY symbol ORDER BY trade_date ROWS BETWEEN 250 PRECEDING AND 1 PRECEDING) prev_max_250,
            min(adj_close) OVER(PARTITION BY symbol ORDER BY trade_date ROWS BETWEEN 250 PRECEDING AND 1 PRECEDING) prev_min_250
          FROM f ORDER BY symbol,trade_date
        ) TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
    finally: con.close()
    return path

def build_candidates(cfg,outdir,features,calendar):
    import duckdb
    path=outdir/'candidate_events.parquet'
    if path.exists():return path
    u=qpath(cfg['source']['full_market_universe']); f=qpath(features); cal=qpath(calendar)
    st=qpath(cfg['source']['trading_status']); dest=qpath(path)
    start=cfg['window']['signal_start']; cut=cfg['window']['signal_cutoff_h120']
    con=duckdb.connect()
    try:
        con.execute('PRAGMA threads=8')
        con.execute(f"""
        COPY (
          WITH sig0 AS (
            SELECT x.symbol,x.trade_date signal_date,x.adj_close,x.prev_max_250,x.prev_min_250,
              CASE WHEN x.adj_close>x.prev_max_250 THEN 'POS'
                   WHEN x.adj_close<x.prev_min_250 THEN 'NEG' END grp
            FROM read_parquet('{f}') x
            JOIN read_parquet('{u}') u ON u.symbol=x.symbol AND CAST(u.trade_date AS DATE)=x.trade_date
            WHERE x.trade_date BETWEEN CAST('{start}' AS DATE) AND CAST('{cut}' AS DATE)
              AND x.prev_n_250=250
              AND (x.adj_close>x.prev_max_250 OR x.adj_close<x.prev_min_250)
          ),
          dated AS (
            SELECT s.*,c0.session_idx,c1.trade_date entry_date,c20.trade_date exit_date_20,
                   c60.trade_date exit_date_60,c120.trade_date exit_date_120
            FROM sig0 s JOIN read_parquet('{cal}') c0 ON c0.trade_date=s.signal_date
            LEFT JOIN read_parquet('{cal}') c1 ON c1.session_idx=c0.session_idx+1
            LEFT JOIN read_parquet('{cal}') c20 ON c20.session_idx=c0.session_idx+20
            LEFT JOIN read_parquet('{cal}') c60 ON c60.session_idx=c0.session_idx+60
            LEFT JOIN read_parquet('{cal}') c120 ON c120.session_idx=c0.session_idx+120
          ),
          hs AS (
            SELECT d.*,ss.st_known signal_st_known,ss.st_status signal_st_status,
                   ss.new_listing_special signal_new_listing_special,
                   es.is_suspended entry_is_suspended,es.can_buy_at_open,
                   ef.adj_open entry_adj_open,f20.adj_close exit_adj_close_20,
                   f60.adj_close exit_adj_close_60,f120.adj_close exit_adj_close_120
            FROM dated d
            LEFT JOIN read_parquet('{st}') ss ON ss.symbol=d.symbol AND CAST(ss.trade_date AS DATE)=d.signal_date
            LEFT JOIN read_parquet('{st}') es ON es.symbol=d.symbol AND CAST(es.trade_date AS DATE)=d.entry_date
            LEFT JOIN read_parquet('{f}') ef ON ef.symbol=d.symbol AND ef.trade_date=d.entry_date
            LEFT JOIN read_parquet('{f}') f20 ON f20.symbol=d.symbol AND f20.trade_date=d.exit_date_20
            LEFT JOIN read_parquet('{f}') f60 ON f60.symbol=d.symbol AND f60.trade_date=d.exit_date_60
            LEFT JOIN read_parquet('{f}') f120 ON f120.symbol=d.symbol AND f120.trade_date=d.exit_date_120
          )
          SELECT *,
            CASE WHEN signal_st_known AND signal_st_status IN ('ST','STAR_ST') THEN 'SIGNAL_KNOWN_ST'
                 WHEN signal_new_listing_special THEN 'SIGNAL_NEW_LISTING_SPECIAL'
                 WHEN entry_is_suspended THEN 'ENTRY_SUSPENDED'
                 WHEN can_buy_at_open IS DISTINCT FROM TRUE THEN 'ENTRY_NOT_BUYABLE_AT_OPEN'
                 WHEN entry_adj_open IS NULL OR entry_adj_open<=0 THEN 'NO_EXACT_ENTRY_PRICE' END exclude_reason,
            CASE WHEN entry_adj_open>0 AND exit_adj_close_20>0 THEN exit_adj_close_20/entry_adj_open-1 END raw_return_20,
            CASE WHEN entry_adj_open>0 AND exit_adj_close_60>0 THEN exit_adj_close_60/entry_adj_open-1 END raw_return_60,
            CASE WHEN entry_adj_open>0 AND exit_adj_close_120>0 THEN exit_adj_close_120/entry_adj_open-1 END raw_return_120,
            COALESCE(
              (entry_adj_open>0 AND exit_adj_close_20>0 AND exit_adj_close_60>0 AND exit_adj_close_120>0),
              FALSE
            ) complete_h120_cohort
          FROM hs ORDER BY signal_date,symbol
        ) TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
    finally: con.close()
    return path

def build_benchmarks(cfg,outdir,features,candidates):
    import duckdb
    path=outdir/'market_benchmarks.parquet'
    if path.exists():return path
    u=qpath(cfg['source']['full_market_universe']); f=qpath(features); c=qpath(candidates); dest=qpath(path)
    con=duckdb.connect()
    try:
        con.execute('PRAGMA threads=8')
        con.execute(f"""
        COPY (
          WITH dates AS (
            SELECT DISTINCT signal_date,entry_date,exit_date_20,exit_date_60,exit_date_120
            FROM read_parquet('{c}') WHERE exclude_reason IS NULL AND complete_h120_cohort
          ),
          m AS (
            SELECT d.signal_date,u.symbol,e.adj_open,x20.adj_close c20,x60.adj_close c60,x120.adj_close c120
            FROM dates d JOIN read_parquet('{u}') u ON CAST(u.trade_date AS DATE)=d.signal_date
            LEFT JOIN read_parquet('{f}') e ON e.symbol=u.symbol AND e.trade_date=d.entry_date
            LEFT JOIN read_parquet('{f}') x20 ON x20.symbol=u.symbol AND x20.trade_date=d.exit_date_20
            LEFT JOIN read_parquet('{f}') x60 ON x60.symbol=u.symbol AND x60.trade_date=d.exit_date_60
            LEFT JOIN read_parquet('{f}') x120 ON x120.symbol=u.symbol AND x120.trade_date=d.exit_date_120
          ),
          long AS (
            SELECT signal_date,20 horizon,CASE WHEN adj_open>0 AND c20>0 THEN c20/adj_open-1 END ret FROM m
            UNION ALL SELECT signal_date,60,CASE WHEN adj_open>0 AND c60>0 THEN c60/adj_open-1 END FROM m
            UNION ALL SELECT signal_date,120,CASE WHEN adj_open>0 AND c120>0 THEN c120/adj_open-1 END FROM m
          )
          SELECT signal_date,horizon,median(ret) FILTER(WHERE ret IS NOT NULL) market_median_return,
                 count(ret) FILTER(WHERE ret IS NOT NULL) market_n
          FROM long GROUP BY signal_date,horizon ORDER BY signal_date,horizon
        ) TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
    finally: con.close()
    return path

def build_event_returns(outdir,candidates,benchmarks):
    import duckdb
    path=outdir/'event_returns.parquet'
    if path.exists():return path
    c=qpath(candidates); b=qpath(benchmarks); dest=qpath(path); con=duckdb.connect()
    try:
        con.execute(f"""
        COPY (
          WITH ev AS (
            SELECT symbol,signal_date,grp,entry_date,20 horizon,exit_date_20 exit_date,raw_return_20 raw_return,exclude_reason,complete_h120_cohort FROM read_parquet('{c}')
            UNION ALL SELECT symbol,signal_date,grp,entry_date,60,exit_date_60,raw_return_60,exclude_reason,complete_h120_cohort FROM read_parquet('{c}')
            UNION ALL SELECT symbol,signal_date,grp,entry_date,120,exit_date_120,raw_return_120,exclude_reason,complete_h120_cohort FROM read_parquet('{c}')
          )
          SELECT e.*,b.market_median_return,b.market_n,e.raw_return-b.market_median_return adjusted_return,
            CASE WHEN e.exclude_reason IS NOT NULL THEN e.exclude_reason
                 WHEN COALESCE(e.complete_h120_cohort,FALSE)=FALSE THEN 'INCOMPLETE_H120_COHORT'
                 WHEN e.raw_return IS NULL THEN 'NO_EXACT_EXIT_BAR'
                 WHEN b.market_median_return IS NULL THEN 'NO_MARKET_BENCHMARK'
                 ELSE 'OK' END measurement_status
          FROM ev e LEFT JOIN read_parquet('{b}') b USING(signal_date,horizon)
          ORDER BY signal_date,symbol,horizon
        ) TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
    finally:con.close()
    return path

def summarize_horizon(er,h,cfg):
    x=er[(er['horizon']==h)&(er['measurement_status']=='OK')].copy()
    pos=x[x['grp']=='POS'].copy(); neg=x[x['grp']=='NEG'].copy()
    pmean=float(pos['adjusted_return'].mean()) if len(pos) else np.nan
    nmean=float(neg['adjusted_return'].mean()) if len(neg) else np.nan
    ptrim=trim_mean_5(pos['adjusted_return'])
    de=x.groupby(['signal_date','grp'],as_index=False)['adjusted_return'].mean()
    pv=de[de['grp']=='POS'].set_index('signal_date')['adjusted_return']
    nv=de[de['grp']=='NEG'].set_index('signal_date')['adjusted_return']
    pde=float(pv.mean()) if len(pv) else np.nan
    common=pv.to_frame('p').join(nv.to_frame('n'),how='inner')
    de_spread=float((common['p']-common['n']).mean()) if len(common) else np.nan
    spread=pmean-nmean if math.isfinite(pmean) and math.isfinite(nmean) else np.nan
    x['year']=pd.to_datetime(x['signal_date']).dt.year
    yg=x.groupby(['year','grp'])['adjusted_return'].mean().unstack()
    years=[]
    for y,row in yg.iterrows():
        p=row.get('POS',np.nan); n=row.get('NEG',np.nan)
        years.append({'year':int(y),'pos_adjusted_mean':safe(p),'neg_adjusted_mean':safe(n),
                      'spread':safe(p-n) if pd.notna(p) and pd.notna(n) else None})
    valid_pos=sum(r['pos_adjusted_mean'] is not None for r in years)
    pos_years=sum(r['pos_adjusted_mean']>0 for r in years if r['pos_adjusted_mean'] is not None)
    valid_spread=sum(r['spread'] is not None for r in years)
    spread_years=sum(r['spread']>0 for r in years if r['spread'] is not None)
    g=cfg['gate']; min_n=min(len(pos),len(neg))
    out={
      'pos_adjusted_mean':safe(pmean),'neg_adjusted_mean':safe(nmean),'pos_adjusted_trim5_mean':safe(ptrim),
      'pos_date_equal_adjusted_mean':safe(pde),'pos_neg_adjusted_mean_spread':safe(spread),
      'pos_neg_date_equal_spread':safe(de_spread),'pos_adjusted_mean_positive':bool(math.isfinite(pmean) and pmean>0),
      'pos_adjusted_trim5_positive':bool(math.isfinite(ptrim) and ptrim>0),'pos_date_equal_adjusted_mean_positive':bool(math.isfinite(pde) and pde>0),
      'pos_neg_adjusted_mean_spread_positive':bool(math.isfinite(spread) and spread>0),
      'pos_neg_date_equal_spread_positive':bool(math.isfinite(de_spread) and de_spread>0),
      'min_group_n':int(min_n),'min_group_n_pass':int(min_n)>=int(g['min_group_n']),
      'pos_positive_years':int(pos_years),'valid_pos_years':int(valid_pos),
      'pos_year_gate':year_gate(pos_years,valid_pos,g['positive_years_min']),
      'spread_positive_years':int(spread_years),'valid_spread_years':int(valid_spread),
      'spread_year_gate':year_gate(spread_years,valid_spread,g['spread_positive_years_min']),'by_year':years,
    }
    out['horizon_pass']=all([out['pos_adjusted_mean_positive'],out['pos_adjusted_trim5_positive'],
        out['pos_date_equal_adjusted_mean_positive'],out['pos_neg_adjusted_mean_spread_positive'],
        out['pos_neg_date_equal_spread_positive'],out['min_group_n_pass'],out['pos_year_gate'],out['spread_year_gate']])
    return out

def check(cfg):
    inputs,ok=validate_inputs(cfg); _,mm=market_schema(cfg); _,sm=status_schema(cfg); ok=ok and not mm and not sm
    out={'status':'PASS_FULL_MARKET_52WH_GENERALIZATION_V0_1_1_CHECK' if ok else 'BLOCKED_FULL_MARKET_52WH_GENERALIZATION_V0_1_CHECK',
         'inputs':inputs,'market_missing_columns':mm,'status_missing_columns':sm,'definition':cfg['definition'],
         'window':cfg['window'],'gate':cfg['gate'],'only_intended_change':cfg['safety']['only_intended_change']}
    print(json.dumps(out,ensure_ascii=False,indent=2)); return 0 if ok else 3

def run(cfg,force=False):
    inputs,ok=validate_inputs(cfg)
    if not ok:raise RuntimeError('input hash/existence check failed')
    _,mm=market_schema(cfg);_,sm=status_schema(cfg)
    if mm or sm:raise RuntimeError(f'schema missing market={mm} status={sm}')
    outdir=Path(cfg['output_dir']);outdir.mkdir(parents=True,exist_ok=True)
    if force:
        for n in ['features_52wh_v0_1.parquet','calendar.parquet','candidate_events.parquet','market_benchmarks.parquet','event_returns.parquet']:
            p=outdir/n
            if p.exists():p.unlink()
    cal=build_calendar(cfg,outdir); print('[1/5] calendar',cal)
    feat=build_features(cfg,outdir); print('[2/5] features',feat)
    cand=build_candidates(cfg,outdir,feat,cal); print('[3/5] candidates',cand)
    bench=build_benchmarks(cfg,outdir,feat,cand); print('[4/5] benchmarks',bench)
    erp=build_event_returns(outdir,cand,bench); print('[5/5] event returns',erp)
    canddf=pd.read_parquet(cand); er=pd.read_parquet(erp)
    eligible=canddf[canddf['exclude_reason'].isna()]
    complete=eligible[bool_mask(eligible['complete_h120_cohort'])]
    h20=summarize_horizon(er,20,cfg);h60=summarize_horizon(er,60,cfg);h120=summarize_horizon(er,120,cfg)
    verdict=decision_from_horizons(h60,h120,cfg)
    gs=canddf.groupby('signal_date').size().sort_values(ascending=False); raw=len(canddf)
    result={
      'status':'PASS_FULL_MARKET_52WH_GENERALIZATION_V0_1_1_RUN','generated_at':datetime.now().isoformat(timespec='seconds'),
      'research_id':cfg['research_id'],'contract':{'only_intended_change':cfg['safety']['only_intended_change'],
       'signal_definition_changed':False,'entry_timing_changed':False,'horizons_changed':False,'gate_logic_changed':False,'parameter_scan':False,
       'hotfix_v0_1_1':'NULL complete_h120_cohort is treated as FALSE in SQL and pandas; cached event_returns rebuilt only'},
      'full_market_universe':{'path':cfg['source']['full_market_universe'],'sha256':sha256_file(cfg['source']['full_market_universe'])},
      'frozen_csi500_reference':cfg['frozen_csi500_reference'],'definition':cfg['definition'],'window':cfg['window'],
      'event_audit':{'raw_events':raw,'raw_pos':int((canddf['grp']=='POS').sum()),'raw_neg':int((canddf['grp']=='NEG').sum()),
       'excluded':{str(k):int(v) for k,v in canddf['exclude_reason'].dropna().value_counts().items()},
       'eligible_events':int(len(eligible)),'eligible_pos':int((eligible['grp']=='POS').sum()),'eligible_neg':int((eligible['grp']=='NEG').sum()),
       'complete_h120_events':int(len(complete))},
      'measurement_status_counts':{str(k):int(v) for k,v in er['measurement_status'].value_counts().items()},
      'market_adjustment':cfg['market_adjustment'],
      'clustering':{'signal_dates':int(canddf['signal_date'].nunique()),'events_per_date_mean':safe(gs.mean()),
       'events_per_date_p95':safe(gs.quantile(.95)),'events_per_date_max':int(gs.max()) if raw else 0,
       'top_5_signal_dates_event_share':safe(gs.head(5).sum()/raw) if raw else None,
       'top_10_signal_dates':[{'signal_date':safe(k),'events':int(v)} for k,v in gs.head(10).items()]},
      'decision':{'verdict':verdict,'h20':h20,'h60':h60,'h120':h120,'h20_role':'reaction diagnostic only','rule':cfg['gate']},
      'comparison':{'old_csi500_verdict':cfg['frozen_csi500_reference']['verdict'],'new_full_market_verdict':verdict,
       'interpretation':'Universe-transfer evidence only. Full-market PASS advances to frozen H120 standalone portfolio generalization; it does not qualify a Strategy.'},
      'artifacts':{'candidate_events':str(cand),'candidate_events_sha256':sha256_file(cand),'event_returns':str(erp),
       'event_returns_sha256':sha256_file(erp),'market_benchmarks':str(bench),'market_benchmarks_sha256':sha256_file(bench)},
      'safety':cfg['safety']}
    sp=outdir/'full_market_52wh_generalization_v0_1_summary.json';json_write(sp,result)
    print(json.dumps(result,ensure_ascii=False,indent=2,default=safe));print('OUTPUT:',outdir);return 0

def main():
    ap=argparse.ArgumentParser();ap.add_argument('command',choices=['check','run']);ap.add_argument('--force',action='store_true')
    a=ap.parse_args();cfg=load_cfg()
    try:rc=check(cfg) if a.command=='check' else run(cfg,a.force)
    except Exception as e:
        print(json.dumps({'status':'FAIL_CLOSED_FULL_MARKET_52WH_GENERALIZATION_V0_1_1','error':f'{type(e).__name__}: {e}'},ensure_ascii=False,indent=2));rc=3
    raise SystemExit(rc)
if __name__=='__main__':main()
