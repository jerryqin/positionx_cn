from __future__ import annotations
import argparse, hashlib, json, os, shutil, subprocess, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT=Path(__file__).resolve().parents[1]
CONFIG=PROJECT_ROOT/"config"/"research"/"optimus_post_leisai_full_rebase_v0741a.json"
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

def payload_path(cfg:dict[str,Any],rel:str)->Path:
    return PROJECT_ROOT/cfg["payload_root"]/rel

def child_env()->dict[str,str]:
    e=os.environ.copy(); e["PYTHONIOENCODING"]="utf-8"; e["PYTHONUTF8"]="1"; return e

def run_json(rel:str,*args:str)->dict[str,Any]:
    p=PROJECT_ROOT/rel
    r=subprocess.run([sys.executable,str(p),*args],cwd=PROJECT_ROOT,
        capture_output=True,text=True,encoding="utf-8",errors="strict",env=child_env())
    if r.returncode!=0:
        raise RuntimeError(f"COMMAND_FAILED {rel} {' '.join(args)} rc={r.returncode} stderr={r.stderr} stdout={r.stdout[:1000]!r}")
    i=r.stdout.find("{")
    if i<0: raise RuntimeError(f"NO_JSON_OUTPUT {rel} stdout={r.stdout[:1000]!r}")
    return json.loads(r.stdout[i:])

def verify_post_db_state(cfg:dict[str,Any])->dict[str,Any]:
    reg=run_json("scripts/run_optimus_candidate_registry_v03.py","status")
    can=run_json("scripts/run_optimus_canonicalization_v05.py","status")
    p=cfg["post_state"]; lc=reg.get("lifecycle_counts") or {}
    checks={
        "registry_sha":reg.get("registry_sha256")==cfg["approved_apply"]["post_registry_sha256"],
        "candidate_count":int(reg.get("candidate_count",-1))==p["candidate_count"],
        "active_canonical":int(lc.get("ACTIVE_CANONICAL",0))==p["active_canonical"],
        "discovery_only":int(lc.get("DISCOVERY_ONLY",0))==p["discovery_only"],
        "canonical_candidates":int(reg.get("canonical_candidates",-1))==p["canonical_candidates"],
        "needs_review":int(reg.get("needs_review_candidates",-1))==p["needs_review_candidates"],
        "evidence_count":int(reg.get("evidence_count",-1))==p["evidence_count"],
        "review_count":int(reg.get("review_decision_count",-1))==p["review_decision_count"],
        "provisional":int(reg.get("provisional_evidence_count",-1))==p["provisional_evidence_count"],
        "fact_count":int(can.get("fact_count",-1))==p["canonical_fact_count"],
        "canonical_symbols":int(can.get("canonical_symbol_count",-1))==p["canonical_symbol_count"],
        "materialized":int(can.get("materialized_fact_evidence_count",-1))==p["materialized_fact_evidence_count"],
    }
    latest=can.get("latest_batch") or {}
    checks.update({
        "batch":latest.get("batch_id")==cfg["approved_apply"]["batch_id"],
        "plan":latest.get("plan_sha256")==cfg["approved_apply"]["plan_sha256"],
        "result_manifest":latest.get("result_manifest_sha256")==cfg["approved_apply"]["result_manifest_sha256"],
    })
    bad=[k for k,v in checks.items() if not v]
    if bad: raise RuntimeError(f"POST_DB_STATE_MISMATCH {bad}")
    return {"registry":reg,"canonicalization":can}

def file_state(cfg:dict[str,Any])->dict[str,Any]:
    rows=[]; all_old=True; all_new=True
    for rel,spec in cfg["files"].items():
        p=PROJECT_ROOT/rel
        if not p.exists(): raise RuntimeError(f"TARGET_FILE_MISSING {rel}")
        got=sha(p)
        state="OLD" if got==spec["old_sha256"] else "NEW" if got==spec["new_sha256"] else "UNEXPECTED"
        rows.append({"path":rel,"state":state,"sha256":got})
        all_old &= state=="OLD"; all_new &= state=="NEW"
    for rel,expected in cfg["unchanged_frozen"].items():
        p=PROJECT_ROOT/rel
        if not p.exists(): raise RuntimeError(f"UNCHANGED_FILE_MISSING {rel}")
        got=sha(p)
        if got!=expected: raise RuntimeError(f"UNCHANGED_FROZEN_SHA_DRIFT {rel} actual={got} expected={expected}")
    if not (all_old or all_new): raise RuntimeError(f"MIXED_OR_UNEXPECTED_RUNTIME_STATE {rows}")
    return {"state":"OLD" if all_old else "NEW","files":rows}

