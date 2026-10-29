"""HTTPS data tasks and local model reports; no Broker or formal-ledger writes."""
from __future__ import annotations
import argparse
import csv
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import msvcrt

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import pandas as pd
from core import p0_rq_input as rq
from core.rqdata_https import RQDataHTTPSProvider, HTTPSTransportError
from core.rqdata_identity import _full_checkpoint
from scripts import prepare_daily_data as prep, collect_postclose_candidate as close
from scripts import run_postclose_local_candidate as local

OPS = Path('D:/positionx_data/ops/three_strategy_data_v1')
TRANSPORT = 'RQDATA_HTTPS_V1'


def register_pit(source, day):
    """Copy actual evidence unchanged; registration only relocates manifest paths."""
    source=Path(source)
    local.membership({'pit_root':str(source)},day)
    original=sorted(source.glob(day+'T*_preopen.json'))[-1]
    m=local.read_json(original)
    proof=Path(m['identity_capture']['manifest'])
    _,identity=_full_checkpoint(proof,day)
    src=Path(m['items'][0]['file_path'])
    if (identity.attrs['identity_capture']!=m['identity_capture']
            or rq.sha(src.parent/'identity.parquet')!=m['identity_sha256']
            or pd.Timestamp(m['identity_capture']['observed_range']['last'])>pd.Timestamp(m['observed_at'])):
        raise ValueError('PIT_IDENTITY_BINDING_INVALID')
    root=Path(rq.profile()['pit_root'])
    ret_profile=local.read_json(ROOT/'config/ops/p0_core_recovery_v01.json')
    if (root.resolve()!=root or Path(ret_profile['pit_root'])!=root/'manifests'
            or rq.clock().strftime('%Y%m%d')!=day):
        raise ValueError('PIT_PRODUCTION_DATE_OR_PATH_INVALID')
    capture=original.stem
    dest=root/'raw'/day/'preopen'/capture
    if dest.resolve()!=dest or (root/'manifests').resolve()!=root/'manifests':
        raise ValueError('PIT_PRODUCTION_PATH_REDIRECTED')
    dest.mkdir(parents=True,exist_ok=True)
    target=dest/'index_membership_rqdata.parquet'
    if target.resolve()!=target:raise ValueError('PIT_PRODUCTION_PATH_REDIRECTED')
    if target.exists():
        if rq.sha(target)!=rq.sha(src):raise ValueError('PIT_REGISTRATION_COLLISION')
    else:shutil.copyfile(src,target)
    for item in m['items']:item['file_path']=str(target)
    m['registration_source_manifest_sha256']=rq.sha(original)
    out=root/'manifests'/original.name
    if out.exists():
        if local.read_json(out)!=m:raise ValueError('PIT_MANIFEST_COLLISION')
    else:local.write_json(out,m)
    sets,binding=local.membership(ret_profile,day)
    universe=rq.current_universe()
    if (Path(binding['manifest'])!=out or binding['sha256']!=rq.sha(src)
            or universe.get('ok') is not True or set(universe['members'])!=sets['000905.SH']
            or universe['source']!='RQDATA_COMPONENTS_PROVIDER|preopen_sha256='+binding['sha256']):
        raise ValueError('RET120_REGISTERED_PIT_READ_MISMATCH')
    return sets,binding


def retryable_transport(details, retryable=False):
    """Retry only transport failures, never malformed or unqualified data."""
    return retryable or details.get('error_type') in (
        'gaierror','TimeoutError','ConnectionRefusedError','ConnectionResetError',
        'ConnectionAbortedError','BrokenPipeError','RemoteDisconnected','IncompleteRead')


def legacy_transport_block(item):
    """Recognize the old failed-checkpoint misclassification without rewriting it."""
    day=item.get('date','')
    if (item.get('phase')!='preopen' or item.get('transport')!=TRANSPORT
            or item.get('source_block_scope')!='preopen'
            or item.get('error_type')!='InputBlocked'
            or item.get('error')!='IDENTITY_FULL_CHECKPOINT_CONTRACT_INVALID'
            or len(day)!=8 or not day.isdecimal()):
        return False
    proof=ROOT/'reports'/('data_only_'+day)/'identity/full/checkpoint.json'
    if not proof.is_file():return False
    state=local.read_json(proof)
    return (state.get('schema')=='RQDATA_IDENTITY_FULL_V1' and state.get('status')=='FAILED'
            and state.get('business_date')==day and state.get('mode')=='FULL_HTTP_RESPONSE'
            and state.get('method')=='all_instruments'
            and state.get('parameters')==dict(type='CS',date=day,market='cn')
            and state.get('error_type')=='HTTPSTransportError'
            and retryable_transport(state.get('details',{})))


