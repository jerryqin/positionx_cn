"""Broker-independent data-only task entry; no orders or model ledger operations."""
from __future__ import annotations
import argparse
import csv
import json
import os
from pathlib import Path
import shutil
import sys
import msvcrt

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import pandas as pd
from core import p0_rq_input as rq
from scripts import prepare_daily_data as prep, collect_postclose_candidate as close
from scripts import run_postclose_local_candidate as local

OPS = Path('D:/positionx_data/ops/three_strategy_data_v1')


def register_pit(source, day):
    """Copy actual evidence unchanged; registration only relocates manifest paths."""
    source=Path(source)
    local.membership({'pit_root':str(source)},day)
    original=sorted(source.glob(day+'T*_preopen.json'))[-1]
    m=local.read_json(original)
    root=Path(rq.profile()['pit_root'])
    capture=original.stem
    dest=root/'raw'/day/'preopen'/capture
    dest.mkdir(parents=True,exist_ok=True)
    src=Path(m['items'][0]['file_path'])
    target=dest/'index_membership_rqdata.parquet'
    if target.exists():
        if rq.sha(target)!=rq.sha(src):raise ValueError('PIT_REGISTRATION_COLLISION')
    else:shutil.copyfile(src,target)
    for item in m['items']:item['file_path']=str(target)
    m['registration_source_manifest_sha256']=rq.sha(original)
    out=root/'manifests'/original.name
    if out.exists():
        if local.read_json(out)!=m:raise ValueError('PIT_MANIFEST_COLLISION')
    else:local.write_json(out,m)
    return local.membership({'pit_root':str(root/'manifests')},day)