def verify_payload(cfg:dict[str,Any])->dict[str,str]:
    actual={}
    for rel,spec in cfg["files"].items():
        p=payload_path(cfg,rel)
        if not p.exists(): raise RuntimeError(f"PAYLOAD_MISSING {rel}")
        got=sha(p); actual[rel]=got
        if got!=spec["new_sha256"]: raise RuntimeError(f"PAYLOAD_SHA_MISMATCH {rel} actual={got} expected={spec['new_sha256']}")
    baseline_rel="config/research/optimus_steady_state_baseline_v074.json"
    if actual[baseline_rel]!=cfg["expected_new_baseline_sha256"]:
        raise RuntimeError("EXPECTED_NEW_BASELINE_SHA_MISMATCH")
    return actual

def check(cfg:dict[str,Any])->dict[str,Any]:
    db=verify_post_db_state(cfg); payload=verify_payload(cfg); fs=file_state(cfg)
    return {
        "status":"PASS_OPTIMUS_POST_LEISAI_FULL_REBASE_V0_7_4_1A_CHECK",
        "runtime_state":fs["state"],
        "old_steady_baseline_sha256":cfg["files"]["config/research/optimus_steady_state_baseline_v074.json"]["old_sha256"],
        "new_steady_baseline_sha256":cfg["expected_new_baseline_sha256"],
        "post_registry_sha256":db["registry"]["registry_sha256"],
        "canonical_fact_count":db["canonicalization"]["fact_count"],
        "materialized_fact_evidence_count":db["canonicalization"]["materialized_fact_evidence_count"],
        "payload_sha256":payload,
        "mutation":False,"registry_mutation":False,"canonicalization_mutation":False,"theme_stage_mutation":False,
    }

