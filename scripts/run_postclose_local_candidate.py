"""Local input helpers shared by isolated data collectors; no acquisition or ledger writes."""
from __future__ import annotations
import json
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]

def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))



def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                   allow_nan=False, default=str) + '\n', encoding='utf-8')



def read_db(path):
    import duckdb
    p = Path(path).resolve()
    if not p.is_file():
        raise ValueError('SOURCE_DATABASE_MISSING:' + str(p))
    return duckdb.connect(str(p), read_only=True, config={
        'temp_directory': '', 'autoinstall_known_extensions': 'false',
        'autoload_known_extensions': 'false', 'threads': '1'})



def sessions(calendar_path, day):
    import pandas as pd
    c = read_json(calendar_path)
    if not c['valid_from'] <= day <= c['valid_to']:
        raise ValueError('EXCHANGE_CALENDAR_OUT_OF_RANGE')
    dates = [d.strftime('%Y%m%d') for d in pd.date_range(c['valid_from'], day)
             if d.weekday() < 5 and not any(r['start'] <= d.strftime('%Y%m%d') <= r['end'] for r in c['closed_ranges'])]
    if not dates or dates[-1] != day:
        raise ValueError('NOT_APPROVED_TRADING_DATE')
    return dates



def membership(profile, day):
    import pandas as pd
    from core import p0_rq_input as rq
    from scripts.p0_core_recovery_v01 import validate_pit
    manifest_hash = validate_pit(profile, day)
    manifest_path = sorted(Path(profile['pit_root']).glob(day + 'T*_preopen.json'))[-1]
    manifest = read_json(manifest_path)
    path = Path(next(i['file_path'] for i in manifest['items'] if i['dataset'] == 'index_membership_000905.SH'))
    sets = rq.validate_preopen_membership(pd.read_parquet(path), day)
    return sets, {'path': str(path), 'sha256': rq.sha(path),
                  'manifest': str(manifest_path), 'manifest_sha256': manifest_hash}
