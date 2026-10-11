
from __future__ import annotations
import argparse, json, os, re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
import pandas as pd, yaml

ROOT=Path(__file__).resolve().parents[1]
CFG=ROOT/'config/data/positionx_local_data_inventory_v0_1.yaml'

def load_cfg(): return yaml.safe_load(CFG.read_text(encoding='utf-8'))
def norm(x): return str(x or '').lower().replace('\\','/')
def family(p):
    s=norm(p)
    if 'forecast_semantic' in s or 'cninfo_earnings' in s or 'earnings' in s: return 'EARNINGS_FORECAST_FOCUSED'
    if 'cninfo' in s: return 'CNINFO_OTHER_OR_UNKNOWN'
    if 'announcement' in s or 'event' in s: return 'EVENT_OR_ANNOUNCEMENT'
    return 'OTHER'

def text_cols(cols):
    hits=[]
    for c in cols:
        s=str(c).lower()
        if any(k in s for k in ['title','keyword','announcement','event_class','event_kind','adjunct','pdf_url','source_url','source_ref']): hits.append(str(c))
    return hits

def kw_counts(series,cfg):
    s=series.astype('string').fillna('')
    out={}
    for grp,words in cfg['keywords'].items():
        m=pd.Series(False,index=s.index)
        for w in words: m |= s.str.contains(re.escape(w),case=False,regex=True,na=False)
        out[grp]=int(m.sum())
    return out

def inspect_df(df,cfg):
    cols=text_cols(df.columns)
    if not cols: return None
    joined=df[cols].astype('string').fillna('').agg(' | '.join,axis=1)
    return {'rows_sampled':int(len(df)),'candidate_text_columns':cols,'keyword_hits':kw_counts(joined,cfg)}

def inspect_duckdb(p,cfg):
    import duckdb
    out=[]; con=duckdb.connect(str(p),read_only=True)
    try:
        tabs=con.execute("select table_schema,table_name from information_schema.tables where table_type='BASE TABLE' order by 1,2").fetchall()
        for sch,tab in tabs:
            q='"'+sch.replace('"','""')+'"."'+tab.replace('"','""')+'"'
            try:
                d=con.execute('describe '+q).fetchdf(); cols=d['column_name'].astype(str).tolist(); tcols=text_cols(cols)
                if not tcols: continue
                n=int(con.execute('select count(*) from '+q).fetchone()[0]); lim=min(n,int(cfg['limits']['sample_rows']))
                qs=', '.join('"'+c.replace('"','""')+'"' for c in tcols)
                df=con.execute(f'select {qs} from {q} limit {lim}').fetchdf(); info=inspect_df(df,cfg)
                out.append({'source_type':'DUCKDB_TABLE','path':str(p),'table':f'{sch}.{tab}','row_count':n,**(info or {})})
            except Exception as e: out.append({'source_type':'DUCKDB_TABLE_ERROR','path':str(p),'table':f'{sch}.{tab}','error':f'{type(e).__name__}: {e}'})
    finally: con.close()
    return out

def inspect_parquet(p,cfg):
    import duckdb
    con=duckdb.connect()
    try:
        qp=str(p).replace('\\','/').replace("'","''")
        d=con.execute(f"describe select * from read_parquet('{qp}')").fetchdf(); cols=d['column_name'].astype(str).tolist(); tcols=text_cols(cols)
        if not tcols: return None
        qs=', '.join('"'+c.replace('"','""')+'"' for c in tcols)
        df=con.execute(f"select {qs} from read_parquet('{qp}') limit {int(cfg['limits']['sample_rows'])}").fetchdf(); info=inspect_df(df,cfg)
        return {'source_type':'PARQUET','path':str(p),**(info or {})}
    finally: con.close()

def inspect_csv(p,cfg):
    try:
        df=pd.read_csv(p,nrows=int(cfg['limits']['sample_rows']),low_memory=False); info=inspect_df(df,cfg)
        return {'source_type':'CSV','path':str(p),**info} if info else None
    except Exception as e: return {'source_type':'CSV_ERROR','path':str(p),'error':f'{type(e).__name__}: {e}'}

