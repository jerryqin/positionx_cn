"""Exact-day complete RQ HTTPS identity snapshots and explicit batch recovery.

Daily snapshots use their own complete response. Historical batch checkpoints
remain separate and never supply rows or the pool for a daily full snapshot.
"""
from pathlib import Path
import hashlib
import json
import re
import time

import pandas as pd

from core import p0_rq_input as rq
from core.rqdata_https import decode, HTTPSTransportError

ROOT=Path(__file__).resolve().parents[1]


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _save(path,value):
    path=Path(path)
    temp=path.with_name(path.name+'.new')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
    temp.replace(path)


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _attrs(state,checkpoint):
    return dict(manifest=str(checkpoint),manifest_sha256=_sha(checkpoint),business_date=state['business_date'],
        target_list_sha256=state['target_list_sha256'],observed_range=state['observed_range'],
        atomic_snapshot=False,rows=state['rows'],mode=state.get('mode','BATCH_HTTP_RESPONSES'),
        scope=state.get('scope'))


def _day(value):
    return pd.Timestamp(value).strftime('%Y%m%d')


def _validate(frame,ids,day,fields):
    required=['order_book_id','symbol','listed_date','de_listed_date','special_type','round_lot','status','type']
    if (not set(required).issubset(frame) or frame.empty or frame.order_book_id.duplicated().any()
            or set(frame.order_book_id)!=set(ids) or not frame.type.eq('CS').all()):
        raise rq.InputBlocked('IDENTITY_EXACT_SET_OR_FIELD_CONFLICT')
    if frame[required].isna().any().any():
        raise rq.InputBlocked('IDENTITY_REQUIRED_FIELD_UNKNOWN')
    listed=pd.to_datetime(frame.listed_date,errors='coerce')
    lot=pd.to_numeric(frame.round_lot,errors='coerce')
    if (listed.isna().any() or listed.dt.date.gt(pd.Timestamp(day).date()).any()
            or lot.isna().any() or not lot.gt(0).all() or not lot.mod(1).eq(0).all()):
        raise rq.InputBlocked('IDENTITY_LISTING_OR_LOT_CONTRACT_FAILED')
    for symbol in frame.order_book_id:rq.qmt_id(symbol)
    # Instrument dictionaries have optional keys. HTTP exposes each response's
    # column union; retain every supplied field without inventing missing ones.
    return frame.copy()


def _observed(value,day):
    stamp=pd.Timestamp(value)
    now=pd.Timestamp(rq.clock())
    if stamp.tzinfo is None or _day(stamp.tz_convert(rq.CST))!=day or stamp>now:
        raise rq.InputBlocked('IDENTITY_OBSERVATION_DATE_OR_TIME_INVALID')
    return stamp.isoformat()


def target_list(day,manifest=None):
    day=_day(day)
    if day!=rq.clock().strftime('%Y%m%d'):
        raise rq.InputBlocked('IDENTITY_TARGET_MUST_BE_ACTUAL_BUSINESS_DATE')
    rq.require_trading_date(day)
    paths=[Path(manifest)] if manifest else list((ROOT/f'reports/data_only_{day}/pit/manifests').glob(day+'T*_preopen.json'))
    if not paths:
        raise rq.InputBlocked('IDENTITY_SAME_DAY_QUALIFIED_TARGET_MISSING:'+day)
    candidates=[]
    for path in paths:
        m=_read(path)
        if m.get('status')!='SUCCESS' or _day(m['observed_at'])!=day:
            raise rq.InputBlocked('IDENTITY_TARGET_MANIFEST_DATE_OR_STATUS_INVALID')
        observed=pd.Timestamp(_observed(m['observed_at'],day))
        if observed.strftime('%H%M')>='0920':
            raise rq.InputBlocked('IDENTITY_TARGET_PREOPEN_PROVENANCE_INVALID')
        src=Path(m['items'][0]['file_path']).parent/'identity.parquet'
        if _sha(src)!=m.get('identity_sha256'):
            raise rq.InputBlocked('IDENTITY_TARGET_FILE_HASH_MISMATCH')
        frame=pd.read_parquet(src)
        ids=sorted(frame.order_book_id.tolist())
        _validate(frame,ids,day,list(frame.columns))
        if len(ids)<3000:
            raise rq.InputBlocked('IDENTITY_TARGET_POOL_INCOMPLETE')
        candidates.append((observed,path,src,frame,ids,m))
    _,path,src,frame,ids,m=max(candidates,key=lambda row:row[0])
    return dict(day=day,ids=ids,fields=list(frame.columns),target_list_sha256=hashlib.sha256(
        json.dumps(ids,separators=(',',':')).encode('utf-8')).hexdigest(),
        source=dict(manifest=str(path.resolve()),manifest_sha256=_sha(path),file=str(src.resolve()),
            file_sha256=_sha(src),observed_at=m['observed_at'],finished_at=m.get('finished_at'),
            attributes_not_reused=True))


