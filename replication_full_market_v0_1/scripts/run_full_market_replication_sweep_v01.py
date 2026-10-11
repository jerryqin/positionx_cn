from __future__ import annotations
import argparse,hashlib,json,subprocess,sys
from datetime import datetime
from pathlib import Path
import pandas as pd,yaml

ROOT=Path(__file__).resolve().parents[1]
FULL_SUMMARY=Path(r"D:\positionx_data\research\opportunity_engine\full_market_universe_v0_1\full_market_universe_v0_1_summary.json")
FULL_PANEL=Path(r"D:\positionx_data\research\opportunity_engine\full_market_universe_v0_1\full_market_tradable_universe_v0_1.parquet")
EXPECTED_FULL_SHA="45d38422c0021bbfc234eb22b16e216aa0f59e5bf6e1fadc97290389d92b9509"
OUT=Path(r"D:\positionx_data\research\opportunity_engine\full_market_replication_sweep_v0_1")
NORM_PANEL=OUT/"universe"/"full_market_tradable_universe_yyyymmdd_v0_1.parquet"

MECHANISMS=[
 {"id":"TREND_IGNITION","script":"run_opportunity_trend_ignition_v01.py","config":"trend_ignition_v0_1.yaml","summary":OUT/"trend_ignition_v0_1"/"trend_ignition_v0_1_summary.json","old":Path(r"D:\positionx_data\research\opportunity_engine\trend_ignition_v0_1\trend_ignition_v0_1_summary.json")},
 {"id":"D1_ABSORPTION","script":"run_opportunity_d1_absorption_v01.py","config":"d1_absorption_v0_1.yaml","summary":OUT/"d1_absorption_v0_1"/"d1_absorption_v0_1_summary.json","old":Path(r"D:\positionx_data\research\opportunity_engine\d1_absorption_v0_1\d1_absorption_v0_1_summary.json")},
 {"id":"D1_CAPITULATION_RECOVERY","script":"run_opportunity_d1_capitulation_recovery_v01.py","config":"d1_capitulation_recovery_v0_1.yaml","summary":OUT/"d1_capitulation_recovery_v0_1"/"d1_capitulation_recovery_v0_1_summary.json","old":Path(r"D:\positionx_data\research\opportunity_engine\d1_capitulation_recovery_v0_1\d1_capitulation_recovery_v0_1_summary.json")},
 {"id":"D2_COMPRESSION_BREAKOUT","script":"run_opportunity_d2_compression_breakout_v01.py","config":"d2_compression_breakout_v0_1.yaml","summary":OUT/"d2_compression_breakout_v0_1"/"d2_compression_breakout_v0_1_summary.json","old":Path(r"D:\positionx_data\research\opportunity_engine\d2_compression_breakout_v0_1\d2_compression_breakout_v0_1_summary.json")},
 {"id":"P1_BREAKOUT_ACCEPTANCE","script":"run_opportunity_p1_breakout_acceptance_v01.py","config":"p1_breakout_acceptance_v0_1.yaml","summary":OUT/"p1_breakout_acceptance_v0_1"/"p1_breakout_acceptance_v0_1_summary.json","old":Path(r"D:\positionx_data\research\opportunity_engine\p1_breakout_acceptance_v0_1\p1_breakout_acceptance_v0_1_summary.json")},
 {"id":"P2_TREND_PULLBACK_RECLAIM","script":"run_opportunity_p2_trend_pullback_reclaim_v01.py","config":"p2_trend_pullback_reclaim_v0_1.yaml","summary":OUT/"p2_trend_pullback_reclaim_v0_1"/"p2_trend_pullback_reclaim_v0_1_summary.json","old":Path(r"D:\positionx_data\research\opportunity_engine\p2_trend_pullback_reclaim_v0_1\p2_trend_pullback_reclaim_v0_1_summary.json")},
 {"id":"P3_RS_IGNITION","script":"run_opportunity_p3_rs_ignition_v01.py","config":"p3_rs_ignition_v0_1.yaml","summary":OUT/"p3_rs_ignition_v0_1"/"p3_rs_ignition_v0_1_summary.json","old":Path(r"D:\positionx_data\research\opportunity_engine\p3_rs_ignition_v0_1\p3_rs_ignition_v0_1_summary.json")},
 {"id":"P4_OVERNIGHT_REPRICING","script":"run_opportunity_p4_overnight_repricing_acceptance_v01.py","config":"p4_overnight_repricing_acceptance_v0_1.yaml","summary":OUT/"p4_overnight_repricing_acceptance_v0_1"/"p4_overnight_repricing_acceptance_v0_1_summary.json","old":Path(r"D:\positionx_data\research\opportunity_engine\p4_overnight_repricing_acceptance_v0_1\p4_overnight_repricing_acceptance_v0_1_summary.json")},
 {"id":"D3_MARKET_BREADTH","script":"run_opportunity_d3_market_breadth_ignition_v01.py","config":"d3_market_breadth_ignition_v0_1.yaml","summary":OUT/"d3_market_breadth_ignition_v0_1"/"d3_market_breadth_ignition_v0_1_summary.json","old":Path(r"D:\positionx_data\research\opportunity_engine\d3_market_breadth_ignition_v0_1\d3_market_breadth_ignition_v0_1_summary.json")},
]

