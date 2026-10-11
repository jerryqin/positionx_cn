from __future__ import annotations
import argparse, hashlib, json, re
from datetime import datetime
from pathlib import Path
import duckdb, pandas as pd, yaml
PROJECT_ROOT=Path(__file__).resolve().parents[1]
CFG=PROJECT_ROOT/'config/opportunity/sector_pit_asset_audit_v0_1.yaml'
NAME_KEYS=('sector','industry','classification','classify','board','member','constituent','shenwan','sw_','citic','csrc','gics','申万','中信','行业','板块','成分')
SYMBOL_KEYS=('symbol','stock_code','security_code','ticker','ts_code','wind_code','code')
DATE_KEYS=('trade_date','date','snapshot_date','asof_date','as_of_date','valid_from','effective_date','start_date','begin_date','entry_date','announcement_date','announce_date')
END_KEYS=('valid_to','end_date','expire_date','exit_date')

def sha256_file(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for c in iter(lambda:f.read(1024*1024),b''): h.update(c)
    return h.hexdigest()

def load_cfg(): return yaml.safe_load(CFG.read_text(encoding='utf-8'))
def norm(s): return re.sub(r'[^a-z0-9_\u4e00-\u9fff]+','_',str(s).lower())
def contains_name(s):
    t=norm(s); return any(k in t for k in NAME_KEYS)
def first_match(cols,keys):
    for c in cols:
        n=norm(c)
        if any(n==k or n.endswith('_'+k) or k in n for k in keys): return c
    return None

def classify(cols):
    sym=first_match(cols,SYMBOL_KEYS)
    cls=next((c for c in cols if contains_name(c)),None)
    dt=first_match(cols,DATE_KEYS)
    end=first_match(cols,END_KEYS)
    detail={'symbol_col':sym,'classification_col':cls,'date_col':dt,'end_col':end}
    if sym and cls and dt: return 'PIT_CANDIDATE',detail
    if sym and cls and not dt: return 'CURRENT_ONLY_PIT_UNSAFE',detail
    if cls and dt and not sym: return 'INCOMPLETE_NO_SYMBOL',detail
    return 'NOT_READY',detail

def score(name,cols):
    s=3 if contains_name(name) else 0
    if first_match(cols,SYMBOL_KEYS): s+=2
    if any(contains_name(c) for c in cols): s+=3
    if first_match(cols,DATE_KEYS): s+=3
    if first_match(cols,END_KEYS): s+=1
    return s

def qid(s): return '"'+str(s).replace('"','""')+'"'

def inspect_catalog(path):
    con=duckdb.connect(str(path),read_only=True)
    try:
        tabs=con.execute("""select table_schema,table_name,table_type from information_schema.tables where table_schema not in ('information_schema','pg_catalog') order by 1,2""").fetchdf()
        out=[]
        for r in tabs.itertuples(index=False):
            schema,table,typ=map(str,[r.table_schema,r.table_name,r.table_type])
            cdf=con.execute("select column_name from information_schema.columns where table_schema=? and table_name=? order by ordinal_position",[schema,table]).fetchdf()
            cols=[str(x) for x in cdf.column_name.tolist()]
            sc=score(f'{schema}.{table}',cols)
            if sc<3: continue
            status,detail=classify(cols)
            rec={'catalog':str(path),'schema':schema,'table':table,'table_type':typ,'candidate_score':sc,'pit_status':status,'columns':cols,**detail}
            if status in ('PIT_CANDIDATE','CURRENT_ONLY_PIT_UNSAFE','INCOMPLETE_NO_SYMBOL'):
                fq=f'{qid(schema)}.{qid(table)}'; stats={}
                try: stats['row_count']=int(con.execute(f'select count(*) from {fq}').fetchone()[0])
                except Exception as e: stats['row_count_error']=f'{type(e).__name__}: {e}'
                if detail['date_col']:
                    try:
                        a,b=con.execute(f'select min({qid(detail["date_col"])}),max({qid(detail["date_col"])}) from {fq}').fetchone(); stats['date_min']=str(a); stats['date_max']=str(b)
                    except Exception as e: stats['date_range_error']=f'{type(e).__name__}: {e}'
                if detail['symbol_col']:
                    try: stats['distinct_symbols']=int(con.execute(f'select count(distinct {qid(detail["symbol_col"])}) from {fq}').fetchone()[0])
                    except Exception: pass
                if detail['classification_col']:
                    try: stats['distinct_classifications']=int(con.execute(f'select count(distinct {qid(detail["classification_col"])}) from {fq}').fetchone()[0])
                    except Exception: pass
                rec['stats']=stats
            out.append(rec)
        return out
    finally: con.close()

def scan_files(roots,max_results=300):
    exts={'.parquet','.csv','.duckdb','.db','.json','.jsonl','.xlsx','.feather'}; out=[]
    for root in map(Path,roots):
        if not root.exists(): continue
        for p in root.rglob('*'):
            if len(out)>=max_results: break
            if p.is_file() and p.suffix.lower() in exts and contains_name(p.name):
                try: out.append({'path':str(p),'size_bytes':p.stat().st_size,'suffix':p.suffix.lower()})
                except OSError: pass
    return out

def inspect_panel(path):
    p=Path(path)
    if not p.exists(): return {'path':str(p),'exists':False}
    df=pd.read_parquet(p); cols=[str(c) for c in df.columns]; rel=[c for c in cols if contains_name(c)]
    return {'path':str(p),'exists':True,'sha256':sha256_file(p),'row_count':len(df),'sector_like_columns':rel}

def summarize(cands,panel,hits):
    pit=[x for x in cands if x['pit_status']=='PIT_CANDIDATE']
    unsafe=[x for x in cands if x['pit_status']=='CURRENT_ONLY_PIT_UNSAFE']
    if pit:
        verdict='PIT_SECTOR_ASSET_CANDIDATES_FOUND_REQUIRES_SOURCE_VALIDATION'
        next_action='Validate temporal semantics, lineage, coverage and reclassification history before sector research.'
    elif unsafe:
        verdict='ONLY_CURRENT_OR_UNSAFE_SECTOR_ASSETS_FOUND'
        next_action='Do not backfill current classifications. Acquire/build PIT sector membership.'
    else:
        verdict='NO_READY_PIT_SECTOR_ASSET_FOUND'
        next_action='Acquire/build PIT sector membership before Sector Ignition.'
    return {'verdict':verdict,'next_action':next_action,'pit_candidate_count':len(pit),'current_only_unsafe_count':len(unsafe),'filesystem_name_hits':len(hits),'pit_candidates':pit,'current_only_unsafe':unsafe,'known_pit_panel':panel}

def cmd_check(cfg):
    cats=[Path(x) for x in cfg['catalogs']]
    r={'status':'PASS_SECTOR_PIT_ASSET_AUDIT_V0_1_CHECK' if any(x.exists() for x in cats) else 'BLOCKED_SECTOR_PIT_ASSET_AUDIT_V0_1_CHECK','catalogs':[{'path':str(x),'exists':x.exists()} for x in cats],'known_pit_panel':cfg['known_pit_panel'],'read_only':True}
    print(json.dumps(r,ensure_ascii=False,indent=2)); return 0 if any(x.exists() for x in cats) else 3

def cmd_run(cfg):
    cands=[]
    for c in map(Path,cfg['catalogs']):
        if c.exists(): cands.extend(inspect_catalog(c))
    panel=inspect_panel(cfg['known_pit_panel']); hits=scan_files(cfg['filesystem_roots'],int(cfg.get('filesystem_max_results',300)))
    summ=summarize(cands,panel,hits); out=Path(cfg['output_dir']); out.mkdir(parents=True,exist_ok=True)
    detail=out/'sector_pit_asset_candidates_v0_1.json'; detail.write_text(json.dumps({'duckdb_candidates':cands,'filesystem_hits':hits},ensure_ascii=False,indent=2),encoding='utf-8')
    csv=out/'sector_pit_asset_candidates_v0_1.csv'
    pd.DataFrame([{k:x.get(k) for k in ('catalog','schema','table','candidate_score','pit_status','symbol_col','classification_col','date_col','end_col')}|{'row_count':(x.get('stats') or {}).get('row_count'),'date_min':(x.get('stats') or {}).get('date_min'),'date_max':(x.get('stats') or {}).get('date_max')} for x in cands]).to_csv(csv,index=False,encoding='utf-8-sig')
    r={'status':'PASS_SECTOR_PIT_ASSET_AUDIT_V0_1_RUN','generated_at':datetime.now().isoformat(timespec='seconds'),'research_id':cfg['research_id'],'summary':summ,'artifacts':{'candidate_json':str(detail),'candidate_json_sha256':sha256_file(detail),'candidate_csv':str(csv),'candidate_csv_sha256':sha256_file(csv)},'safety':{'read_only':True,'production_db':False,'broker':False,'qmt':False}}
    (out/'sector_pit_asset_audit_v0_1_summary.json').write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(r,ensure_ascii=False,indent=2)); print('OUTPUT:',out); return 0

def main():
    p=argparse.ArgumentParser(); p.add_argument('command',choices=['check','run']); a=p.parse_args(); cfg=load_cfg(); return cmd_check(cfg) if a.command=='check' else cmd_run(cfg)
if __name__=='__main__': raise SystemExit(main())
