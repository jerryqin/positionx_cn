from __future__ import annotations
import argparse, hashlib, json, sys
from pathlib import Path
from datetime import datetime

PROJECT_ROOT=Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:sys.path.insert(0,str(PROJECT_ROOT))

from research.g4_forward_reservation_adapter import (
    POLICY_ID, DEEP_ID, RET120_ID, PendingSignal,
    infer_pending_statuses, reserve, fingerprint
)

DB=PROJECT_ROOT/"data/research/positionx_research.duckdb"
DEEP_STATE=PROJECT_ROOT/"data/research/forward_deep_oversold_csi500_v1_1/state.json"
RET_STATE=PROJECT_ROOT/"data/research/forward_ret120_csi500_v1_0/state.json"
G7=Path(r"D:\positionx_data\research\pilot_live_readiness\pilot_policy_freeze_v0_2_600k\pilot_policy_freeze_summary_v02_600k.json")
OUT=Path(r"D:\positionx_data\research\pilot_live_readiness\g4_forward_reservation_v0_2")

SOURCES=[
  {"strategy_id":DEEP_ID,"table":"forward_signals","state":DEEP_STATE},
  {"strategy_id":RET120_ID,"table":"ret120_forward_signals","state":RET_STATE},
]

def readj(p:Path):
    return json.loads(p.read_text(encoding="utf-8-sig")) if p.exists() else {}

def sha(p:Path):
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None

def connect(read_only:bool):
    import duckdb
    return duckdb.connect(str(DB),read_only=read_only)

def table_exists(con,t):
    return bool(con.execute("select 1 from information_schema.tables where table_schema='main' and table_name=?",[t]).fetchone())

def inspect_source(con,src):
    st=src["state_data"] if "state_data" in src else readj(src["state"])
    d=str(st.get("last_processed_date") or "").replace("-","")
    expected=int(st.get("pending_signals") or 0)
    if not d:
        return {"strategy_id":src["strategy_id"],"table":src["table"],"state_status":"NO_LAST_PROCESSED_DATE","expected_pending":expected}
    rows=con.execute(
      f"""select strategy_id,symbol,signal_date,status,reason,actual_entry_date
          from {src['table']}
          where strategy_id=? and signal_date=? and actual_entry_date is null
          order by symbol""",[src["strategy_id"],d]
    ).fetchall()
    status_counts={}
    for r in rows:status_counts[r[3]]=status_counts.get(r[3],0)+1
    try:
        pending_statuses=infer_pending_statuses(status_counts,expected)
        mapping="EXACT_UNIQUE_COUNT_RECONCILIATION"
        ok=True
    except Exception as e:
        pending_statuses=[]
        mapping=f"{type(e).__name__}: {e}"
        ok=False
    pending_rows=[r for r in rows if r[3] in pending_statuses]
    return {
      "strategy_id":src["strategy_id"],"table":src["table"],
      "state_file":str(src["state"]),"state_sha256":src["state_sha256"] if "state_sha256" in src else sha(src["state"]),
      "last_processed_date":d,"expected_pending":expected,
      "unentered_latest_signal_rows":len(rows),"status_counts":status_counts,
      "inferred_pending_statuses":pending_statuses,
      "inferred_pending_rows":len(pending_rows),
      "mapping_status":mapping,
      "mapping_ok":ok and len(pending_rows)==expected,
      "rows":[{"strategy_id":r[0],"symbol":r[1],"signal_date":r[2],"status":r[3],"reason":r[4]} for r in pending_rows],
    }

def ensure_tables(con):
    con.execute("""
      create table if not exists g4_shadow_reservation_candidates(
        shadow_decision_id varchar primary key,
        signal_date varchar not null,
        strategy_id varchar not null,
        symbol varchar not null,
        side varchar not null,
        source_table varchar not null,
        source_status varchar not null,
        source_reason varchar,
        sample_hash varchar not null,
        sample_rank integer,
        stage varchar not null,
        selected boolean not null,
        source_state_sha256 varchar not null,
        policy_id varchar not null,
        created_at timestamp default current_timestamp
      )
    """)
    con.execute("""
      create table if not exists g4_shadow_reservations(
        shadow_decision_id varchar primary key,
        signal_date varchar not null,
        strategy_id varchar not null,
        symbol varchar not null,
        side varchar not null,
        sample_hash varchar not null,
        sample_rank integer not null,
        stage varchar not null,
        reservation_status varchar not null,
        confirmed_entry_trade_date varchar,
        confirmed_shadow_notional_rmb double,
        pilot_target_notional_rmb double,
        source_state_sha256 varchar not null,
        policy_id varchar not null,
        created_at timestamp default current_timestamp,
        updated_at timestamp default current_timestamp
      )
    """)

