"""Explicit DEEP independent forward segment; never initialize from the Scheduler."""
from __future__ import annotations
import argparse,datetime as dt,json,pathlib
from research.forward_deep_oversold import DeepOversoldForwardPolicy,initial_state
from research.research_store import ResearchStore
from scripts.p0_core_recovery_v01 import calendar,previous_session,digest
ROOT=pathlib.Path(__file__).resolve().parents[1]
DEFAULT_PROFILE=ROOT/'config/ops/deep_forward_segment_v01.json'

def load_profile(path):
    p=json.loads(pathlib.Path(path).read_text(encoding='utf-8-sig'))
    if p.get('enabled') is not True:raise ValueError('DEEP_SEGMENT_DISABLED')
    if p.get('version')!='deep-independent-v0.1' or not calendar(p,p['start_date']):raise ValueError('DEEP_SEGMENT_PROFILE')
    root=pathlib.Path(p['segment_root']).resolve()
    for key in ('segment_db','output_dir','backup_root'):
        value=pathlib.Path(p[key]).resolve()
        if value==root or not value.is_relative_to(root):raise ValueError('DEEP_SEGMENT_PATH')
    if pathlib.Path(p['segment_db']).resolve()==(ROOT/'data/research/positionx_research.duckdb').resolve():raise ValueError('OLD_DATABASE_FORBIDDEN')
    return p

def initialize(p,authorized=False,now=None):
    if authorized is not True:raise ValueError('DEEP_INITIALIZATION_NOT_AUTHORIZED')
    if p.get('enabled') is not True or p.get('version')!='deep-independent-v0.1' or not calendar(p,p['start_date']):raise ValueError('DEEP_SEGMENT_PROFILE')
    checked_root=pathlib.Path(p['segment_root']).resolve()
    if any(not pathlib.Path(p[k]).resolve().is_relative_to(checked_root) or pathlib.Path(p[k]).resolve()==checked_root for k in ('segment_db','output_dir','backup_root')):raise ValueError('DEEP_SEGMENT_PATH')
    now=now or dt.datetime.now()
    if p['start_date']<=now.strftime('%Y%m%d'):raise ValueError('DEEP_START_MUST_BE_FUTURE')
    root=pathlib.Path(p['segment_root']);db=pathlib.Path(p['segment_db'])
    if db.exists() or root.exists() and any(root.iterdir()):raise ValueError('DEEP_SEGMENT_NOT_EMPTY')
    policy=DeepOversoldForwardPolicy();state=initial_state(policy)
    state.update(segment_start=p['start_date'],segment_boundary_cursor=previous_session(p,p['start_date']),last_processed_date=previous_session(p,p['start_date']))
    root.mkdir(parents=True,exist_ok=True)
    with ResearchStore(db) as store:
        store.con.execute('BEGIN TRANSACTION')
        try:
            store.con.execute('CREATE TABLE deep_recovery_segment(metadata VARCHAR NOT NULL)')
            store.con.execute('INSERT INTO deep_recovery_segment VALUES (?)',[json.dumps({'profile_sha256':digest(p),'policy_hash':policy.fingerprint(),'start_date':p['start_date'],'boundary_cursor_is_not_a_processed_session':True,'old_positions_or_signals_inherited':False})])
            store.save_state(policy.strategy_id,policy.fingerprint(),state)
            store.con.execute('COMMIT')
        except BaseException:store.con.execute('ROLLBACK');raise
    return {'status':'PASS_DEEP_SEGMENT_INITIALIZED','start_date':p['start_date'],'historical_forward_backfill':False}

def require_state(p,state,now=None,con=None):
    today=(now or dt.datetime.now()).strftime('%Y%m%d')
    if today<p['start_date']:raise ValueError('DEEP_SEGMENT_NOT_STARTED')
    db=pathlib.Path(p['segment_db'])
    if not db.is_file():raise ValueError('DEEP_SEGMENT_NOT_INITIALIZED')
    import duckdb
    owned=con is None
    if owned:con=duckdb.connect(str(db),read_only=True)
    try:rows=con.execute('SELECT metadata FROM deep_recovery_segment').fetchall()
    finally:
        if owned:con.close()
    if len(rows)!=1 or json.loads(rows[0][0])['profile_sha256']!=digest(p):raise ValueError('DEEP_SEGMENT_BINDING')
    if state.get('segment_start')!=p['start_date'] or state.get('segment_boundary_cursor')!=previous_session(p,p['start_date']):raise ValueError('DEEP_SEGMENT_STATE')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--profile',default=str(DEFAULT_PROFILE));parser.add_argument('--authorize-initialize',action='store_true')
    args=parser.parse_args();print(json.dumps(initialize(load_profile(args.profile),args.authorize_initialize)))
if __name__=='__main__':main()