def failure_result(phase, day, e):
    safe_https=type(e).__module__=='core.rqdata_https' and type(e).__name__ in ('HTTPSNotReady','HTTPSContractError','HTTPSAuthError','HTTPSSecurityError','HTTPSTransportError')
    error=str(e) if isinstance(e,(ValueError,rq.InputBlocked)) or safe_https else type(e).__name__
    not_ready=type(e).__name__=='HTTPSNotReady' or error=='SUPPLIER_NOT_READY'
    permanent=not not_ready and (type(e).__name__ in ('HTTPSContractError','HTTPSAuthError','HTTPSSecurityError') or any(k in error.lower() for k in ('auth','permission','license','schema','nonretryable','unsupported','contract','security')))
    if phase=='preopen':
        recoverable=isinstance(e,HTTPSTransportError) and retryable_transport(e.details,e.retryable)
        # Time/date stops fail this run; they are not supplier contract failures.
        window_stop=error in ('MISSED_PREOPEN','INSUFFICIENT_PREOPEN_TIME','PREOPEN_WINDOW_NOT_STARTED',
            'ONLY_ACTUAL_CURRENT_DAY_CAPTURE_ALLOWED','ACTUAL_SAME_DAY_BEFORE_0920_REQUIRED',
            'IDENTITY_CAPTURE_FINISHED_AFTER_0920','CAPTURE_FINISHED_AFTER_0920',
            'IDENTITY_PHASE_DEADLINE_REACHED','HTTPS_OVERALL_DEADLINE_NO_RETRY')
        permanent=not (not_ready or recoverable or window_stop)
    shared=type(e).__name__ in ('HTTPSAuthError','HTTPSSecurityError')
    if phase=='preopen' and not permanent:shared=False
    return dict(date=day,phase=phase,status='NOT_READY' if not_ready else 'RED',error=error,
                error_type=type(e).__name__,nonretryable=permanent,transport=TRANSPORT,
                source_block_scope='all' if shared else phase,published_previous_version_retained=True)