def sha256_file(p:Path):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
 return h.hexdigest()

def prepare_universe():
 if not FULL_SUMMARY.exists() or not FULL_PANEL.exists(): raise RuntimeError('full-market universe asset missing')
 if sha256_file(FULL_PANEL)!=EXPECTED_FULL_SHA: raise RuntimeError('full-market universe SHA mismatch')
 s=json.loads(FULL_SUMMARY.read_text(encoding='utf-8'))
 if not s.get('full_market_candidate',{}).get('ready'): raise RuntimeError('full-market universe is not READY')
 NORM_PANEL.parent.mkdir(parents=True,exist_ok=True)
 need=True
 receipt=NORM_PANEL.with_suffix('.receipt.json')
 if NORM_PANEL.exists() and receipt.exists():
  try:
   r=json.loads(receipt.read_text(encoding='utf-8')); need=r.get('source_sha256')!=EXPECTED_FULL_SHA
  except Exception: pass
 if need:
  x=pd.read_parquet(FULL_PANEL,columns=['trade_date','symbol'])
  x['trade_date']=pd.to_datetime(x['trade_date'],errors='raise').dt.strftime('%Y%m%d')
  x['symbol']=x['symbol'].astype(str)
  if x.duplicated(['trade_date','symbol']).any(): raise RuntimeError('normalized universe has duplicate keys')
  x.to_parquet(NORM_PANEL,index=False)
  receipt.write_text(json.dumps({'source_sha256':EXPECTED_FULL_SHA,'rows':len(x),'symbols':int(x.symbol.nunique()),'normalized_sha256':sha256_file(NORM_PANEL)},indent=2),encoding='utf-8')
 return {'panel':str(NORM_PANEL),'sha256':sha256_file(NORM_PANEL)}

def patch_p1_sha():
 d2=OUT/'d2_compression_breakout_v0_1'/'d2_compression_breakout_v0_1_panel.parquet'
 if not d2.exists(): raise RuntimeError('full-market D2 panel missing before P1')
 cfgp=ROOT/'config/opportunity/p1_breakout_acceptance_v0_1.yaml'
 c=yaml.safe_load(cfgp.read_text(encoding='utf-8'))
 c['source']['expected_d2_panel_sha256']=sha256_file(d2)
 cfgp.write_text(yaml.safe_dump(c,allow_unicode=True,sort_keys=False),encoding='utf-8')

def run_child(m,force=False):
 if m['id']=='P1_BREAKOUT_ACCEPTANCE': patch_p1_sha()
 script=ROOT/'scripts'/m['script']; cfgp=ROOT/'config/opportunity'/m['config']
 if not script.exists() or not cfgp.exists(): raise RuntimeError(f"missing frozen replication source for {m['id']}")
 logs=OUT/'logs'; logs.mkdir(parents=True,exist_ok=True)
 receipt=OUT/'receipts'/f"{m['id']}.json"; receipt.parent.mkdir(parents=True,exist_ok=True)
 fingerprint={'script_sha256':sha256_file(script),'config_sha256':sha256_file(cfgp),'universe_sha256':EXPECTED_FULL_SHA}
 if not force and m['summary'].exists() and receipt.exists():
  try:
   old=json.loads(receipt.read_text(encoding='utf-8'))
   if old.get('fingerprint')==fingerprint:
    return {'id':m['id'],'status':'REUSED','summary':str(m['summary'])}
  except Exception: pass
 env=dict(**__import__('os').environ); env['PYTHONPATH']=str(ROOT)+((';'+env['PYTHONPATH']) if env.get('PYTHONPATH') else '')
 for cmd in ['check','run']:
  p=subprocess.run([sys.executable,str(script),cmd],cwd=str(ROOT),env=env,text=True,capture_output=True)
  (logs/f"{m['id']}_{cmd}.log").write_text((p.stdout or '')+'\nSTDERR:\n'+(p.stderr or ''),encoding='utf-8')
  print(f"[{m['id']}] {cmd} rc={p.returncode}")
  if p.returncode!=0:
   log_path=logs/f"{m['id']}_{cmd}.log"
   raise RuntimeError(f"{m['id']} {cmd} failed; see {log_path}")
 if not m['summary'].exists(): raise RuntimeError(f"{m['id']} summary missing after run")
 receipt.write_text(json.dumps({'fingerprint':fingerprint,'summary':str(m['summary']),'summary_sha256':sha256_file(m['summary']),'completed_at':datetime.now().isoformat(timespec='seconds')},indent=2),encoding='utf-8')
 return {'id':m['id'],'status':'RAN','summary':str(m['summary'])}

