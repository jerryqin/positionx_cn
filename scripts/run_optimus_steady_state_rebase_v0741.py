from __future__ import annotations
import argparse, hashlib, json, os, shutil, subprocess, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT=Path(__file__).resolve().parents[1]
CONFIG=PROJECT_ROOT/"config"/"research"/"optimus_steady_state_rebase_v0741.json"
CST=timezone(timedelta(hours=8))

def now_cst()->datetime:
    return datetime.now(CST)

def sha(p:Path)->str:
    h=hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""): h.update(b)
    return h.hexdigest()

def stable_sha(x:Any)->str:
    s=json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(",",":"),default=str)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def load_cfg()->dict[str,Any]:
    return json.loads(CONFIG.read_text(encoding="utf-8"))

def child_env()->dict[str,str]:
    e=os.environ.copy()
    e["PYTHONIOENCODING"]="utf-8"
    e["PYTHONUTF8"]="1"
    return e

def run_json(rel:str,*args:str)->dict[str,Any]:
    p=PROJECT_ROOT/rel
    r=subprocess.run(
        [sys.executable,str(p),*args],
        cwd=PROJECT_ROOT,capture_output=True,text=True,
        encoding="utf-8",errors="strict",env=child_env()
    )
    if r.returncode!=0:
        raise RuntimeError(f"COMMAND_FAILED {rel} {' '.join(args)} rc={r.returncode} stderr={r.stderr}")
    i=r.stdout.find("{")
    if i<0:
        raise RuntimeError(f"NO_JSON_OUTPUT {rel}")
    return json.loads(r.stdout[i:])

def verify_dependencies(cfg:dict[str,Any])->dict[str,str]:
    actual={}
    for key,d in cfg["dependencies"].items():
        p=PROJECT_ROOT/d["path"]
        if not p.exists():
            raise RuntimeError(f"DEPENDENCY_MISSING {key} {p}")
        got=sha(p); actual[key]=got
        if got!=d["sha256"]:
            raise RuntimeError(f"DEPENDENCY_SHA_MISMATCH {key} actual={got} expected={d['sha256']}")
    return actual

def canonical_status()->dict[str,Any]:
    return run_json("scripts/run_optimus_canonicalization_v05.py","status")

def registry_status()->dict[str,Any]:
    return run_json("scripts/run_optimus_candidate_registry_v03.py","status")

def verify_post_state(cfg:dict[str,Any])->dict[str,Any]:
    deps=verify_dependencies(cfg)
    r=registry_status()
    c=canonical_status()
    p=cfg["post_apply"]

    checks={
        "registry_sha256":r.get("registry_sha256")==p["registry_sha256"],
        "candidate_count":int(r.get("candidate_count",-1))==p["candidate_count"],
        "active_canonical":int((r.get("lifecycle_counts") or {}).get("ACTIVE_CANONICAL",0))==p["active_canonical"],
        "discovery_only":int((r.get("lifecycle_counts") or {}).get("DISCOVERY_ONLY",0))==p["discovery_only"],
        "canonical_candidates":int(r.get("canonical_candidates",-1))==p["canonical_candidates"],
        "needs_review_candidates":int(r.get("needs_review_candidates",-1))==p["needs_review_candidates"],
        "evidence_count":int(r.get("evidence_count",-1))==p["evidence_count"],
        "review_decision_count":int(r.get("review_decision_count",-1))==p["review_decision_count"],
        "provisional_evidence_count":int(r.get("provisional_evidence_count",-1))==p["provisional_evidence_count"],
        "fact_count":int(c.get("fact_count",-1))==p["canonical_fact_count"],
        "canonical_symbol_count":int(c.get("canonical_symbol_count",-1))==p["canonical_symbol_count"],
        "materialized_fact_evidence_count":int(c.get("materialized_fact_evidence_count",-1))==p["materialized_fact_evidence_count"],
    }
    latest=c.get("latest_batch") or {}
    checks.update({
        "latest_batch_id":latest.get("batch_id")==p["batch_id"],
        "latest_plan_sha":latest.get("plan_sha256")==p["approved_plan_sha256"],
        "latest_result_manifest_sha":latest.get("result_manifest_sha256")==p["result_manifest_sha256"],
    })
    bad=[k for k,v in checks.items() if not v]
    if bad:
        raise RuntimeError(f"POST_APPLY_STATE_MISMATCH {bad}")

    return {"registry":r,"canonicalization":c,"dependency_sha256":deps}