def result_files(r):
    ROOT.joinpath('RESULT.md').write_text('# Daily data status\n\n'+json.dumps(r,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
    with ROOT.joinpath('DATA_STATUS.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f);w.writerow(['target_date','strategy','status','version'])
        for key,value in r.get('strategies',{}).items():w.writerow([r['date'],key,value,r.get('version')])
        funnel=r.get('funnel') or {}
        structure=funnel.get('structure_input_status',funnel.get('status','NOT_RUN'))
        risk=(funnel.get('execution_preview') or {}).get('status','NOT_RUN')
        full=(funnel.get('model') or {}).get('status',funnel.get('full_model_status','NOT_RUN'))
        w.writerow([r['date'],'funnel',f'structure={structure};risk={risk};full={full}',r.get('version')])


def source_block(phase, day):
    """Keep permanent HTTPS failures blocked across scheduled dates, per phase."""
    for path in sorted(OPS.glob('*_last_error.json'), reverse=True):
        item=local.read_json(path)
        if not item.get('nonretryable'):
            continue
        if legacy_transport_block(item):
            continue
        same_attempt=item.get('date',path.name[:8])==day and item.get('phase',path.name.split('_')[1])==phase
        same_transport=item.get('transport')==TRANSPORT
        if same_attempt or (same_transport and (item.get('phase')==phase or item.get('source_block_scope')=='all')):
            return path,item
    return None


def require_source_allowed(phase, day):
    blocked=source_block(phase,day)
    if blocked:
        raise ValueError('NONRETRYABLE_SOURCE_BLOCKED_NO_RETRY:'+str(blocked[0]))


def latest_base(day):
    """Choose the newest accepted seal, never an older same-date path by name."""
    candidates=[]
    asset_root=Path(rq.profile()['asset_root'])
    for path in list(asset_root.glob('versions/*/source_metadata.json'))+list(asset_root.glob('strategy_versions/*/source_metadata.json')):
        meta=local.read_json(path)
        full=meta.get('quality_status')=='PASS' and meta.get('engineering_only') is False
        core=(meta.get('qualification_scope')=='CORE_MODELS_V1'
              and meta.get('quality_status')=='CORE_MODELS_PASS_V1' and meta.get('engineering_only') is True
              and meta.get('full_p0_pass') is False and meta.get('execution_qualified') is True
              and meta.get('model_continuation_qualified') is True)
        if ((full or core)
                and meta.get('source')=='RQDATA' and meta['asof']<str(pd.Timestamp(day).date())
                and pd.Timestamp(meta['availability'])<=pd.Timestamp(rq.clock())):
            candidates.append((meta['asof'],pd.Timestamp(meta['availability']),path.parent/'manifest.json'))
    if not candidates:
        raise ValueError('NO_ACCEPTED_HISTORY_BASE')
    selected=max((row[0],row[1]) for row in candidates)
    profile=local.read_json(ROOT/'config/ops/p0_core_recovery_v01.json')
    dates=local.sessions(profile['calendar'],day)
    if len(dates)<2 or selected[0].replace('-','')!=dates[-2]:
        raise ValueError('ADJACENT_ACCEPTED_HISTORY_BASE_MISSING')
    matches=[row[2] for row in candidates if row[:2]==selected]
    if len(matches)!=1:
        raise ValueError('AMBIGUOUS_ACCEPTED_HISTORY_BASE')
    return matches[0]


def model_report(reader, day):
    """Run the existing report in a child because its offline guard is process-wide."""
    from scripts import run_postclose_daily as report_entry
    def validate(root):
        root,report,_=report_entry.model_report(root)
        if (report.get('business_date')!=day or report.get('input',{}).get('version')!=reader.meta['version']
                or report.get('database_sha256')!=reader.meta['database_sha256']
                or report.get('manifest_sha256')!=rq.sha(reader.root/'manifest.json')
                or report.get('closeout_version')!=report_entry.CLOSEOUT_VERSION):
            raise ValueError('MODEL_REPORT_PUBLISHED_INPUT_IDENTITY_MISMATCH')
        return root,report
    complete=[row for row in report_entry.daily_reports(day)
              if row[3].get('status')=='TWO_STRATEGY_REPORT_COMPLETE'
               and row[3].get('funnel',{}).get('model',{}).get('status')=='FUNNEL_MODEL_SESSION_COMPLETE'
               and row[3].get('closeout_version')==report_entry.CLOSEOUT_VERSION
               and (not report_entry.actual_forward_window(day) or row[3].get('forward_acceptance') is True)
              and row[3].get('input',{}).get('version')==reader.meta['version']
              and row[3].get('database_sha256')==reader.meta['database_sha256']]
    if complete:
        root,report=validate(complete[-1][2])
        return root,report,True
    timeout=600
    now=rq.clock()
    if day>'20261028' and day==now.strftime('%Y%m%d'):
        deadline=now.replace(hour=17,minute=15,second=0,microsecond=0)
        remaining=(deadline-now).total_seconds()
        if remaining<=0:
            raise ValueError('MODEL_REPORT_FINAL_DEADLINE_REACHED_NO_NEW_CALCULATION')
        timeout=min(timeout,remaining)
    args=[str(ROOT/'.venv/Scripts/python.exe'),'-X','utf8','-B',
          str(ROOT/'scripts/run_postclose_daily.py'),'--date',day,'--allow-core-models']
    # A report from another same-day seal must not be mistaken for this input.
    if any(row[3].get('status')=='TWO_STRATEGY_REPORT_COMPLETE' for row in report_entry.daily_reports(day)):
        args.extend(['--output',str(report_entry.new_attempt(day))])
    completed=subprocess.run(args,cwd=str(ROOT),capture_output=True,text=True,encoding='utf-8',timeout=timeout)
    try:
        summary=json.loads(completed.stdout.strip().splitlines()[-1])
        path=Path(summary['report']).resolve()
        if not path.is_relative_to((ROOT/'reports/postclose_daily').resolve()) or path.name!='report.md':
            raise ValueError('MODEL_REPORT_OUTPUT_OUTSIDE_DAILY_REPORTS')
        root,report=validate(path.parent)
    except (KeyError,IndexError,json.JSONDecodeError) as exc:
        raise ValueError('MODEL_REPORT_CHILD_DID_NOT_RETURN_A_REPORT') from exc
    if (completed.returncode==0)!=(report.get('three_strategy_status')=='THREE_STRATEGY_MODELS_COMPLETE'):
        raise ValueError('MODEL_REPORT_CHILD_EXIT_STATUS_CONFLICT')
    return root,report,False


def run(phase, day=None):
    day=day or rq.clock().strftime('%Y%m%d')
    rq.require_trading_date(day)
    OPS.mkdir(parents=True,exist_ok=True)
    receipt=OPS/(day+'_'+phase+'.json')
    if phase=='preopen':
        require_source_allowed(phase,day)
        now=rq.clock()
        if now.strftime('%H%M')>='0920':raise ValueError('MISSED_PREOPEN')
        if now.strftime('%Y%m%d')!=day:raise ValueError('ONLY_ACTUAL_CURRENT_DAY_CAPTURE_ALLOWED')
        base=ROOT/'reports'/('data_only_'+day)
        manifests=list((base/'pit/manifests').glob(day+'T*_preopen.json'))
        manifests+=list((base/'attempts').glob('*/pit/manifests/'+day+'T*_preopen.json'))
        if manifests:
            source=max(manifests,key=lambda p:p.name).parent
            r=dict(status='GREEN_PIT',reused=True)
        else:
            windows=[s for s in ('0820','0900','0910') if now.strftime('%H%M')>=s]
            if not windows:raise ValueError('PREOPEN_WINDOW_NOT_STARTED')
            out=base/'attempts'/windows[-1]
            if out.resolve()!=out:raise ValueError('PREOPEN_OUTPUT_PATH_REDIRECTED')
            if out.exists():
                return dict(date=day,phase=phase,status='RED',error='PREOPEN_WINDOW_ALREADY_ATTEMPTED_WAIT_NEXT_WINDOW')
            remaining=(now.replace(hour=9,minute=20,second=0,microsecond=0)-now).total_seconds()-5
            if remaining<=0:raise ValueError('INSUFFICIENT_PREOPEN_TIME')
            out.mkdir(parents=True,exist_ok=False)
            provider=RQDataHTTPSProvider(output=out/'https',api_limit=3,total_seconds=min(240,remaining))
            r=prep.preopen(day,out,provider=provider)
            source=out/'pit/manifests'
        if rq.clock().strftime('%H%M')>='0920':raise ValueError('MISSED_PREOPEN')
        _,binding=register_pit(source,day)
        if rq.clock().strftime('%H%M')>='0920':raise ValueError('MISSED_PREOPEN')
        r=dict(r,date=day,phase=phase,production=True,version=Path(binding['manifest']).stem,
               pit=binding,ret120_pit_readable=True)
        local.write_json(receipt,r)
        return r
    from scripts import run_postclose_daily as report_entry
    try:reader=report_entry.full_model_input(day,allow_core_models=True)
    except rq.InputBlocked as exc:
        if not str(exc).startswith('RQ_EXACT_DATE_VERSION_MISSING:'):
            raise
        if pd.Timestamp(day).date()!=rq.clock().date():
            raise ValueError('ONLY_ACTUAL_CURRENT_DAY_CAPTURE_ALLOWED')
        if rq.clock().strftime('%H%M')<'1530':
            return dict(date=day,phase=phase,status='NOT_READY',reason='COMPLETE_CLOSE_WINDOW_NOT_REACHED',
                        published_previous_version_retained=True,transport=TRANSPORT)
        require_source_allowed(phase,day)
        stock=ROOT/'reports'/('postclose_stock_'+day+'_resume')
        if not (stock/'manifest.json').exists():
            if rq.clock().strftime('%H%M')>='1715':raise ValueError('FINAL_DEADLINE_NO_COMPLETE_STOCK_PARTITION')
            base=latest_base(day)
            close.run(day,base,stock,scope='CORE_MODELS_V1',
                      execution_symbols=report_entry.required_execution_symbols(day))
            if rq.clock().strftime('%H%M')>='1715':raise ValueError('CAPTURE_FINISHED_AFTER_FINAL_DEADLINE')
        reader=prep.merge_candidate(stock,live=True,scope='CORE_MODELS_V1')
        # Publish only an internally complete stock version; strategy gates remain separate.
        from scripts.collect_p0_rq_close_v01 import publish
        from scripts.postclose_data_admission_v1 import audit_core_model_asset
        audit_core_model_asset(reader,day)
        publish(reader.root,scope='CORE_MODELS_V1')
        reader=report_entry.full_model_input(day,allow_core_models=True)
    report_root,report,reused=model_report(reader,day)
    strategies={kind:('PASS' if report.get('strategies',{}).get(kind,{}).get('operations') else
                         'BLOCKED:'+str(report.get('strategies',{}).get(kind,{}).get('error',
                         report.get('strategies',{}).get(kind,{}).get('operations_error',report.get('input_error','NO_MODEL_OPERATIONS')))))
                for kind in ('deep','ret120')}
    status='GREEN' if report.get('status')=='TWO_STRATEGY_REPORT_COMPLETE' and all(v=='PASS' for v in strategies.values()) else 'PARTIAL' if 'PASS' in strategies.values() else 'RED'
    funnel_model_status=report.get('funnel',{}).get('model',{}).get('status','NOT_RUN')
    if status=='GREEN' and funnel_model_status!='FUNNEL_MODEL_SESSION_COMPLETE':
        status='PARTIAL'
    r=dict(date=day,phase=phase,status=status,
           version=reader.meta['version'],manifest_sha256=rq.sha(reader.root/'manifest.json'),
           strategies=strategies,details=report.get('strategies',{}),pit=report.get('pit'),
           report=str(report_root/'report.md'),report_status=report.get('status'),
           funnel=report.get('funnel'),idempotent_reuse=reused,transport=TRANSPORT,
           funnel_model_status=funnel_model_status,three_strategy_status=report.get('three_strategy_status'),
            consumer_versions={k:reader.meta['version'] for k in strategies},
            model_forward=report.get('forward_acceptance') is True,
            real_trading_authorized=False,forward_or_trading_authorized=False)
    local.write_json(receipt,r)
    local.write_json(OPS/'current.json',dict(date=day,version=r['version'],manifest_sha256=r['manifest_sha256']))
    result_files(r)
    return r


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--phase',choices=['preopen','postclose'],required=True)
    a=p.parse_args();day=rq.clock().strftime('%Y%m%d')
    lock=OPS/(a.phase+'.lock');OPS.mkdir(parents=True,exist_ok=True)
    fd=None;locked=False;started_at=rq.clock().isoformat()
    try:
        try:rq.require_trading_date(day)
        except rq.InputBlocked:
            print(json.dumps({'status':'SKIP_CLOSED','date':day}));return 0
        fd=os.open(lock,os.O_CREAT|os.O_RDWR)
        if os.fstat(fd).st_size==0:os.write(fd,b'1')
        os.lseek(fd,0,os.SEEK_SET)
        msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
        locked=True
        r=run(a.phase,day)
        local.write_json(OPS/'last_run.json',dict(r,completed_at=rq.clock().isoformat()))
        if r['status']=='NOT_READY':result_files(r)
        print(json.dumps(r,ensure_ascii=False,default=str),flush=True)
        return 0 if r['status'] in ('GREEN','GREEN_PIT') else 3
    except Exception as e:
        if a.phase=='preopen' and not locked:
            print(json.dumps(dict(date=day,phase=a.phase,status='RED',error='PREOPEN_LOCK_NOT_ACQUIRED')),flush=True)
            return 3
        r=failure_result(a.phase,day,e)
        r.update(actual_started_at=started_at,completed_at=rq.clock().isoformat(),exit_code=3)
        if a.phase=='postclose':
            r.update(source_step=dict(close.SOURCE_STEP),transport_details=getattr(e,'details',{}))
        archive=OPS/(day+'_'+a.phase+'_'+rq.clock().strftime('%H%M%S%f')+'_last_error.json')
        if not archive.exists():local.write_json(archive,r)
        error_path=OPS/(day+'_'+a.phase+'_last_error.json')
        # Preserve the original permanent failure instead of replacing its scope
        # with a later lock/error/blocked-attempt diagnostic.
        if not (error_path.exists() and local.read_json(error_path).get('nonretryable')):
            local.write_json(error_path,r)
        local.write_json(OPS/'last_run.json',dict(r,completed_at=rq.clock().isoformat()))
        result_files(r)
        print(json.dumps(r),flush=True);return 3
    finally:
        if fd is not None:os.close(fd)


if __name__=='__main__':raise SystemExit(main())