def _batch_frame(root,batch,day,fields):
    path=Path(root)/batch['raw_file']
    if _sha(path)!=batch['raw_sha256']:
        raise rq.InputBlocked('IDENTITY_BATCH_ORIGINAL_HASH_MISMATCH')
    _observed(batch['observed_at'],day)
    return _validate(decode('instruments',path.read_bytes(),{}),batch['ids'],day,fields)


def _seed(state,out,source):
    src=Path(source)
    other=_read(src/'checkpoint.json')
    for key in ('business_date','target_list_sha256','fields'):
        if other[key]!=state[key]:raise rq.InputBlocked('IDENTITY_SEED_DATE_LIST_OR_FIELDS_MISMATCH')
    if len(other['batches'])!=len(state['batches']):
        raise rq.InputBlocked('IDENTITY_SEED_BATCH_SET_MISMATCH')
    for old,new in zip(other['batches'],state['batches']):
        if old['ids']!=new['ids']:raise rq.InputBlocked('IDENTITY_SEED_BATCH_SET_MISMATCH')
        if old['status']=='COMPLETE':
            _batch_frame(src,old,state['business_date'],state['fields'])
            original=src/old['raw_file'];dest=out/old['raw_file']
            dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(original.read_bytes())
        new.update(old)
    state['checkpoint_seed']=dict(path=str(src/'checkpoint.json'),sha256=_sha(src/'checkpoint.json'))


def _legacy_seed(state,out,seed):
    receipt=Path(seed['receipt']);r=_read(receipt)
    if r['business_date']!=state['business_date']:
        raise rq.InputBlocked('IDENTITY_LEGACY_SEED_DATE_MISMATCH')
    old=next(a for a in r['attempts'] if a.get('label')=='batch_01')
    batch=state['batches'][0];raw=receipt.parent/old['raw_response']
    framing=old.get('response_framing',{})
    if (old.get('status')!='HTTP_CSV_COMPLETE' or old.get('http_status')!=200
            or old['request']['order_book_ids']!=batch['ids'] or not framing.get('transfer_complete')
            or _sha(raw)!=seed['raw_sha256'] or raw.stat().st_size!=framing['decoded_body_bytes']):
        raise rq.InputBlocked('IDENTITY_LEGACY_SEED_BINDING_FAILED')
    _validate(decode('instruments',raw.read_bytes(),{}),batch['ids'],state['business_date'],state['fields'])
    _observed(old['observed_at'],state['business_date'])
    name='batch_001_imported.raw.csv';(out/name).write_bytes(raw.read_bytes())
    batch.update(status='COMPLETE',raw_file=name,raw_sha256=_sha(raw),observed_at=old['observed_at'],
        framing=framing,imported=True,source_receipt=str(receipt),source_receipt_sha256=_sha(receipt),
        hash_binding_semantics='Original body hash independently checked and recorded at this import; not claimed present in prior receipt')