def build_new_baseline(cfg:dict[str,Any],old:dict[str,Any])->dict[str,Any]:
    p=cfg["post_apply"]
    x=json.loads(json.dumps(old,ensure_ascii=False))
    x["baseline_id"]="OPTIMUS_STEADY_STATE_V0_7_4_1_POST_LEISAI_CANONICALIZATION_FREEZE_BASELINE"
    x["baseline_date"]="2026-10-10"
    x["mode"]="STEADY_STATE_OPERATIONS"
    x["previous_baseline_sha256"]=cfg["previous_baseline"]["sha256"]

    r=x["registry"]
    r["registry_sha256"]=p["registry_sha256"]
    r["candidate_count"]=p["candidate_count"]
    r["active_canonical"]=p["active_canonical"]
    r["discovery_only"]=p["discovery_only"]
    r["active_seed"]=p["active_seed"]
    r["canonical_candidates"]=p["canonical_candidates"]
    r["needs_review_candidates"]=p["needs_review_candidates"]
    r["evidence_count"]=p["evidence_count"]
    r["review_decision_count"]=p["review_decision_count"]
    r["provisional_evidence_count"]=p["provisional_evidence_count"]
    r["canonical_fact_count"]=p["canonical_fact_count"]
    r["materialized_fact_evidence_count"]=p["materialized_fact_evidence_count"]
    r["f_status_counts"]=p["f_status_counts"]
    r["maintenance_counts"]=p["maintenance_counts"]

    x.setdefault("frozen_history",{})["v0_5_4_leisai_canonicalization"]={
        "status":"FROZEN_APPLIED_DO_NOT_RERUN",
        "approved_plan_sha256":p["approved_plan_sha256"],
        "batch_id":p["batch_id"],
        "fact_id":p["fact_id"],
        "materialized_evidence_id":p["materialized_evidence_id"],
        "result_manifest_sha256":p["result_manifest_sha256"],
        "post_registry_sha256":p["registry_sha256"],
        "theme_stage_change":False,
        "tesla_supplier_claim":False,
    }
    x["last_governance_event"]={
        "type":"CANONICAL_FACT_APPLY",
        "symbol":"002979.SZ",
        "batch_id":p["batch_id"],
        "fact_id":p["fact_id"],
        "result_manifest_sha256":p["result_manifest_sha256"],
    }
    return x

def intended(cfg:dict[str,Any])->tuple[dict[str,Any],str]:
    bp=PROJECT_ROOT/cfg["previous_baseline"]["path"]
    if not bp.exists():
        raise RuntimeError(f"BASELINE_MISSING {bp}")
    current_sha=sha(bp)

    if current_sha==cfg["previous_baseline"]["sha256"]:
        old=json.loads(bp.read_text(encoding="utf-8"))
        new=build_new_baseline(cfg,old)
        raw=(json.dumps(new,ensure_ascii=False,indent=2)+"\n").encode("utf-8")
        return new,hashlib.sha256(raw).hexdigest()

    current=json.loads(bp.read_text(encoding="utf-8"))
    if current.get("baseline_id")=="OPTIMUS_STEADY_STATE_V0_7_4_1_POST_LEISAI_CANONICALIZATION_FREEZE_BASELINE":
        raw=bp.read_bytes()
        return current,hashlib.sha256(raw).hexdigest()

    raise RuntimeError(
        f"UNEXPECTED_BASELINE_SHA actual={current_sha} expected_old={cfg['previous_baseline']['sha256']}"
    )

def check(cfg:dict[str,Any])->dict[str,Any]:
    state=verify_post_state(cfg)
    bp=PROJECT_ROOT/cfg["previous_baseline"]["path"]
    current_sha=sha(bp)
    new,new_sha=intended(cfg)
    return {
        "status":"PASS_OPTIMUS_STEADY_STATE_V0_7_4_1_REBASE_CHECK",
        "current_baseline_sha256":current_sha,
        "intended_baseline_sha256":new_sha,
        "intended_baseline_id":new["baseline_id"],
        "post_registry_sha256":state["registry"]["registry_sha256"],
        "canonical_fact_count":state["canonicalization"]["fact_count"],
        "materialized_fact_evidence_count":state["canonicalization"]["materialized_fact_evidence_count"],
        "latest_batch":state["canonicalization"].get("latest_batch"),
        "mutation":False,
        "registry_mutation":False,
        "canonicalization_mutation":False,
        "theme_stage_mutation":False,
    }