def run(cfg):
    roots=[Path(x) for x in cfg['roots']]; files=[]; pdf=defaultdict(lambda:{'count':0,'bytes':0,'examples':[]}); ext=Counter(); fam=Counter()
    for r in roots:
        if not r.exists(): continue
        for base,dirs,names in os.walk(r):
            dirs[:]=[d for d in dirs if d.lower() not in {'.git','__pycache__','.venv','venv'}]
            for name in names:
                p=Path(base)/name
                try: st=p.stat()
                except: continue
                files.append((p,st)); ext[p.suffix.lower()]+=1; fam[family(p)]+=1
                if p.suffix.lower()=='.pdf':
                    z=pdf[str(p.parent)]; z['count']+=1; z['bytes']+=int(st.st_size)
                    if len(z['examples'])<5:z['examples'].append(p.name)
    assets=[]
    for p,st in files:
        e=p.suffix.lower(); s=norm(p)
        if not any(t in s for t in ['/events/','/research/','/incoming/','/catalog/','cninfo','announcement','event']): continue
        try:
            if e=='.duckdb': assets.extend(inspect_duckdb(p,cfg))
            elif e=='.parquet' and st.st_size<=int(cfg['limits']['max_structured_file_bytes']):
                z=inspect_parquet(p,cfg); assets += [z] if z else []
            elif e=='.csv' and st.st_size<=int(cfg['limits']['max_structured_file_bytes']):
                z=inspect_csv(p,cfg); assets += [z] if z else []
        except Exception as ex: assets.append({'source_type':'INSPECT_ERROR','path':str(p),'error':f'{type(ex).__name__}: {ex}'})
    def score(a):
        h=a.get('keyword_hits',{}) or {}; return 10*h.get('HARD_CONFIRMATION',0)+5*h.get('SOFT_INTENT',0)-h.get('EARNINGS_FORECAST',0)
    assets.sort(key=score,reverse=True)
    cands=[a for a in assets if (a.get('keyword_hits',{}) or {}).get('HARD_CONFIRMATION',0)>0 or (a.get('keyword_hits',{}) or {}).get('SOFT_INTENT',0)>0]
    out=Path(cfg['output_dir']); out.mkdir(parents=True,exist_ok=True)
    rows=[]
    for a in assets:
        h=a.get('keyword_hits',{}) or {}; rows.append({'source_type':a.get('source_type'),'path':a.get('path'),'table':a.get('table'),'row_count':a.get('row_count'),'rows_sampled':a.get('rows_sampled'),'hard_hits':h.get('HARD_CONFIRMATION',0),'soft_hits':h.get('SOFT_INTENT',0),'earnings_hits':h.get('EARNINGS_FORECAST',0),'candidate_text_columns':';'.join(a.get('candidate_text_columns',[]) or []),'error':a.get('error')})
    pd.DataFrame(rows).to_csv(out/'structured_asset_inventory_v0_1.csv',index=False,encoding='utf-8-sig')
    pdfs=sorted([{'directory':k,**v} for k,v in pdf.items()],key=lambda x:x['count'],reverse=True)
    (out/'pdf_directory_inventory_v0_1.json').write_text(json.dumps(pdfs,ensure_ascii=False,indent=2),encoding='utf-8')
    summary={'status':'PASS_POSITIONX_LOCAL_DATA_INVENTORY_V0_1_RUN','generated_at':datetime.now().isoformat(timespec='seconds'),'inventory_id':cfg['inventory_id'],'file_extension_counts':dict(ext),'path_family_counts':dict(fam),'pdf_total_count':sum(x['count'] for x in pdfs),'pdf_total_bytes':sum(x['bytes'] for x in pdfs),'pdf_top_directories':pdfs[:30],'structured_assets_inspected':len(assets),'commercial_catalyst_candidate_assets':len(cands),'top_candidate_assets':cands[:50],'interpretation':{'local_commercial_catalyst_data_confirmed_present':bool(cands),'next_action_if_present':'Reuse highest-hit local structured assets and their linked PDFs before any new acquisition.','next_action_if_absent':'Build direct CNINFO/SSE/SZSE/BSE acquisition before Eva.','data_priority':['LOCAL_ASSETS','DIRECT_FIRST_PARTY','FREE_PUBLIC_THIRD_PARTY','EVA','PAID_PROFESSIONAL']},'artifacts':{'structured_asset_inventory_csv':str(out/'structured_asset_inventory_v0_1.csv'),'pdf_directory_inventory_json':str(out/'pdf_directory_inventory_v0_1.json')},'safety':cfg['safety']}
    (out/'positionx_local_data_inventory_v0_1_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2)); print('OUTPUT:',out); return 0

def check(cfg):
    roots=[{'path':r,'exists':Path(r).exists()} for r in cfg['roots']]; ok=any(x['exists'] for x in roots); print(json.dumps({'status':'PASS_POSITIONX_LOCAL_DATA_INVENTORY_V0_1_CHECK' if ok else 'BLOCKED_POSITIONX_LOCAL_DATA_INVENTORY_V0_1_CHECK','roots':roots,'read_only':True,'network':False,'eva_dependency':False},ensure_ascii=False,indent=2)); return 0 if ok else 3

def main():
    p=argparse.ArgumentParser(); p.add_argument('command',choices=['check','run']); a=p.parse_args(); cfg=load_cfg()
    try: raise SystemExit(check(cfg) if a.command=='check' else run(cfg))
    except Exception as e:
        print(json.dumps({'status':'FAIL_CLOSED_POSITIONX_LOCAL_DATA_INVENTORY_V0_1','error':f'{type(e).__name__}: {e}'},ensure_ascii=False,indent=2)); raise SystemExit(3)
if __name__=='__main__': main()