def persist_idempotent(con,table,row,key="shadow_decision_id"):
    existing=con.execute(f"select * from {table} where {key}=?",[row[key]]).fetchone()
    if existing is not None:
        return "EXISTS"
    cols=list(row)
    ph=",".join(["?"]*len(cols))
    con.execute(f"insert into {table} ({','.join(cols)}) values ({ph})",[row[c] for c in cols])
    return "INSERTED"


def inspect_segments():
    import duckdb
    from core.qmt_runtime import segment_bindings
    paths=segment_bindings();dbs={DEEP_ID:paths['deep_db'],RET120_ID:paths['ret120_db']}
    records=[];day=datetime.now().strftime('%Y%m%d')
    for original in SOURCES:
        src=dict(original);db=dbs[src['strategy_id']];con=duckdb.connect(str(db),read_only=True)
        try:
            row=con.execute('SELECT state_json FROM forward_state WHERE strategy_id=?',[src['strategy_id']]).fetchone()
            state=json.loads(row[0]) if row else {}
            src.update(state_data=state,state=Path(db),state_sha256=hashlib.sha256(row[0].encode()).hexdigest() if row else None)
            record=inspect_source(con,src);record['current']=str(state.get('last_processed_date','')).replace('-','')==day
            records.append(record)
        finally:con.close()
    ok=all(x.get('mapping_ok') is True and x['current'] for x in records)
    print(json.dumps({'status':'PASS_G4_FORWARD_RESERVATION_V0_2_INSPECT' if ok else 'BLOCKED_G4_SEGMENT_INPUTS','sources':records,'safety':{'qmt':False,'broker':False,'research_db_read_only':True}},ensure_ascii=False,indent=2));return 0 if ok else 3

def inspect():
    from core.qmt_runtime import is_full
    if is_full():return inspect_segments()
    g7=readj(G7)
    con=connect(True)
    try:
        srcs=[inspect_source(con,s) for s in SOURCES]
        result={
          "status":"PASS_G4_FORWARD_RESERVATION_V0_2_INSPECT" if all(x.get("mapping_ok") for x in srcs) else "BLOCKED_G4_FORWARD_RESERVATION_V0_2_INSPECT",
          "generated_at":datetime.now().isoformat(timespec="seconds"),
          "g7_status":g7.get("status"),
          "sources":srcs,
          "safety":{"qmt":False,"broker":False,"production_db_opened":False,"research_db_read_only":True}
        }
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result["status"].startswith("PASS") else 3
    finally:con.close()

def bootstrap():
    con=connect(False)
    try:
        ensure_tables(con)
        result={"status":"PASS_G4_FORWARD_RESERVATION_V0_2_BOOTSTRAP","tables":["g4_shadow_reservation_candidates","g4_shadow_reservations"],
                "research_db":str(DB),"production_db_mutation":False,"qmt":False,"broker":False}
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    finally:con.close()