def apply(cfg:dict[str,Any])->dict[str,Any]:
    state=verify_post_state(cfg)
    bp=PROJECT_ROOT/cfg["previous_baseline"]["path"]
    current_sha=sha(bp)
    new,new_sha=intended(cfg)

    if current_sha==new_sha and new.get("baseline_id")=="OPTIMUS_STEADY_STATE_V0_7_4_1_POST_LEISAI_CANONICALIZATION_FREEZE_BASELINE":
        return {
            "status":"ALREADY_APPLIED_OPTIMUS_STEADY_STATE_V0_7_4_1_REBASE",
            "baseline_sha256":current_sha,
            "mutation":False,
        }

    if current_sha!=cfg["previous_baseline"]["sha256"]:
        raise RuntimeError(f"REFUSE_BASELINE_PREIMAGE_MISMATCH actual={current_sha}")

    out_root=Path(cfg["audit_output_root"])
    out=out_root/now_cst().strftime("%Y%m%dT%H%M%S+0800")
    out.mkdir(parents=True,exist_ok=False)
    backup=out/"optimus_steady_state_baseline_v074.previous.json"
    shutil.copy2(bp,backup)
    if sha(backup)!=cfg["previous_baseline"]["sha256"]:
        raise RuntimeError("BASELINE_BACKUP_SHA_MISMATCH")

    tmp=bp.with_suffix(bp.suffix+".tmp")
    tmp.write_text(json.dumps(new,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    if sha(tmp)!=new_sha:
        raise RuntimeError("INTENDED_BASELINE_SHA_MISMATCH")
    os.replace(tmp,bp)

    # Real checker must now pass against the rebased baseline.
    steady=run_json("scripts/run_optimus_steady_state_v074.py","check")
    if steady.get("status")!="PASS_OPTIMUS_STEADY_STATE_V0_7_4_CHECK":
        shutil.copy2(backup,bp)
        raise RuntimeError(f"REBASED_STEADY_CHECK_FAILED_AND_BASELINE_RESTORED {steady}")

    manifest={
        "schema_version":"OPTIMUS_STEADY_STATE_REBASE_V0_7_4_1_MANIFEST",
        "rebase_id":cfg["rebase_id"],
        "applied_at":now_cst().isoformat(timespec="seconds"),
        "previous_baseline_sha256":cfg["previous_baseline"]["sha256"],
        "new_baseline_sha256":sha(bp),
        "new_baseline_id":new["baseline_id"],
        "post_registry_sha256":cfg["post_apply"]["registry_sha256"],
        "batch_id":cfg["post_apply"]["batch_id"],
        "fact_id":cfg["post_apply"]["fact_id"],
        "result_manifest_sha256":cfg["post_apply"]["result_manifest_sha256"],
        "backup_path":str(backup),
        "steady_state_check":steady,
        "registry_mutation":False,
        "canonicalization_mutation":False,
        "theme_stage_mutation":False,
    }
    manifest["manifest_payload_sha256"]=stable_sha(manifest)
    mp=out/"MANIFEST.json"
    mp.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    return {
        "status":"PASS_OPTIMUS_STEADY_STATE_V0_7_4_1_REBASE_APPLY",
        "output":str(out),
        "previous_baseline_sha256":cfg["previous_baseline"]["sha256"],
        "new_baseline_sha256":sha(bp),
        "new_baseline_id":new["baseline_id"],
        "post_registry_sha256":cfg["post_apply"]["registry_sha256"],
        "steady_state_status":steady["status"],
        "manifest_payload_sha256":manifest["manifest_payload_sha256"],
        "mutation":"BASELINE_CONFIG_ONLY",
        "registry_mutation":False,
        "canonicalization_mutation":False,
        "theme_stage_mutation":False,
        "broker":False,
        "orders":False,
        "production_db":False,
    }

def status(cfg:dict[str,Any])->dict[str,Any]:
    bp=PROJECT_ROOT/cfg["previous_baseline"]["path"]
    x=json.loads(bp.read_text(encoding="utf-8"))
    return {
        "status":"PASS_OPTIMUS_STEADY_STATE_V0_7_4_1_REBASE_STATUS",
        "baseline_id":x.get("baseline_id"),
        "baseline_sha256":sha(bp),
        "registry_sha256":(x.get("registry") or {}).get("registry_sha256"),
        "canonical_fact_count":(x.get("registry") or {}).get("canonical_fact_count"),
        "materialized_fact_evidence_count":(x.get("registry") or {}).get("materialized_fact_evidence_count"),
        "last_governance_event":x.get("last_governance_event"),
        "mutation":False,
    }

def main()->int:
    ap=argparse.ArgumentParser()
    ap.add_argument("command",choices=["check","apply","status"])
    a=ap.parse_args()
    cfg=load_cfg()
    try:
        out=check(cfg) if a.command=="check" else apply(cfg) if a.command=="apply" else status(cfg)
        print(json.dumps(out,ensure_ascii=False,indent=2,default=str))
        return 0
    except Exception as e:
        print(json.dumps({
            "status":f"FAIL_OPTIMUS_STEADY_STATE_V0_7_4_1_REBASE_{a.command.upper()}",
            "error":repr(e),
            "registry_mutation":False,
            "canonicalization_mutation":False,
            "theme_stage_mutation":False,
            "broker":False,
            "orders":False,
            "production_db":False,
        },ensure_ascii=False,indent=2))
        return 2

if __name__=="__main__":
    raise SystemExit(main())
