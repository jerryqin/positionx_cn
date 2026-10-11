from __future__ import annotations
import argparse,hashlib,json,os,sys,tempfile
from pathlib import Path
from datetime import datetime,timezone,timedelta

PROJECT_ROOT=Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:sys.path.insert(0,str(PROJECT_ROOT))

from research.g4_manual_approval import (
    POLICY_ID,APPROVAL_TTL_SECONDS,STAGE_DAILY_LIMIT,
    canonical_bound_fields,approval_token,freshness
)

PREVIEW_DIR=Path(r"D:\positionx_data\research\pilot_live_readiness\g4_isolated_preview_v0_4")
OUT=Path(r"D:\positionx_data\research\pilot_live_readiness\g4_manual_approval_v0_5")

def sha(p:Path):return hashlib.sha256(p.read_bytes()).hexdigest()
def readj(p:Path):return json.loads(p.read_text(encoding="utf-8-sig"))
def iso(dt):return dt.astimezone(timezone.utc).isoformat().replace("+00:00","Z")

def preview_path(decision_id:str)->Path:
    return PREVIEW_DIR/f"preview_{decision_id}.json"

def load(decision_id:str):
    p=preview_path(decision_id)
    if not p.exists():raise FileNotFoundError(p)
    obj=readj(p)
    h=sha(p)
    bound=canonical_bound_fields(obj,h)
    if bound["shadow_decision_id"]!=decision_id:
        raise RuntimeError("decision id mismatch between filename and preview payload")
    mt=datetime.fromtimestamp(p.stat().st_mtime,tz=timezone.utc)
    return p,obj,h,bound,mt

def existing_approvals():
    if not OUT.exists():return []
    rows=[]
    for p in sorted(OUT.glob("approval_*.json")):
        try:rows.append((p,readj(p)))
        except Exception:rows.append((p,{"_parse_error":True}))
    return rows

def daily_count(trade_date:str,stage:str):
    n=0
    for _,x in existing_approvals():
        b=x.get("bound_fields") or {}
        if str(b.get("trade_date"))==trade_date and str(b.get("stage")).upper()==stage.upper():
            n+=1
    return n

def check():
    result={
      "status":"PASS_G4_MANUAL_APPROVAL_CAPSULE_V0_5_CHECK",
      "preview_dir":str(PREVIEW_DIR),
      "approval_dir":str(OUT),
      "approval_ttl_seconds":APPROVAL_TTL_SECONDS,
      "daily_limits":STAGE_DAILY_LIMIT,
      "existing_approval_count":len(existing_approvals()),
      "contract":{
        "approval_is_explicit_cli_action":True,
        "approval_binds_preview_sha256":True,
        "approval_binds_order_identity":True,
        "production_db_write":False,
        "broker_write":False,
        "approval_capsule_only":True,
      }
    }
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0

def prepare(decision_id:str):
    p,obj,h,bound,mt=load(decision_id)
    now=datetime.now(timezone.utc)
    fr=freshness(mt,now)
    token=approval_token(bound)
    count=daily_count(bound["trade_date"],bound["stage"])
    limit=STAGE_DAILY_LIMIT[bound["stage"]]
    status="PASS_G4_MANUAL_APPROVAL_PREPARE_V0_5"
    reasons=[]
    if not fr["fresh"]:reasons.append("PREVIEW_EXPIRED")
    if count>=limit:reasons.append("DAILY_APPROVAL_CAP_REACHED")
    if reasons:status="BLOCKED_G4_MANUAL_APPROVAL_PREPARE_V0_5"
    result={
      "status":status,"preview_file":str(p),"preview_sha256":h,
      "preview_file_mtime_utc":iso(mt),"freshness":fr,
      "bound_fields":bound,
      "approval_token":token if not reasons else None,
      "approval_instruction":(
        f"python scripts\\run_g4_manual_approval_v05.py approve --decision-id {decision_id} --token {token}"
        if not reasons else None
      ),
      "daily_approval_count":count,"daily_limit":limit,"reasons":reasons,
      "safety":{"production_db_write":False,"broker_write":False}
    }
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if not reasons else 3