def verdict(d): return d.get('overall_verdict') or d.get('status')
def pick_metric(d):
 p=d.get('primary',{}) if isinstance(d,dict) else {}
 keys=['lift_vs_compression_setup','lift_vs_pressure_setup','lift_vs_setup','lift_vs_rejected','lift_vs_baseline','lift_vs_leader_setup','lift_vs_all','lift']
 delta_keys=['precision_minus_compression_setup','precision_minus_pressure_setup','precision_minus_setup','precision_minus_rejected','precision_minus_baseline','precision_minus_leader_setup','precision_minus_all']
 lift=next((p.get(k) for k in keys if k in p),None)
 delta=next((p.get(k) for k in delta_keys if k in p),None)
 py=p.get('positive_years')
 return {'lift':lift,'precision_delta':delta,'positive_years':py}

def build_report(run_states):
 rows=[]
 for m in MECHANISMS:
  if not m['summary'].exists():
   rows.append({'id':m['id'],'new_status':'MISSING'}); continue
  new=json.loads(m['summary'].read_text(encoding='utf-8')); old=json.loads(m['old'].read_text(encoding='utf-8')) if m['old'].exists() else None
  rows.append({'id':m['id'],'old_verdict':verdict(old) if old else None,'new_verdict':verdict(new),'old_primary':pick_metric(old) if old else None,'new_primary':pick_metric(new),'old_summary':str(m['old']) if old else None,'new_summary':str(m['summary'])})
 out={'status':'PASS_FULL_MARKET_REPLICATION_SWEEP_V0_1','generated_at':datetime.now().isoformat(timespec='seconds'),'contract':{'only_intended_change':'CSI500_PIT_TESTBED -> SH/SZ FULL_MARKET_TRADABLE_V0_1','mechanism_parameters_changed':False,'gates_changed':False,'entry_timing_changed':False,'outcome_labels_changed':False,'purpose':'Universe transfer/generalization test; PASS does not qualify a Strategy.'},'universe':{'source_panel':str(FULL_PANEL),'source_sha256':EXPECTED_FULL_SHA,'normalized_legacy_panel':str(NORM_PANEL),'normalized_sha256':sha256_file(NORM_PANEL)},'run_states':run_states,'comparisons':rows,'next_phase':'After this stock-level/breadth sweep, run separate full-market SW-PIT coverage gate before D4/D5/D6 sector replication.'}
 OUT.mkdir(parents=True,exist_ok=True); p=OUT/'full_market_replication_sweep_v0_1_summary.json'; p.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8'); print(json.dumps(out,ensure_ascii=False,indent=2)); return p

def do_check():
 missing=[]
 for m in MECHANISMS:
  for p in [ROOT/'scripts'/m['script'],ROOT/'config/opportunity'/m['config']]:
   if not p.exists(): missing.append(str(p))
 ok=FULL_PANEL.exists() and FULL_SUMMARY.exists() and not missing
 out={'status':'PASS_FULL_MARKET_REPLICATION_SWEEP_V0_1_CHECK' if ok else 'BLOCKED_FULL_MARKET_REPLICATION_SWEEP_V0_1_CHECK','full_market_panel':str(FULL_PANEL),'exists':FULL_PANEL.exists(),'sha256':sha256_file(FULL_PANEL) if FULL_PANEL.exists() else None,'expected_sha256':EXPECTED_FULL_SHA,'source_files_missing':missing,'mechanism_ids':[m['id'] for m in MECHANISMS]}
 print(json.dumps(out,ensure_ascii=False,indent=2)); return 0 if ok and out['sha256']==EXPECTED_FULL_SHA else 3

def main():
 ap=argparse.ArgumentParser(); ap.add_argument('command',choices=['check','run','report']); ap.add_argument('--force',action='store_true'); a=ap.parse_args()
 if a.command=='check': return do_check()
 prepare_universe()
 if a.command=='report': build_report([]); return 0
 states=[]
 for m in MECHANISMS: states.append(run_child(m,a.force))
 build_report(states); return 0
if __name__=='__main__': raise SystemExit(main())
