from __future__ import annotations
import argparse, json, os, subprocess, sys
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT=Path(__file__).resolve().parents[1]
DATA_ROOT=Path(r"D:\positionx_data\research\big_opportunity\optimus\scheduled_current22_v0_7_4")
CST=timezone(timedelta(hours=8))

def child_env()->dict[str,str]:
    env=os.environ.copy()
    env["PYTHONIOENCODING"]="utf-8"
    env["PYTHONUTF8"]="1"
    return env

def extract_json(stdout:str)->dict[str,Any]:
    i=stdout.find("{")
    if i<0:
        raise RuntimeError("NO_JSON_OBJECT_IN_STDOUT")
    return json.loads(stdout[i:])

def run_json(rel:str,*args:str)->tuple[dict[str,Any],str,str,int]:
    p=PROJECT_ROOT/rel
    r=subprocess.run(
        [sys.executable,str(p),*args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        env=child_env(),
    )
    if r.returncode!=0:
        raise RuntimeError(
            f"COMMAND_FAILED {rel} {' '.join(args)} rc={r.returncode} stderr={r.stderr}"
        )
    return extract_json(r.stdout),r.stdout,r.stderr,r.returncode

def previous_date_max()->date|None:
    try:
        x,_,_,_=run_json("scripts/run_optimus_prospective_intake_v071.py","status")
    except Exception:
        return None
    raw=x.get("date_max")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None

def compute_days(today:date,last_date:date|None,min_overlap_days:int,max_catchup_days:int)->tuple[int,dict[str,Any]]:
    if min_overlap_days<1:
        raise ValueError("min_overlap_days must be >=1")
    if max_catchup_days<min_overlap_days:
        raise ValueError("max_catchup_days must be >= min_overlap_days")
    if last_date is None:
        days=min_overlap_days
        return days,{"last_success_date_max":None,"gap_days":None,"reason":"NO_PRIOR_STATUS_USE_MIN_OVERLAP"}
    gap=(today-last_date).days
    if gap<0:
        raise RuntimeError(f"LAST_DATE_MAX_IN_FUTURE last={last_date} today={today}")
    # Inclusive window from previous date_max through today. Enforce at least a
    # 2-calendar-day overlap so late announcements from yesterday are rechecked.
    days=max(min_overlap_days,gap+1)
    if days>max_catchup_days:
        raise RuntimeError(
            f"CATCHUP_WINDOW_EXCEEDED required_days={days} max={max_catchup_days}; "
            f"run a reviewed manual backfill before resuming scheduler"
        )
    return days,{"last_success_date_max":last_date.isoformat(),"gap_days":gap,"reason":"DYNAMIC_CATCHUP_WITH_OVERLAP"}

def attention_file()->Path:
    return DATA_ROOT/"attention_pending.json"

def load_attention()->dict[str,Any]:
    p=attention_file()
    if not p.exists():
        return {"schema_version":"OPTIMUS_CURRENT22_ATTENTION_V0_7_4","pending":[],"acknowledged":[]}
    return json.loads(p.read_text(encoding="utf-8"))

def save_attention(x:dict[str,Any])->None:
    DATA_ROOT.mkdir(parents=True,exist_ok=True)
    attention_file().write_text(json.dumps(x,ensure_ascii=False,indent=2,default=str),encoding="utf-8")

def merge_attention(proposals:list[dict[str,Any]],seen_at:str)->dict[str,Any]:
    x=load_attention()
    pending={str(p.get("evidence_id")):p for p in x.get("pending",[]) if p.get("evidence_id")}
    for p in proposals:
        eid=str(p.get("evidence_id") or "")
        if not eid:
            continue
        if eid in pending:
            pending[eid]["latest_seen_at"]=seen_at
            pending[eid]["latest_queue_state"]=p.get("queue_state")
        else:
            pending[eid]={
                "evidence_id":eid,
                "symbol":p.get("symbol"),
                "company":p.get("company"),
                "queue_state":p.get("queue_state"),
                "evidence_level":p.get("evidence_level"),
                "tesla_link_level":p.get("tesla_link_level"),
                "source_ref":p.get("source_ref"),
                "event_time":p.get("event_time"),
                "first_seen_at":seen_at,
                "latest_seen_at":seen_at,
                "latest_queue_state":p.get("queue_state"),
            }
    x["pending"]=sorted(pending.values(),key=lambda z:(str(z.get("symbol")),str(z.get("evidence_id"))))
    save_attention(x)
    return x

def acknowledge_attention(evidence_id:str)->dict[str,Any]:
    x=load_attention()
    pending=x.get("pending",[])
    keep=[]; moved=[]
    for p in pending:
        if str(p.get("evidence_id"))==evidence_id:
            q=dict(p)
            q["acknowledged_at"]=datetime.now(CST).isoformat(timespec="seconds")
            moved.append(q)
        else:
            keep.append(p)
    if not moved:
        raise RuntimeError(f"ATTENTION_EVIDENCE_ID_NOT_PENDING {evidence_id}")
    x["pending"]=keep
    x.setdefault("acknowledged",[]).extend(moved)
    save_attention(x)
    return {
        "status":"PASS_OPTIMUS_CURRENT22_ATTENTION_ACK_V0_7_4",
        "evidence_id":evidence_id,
        "pending_attention_count":len(keep),
        "mutation":False,
    }

def write_log(payload:dict[str,Any])->Path:
    now=datetime.now(CST)
    out=DATA_ROOT/now.strftime("%Y%m%d")
    out.mkdir(parents=True,exist_ok=True)
    p=out/(now.strftime("%Y%m%dT%H%M%S+0800")+"_scheduled.json")
    p.write_text(json.dumps(payload,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    latest=DATA_ROOT/"latest_scheduled.json"
    latest.write_text(json.dumps(payload,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    return p

def run(min_overlap_days:int,max_catchup_days:int)->dict[str,Any]:
    started=datetime.now(CST)
    audit={"started_at":started.isoformat(timespec="seconds"),"steps":[]}

    pre,so,se,rc=run_json("scripts/run_optimus_steady_state_v074.py","check")
    audit["steps"].append({"name":"steady_state_check","result":pre})
    if pre.get("status")!="PASS_OPTIMUS_STEADY_STATE_V0_7_4_CHECK":
        raise RuntimeError(f"STEADY_STATE_PREFLIGHT_NOT_PASS {pre}")

    today=datetime.now(CST).date()
    last=previous_date_max()
    days,catchup=compute_days(today,last,min_overlap_days,max_catchup_days)
    audit["catchup"]={**catchup,"today":today.isoformat(),"days":days}

    intake,so,se,rc=run_json(
        "scripts/run_optimus_prospective_intake_v071.py","daily","--days",str(days)
    )
    audit["steps"].append({"name":"current22_intake","result":intake})
    if intake.get("status")!="PASS_OPTIMUS_PROSPECTIVE_INTAKE_V0_7_1":
        raise RuntimeError(f"INTAKE_NOT_PASS {intake}")
    if intake.get("quality")!="PASS":
        raise RuntimeError(f"INTAKE_QUALITY_NOT_PASS {intake.get('quality')}")
    if intake.get("query_errors"):
        raise RuntimeError(f"INTAKE_QUERY_ERRORS {intake.get('query_errors')}")

    status,_,_,_=run_json("scripts/run_optimus_prospective_intake_v071.py","status")
    queue,_,_,_=run_json("scripts/run_optimus_prospective_intake_v071.py","queue")
    audit["steps"].append({"name":"intake_status","result":status})
    audit["steps"].append({"name":"intake_queue","result":queue})

    proposals=int(queue.get("proposal_count") or 0)
    attention=merge_attention(queue.get("proposals") or [],datetime.now(CST).isoformat(timespec="seconds"))
    pending_attention_count=len(attention.get("pending") or [])
    result={
        "status":"PASS_OPTIMUS_CURRENT22_SCHEDULED_V0_7_4",
        "started_at":started.isoformat(timespec="seconds"),
        "finished_at":datetime.now(CST).isoformat(timespec="seconds"),
        "catchup":audit["catchup"],
        "announcement_count":int(intake.get("announcement_count") or 0),
        "proposal_count":proposals,
        "new_attention_required":proposals>0,
        "pending_attention_count":pending_attention_count,
        "attention_required":pending_attention_count>0,
        "attention_file":str(attention_file()),
        "queue_file":queue.get("queue_file"),
        "manifest_payload_sha256":intake.get("manifest_payload_sha256"),
        "registry_mutation":False,
        "canonicalization_mutation":False,
        "theme_stage_mutation":False,
        "broker":False,
        "orders":False,
        "production_db":False,
        "audit":audit,
    }
    path=write_log(result)
    result["scheduled_log"]=str(path)
    return result

def main()->int:
    ap=argparse.ArgumentParser()
    sp=ap.add_subparsers(dest="command",required=True)
    r=sp.add_parser("run")
    r.add_argument("--min-overlap-days",type=int,default=2)
    r.add_argument("--max-catchup-days",type=int,default=30)
    sp.add_parser("status")
    sp.add_parser("attention")
    ack=sp.add_parser("ack-attention")
    ack.add_argument("--evidence-id",required=True)
    a=ap.parse_args()
    try:
        if a.command=="status":
            p=DATA_ROOT/"latest_scheduled.json"
            if not p.exists():
                out={"status":"NO_OPTIMUS_CURRENT22_SCHEDULED_RUN_YET","mutation":False}
            else:
                out=json.loads(p.read_text(encoding="utf-8"))
        elif a.command=="attention":
            x=load_attention()
            out={
                "status":"PASS_OPTIMUS_CURRENT22_ATTENTION_STATUS_V0_7_4",
                "pending_attention_count":len(x.get("pending") or []),
                "pending":x.get("pending") or [],
                "attention_file":str(attention_file()),
                "mutation":False,
            }
        elif a.command=="ack-attention":
            out=acknowledge_attention(a.evidence_id)
        else:
            out=run(a.min_overlap_days,a.max_catchup_days)
        print(json.dumps(out,ensure_ascii=False,indent=2,default=str))
        return 0
    except Exception as e:
        fail={
            "status":"FAIL_OPTIMUS_CURRENT22_SCHEDULED_V0_7_4",
            "failed_at":datetime.now(CST).isoformat(timespec="seconds"),
            "error":repr(e),
            "registry_mutation":False,
            "canonicalization_mutation":False,
            "theme_stage_mutation":False,
            "broker":False,
            "orders":False,
            "production_db":False,
        }
        try:
            p=write_log(fail)
            fail["scheduled_log"]=str(p)
        except Exception:
            pass
        print(json.dumps(fail,ensure_ascii=False,indent=2,default=str))
        return 2

if __name__=="__main__":
    raise SystemExit(main())