def approve(decision_id:str,token_arg:str):
    p,obj,h,bound,mt=load(decision_id)
    now=datetime.now(timezone.utc)
    fr=freshness(mt,now)
    if not fr["fresh"]:
        print(json.dumps({"status":"BLOCKED_G4_MANUAL_APPROVAL_V0_5","reason":"PREVIEW_EXPIRED","freshness":fr},indent=2));return 3
    expected=approval_token(bound)
    if str(token_arg)!=expected:
        print(json.dumps({"status":"BLOCKED_G4_MANUAL_APPROVAL_V0_5","reason":"TOKEN_MISMATCH"},indent=2));return 3

    OUT.mkdir(parents=True,exist_ok=True)
    out=OUT/f"approval_{decision_id}.json"
    capsule={
      "status":"APPROVED_AWAIT_PRODUCTION_COMMIT",
      "policy_id":POLICY_ID,
      "approved_at_utc":iso(now),
      "expires_at_utc":iso(mt+timedelta(seconds=APPROVAL_TTL_SECONDS)),
      "preview_file":str(p),
      "preview_file_sha256":h,
      "approval_token_sha256":hashlib.sha256(expected.encode("ascii")).hexdigest(),
      "bound_fields":bound,
      "manual_action":"User supplied exact full approval token on CLI.",
      "scope":"Approval capsule only; does not write production DB and does not submit/cancel broker orders.",
    }

    if out.exists():
        old=readj(out)
        if old.get("preview_file_sha256")==h and old.get("bound_fields")==bound:
            print(json.dumps({"status":"PASS_G4_MANUAL_APPROVAL_V0_5_ALREADY_EXISTS_EXACT",
                              "approval_file":str(out),"capsule":old},ensure_ascii=False,indent=2));return 0
        print(json.dumps({"status":"BLOCKED_G4_MANUAL_APPROVAL_V0_5",
                          "reason":"EXISTING_APPROVAL_MISMATCH","approval_file":str(out)},indent=2));return 3

    count=daily_count(bound["trade_date"],bound["stage"])
    limit=STAGE_DAILY_LIMIT[bound["stage"]]
    if count>=limit:
        print(json.dumps({"status":"BLOCKED_G4_MANUAL_APPROVAL_V0_5",
                          "reason":"DAILY_APPROVAL_CAP_REACHED","count":count,"limit":limit},indent=2));return 3

    fd,tmp=tempfile.mkstemp(prefix=out.name+".",suffix=".tmp",dir=str(OUT))
    os.close(fd)
    t=Path(tmp)
    t.write_text(json.dumps(capsule,ensure_ascii=False,indent=2),encoding="utf-8")
    os.replace(t,out)

    result={
      "status":"PASS_G4_MANUAL_APPROVAL_V0_5",
      "approval_file":str(out),
      "approval_file_sha256":sha(out),
      "capsule":capsule,
      "next":"A later production-commit adapter may consume this capsule only after revalidation. No production write occurred here.",
      "safety":{"production_db_write":False,"broker_write":False}
    }
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0

def main():
    p=argparse.ArgumentParser()
    p.add_argument("command",choices=["check","prepare","approve"])
    p.add_argument("--decision-id")
    p.add_argument("--token")
    a=p.parse_args()
    try:
        if a.command=="check":return check()
        if not a.decision_id:raise ValueError("--decision-id required")
        if a.command=="prepare":return prepare(a.decision_id)
        if not a.token:raise ValueError("--token required for approve")
        return approve(a.decision_id,a.token)
    except Exception as e:
        print(json.dumps({"status":"FAIL_CLOSED_G4_MANUAL_APPROVAL_V0_5","error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2));return 3

if __name__=="__main__":raise SystemExit(main())