def result_files(r):
    ROOT.joinpath('RESULT.md').write_text('# Daily data status\n\n'+json.dumps(r,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
    with ROOT.joinpath('DATA_STATUS.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f);w.writerow(['target_date','strategy','status','version'])
        for key,value in r.get('strategies',{}).items():w.writerow([r['date'],key,value,r.get('version')])


def run(phase, day=None):
    day=day or rq.clock().strftime('%Y%m%d')
    rq.require_trading_date(day)
    OPS.mkdir(parents=True,exist_ok=True)
    receipt=OPS/(day+'_'+phase+'.json')
    error_path=OPS/(day+'_'+phase+'_last_error.json')
    if error_path.exists() and local.read_json(error_path).get('nonretryable'):
        raise ValueError('NONRETRYABLE_SOURCE_BLOCKED_NO_RETRY')
    if phase=='preopen':
        if rq.clock().strftime('%H%M')>='0920':raise ValueError('MISSED_PREOPEN')
        out=ROOT/'reports'/('data_only_'+day)
        r=prep.preopen(day,out)
        register_pit(out/'pit/manifests',day)
        return dict(r,date=day)
    unit=ROOT/'reports'/('postclose_stock_'+day+'_resume')/'funnel_live/amount_unit_binding.json'
    if unit.exists() and not (OPS/'funnel_amount_binding.json').exists():shutil.copyfile(unit,OPS/'funnel_amount_binding.json')
    prior=local.read_json(receipt) if receipt.exists() else None
    if prior and prior.get('version') and prior.get('status')=='GREEN':
        reader=rq.open_inputs(day)
        if reader.meta['version']==prior['version'] and rq.sha(reader.root/'manifest.json')==prior['manifest_sha256']:
            return dict(prior,idempotent_reuse=True)
    try:reader=rq.open_inputs(day)
    except rq.InputBlocked:
        stock=ROOT/'reports'/('postclose_stock_'+day+'_resume')
        if not (stock/'manifest.json').exists():
            if rq.clock().strftime('%H%M')>='1715':raise ValueError('FINAL_DEADLINE_NO_COMPLETE_STOCK_PARTITION')
            metas=[(local.read_json(p),p) for p in Path(rq.profile()['asset_root']).glob('versions/*/source_metadata.json')]
            candidates=[(m['asof'],p) for m,p in metas if m.get('quality_status')=='PASS' and m['asof']<str(pd.Timestamp(day).date())]
            if not candidates:raise ValueError('NO_ACCEPTED_HISTORY_BASE')
            base=max(candidates)[1].parent/'manifest.json'
            close.run(day,base,stock)
            if rq.clock().strftime('%H%M')>='1715':raise ValueError('CAPTURE_FINISHED_AFTER_FINAL_DEADLINE')
        reader=prep.merge_candidate(stock,live=True)
        # Publish only an internally complete stock version; strategy gates remain separate.
        from scripts.collect_p0_rq_close_v01 import publish
        from scripts.postclose_data_admission_v1 import audit_stock_asset
        audit_stock_asset(reader,day)
        publish(reader.root)
        reader=rq.open_inputs(day)
    try:sets,pit=local.membership({'pit_root':str(Path(rq.profile()['pit_root'])/'manifests')},day)
    except (Exception,SystemExit):
        binding=Path('D:/TEMP/positionx_pit_binding_postclose_'+day+'T084800/PIT_TEMP_ASSET_BINDING.json')
        if not binding.exists():raise ValueError('EXACT_DAY_PREOPEN_PIT_UNAVAILABLE')
        b=local.read_json(binding)
        for item in b['files']:
            if rq.sha(item['path'])!=item['sha256']:raise ValueError('PIT_SOURCE_HASH_FAILED')
        sets,pit=register_pit(b['consumer_pit_root'],day)
    strategies={};details={}
    members=sorted(sets['000905.SH'])
    for kind,adjust,bars,minimum in [('deep','front',120,120),('ret120','back_ratio',320,121)]:
        try:
            frames=reader.histories(members,day,bars,adjust,minimum,allow_incomplete=kind=='ret120')
            if kind=='ret120':
                from research.forward_ret120 import build_daily_snapshot
                snap=build_daily_snapshot(rq.feature_histories(frames),members,day)
                details[kind]={'valid':int(snap.feature_valid.sum()),'q5':int(snap.selected_q5.sum())}
            else:
                from research.forward_deep_oversold import ret20_at_close
                details[kind]={'histories':len(frames),'ret20_valid':sum(ret20_at_close(f,day) is not None for f in frames.values())}
            strategies[kind]='PASS'
        except Exception as e:strategies[kind]='BLOCKED:'+str(e)
    try:
        from scripts.qualify_postclose_funnel import run as qualify
        stock=ROOT/'reports'/('postclose_stock_'+day+'_resume')
        binding=stock/'funnel_live/amount_unit_binding.json'
        persistent_binding=OPS/'funnel_amount_binding.json'
        if binding.exists() and not persistent_binding.exists():
            shutil.copyfile(binding,persistent_binding)
        if not binding.exists():binding=persistent_binding
        summary=qualify(str(reader.db),day,str(stock/'funnel_published'),membership_database=str(stock/'increment.duckdb'),amount_unit_binding=str(binding))
        details['funnel']=summary
        strategies['funnel']='PASS' if summary.get('status')=='PASS_STRUCTURE_ADV20_INPUTS' else 'BLOCKED:'+str(summary.get('status'))
    except Exception as e:strategies['funnel']='BLOCKED:'+str(e)
    r=dict(date=day,phase=phase,status='GREEN' if all(v=='PASS' for v in strategies.values()) else 'PARTIAL' if 'PASS' in strategies.values() else 'RED',
           version=reader.meta['version'],manifest_sha256=rq.sha(reader.root/'manifest.json'),
           strategies=strategies,details=details,pit=pit,consumer_versions={k:reader.meta['version'] for k in strategies},forward_or_trading_authorized=False)
    local.write_json(receipt,r)
    local.write_json(OPS/'current.json',dict(date=day,version=r['version'],manifest_sha256=r['manifest_sha256']))
    result_files(r)
    return r


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--phase',choices=['preopen','postclose'],required=True)
    a=p.parse_args();day=rq.clock().strftime('%Y%m%d')
    lock=OPS/(a.phase+'.lock');OPS.mkdir(parents=True,exist_ok=True)
    fd=None
    try:
        try:rq.require_trading_date(day)
        except rq.InputBlocked:
            print(json.dumps({'status':'SKIP_CLOSED','date':day}));return 0
        fd=os.open(lock,os.O_CREAT|os.O_RDWR)
        if os.fstat(fd).st_size==0:os.write(fd,b'1')
        os.lseek(fd,0,os.SEEK_SET)
        msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
        r=run(a.phase,day)
        local.write_json(OPS/'last_run.json',dict(r,completed_at=rq.clock().isoformat()))
        print(json.dumps(r,ensure_ascii=False,default=str),flush=True)
        return 0 if r['status'] in ('GREEN','GREEN_PIT') else 3
    except Exception as e:
        error=str(e) if isinstance(e,(ValueError,rq.InputBlocked)) else type(e).__name__
        permanent=any(k in error.lower() for k in ('auth','permission','license','schema','nonretryable','unsupported'))
        r=dict(date=day,phase=a.phase,status='RED',error=error,nonretryable=permanent)
        local.write_json(OPS/(day+'_'+a.phase+'_last_error.json'),r);result_files(r)
        print(json.dumps(r),flush=True);return 3
    finally:
        if fd is not None:os.close(fd)


if __name__=='__main__':raise SystemExit(main())