def reserve_cmd(stage):
    g7=readj(G7)
    if g7.get("status")!="PASS_G7_EXECUTION_PILOT_POLICY_V0_2_600K_FREEZE":
        print(json.dumps({"status":"BLOCKED_G7_NOT_FROZEN"},indent=2));return 3
    con=connect(False)
    try:
        ensure_tables(con)
        inspections=[inspect_source(con,s) for s in SOURCES]
        if not all(x.get("mapping_ok") for x in inspections):
            print(json.dumps({"status":"BLOCKED_PENDING_STATUS_MAPPING","sources":inspections},ensure_ascii=False,indent=2));return 3

        all_pending=[]
        state_hash_by_strategy={}
        for x in inspections:
            state_hash_by_strategy[x["strategy_id"]]=x["state_sha256"]
            for r in x["rows"]:
                all_pending.append(PendingSignal(r["strategy_id"],r["symbol"],r["signal_date"],r["status"],r.get("reason"),x["table"]))

        # There can be no reservation if strategies are on different latest signal dates.
        dates={x.signal_date for x in all_pending}
        if len(dates)>1:
            print(json.dumps({"status":"BLOCKED_MIXED_SIGNAL_DATES","dates":sorted(dates)},indent=2));return 3

        selected=reserve(all_pending,stage) if all_pending else []
        selected_ids={r["shadow_decision_id"] for r in selected}
        selected_rank={r["shadow_decision_id"]:r["sample_rank"] for r in selected}
        selected_hash={r["shadow_decision_id"]:r["sample_hash"] for r in selected}

        candidate_rows=[]
        for x in all_pending:
            from research.g4_forward_reservation_adapter import decision_id,reservation_hash
            did=decision_id(x)
            candidate_rows.append({
              "shadow_decision_id":did,"signal_date":x.signal_date,"strategy_id":x.strategy_id,"symbol":x.symbol,
              "side":"BUY","source_table":x.source_table or "","source_status":x.status,"source_reason":x.reason,
              "sample_hash":reservation_hash(x),"sample_rank":selected_rank.get(did),
              "stage":stage.upper(),"selected":did in selected_ids,
              "source_state_sha256":state_hash_by_strategy[x.strategy_id],"policy_id":POLICY_ID,
            })

        inserted_candidates=0;inserted_reservations=0
        con.execute("begin transaction")
        try:
            for r in candidate_rows:
                inserted_candidates += persist_idempotent(con,"g4_shadow_reservation_candidates",r)=="INSERTED"
            for r in selected:
                rr={
                  "shadow_decision_id":r["shadow_decision_id"],"signal_date":r["signal_date"],"strategy_id":r["strategy_id"],
                  "symbol":r["symbol"],"side":"BUY","sample_hash":r["sample_hash"],"sample_rank":r["sample_rank"],
                  "stage":r["stage"],"reservation_status":r["reservation_status"],
                  "confirmed_entry_trade_date":None,"confirmed_shadow_notional_rmb":None,"pilot_target_notional_rmb":None,
                  "source_state_sha256":state_hash_by_strategy[r["strategy_id"]],"policy_id":POLICY_ID,
                }
                inserted_reservations += persist_idempotent(con,"g4_shadow_reservations",rr)=="INSERTED"
            con.execute("commit")
        except Exception:
            con.execute("rollback");raise

        result={
          "status":"PASS_G4_FORWARD_RESERVATION_V0_2",
          "generated_at":datetime.now().isoformat(timespec="seconds"),
          "stage":stage.upper(),
          "pending_total":len(all_pending),"selected_count":len(selected),
          "inserted_candidates":inserted_candidates,"inserted_reservations":inserted_reservations,
          "selected":selected,"sources":inspections,
          "invariant":"No pilot notional is created at T close. T+1 open confirmation must set confirmed_shadow_notional before preview.",
          "safety":{"qmt":False,"broker":False,"production_db_opened":False,"production_db_mutation":False,"research_db_mutated":True}
        }
        OUT.mkdir(parents=True,exist_ok=True)
        tag=(next(iter(dates)) if dates else "NO_PENDING")
        (OUT/f"reservation_{tag}_{stage.lower()}.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(result,ensure_ascii=False,indent=2));print(f"OUTPUT: {OUT}")
        return 0
    finally:con.close()

def main():
    p=argparse.ArgumentParser()
    p.add_argument("command",choices=["inspect","bootstrap","reserve"])
    p.add_argument("--stage",choices=["CANARY","EXPANDED"],default="CANARY")
    a=p.parse_args()
    try:
        from core.qmt_runtime import is_full
        if is_full() and a.command!="inspect":raise RuntimeError("FULL_QMT_RESERVATION_WRITES_NOT_AUTHORIZED")
        if a.command=="inspect":return inspect()
        if a.command=="bootstrap":return bootstrap()
        return reserve_cmd(a.stage)
    except Exception as e:
        print(json.dumps({"status":"FAIL_CLOSED","error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2));return 3
if __name__=="__main__":raise SystemExit(main())
