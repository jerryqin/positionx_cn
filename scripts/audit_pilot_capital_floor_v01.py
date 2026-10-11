from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path: sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from research.pilot_capital_floor import CapitalFloorPolicy, board_of, evaluate_trade, summarize

POLICY=CapitalFloorPolicy()
CATALOG=Path(r"D:\positionx_data\catalog\market_data.duckdb")
RET_TRADES=Path(r"D:\positionx_data\research\ret120_vs_52wh_family_v0_2_frozen_signal\ret120_trades_20bps.csv")
DEEP_TRADES=Path(r"D:\positionx_data\research\alpha2_incremental_value_v0_1\deep_oversold_export\deep_oversold_20bps_trades.csv")
OUT=Path(r"D:\positionx_data\research\pilot_live_readiness\capital_floor_v0_1")


def write_json_atomic(path:Path,obj:Any):
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_suffix(path.suffix+'.tmp'); tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8'); os.replace(tmp,path)


def load_keys(path:Path,strategy:str)->pd.DataFrame:
    if not path.exists(): raise RuntimeError(f"FAIL_CLOSED missing trades {path}")
    x=pd.read_csv(path,dtype=str)
    need={'symbol','entry_date'}
    if not need.issubset(x.columns): raise RuntimeError(f"FAIL_CLOSED {strategy} trades missing {sorted(need-set(x.columns))}")
    x=x[['symbol','entry_date']].copy(); x['symbol']=x.symbol.astype(str); x['entry_date']=x.entry_date.astype(str).str.replace('-','',regex=False).str[:8]; x['strategy']=strategy
    return x.drop_duplicates(['symbol','entry_date']).reset_index(drop=True)


def attach_raw_open(keys:pd.DataFrame,catalog:Path)->pd.DataFrame:
    if not catalog.exists(): raise RuntimeError(f"FAIL_CLOSED missing catalog {catalog}")
    import duckdb
    con=duckdb.connect(str(catalog),read_only=True)
    try:
        con.register('keys',keys)
        # market.daily_bars_v1 uses actual/raw daily price levels appropriate for lot-notional feasibility.
        q=con.execute("""
          SELECT k.strategy,k.symbol,k.entry_date,b.open AS entry_open
          FROM keys k
          LEFT JOIN market.daily_bars_v1 b
            ON b.symbol=k.symbol
           AND replace(CAST(b.trade_date AS VARCHAR),'-','')=k.entry_date
        """).fetchdf()
    finally: con.close()
    q['entry_open']=pd.to_numeric(q.entry_open,errors='coerce')
    return q


def check()->int:
    status={
      'status':'PASS_PILOT_CAPITAL_FLOOR_V0_1_CHECK',
      'catalog_exists':CATALOG.exists(),'ret_trades_exists':RET_TRADES.exists(),'deep_trades_exists':DEEP_TRADES.exists(),
      'policy':asdict(POLICY),'no_qmt':True,'no_broker':True,'no_database_mutation':True,
    }
    if not all((CATALOG.exists(),RET_TRADES.exists(),DEEP_TRADES.exists())): status['status']='BLOCKED_INPUT_MISSING'
    print(json.dumps(status,ensure_ascii=False,indent=2)); return 0 if status['status'].startswith('PASS_') else 3


def run()->int:
    ret=load_keys(RET_TRADES,'RET120'); deep=load_keys(DEEP_TRADES,'DEEP_OVERSOLD'); keys=pd.concat([deep,ret],ignore_index=True)
    px=attach_raw_open(keys,CATALOG)
    missing=px[px.entry_open.isna() | (px.entry_open<=0)]
    valid=px[px.entry_open.notna() & (px.entry_open>0)].copy()
    if len(missing)/max(1,len(px))>0.01: raise RuntimeError(f"FAIL_CLOSED raw OPEN missing >1% rows={len(missing)}/{len(px)}")
    rows=[]; summary={}
    for cap in POLICY.candidate_capitals:
        z=[]
        for r in valid.itertuples(index=False):
            sw=POLICY.deep_sleeve_weight if r.strategy=='DEEP_OVERSOLD' else POLICY.ret120_sleeve_weight
            ev=evaluate_trade(float(r.entry_open),cap,sw,POLICY.strategy_target_weight,board_of(r.symbol),POLICY.max_relative_target_error)
            z.append({'capital':cap,'strategy':r.strategy,'symbol':r.symbol,'entry_date':r.entry_date,'entry_open':float(r.entry_open),'board':board_of(r.symbol),**ev})
        zdf=pd.DataFrame(z); rows.extend(z)
        summary[str(cap)]={
            'combined':summarize(zdf),
            'deep':summarize(zdf[zdf.strategy=='DEEP_OVERSOLD']),
            'ret120':summarize(zdf[zdf.strategy=='RET120']),
        }
    minimum=None
    for cap in POLICY.candidate_capitals:
        s=summary[str(cap)]
        if all(s[k]['fidelity'] is not None and s[k]['fidelity']>=POLICY.fidelity_min for k in ('deep','ret120')):
            minimum=cap; break
    result={
      'status':'PASS_PILOT_CAPITAL_FLOOR_AUDIT_V0_1',
      'methodology':{
        'portfolio_baseline':'50/50 initial-capital sleeves; no rebalance',
        'target_notional_formula':'total_capital * 0.50 sleeve * 0.01 strategy target = 0.005 * total_capital',
        'price':'canonical market.daily_bars_v1 raw/actual entry OPEN for historical executed trade keys',
        'lot_rules':'SSE/SZSE regular & ChiNext: 100-share lots; STAR: minimum 200 shares then 1-share increments',
        'sizing':'round down / never exceed target',
        'fidelity_pass':'executable shares >0 and relative target underweight <=25%',
        'capital_floor_rule':'lowest tested total capital where BOTH strategies reach >=90% fidelity',
      },
      'inputs':{'deep_trade_keys':int(len(deep)),'ret120_trade_keys':int(len(ret)),'joined':int(len(px)),'valid_open':int(len(valid)),'missing_open':int(len(missing))},
      'policy':asdict(POLICY),'capital_results':summary,'minimum_tested_capital_both_ge_90pct':minimum,
      'interpretation': 'This is a structural sizing audit, not a recommendation to deploy that amount. Final Pilot capital also requires user loss-budget and portfolio risk freeze.',
      'no_qmt':True,'no_broker':True,'catalog_read_only':True,
    }
    OUT.mkdir(parents=True,exist_ok=True); pd.DataFrame(rows).to_csv(OUT/'capital_floor_trade_detail.csv',index=False); write_json_atomic(OUT/'capital_floor_summary_v01.json',result)
    print(json.dumps(result,ensure_ascii=False,indent=2)); print('OUTPUT:',OUT); return 0


def main():
    p=argparse.ArgumentParser(); p.add_argument('command',choices=['check','run']); a=p.parse_args()
    try: return check() if a.command=='check' else run()
    except Exception as exc: print(json.dumps({'status':'FAIL_CLOSED','error':f'{type(exc).__name__}: {exc}'},ensure_ascii=False,indent=2)); return 3
if __name__=='__main__': raise SystemExit(main())
