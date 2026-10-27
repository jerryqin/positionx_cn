"""Manual isolated preopen PIT capture; no Scheduler installation or production writes."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pandas as pd
from core import p0_rq_input as rq
from scripts import collect_postclose_candidate as close
from scripts import run_postclose_local_candidate as local


def output_root(value):
    p = Path(value).absolute()
    if p.resolve() != p or not p.is_relative_to(ROOT/'reports') or p == ROOT/'reports':
        raise ValueError('ISOLATED_REPORTS_OUTPUT_REQUIRED')
    return p

def preopen(day, output, provider=None):
    root = output_root(output)
    now = pd.Timestamp(rq.clock())
    rq.require_trading_date(day)
    if now.strftime('%Y%m%d') != day or now.strftime('%H%M%S') >= '092000':
        raise ValueError('ACTUAL_SAME_DAY_BEFORE_0920_REQUIRED')
    manifests = root/'pit/manifests'
    if list(manifests.glob(day+'T*_preopen.json')):
        local.membership({'pit_root':str(manifests)}, day)
        return {'status':'GREEN_PIT', 'reused':True, 'production':False}
    p = provider or close.bounded_provider()
    started = rq.clock().isoformat()
    capture = pd.Timestamp(started).strftime('%Y%m%dT%H%M%S+0800_preopen')
    frame = rq.membership_frame(p, started, capture)
    identity = close.old.normalize(p.all_instruments(type='CS', date=day))
    if len(identity) < 3000 or identity.order_book_id.duplicated().any():
        raise ValueError('PREOPEN_IDENTITY_INCOMPLETE')
    finished = rq.clock().isoformat()
    if pd.Timestamp(finished).strftime('%H%M%S') >= '092000':
        raise ValueError('CAPTURE_FINISHED_AFTER_0920')
    dest = root/'pit/raw'/day/'preopen'/capture
    dest.mkdir(parents=True, exist_ok=False)
    path = dest/'index_membership_rqdata.parquet'
    frame.to_parquet(path, index=False)
    identity.to_parquet(dest/'identity.parquet', index=False)
    items = [dict(dataset='index_membership_'+code, source='RQDATA_COMPONENTS_PROVIDER',
                  rows=int(frame.index_code.eq(code).sum()), source_max_date=day,
                  health='FRESH', file_path=str(path), sha256=rq.sha(path))
             for code in ('000300.SH','000905.SH','000852.SH')]
    manifest = dict(phase='preopen', status='SUCCESS', observed_at=started,
                    finished_at=finished, items=items, capture_id=capture,
                    identity_sha256=rq.sha(dest/'identity.parquet'))
    manifests.mkdir(parents=True, exist_ok=True)
    local.write_json(manifests/(capture+'.json'), manifest)
    local.membership({'pit_root':str(manifests)}, day)
    return {'status':'GREEN_PIT', 'members':len(frame), 'identity':len(identity),
            'capture_id':capture, 'production':False}

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--phase', required=True, choices=['preopen'])
    p.add_argument('--date', required=True)
    p.add_argument('--output', required=True)
    a = p.parse_args()
    root = output_root(a.output)
    receipt = root/(a.phase+'_'+a.date+'_result.json')
    if receipt.exists():
        raise ValueError('RESULT_EXISTS_PRESERVED_USE_NEW_ATTEMPT_OUTPUT')
    try:
        r = preopen(a.date, root)
    except (Exception, SystemExit) as e:
        r = dict(status='RED', source='RQDATA', date=a.date, error_type=type(e).__name__,
                 source_step=close.SOURCE_STEP, production=False)
    root.mkdir(parents=True, exist_ok=True)
    local.write_json(receipt, r)
    print(json.dumps(r, default=str))
    return 0 if r['status'].startswith('GREEN') else 3


if __name__ == '__main__':
    raise SystemExit(main())