def capture_identity_batches(provider,day,output,*,target_manifest=None,max_calls=None,seed=None,deadline=None):
    day=_day(day);target=target_list(day,target_manifest)
    out=Path(output).absolute()
    if out.resolve()!=out or not out.is_relative_to(ROOT/'reports'):
        raise rq.InputBlocked('IDENTITY_CHECKPOINT_MUST_STAY_IN_REPORTS')
    out.mkdir(parents=True,exist_ok=True);checkpoint=out/'checkpoint.json'
    ids=target['ids'];fields=target['fields']
    if checkpoint.exists():
        state=_read(checkpoint)
        for key,value in [('business_date',day),('target_list_sha256',target['target_list_sha256']),('fields',fields)]:
            if state.get(key)!=value:raise rq.InputBlocked('IDENTITY_CHECKPOINT_DATE_LIST_OR_FIELDS_MISMATCH')
        expected=[ids[n:n+500] for n in range(0,len(ids),500)]
        if ([b['ids'] for b in state['batches']]!=expected
                or [b['number'] for b in state['batches']]!=list(range(1,len(expected)+1))):
            raise rq.InputBlocked('IDENTITY_CHECKPOINT_BATCH_SET_CHANGED')
    else:
        state=dict(schema='RQDATA_IDENTITY_BATCH_V1',business_date=day,target_list_sha256=target['target_list_sha256'],
            target_source=target['source'],fields=fields,status='PARTIAL',atomic_snapshot=False,
            batches=[dict(number=n//500+1,ids=ids[n:n+500],status='PENDING',attempts=[]) for n in range(0,len(ids),500)])
        global_seed=ROOT/f'reports/identity_batches/{day}'
        if isinstance(seed,dict):_legacy_seed(state,out,seed)
        elif seed is not None:_seed(state,out,seed)
        elif global_seed!=out and (global_seed/'checkpoint.json').exists():_seed(state,out,global_seed)
        _save(checkpoint,state)
    # Revalidate every reusable original before any new request.
    reusable=[]
    for batch in state['batches']:
        if batch['status']=='COMPLETE':reusable.append(_batch_frame(out,batch,day,fields))
    if state.get('status')=='COMPLETE':
        combined=_validate(pd.concat(reusable,ignore_index=True),ids,day,fields)
        if _sha(out/'identity.complete.parquet')!=state.get('combined_sha256'):
            raise rq.InputBlocked('IDENTITY_COMPLETE_FILE_HASH_MISMATCH')
        combined.attrs['identity_capture']=_attrs(state,checkpoint)
        return combined
    missing=[b for b in state['batches'] if b['status']!='COMPLETE']
    limit=min(2*len(missing),int(max_calls) if max_calls is not None else 2*len(missing))
    calls=0
    for batch in missing:
        if batch['status']=='FAILED' or len(batch['attempts'])>=2:
            raise rq.InputBlocked('IDENTITY_FAILED_BATCH_REQUIRES_EXPLICIT_RECOVERY:'+str(batch['number']))
        while batch['status']!='COMPLETE' and len(batch['attempts'])<2:
            if calls>=limit:raise rq.InputBlocked('IDENTITY_REQUEST_BUDGET_EXHAUSTED')
            if deadline is not None and pd.Timestamp(rq.clock())>=pd.Timestamp(deadline):
                raise rq.InputBlocked('IDENTITY_PHASE_DEADLINE_REACHED')
            if batch['attempts']:
                if not batch['attempts'][-1].get('retryable'):
                    raise rq.InputBlocked('IDENTITY_NONRECOVERABLE_BATCH_ERROR')
                if deadline is not None and pd.Timestamp(rq.clock())+pd.Timedelta(seconds=.5)>=pd.Timestamp(deadline):
                    raise rq.InputBlocked('IDENTITY_PHASE_DEADLINE_REACHED')
                time.sleep(.5)
            attempt=dict(number=len(batch['attempts'])+1,observed_at=rq.clock().isoformat(),status='STARTED')
            batch['attempts'].append(attempt);_save(checkpoint,state);calls+=1
            error=None
            api_before=provider.state.get('identity_api_attempts',0)
            try:
                frame=provider.call('instruments',_identity_request=True,order_book_ids=batch['ids'])
                frame=_validate(frame,batch['ids'],day,fields)
                attempt['status']='PASS'
            except Exception as exc:
                error=exc
                can_retry=isinstance(exc,HTTPSTransportError) and exc.retryable and provider._token is not None
                attempt.update(status='FAILED',error_type=type(exc).__name__,
                    error=str(exc) if isinstance(exc,(rq.InputBlocked,HTTPSTransportError)) or type(exc).__module__=='core.rqdata_https' else type(exc).__name__,
                    retryable=can_retry,details=getattr(exc,'details',{}))
                if not can_retry or len(batch['attempts'])>=2:batch['status']='FAILED'
            attempt['framing']=dict(provider._response_metadata)
            api_started=provider.state.get('identity_api_attempts',0)>api_before
            body=getattr(provider,'_last_api_body',None) if api_started else None
            if api_started and body is None and provider._response_metadata.get('content_encoding')=='identity':body=getattr(provider,'_wire_body',b'')
            if body is not None and not body.lstrip().startswith(b'{') and not any(
                    secret and secret.encode('utf-8') in body for secret in (provider._license,provider._token)):
                name=f"batch_{batch['number']:03d}_attempt_{attempt['number']:02d}.raw.csv"
                (out/name).write_bytes(body);attempt.update(raw_file=name,raw_sha256=_sha(out/name))
            if error is None:
                if 'raw_file' not in attempt:raise rq.InputBlocked('IDENTITY_SUCCESS_ORIGINAL_BODY_MISSING')
                batch.update(status='COMPLETE',raw_file=attempt['raw_file'],raw_sha256=attempt['raw_sha256'],
                    observed_at=attempt['observed_at'],framing=attempt['framing'])
            _save(checkpoint,state)
            if error is not None and batch['status']=='FAILED':raise error
    frames=[_batch_frame(out,b,day,fields) for b in state['batches']]
    combined=_validate(pd.concat(frames,ignore_index=True),ids,day,fields)
    times=[_observed(b['observed_at'],day) for b in state['batches']]
    state.update(status='COMPLETE',rows=len(combined),observed_range=dict(first=min(times),last=max(times)),
                 completed_at=rq.clock().isoformat())
    temp=out/'identity.complete.parquet.new';combined.to_parquet(temp,index=False)
    temp.replace(out/'identity.complete.parquet');state['combined_sha256']=_sha(out/'identity.complete.parquet')
    _save(checkpoint,state)
    combined.attrs['identity_capture']=_attrs(state,checkpoint)
    return combined


def _full_frame(body,day,framing):
    if (not framing.get('transfer_complete') or framing.get('incomplete')
            or framing.get('decoded_body_bytes')!=len(body)):
        raise rq.InputBlocked('IDENTITY_FULL_HTTP_ENTITY_INCOMPLETE')
    frame=decode('all_instruments',body,dict(type='CS',date=day,market='cn'),response_framing=framing)
    required=['order_book_id','symbol','listed_date','de_listed_date',
              'special_type','round_lot','status','type','exchange']
    if (not set(required).issubset(frame) or frame.empty
            or frame.order_book_id.duplicated().any() or not frame.type.eq('CS').all()):
        raise rq.InputBlocked('IDENTITY_FULL_RAW_IDENTITY_CONTRACT_FAILED')
    if frame[required].isna().any().any():
        raise rq.InputBlocked('IDENTITY_FULL_RAW_REQUIRED_FIELD_UNKNOWN')
    for symbol,exchange in zip(frame.order_book_id,frame.exchange):
        parsed=re.fullmatch(r'\d{6}\.(XSHG|XSHE|BJSE)',symbol) if isinstance(symbol,str) else None
        if parsed is None or parsed.group(1)!=exchange:
            raise rq.InputBlocked('IDENTITY_FULL_RAW_UNKNOWN_OR_CONFLICTING_EXCHANGE')
    listed=pd.to_datetime(frame.listed_date,errors='coerce')
    lot=pd.to_numeric(frame.round_lot,errors='coerce')
    if (listed.isna().any() or listed.dt.date.gt(pd.Timestamp(day).date()).any()
            or lot.isna().any() or not lot.gt(0).all() or not lot.mod(1).eq(0).all()):
        raise rq.InputBlocked('IDENTITY_FULL_RAW_LISTING_OR_LOT_CONTRACT_FAILED')
    # The official date filter for CS excludes the delisting day itself.
    # Keep the vendor's open-ended sentinel; never infer delisting from absence.
    explicit=frame.loc[~frame.de_listed_date.eq('0000-00-00'),'de_listed_date']
    delisted=pd.to_datetime(explicit,errors='coerce')
    if delisted.isna().any() or delisted.dt.date.le(pd.Timestamp(day).date()).any():
        raise rq.InputBlocked('IDENTITY_FULL_DELISTING_DATE_CONTRACT_FAILED')
    # Validate the complete native response before the existing SH/SZ mapping.
    # Legal BJSE identities remain in the raw original and exclusion evidence.
    selected=frame.exchange.isin(['XSHG','XSHE'])
    exclusions=frame.loc[~selected,['order_book_id','exchange','type']].copy()
    exclusions['reason']='OUTSIDE_EXISTING_SHSZ_CS_SCOPE'
    scope=dict(contract='RQDATA_NATIVE_CS_SHSZ_V1',raw_rows=len(frame),
        in_scope_rows=int(selected.sum()),excluded_rows=len(exclusions),
        exclusions=exclusions.to_dict('records'))
    projected=frame.loc[selected].copy()
    ids=projected.order_book_id.tolist()
    projected=_validate(projected,ids,day,list(projected.columns))
    if len(projected)<3000:
        raise rq.InputBlocked('IDENTITY_FULL_POOL_INCOMPLETE')
    projected.attrs['identity_scope']=scope
    return projected


def _full_checkpoint(checkpoint,day):
    """Recheck a complete full response without rewriting its evidence."""
    checkpoint=Path(checkpoint)
    state=_read(checkpoint)
    if (state.get('schema')!='RQDATA_IDENTITY_FULL_V1' or state.get('status')!='COMPLETE'
            or state.get('mode')!='FULL_HTTP_RESPONSE' or state.get('business_date')!=day
            or state.get('method')!='all_instruments' or state.get('http_status')!=200
            or state.get('parameters')!=dict(type='CS',date=day,market='cn') or state.get('raw_file')!='raw.csv'):
        raise rq.InputBlocked('IDENTITY_FULL_CHECKPOINT_CONTRACT_INVALID')
    raw=checkpoint.parent/'raw.csv'
    if not 0<raw.stat().st_size<=128*1024*1024 or _sha(raw)!=state.get('raw_sha256'):
        raise rq.InputBlocked('IDENTITY_FULL_ORIGINAL_HASH_OR_SIZE_MISMATCH')
    frame=_full_frame(raw.read_bytes(),day,state['framing'])
    ids=sorted(frame.order_book_id.tolist())
    expected=hashlib.sha256(json.dumps(ids,separators=(',',':')).encode('utf-8')).hexdigest()
    if (state.get('rows')!=len(frame) or state.get('target_list_sha256')!=expected
            or state.get('fields')!=list(frame.columns)):
        raise rq.InputBlocked('IDENTITY_FULL_POOL_OR_FIELD_BINDING_CHANGED')
    if (state.get('scope')!=frame.attrs['identity_scope']
            or state.get('scope_file')!='scope_exclusions.json'):
        raise rq.InputBlocked('IDENTITY_FULL_SCOPE_BINDING_CHANGED')
    scope_file=checkpoint.parent/state['scope_file']
    if (not scope_file.is_file() or _sha(scope_file)!=state.get('scope_sha256')
            or _read(scope_file)!=state['scope']['exclusions']):
        raise rq.InputBlocked('IDENTITY_FULL_SCOPE_ORIGINAL_HASH_OR_CONTENT_MISMATCH')
    first=pd.Timestamp(_observed(state['observed_range']['first'],day))
    last=pd.Timestamp(_observed(state['observed_range']['last'],day))
    if first>last:
        raise rq.InputBlocked('IDENTITY_FULL_OBSERVATION_RANGE_INVALID')
    frame.attrs['identity_capture']=_attrs(state,checkpoint)
    return state,frame


def capture_identity(provider,day,output,*,target_manifest=None,max_calls=None,seed=None,deadline=None):
    """One complete current-day snapshot; no prior pool or batch-row fallback.

    An already qualified same-day full response is reusable with its original
    observation range. Failed attempts stay intact and need a new output path.
    """
    day=_day(day)
    if day!=rq.clock().strftime('%Y%m%d'):
        raise rq.InputBlocked('IDENTITY_FULL_MUST_BE_ACTUAL_BUSINESS_DATE')
    rq.require_trading_date(day)
    if target_manifest is not None:
        raise rq.InputBlocked('IDENTITY_FULL_DOES_NOT_USE_PIT_TARGET_MANIFEST')
    out=Path(output).absolute()/'full'
    if out.resolve()!=out or not out.is_relative_to(ROOT/'reports'):
        raise rq.InputBlocked('IDENTITY_FULL_OUTPUT_MUST_STAY_IN_REPORTS')
    checkpoint=out/'checkpoint.json'
    if checkpoint.exists():
        return _full_checkpoint(checkpoint,day)[1]
    if out.exists() and any(out.iterdir()):
        raise rq.InputBlocked('IDENTITY_FULL_INCOMPLETE_ATTEMPT_RETAINED_USE_NEW_OUTPUT')
    global_checkpoint=ROOT/f'reports/identity_full/{day}/checkpoint.json'
    if seed is not None:
        if isinstance(seed,dict):
            raise rq.InputBlocked('IDENTITY_FULL_BATCH_SEED_FORBIDDEN')
        source=Path(seed)
        source=source/'checkpoint.json' if source.is_dir() else source
    else:
        source=global_checkpoint if global_checkpoint.exists() else None
    if source is not None:
        state,frame=_full_checkpoint(source,day)
        out.mkdir(parents=True,exist_ok=True)
        (out/'raw.csv').write_bytes((source.parent/'raw.csv').read_bytes())
        (out/'scope_exclusions.json').write_bytes((source.parent/'scope_exclusions.json').read_bytes())
        state=dict(state,full_seed=dict(manifest=str(source.resolve()),manifest_sha256=_sha(source)))
        _save(checkpoint,state)
        frame.attrs['identity_capture']=_attrs(state,checkpoint)
        return frame
    if max_calls is not None and int(max_calls)<1:
        raise rq.InputBlocked('IDENTITY_FULL_REQUEST_BUDGET_EXHAUSTED')
    if deadline is not None and pd.Timestamp(rq.clock())>=pd.Timestamp(deadline):
        raise rq.InputBlocked('IDENTITY_PHASE_DEADLINE_REACHED')
    started=rq.clock().isoformat()
    out.mkdir(parents=True,exist_ok=True)
    state=dict(schema='RQDATA_IDENTITY_FULL_V1',mode='FULL_HTTP_RESPONSE',business_date=day,
        method='all_instruments',parameters=dict(type='CS',date=day,market='cn'),status='STARTED',
        observed_range=dict(first=started,last=None),atomic_snapshot=False)
    _save(checkpoint,state)
    api_before=provider.state.get('identity_api_attempts',0)
    try:
        provider.call('all_instruments',_identity_request=True,type='CS',date=day,market='cn')
        state.update(http_status=getattr(provider,'_last_api_status',None),framing=dict(provider._response_metadata),
                     observed_range=dict(first=started,last=rq.clock().isoformat()))
        body=getattr(provider,'_last_api_body',None)
        if (state['http_status']!=200 or not isinstance(body,bytes)
                or not 0<len(body)<=128*1024*1024):
            raise rq.InputBlocked('IDENTITY_FULL_SUCCESS_ORIGINAL_BODY_MISSING')
        if any(secret and secret.encode('utf-8') in body for secret in
               (getattr(provider,'_license',None),getattr(provider,'_token',None))):
            raise rq.InputBlocked('IDENTITY_FULL_SECRET_CONTENT_FORBIDDEN')
        (out/'raw.csv').write_bytes(body)
        state.update(raw_file='raw.csv',raw_sha256=_sha(out/'raw.csv'))
        frame=_full_frame(body,day,state['framing'])
        ids=sorted(frame.order_book_id.tolist())
        _save(out/'scope_exclusions.json',frame.attrs['identity_scope']['exclusions'])
        state.update(status='COMPLETE',rows=len(frame),fields=list(frame.columns),
            scope=frame.attrs['identity_scope'],scope_file='scope_exclusions.json',
            scope_sha256=_sha(out/'scope_exclusions.json'),
            target_list_sha256=hashlib.sha256(json.dumps(ids,separators=(',',':')).encode('utf-8')).hexdigest())
        _observed(state['observed_range']['first'],day)
        _observed(state['observed_range']['last'],day)
        _save(checkpoint,state)
        frame.attrs['identity_capture']=_attrs(state,checkpoint)
        return frame
    except Exception as exc:
        state.update(status='FAILED',error_type=type(exc).__name__,
            error=str(exc) if isinstance(exc,(rq.InputBlocked,HTTPSTransportError)) or type(exc).__module__=='core.rqdata_https' else type(exc).__name__,
            details=getattr(exc,'details',{}),framing=dict(provider._response_metadata),
            observed_range=dict(first=started,last=rq.clock().isoformat()))
        api_started=provider.state.get('identity_api_attempts',0)>api_before
        body=getattr(provider,'_last_api_body',None) if api_started else None
        if body is None and api_started and provider._response_metadata.get('content_encoding')=='identity':
            body=getattr(provider,'_wire_body',None)
        if (isinstance(body,bytes) and len(body)<=128*1024*1024 and not body.lstrip().startswith(b'{')
                and not any(secret and secret.encode('utf-8') in body for secret in
                    (getattr(provider,'_license',None),getattr(provider,'_token',None)))):
            if not (out/'raw.csv').exists():
                (out/'raw.csv').write_bytes(body)
            state.update(raw_file='raw.csv',raw_sha256=_sha(out/'raw.csv'))
        _save(checkpoint,state)
        raise
