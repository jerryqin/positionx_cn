from __future__ import annotations

"""
PositionX Optimus Seed Debt Resolution V0.6
Read-only Registry/Canonicalization precondition collector.

This command performs NO mutation itself. It invokes only these existing
read-only CLI commands:
  candidate_registry status
  candidate_registry maintenance-queue
  canonicalization check
  canonicalization status
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CST = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]
OPTIMUS_ROOT = Path(r"D:\positionx_data\research\big_opportunity\optimus")

REGISTRY_SCRIPT = ROOT / "scripts" / "run_optimus_candidate_registry_v03.py"
CANON_SCRIPT = ROOT / "scripts" / "run_optimus_canonicalization_v05.py"
REGISTRY_SCRIPT_EXPECTED_SHA256 = "3955c6deb314bee4bf6821ae9cc0d2f09c606d0fbd695f34877afa3d4b782884"

DEFAULT_APPLY_PREVIEW = OPTIMUS_ROOT / "seed_debt_resolution_v0_6" / "apply_preview" / "20261009T220159+0800" / "APPLY_PREVIEW.json"
PRECONDITION_ROOT = OPTIMUS_ROOT / "seed_debt_resolution_v0_6" / "precondition"

EXPECTED_PLAN_ID = "OPTIMUS_SEED_DEBT_RESOLUTION_V0_6_PLAN_20261009"
EXPECTED_PLAN_SHA256 = "2c164036dd7dd44ded7211bf6ec33e9e69628e9ca8aa1fe88cfa195144d5a9a5"
EXPECTED_SOURCE_FREEZE_MANIFEST_SHA256 = "5a26a75e93375cdd50d050c349450027edb01748393a6307354527201004adca"
EXPECTED_PREVIEW_PAYLOAD_SHA256 = "1f890d7ea1ed12cf49919b937b8d502f4be60be73644d925bdd7e5e93e43a007"

EXPECTED_REGISTRY_DATA_SHA256 = "8e52129b22a2791908d63f4566c2b90651f42f311176238c9dc04b1a390bb6cd"
EXPECTED_LIFECYCLE = {"ACTIVE_CANONICAL": 17, "ACTIVE_SEED": 5}
EXPECTED_MAINTENANCE = {
    "provisional_evidence": 0,
    "seed_audit_debt": 5,
    "stale_canonical": 0,
    "discovery_only": 0,
}
EXPECTED_SEED_SYMBOLS = {
    "601100.SH", "603009.SH", "603119.SH", "601689.SH", "600699.SH"
}
EXPECTED_LATEST_BATCH_ID = "CB_41b0746b2bdb0a3658e01f70"
EXPECTED_LATEST_BATCH_RESULT_SHA256 = "46818ccec9df9c62e98d54cd26557ac6bb1694ef0ab791299a03a61b7640bf3f"


def now_cst() -> datetime:
    return datetime.now(CST)


def iso_now() -> str:
    return now_cst().isoformat(timespec="seconds")


def sha256_file(p: Path) -> str:
    h=hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda:f.read(1024*1024), b""):
            h.update(b)
    return h.hexdigest()


def stable_json(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",",":"), default=str)


def sha256_obj(x: Any) -> str:
    return hashlib.sha256(stable_json(x).encode("utf-8")).hexdigest()


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    os.replace(tmp,path)


def extract_json(stdout: str) -> dict[str, Any]:
    s=stdout.strip()
    try:
        x=json.loads(s)
        if isinstance(x,dict): return x
    except Exception:
        pass

    dec=json.JSONDecoder()
    found=[]
    for i,ch in enumerate(s):
        if ch!="{": continue
        try:
            x,end=dec.raw_decode(s[i:])
            if isinstance(x,dict):
                found.append(x)
        except Exception:
            continue
    if not found:
        raise RuntimeError(f"NO_JSON_OBJECT_IN_STDOUT: {s[-1200:]}")
    return found[-1]


def run_json(cmd: list[str]) -> dict[str, Any]:
    p=subprocess.run(cmd,cwd=ROOT,capture_output=True,text=True,encoding="utf-8",errors="replace")
    try:
        obj=extract_json(p.stdout)
    except Exception as e:
        obj={"_parse_error":repr(e)}
    return {
        "command":cmd,
        "returncode":p.returncode,
        "stdout":p.stdout,
        "stderr":p.stderr,
        "json":obj,
    }


def check_preview(path: Path) -> list[dict[str,Any]]:
    errs=[]
    if not path.exists():
        return [{"code":"APPLY_PREVIEW_MISSING","path":str(path)}]
    try:
        x=json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return [{"code":"APPLY_PREVIEW_INVALID_JSON","error":repr(e)}]

    checks=[
        ("status",x.get("status"),"READY_FOR_USER_APPROVAL"),
        ("preview_only",x.get("preview_only"),True),
        ("apply_executed",x.get("apply_executed"),False),
        ("plan_id",x.get("plan_id"),EXPECTED_PLAN_ID),
        ("plan_sha256",x.get("plan_sha256"),EXPECTED_PLAN_SHA256),
        ("source_freeze_manifest_sha256",x.get("source_freeze_manifest_sha256"),EXPECTED_SOURCE_FREEZE_MANIFEST_SHA256),
        ("preview_payload_sha256",x.get("preview_payload_sha256"),EXPECTED_PREVIEW_PAYLOAD_SHA256),
        ("review_decision_count",x.get("review_decision_count"),7),
        ("canonical_fact_count",x.get("canonical_fact_count"),6),
        ("lifecycle_transition_count",x.get("lifecycle_transition_count"),5),
        ("theme_stage_change",x.get("theme_stage_change"),False),
        ("all_tesla_links",x.get("all_tesla_links"),"T0"),
        ("required_user_action",x.get("required_user_action"),"EXPLICIT_APPROVAL_BEFORE_APPLY"),
    ]
    for field,actual,expected in checks:
        if actual!=expected:
            errs.append({"code":"APPLY_PREVIEW_FIELD_MISMATCH","field":field,"actual":actual,"expected":expected})
    ep=x.get("proposed_endpoint") or {}
    if ep!={"active_canonical":22,"active_seed":0,"discovery_only":0,"removed":0}:
        errs.append({"code":"APPLY_PREVIEW_ENDPOINT_MISMATCH","actual":ep})
    safety=x.get("safety") or {}
    for k in ("registry_opened","registry_mutation","canonicalization_mutation","db_opened","broker","orders","production_db"):
        if safety.get(k) is not False:
            errs.append({"code":"APPLY_PREVIEW_SAFETY_MISMATCH","field":k,"actual":safety.get(k)})
    return errs


def evaluate_registry_status(obj: dict[str,Any]) -> list[dict[str,Any]]:
    errs=[]
    if obj.get("status")!="PASS_OPTIMUS_CANDIDATE_REGISTRY_V0_3_STATUS":
        errs.append({"code":"REGISTRY_STATUS_NOT_PASS","actual":obj.get("status")})
    if obj.get("candidate_count")!=22:
        errs.append({"code":"CANDIDATE_COUNT_DRIFT","actual":obj.get("candidate_count"),"expected":22})
    if obj.get("registry_sha256")!=EXPECTED_REGISTRY_DATA_SHA256:
        errs.append({"code":"REGISTRY_DATA_SHA_DRIFT","actual":obj.get("registry_sha256"),"expected":EXPECTED_REGISTRY_DATA_SHA256})
    lc=obj.get("lifecycle_counts") or {}
    for k,v in EXPECTED_LIFECYCLE.items():
        if lc.get(k)!=v:
            errs.append({"code":"LIFECYCLE_COUNT_DRIFT","field":k,"actual":lc.get(k),"expected":v})
    maint=obj.get("maintenance") or {}
    for k,v in EXPECTED_MAINTENANCE.items():
        if k in maint and maint.get(k)!=v:
            errs.append({"code":"REGISTRY_MAINTENANCE_DRIFT","field":k,"actual":maint.get(k),"expected":v})
    return errs


def evaluate_maintenance(obj: dict[str,Any]) -> list[dict[str,Any]]:
    errs=[]
    if obj.get("status")!="PASS_OPTIMUS_MAINTENANCE_QUEUE_V0_3_1":
        errs.append({"code":"MAINTENANCE_STATUS_NOT_PASS","actual":obj.get("status")})
    sm=obj.get("summary") or {}
    for k,v in EXPECTED_MAINTENANCE.items():
        if sm.get(k)!=v:
            errs.append({"code":"MAINTENANCE_COUNT_DRIFT","field":k,"actual":sm.get(k),"expected":v})
    rows=obj.get("seed_audit_debt") or []
    got={str(x.get("symbol") or "") for x in rows}
    if got!=EXPECTED_SEED_SYMBOLS:
        errs.append({"code":"SEED_DEBT_SCOPE_DRIFT","actual":sorted(got),"expected":sorted(EXPECTED_SEED_SYMBOLS)})
    for x in rows:
        if x.get("lifecycle_state")!="ACTIVE_SEED":
            errs.append({"code":"SEED_NOT_ACTIVE_SEED","symbol":x.get("symbol"),"actual":x.get("lifecycle_state")})
        if x.get("state_basis")!="SEED_V0_2_BOOTSTRAP_UNVERIFIED":
            errs.append({"code":"SEED_BASIS_DRIFT","symbol":x.get("symbol"),"actual":x.get("state_basis")})
    return errs


def evaluate_canon_check(obj: dict[str,Any]) -> list[dict[str,Any]]:
    errs=[]
    status=str(obj.get("status") or "")
    if not status.startswith("PASS_OPTIMUS_CANONICALIZATION_"):
        errs.append({"code":"CANONICALIZATION_CHECK_NOT_PASS","actual":obj.get("status")})
    if obj.get("registry_compatible") is not True:
        errs.append({"code":"CANONICALIZATION_REGISTRY_INCOMPATIBLE","actual":obj.get("registry_compatible")})
    if obj.get("auto_apply") is not False:
        errs.append({"code":"CANONICALIZATION_AUTO_APPLY_UNSAFE","actual":obj.get("auto_apply")})
    if obj.get("apply_requires_exact_plan_sha256") is not True:
        errs.append({"code":"CANONICALIZATION_EXACT_HASH_GATE_MISSING","actual":obj.get("apply_requires_exact_plan_sha256")})
    if obj.get("research_only") is not True or obj.get("broker") is not False or obj.get("production_db") is not False:
        errs.append({"code":"CANONICALIZATION_SAFETY_DRIFT"})
    return errs


def evaluate_canon_status(obj: dict[str,Any]) -> list[dict[str,Any]]:
    errs=[]
    status=str(obj.get("status") or "")
    if not status.startswith("PASS_OPTIMUS_CANONICALIZATION_"):
        errs.append({"code":"CANONICALIZATION_STATUS_NOT_PASS","actual":obj.get("status")})
    latest=obj.get("latest_batch") or {}
    if latest.get("batch_id")!=EXPECTED_LATEST_BATCH_ID:
        errs.append({"code":"LATEST_BATCH_DRIFT","actual":latest.get("batch_id"),"expected":EXPECTED_LATEST_BATCH_ID})
    if latest.get("result_manifest_sha256")!=EXPECTED_LATEST_BATCH_RESULT_SHA256:
        errs.append({"code":"LATEST_BATCH_RESULT_SHA_DRIFT","actual":latest.get("result_manifest_sha256"),"expected":EXPECTED_LATEST_BATCH_RESULT_SHA256})
    # Strong expectations after Batch 3; if fields are exposed, require them.
    if "canonical_symbol_count" in obj and obj.get("canonical_symbol_count")!=17:
        errs.append({"code":"CANONICAL_SYMBOL_COUNT_DRIFT","actual":obj.get("canonical_symbol_count"),"expected":17})
    if "fact_count" in obj and obj.get("fact_count")!=18:
        errs.append({"code":"FACT_COUNT_DRIFT","actual":obj.get("fact_count"),"expected":18})
    maint=obj.get("maintenance") or {}
    for k,v in EXPECTED_MAINTENANCE.items():
        if k in maint and maint.get(k)!=v:
            errs.append({"code":"CANON_STATUS_MAINTENANCE_DRIFT","field":k,"actual":maint.get(k),"expected":v})
    return errs


def main() -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument("--apply-preview",default=str(DEFAULT_APPLY_PREVIEW))
    a=ap.parse_args()
    preview=Path(a.apply_preview)

    cap=PRECONDITION_ROOT/now_cst().strftime("%Y%m%dT%H%M%S+0800")
    cap.mkdir(parents=True,exist_ok=False)

    errors=[]
    errors.extend(check_preview(preview))

    script_checks={
        "registry_script":str(REGISTRY_SCRIPT),
        "registry_script_exists":REGISTRY_SCRIPT.exists(),
        "registry_script_sha256":sha256_file(REGISTRY_SCRIPT) if REGISTRY_SCRIPT.exists() else None,
        "registry_script_expected_sha256":REGISTRY_SCRIPT_EXPECTED_SHA256,
        "canonicalization_script":str(CANON_SCRIPT),
        "canonicalization_script_exists":CANON_SCRIPT.exists(),
        "canonicalization_script_sha256":sha256_file(CANON_SCRIPT) if CANON_SCRIPT.exists() else None,
    }
    if not REGISTRY_SCRIPT.exists() or script_checks["registry_script_sha256"]!=REGISTRY_SCRIPT_EXPECTED_SHA256:
        errors.append({"code":"REGISTRY_SCRIPT_SHA_MISMATCH",**script_checks})
    if not CANON_SCRIPT.exists():
        errors.append({"code":"CANONICALIZATION_SCRIPT_MISSING","path":str(CANON_SCRIPT)})

    py=sys.executable
    results={}
    commands={
        "registry_status":[py,str(REGISTRY_SCRIPT),"status"],
        "maintenance_queue":[py,str(REGISTRY_SCRIPT),"maintenance-queue"],
        "canonicalization_check":[py,str(CANON_SCRIPT),"check"],
        "canonicalization_status":[py,str(CANON_SCRIPT),"status"],
    }
    for name,cmd in commands.items():
        if not Path(cmd[1]).exists():
            results[name]={"command":cmd,"returncode":None,"stdout":"","stderr":"SCRIPT_MISSING","json":{}}
            continue
        r=run_json(cmd)
        results[name]=r
        if r["returncode"]!=0:
            errors.append({"code":"READ_ONLY_COMMAND_FAILED","name":name,"returncode":r["returncode"],"stderr":r["stderr"][-1200:]})

    if results.get("registry_status"):
        errors.extend(evaluate_registry_status(results["registry_status"].get("json") or {}))
    if results.get("maintenance_queue"):
        errors.extend(evaluate_maintenance(results["maintenance_queue"].get("json") or {}))
    if results.get("canonicalization_check"):
        errors.extend(evaluate_canon_check(results["canonicalization_check"].get("json") or {}))
    if results.get("canonicalization_status"):
        errors.extend(evaluate_canon_status(results["canonicalization_status"].get("json") or {}))

    status="PASS_OPTIMUS_SEED_DEBT_V0_6_PRECONDITION" if not errors else "BLOCKED_OPTIMUS_SEED_DEBT_V0_6_PRECONDITION"
    manifest={
        "status":status,
        "observed_at":iso_now(),
        "apply_preview":str(preview),
        "apply_preview_file_sha256":sha256_file(preview) if preview.exists() else None,
        "expected":{
            "plan_id":EXPECTED_PLAN_ID,
            "plan_sha256":EXPECTED_PLAN_SHA256,
            "source_freeze_manifest_sha256":EXPECTED_SOURCE_FREEZE_MANIFEST_SHA256,
            "preview_payload_sha256":EXPECTED_PREVIEW_PAYLOAD_SHA256,
            "registry_data_sha256":EXPECTED_REGISTRY_DATA_SHA256,
            "candidate_count":22,
            "lifecycle_counts":EXPECTED_LIFECYCLE,
            "maintenance":EXPECTED_MAINTENANCE,
            "seed_symbols":sorted(EXPECTED_SEED_SYMBOLS),
            "latest_batch_id":EXPECTED_LATEST_BATCH_ID,
            "latest_batch_result_sha256":EXPECTED_LATEST_BATCH_RESULT_SHA256,
        },
        "script_checks":script_checks,
        "results":results,
        "errors":errors,
        "next_gate":"EXPLICIT_USER_APPROVAL_FOR_APPLY" if not errors else "RESOLVE_PRECONDITION_DRIFT",
        "safety":{
            "collector_mutation":False,
            "called_commands_read_only":True,
            "registry_mutation":False,
            "canonicalization_mutation":False,
            "broker":False,
            "orders":False,
            "production_db":False,
        },
    }
    manifest["manifest_payload_sha256"]=sha256_obj(manifest)
    out=cap/"PRECONDITION.json"
    write_json(out,manifest)

    print(json.dumps({
        "status":status,
        "output":str(cap),
        "precondition":str(out),
        "error_count":len(errors),
        "errors":errors,
        "next_gate":manifest["next_gate"],
        "manifest_payload_sha256":manifest["manifest_payload_sha256"],
        "registry_mutation":False,
        "canonicalization_mutation":False,
    },ensure_ascii=False,indent=2))
    return 0 if not errors else 4


if __name__=="__main__":
    raise SystemExit(main())