def apply(cfg:dict[str,Any])->dict[str,Any]:
    db=verify_post_db_state(cfg); verify_payload(cfg); fs=file_state(cfg)
    if fs["state"]=="NEW":
        steady=run_json("scripts/run_optimus_steady_state_v074.py","check")
        return {"status":"ALREADY_APPLIED_OPTIMUS_POST_LEISAI_FULL_REBASE_V0_7_4_1A",
                "steady_state_status":steady.get("status"),
                "baseline_sha256":sha(PROJECT_ROOT/"config/research/optimus_steady_state_baseline_v074.json"),
                "mutation":False}

    out=Path(cfg["audit_output_root"])/now_cst().strftime("%Y%m%dT%H%M%S+0800")
    backup=out/"backup"; out.mkdir(parents=True,exist_ok=False); backup.mkdir(parents=True,exist_ok=False)
    for rel,spec in cfg["files"].items():
        src=PROJECT_ROOT/rel; dst=backup/rel; dst.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(src,dst)
        if sha(dst)!=spec["old_sha256"]: raise RuntimeError(f"BACKUP_SHA_MISMATCH {rel}")

    try:
        for rel,spec in cfg["files"].items():
            src=payload_path(cfg,rel); dst=PROJECT_ROOT/rel; tmp=dst.with_name(dst.name+".v0741a.tmp")
            tmp.write_bytes(src.read_bytes())
            if sha(tmp)!=spec["new_sha256"]: raise RuntimeError(f"TEMP_PAYLOAD_SHA_MISMATCH {rel}")
            os.replace(tmp,dst)
            if sha(dst)!=spec["new_sha256"]: raise RuntimeError(f"INSTALLED_PAYLOAD_SHA_MISMATCH {rel}")

        steady=run_json("scripts/run_optimus_steady_state_v074.py","check")
        if steady.get("status")!="PASS_OPTIMUS_STEADY_STATE_V0_7_4_CHECK":
            raise RuntimeError(f"FINAL_STEADY_CHECK_NOT_PASS {steady}")

        manifest={
            "schema_version":"OPTIMUS_POST_LEISAI_FULL_REBASE_V0_7_4_1A_MANIFEST",
            "rebase_id":cfg["rebase_id"],"applied_at":now_cst().isoformat(timespec="seconds"),
            "approved_apply":cfg["approved_apply"],"post_registry_sha256":db["registry"]["registry_sha256"],
            "new_baseline_sha256":sha(PROJECT_ROOT/"config/research/optimus_steady_state_baseline_v074.json"),
            "mutated_contract_files":cfg["files"],"backup_root":str(backup),"steady_state_check":steady,
            "research_db_mutation":False,"registry_mutation":False,"canonicalization_mutation":False,
            "theme_stage_mutation":False,
        }
        manifest["manifest_payload_sha256"]=stable_sha(manifest)
        (out/"MANIFEST.json").write_bytes((json.dumps(manifest,ensure_ascii=False,indent=2)+"\n").encode("utf-8"))
        return {
            "status":"PASS_OPTIMUS_POST_LEISAI_FULL_REBASE_V0_7_4_1A_APPLY",
            "output":str(out),"backup_root":str(backup),
            "new_baseline_sha256":manifest["new_baseline_sha256"],
            "post_registry_sha256":manifest["post_registry_sha256"],
            "steady_state_status":steady["status"],
            "manifest_payload_sha256":manifest["manifest_payload_sha256"],
            "mutation":"RUNTIME_CONTRACT_AND_BASELINE_FILES_ONLY",
            "research_db_mutation":False,"registry_mutation":False,"canonicalization_mutation":False,
            "theme_stage_mutation":False,"broker":False,"orders":False,"production_db":False,
        }
    except Exception as e:
        restored=[]; restore_errors=[]
        for rel,spec in cfg["files"].items():
            src=backup/rel; dst=PROJECT_ROOT/rel
            try:
                shutil.copy2(src,dst); ok=sha(dst)==spec["old_sha256"]
                restored.append({"path":rel,"restored":ok})
                if not ok: restore_errors.append(rel)
            except Exception as rexc:
                restored.append({"path":rel,"restored":False,"error":repr(rexc)}); restore_errors.append(rel)
        (out/"ROLLBACK_NOTICE.json").write_bytes(
            (json.dumps({"status":"ROLLED_BACK_OPTIMUS_POST_LEISAI_FULL_REBASE_V0_7_4_1A",
                         "original_error":repr(e),"restore_errors":restore_errors,"restored":restored},
                        ensure_ascii=False,indent=2)+"\n").encode("utf-8"))
        raise RuntimeError(f"FULL_REBASE_FAILED_ROLLBACK_ERRORS={restore_errors} original_error={e!r}") from e

def status(cfg:dict[str,Any])->dict[str,Any]:
    fs=file_state(cfg); bp=PROJECT_ROOT/"config/research/optimus_steady_state_baseline_v074.json"
    base=json.loads(bp.read_text(encoding="utf-8"))
    out={"status":"PASS_OPTIMUS_POST_LEISAI_FULL_REBASE_V0_7_4_1A_STATUS",
         "runtime_state":fs["state"],"baseline_id":base.get("baseline_id"),"baseline_sha256":sha(bp),
         "baseline_registry_sha256":(base.get("registry") or {}).get("registry_sha256"),
         "baseline_fact_count":(base.get("registry") or {}).get("canonical_fact_count"),
         "baseline_materialized_count":(base.get("registry") or {}).get("materialized_fact_evidence_count"),
         "last_governance_event":base.get("last_governance_event"),"mutation":False}
    if fs["state"]=="NEW":
        out["steady_state_check"]=run_json("scripts/run_optimus_steady_state_v074.py","check")
    return out

def main()->int:
    ap=argparse.ArgumentParser(); ap.add_argument("command",choices=["check","apply","status"]); a=ap.parse_args()
    cfg=load_cfg()
    try:
        out=check(cfg) if a.command=="check" else apply(cfg) if a.command=="apply" else status(cfg)
        print(json.dumps(out,ensure_ascii=False,indent=2,default=str)); return 0
    except Exception as e:
        print(json.dumps({"status":f"FAIL_OPTIMUS_POST_LEISAI_FULL_REBASE_V0_7_4_1A_{a.command.upper()}",
            "error":repr(e),"registry_mutation":False,"canonicalization_mutation":False,
            "theme_stage_mutation":False,"broker":False,"orders":False,"production_db":False},
            ensure_ascii=False,indent=2)); return 2

if __name__=="__main__":
    raise SystemExit(main())
