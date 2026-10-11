from __future__ import annotations
import argparse, hashlib, json, os, shutil, subprocess, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT=Path(__file__).resolve().parents[1]
CONFIG=PROJECT_ROOT/"config"/"research"/"optimus_component_domain_v045_production_patch.json"
CST=timezone(timedelta(hours=8))

def now_cst():
    return datetime.now(CST)

def sha(p:Path)->str:
    h=hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""): h.update(b)
    return h.hexdigest()

def stable_sha(x:Any)->str:
    s=json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(",",":"),default=str)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def load_cfg():
    return json.loads(CONFIG.read_text(encoding="utf-8"))

def child_env():
    e=os.environ.copy(); e["PYTHONIOENCODING"]="utf-8"; e["PYTHONUTF8"]="1"; return e

def run_json(rel:str,*args:str)->dict[str,Any]:
    p=PROJECT_ROOT/rel
    r=subprocess.run([sys.executable,str(p),*args],cwd=PROJECT_ROOT,
        capture_output=True,text=True,encoding="utf-8",errors="strict",env=child_env())
    if r.returncode!=0:
        raise RuntimeError(f"COMMAND_FAILED {rel} {' '.join(args)} rc={r.returncode} stdout={r.stdout[:1600]!r} stderr={r.stderr[:1600]!r}")
    i=r.stdout.find("{")
    if i<0: raise RuntimeError(f"NO_JSON_OUTPUT {rel} stdout={r.stdout[:1200]!r}")
    return json.loads(r.stdout[i:])

def payload_path(cfg,rel):
    return PROJECT_ROOT/cfg["payload_root"]/rel

def verify_eval(cfg):
    g=cfg["evaluation_gate"]; p=Path(g["latest_summary_path"])
    if not p.exists(): raise RuntimeError(f"EVALUATION_SUMMARY_MISSING {p}")
    x=json.loads(p.read_text(encoding="utf-8"))
    if x.get("manifest_payload_sha256")!=g["manifest_payload_sha256"]:
        raise RuntimeError("EVALUATION_MANIFEST_SHA_MISMATCH")
    if x.get("rows_sha256")!=g["rows_sha256"]:
        raise RuntimeError("EVALUATION_ROWS_SHA_MISMATCH")
    if x.get("adoption_decision")!=g["adoption_decision"]:
        raise RuntimeError(f"EVALUATION_DECISION_MISMATCH actual={x.get('adoption_decision')}")
    counts=x.get("counts") or {}
    if int(counts.get("evaluated_rows",-1))!=g["evaluated_rows"]:
        raise RuntimeError("EVALUATION_COVERAGE_MISMATCH")
    if int(counts.get("minority_risk_rows_still_kept_as_old_domain",-1))!=g["minority_risk_rows_still_old"]:
        raise RuntimeError("EVALUATION_MINORITY_RISK_GATE_MISMATCH")
    return x

def verify_payload(cfg):
    out={}
    for rel,spec in cfg["files"].items():
        p=payload_path(cfg,rel)
        if not p.exists(): raise RuntimeError(f"PAYLOAD_MISSING {rel}")
        got=sha(p); out[rel]=got
        if got!=spec["new_sha256"]:
            raise RuntimeError(f"PAYLOAD_SHA_MISMATCH {rel} actual={got} expected={spec['new_sha256']}")
    if out["config/research/optimus_steady_state_baseline_v074.json"]!=cfg["expected_new_baseline_sha256"]:
        raise RuntimeError("NEW_BASELINE_SHA_MISMATCH")
    return out

def verify_unchanged(cfg):
    out={}
    for rel,expected in cfg["unchanged_frozen"].items():
        p=PROJECT_ROOT/rel
        if not p.exists(): raise RuntimeError(f"UNCHANGED_FILE_MISSING {rel}")
        got=sha(p); out[rel]=got
        if got!=expected:
            raise RuntimeError(f"UNCHANGED_FROZEN_SHA_DRIFT {rel} actual={got} expected={expected}")
    return out

def runtime_state(cfg):
    rows=[]; all_old=True; all_new=True
    for rel,spec in cfg["files"].items():
        p=PROJECT_ROOT/rel
        if not p.exists(): raise RuntimeError(f"TARGET_FILE_MISSING {rel}")
        got=sha(p)
        state="OLD" if got==spec["old_sha256"] else "NEW" if got==spec["new_sha256"] else "UNEXPECTED"
        rows.append({"path":rel,"state":state,"sha256":got})
        all_old &= state=="OLD"; all_new &= state=="NEW"
    if not (all_old or all_new):
        raise RuntimeError(f"MIXED_OR_UNEXPECTED_RUNTIME_STATE {rows}")
    return {"state":"OLD" if all_old else "NEW","files":rows}

def verify_registry_frozen(cfg):
    reg=run_json("scripts/run_optimus_candidate_registry_v03.py","status")
    f=cfg["frozen_registry"]; lc=reg.get("lifecycle_counts") or {}
    checks={
        "registry_sha":reg.get("registry_sha256")==f["registry_sha256"],
        "candidate_count":int(reg.get("candidate_count",-1))==f["candidate_count"],
        "active_canonical":int(lc.get("ACTIVE_CANONICAL",0))==f["active_canonical"],
        "discovery_only":int(lc.get("DISCOVERY_ONLY",0))==f["discovery_only"],
        "evidence_count":int(reg.get("evidence_count",-1))==f["evidence_count"],
        "review_count":int(reg.get("review_decision_count",-1))==f["review_decision_count"],
    }
    bad=[k for k,v in checks.items() if not v]
    if bad: raise RuntimeError(f"REGISTRY_FROZEN_STATE_MISMATCH {bad}")
    return reg

def check(cfg):
    ev=verify_eval(cfg); verify_payload(cfg); unchanged=verify_unchanged(cfg)
    rs=runtime_state(cfg); reg=verify_registry_frozen(cfg)
    if rs["state"]=="OLD":
        steady=run_json("scripts/run_optimus_steady_state_v074.py","check")
        if steady.get("baseline_sha256")!=cfg["files"]["config/research/optimus_steady_state_baseline_v074.json"]["old_sha256"]:
            raise RuntimeError("OLD_STEADY_BASELINE_NOT_CURRENT")
    else:
        steady=run_json("scripts/run_optimus_steady_state_v074.py","check")
        if steady.get("baseline_sha256")!=cfg["expected_new_baseline_sha256"]:
            raise RuntimeError("NEW_STEADY_BASELINE_NOT_CURRENT")
    return {
        "status":"PASS_OPTIMUS_COMPONENT_DOMAIN_V0_4_5_PRODUCTION_PATCH_CHECK",
        "runtime_state":rs["state"],
        "evaluation_manifest_payload_sha256":ev["manifest_payload_sha256"],
        "registry_sha256":reg["registry_sha256"],
        "old_baseline_sha256":cfg["files"]["config/research/optimus_steady_state_baseline_v074.json"]["old_sha256"],
        "new_baseline_sha256":cfg["expected_new_baseline_sha256"],
        "b8_review":cfg["b8_review"],
        "mutation":False,"registry_mutation":False,"canonicalization_mutation":False,"theme_stage_mutation":False,
    }

def apply(cfg):
    pre=check(cfg)
    if pre["runtime_state"]=="NEW":
        return {
            "status":"ALREADY_APPLIED_OPTIMUS_COMPONENT_DOMAIN_V0_4_5_PRODUCTION_PATCH",
            "baseline_sha256":cfg["expected_new_baseline_sha256"],
            "mutation":False,
        }

    stamp=now_cst().strftime("%Y%m%dT%H%M%S+0800")
    out=Path(cfg["audit_output_root"])/stamp
    backup=Path(cfg["backup_root"])/stamp
    out.mkdir(parents=True,exist_ok=False); backup.mkdir(parents=True,exist_ok=False)

    for rel,spec in cfg["files"].items():
        src=PROJECT_ROOT/rel; dst=backup/rel
        dst.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(src,dst)
        if sha(dst)!=spec["old_sha256"]:
            raise RuntimeError(f"BACKUP_SHA_MISMATCH {rel}")

    try:
        for rel,spec in cfg["files"].items():
            src=payload_path(cfg,rel); dst=PROJECT_ROOT/rel
            tmp=dst.with_name(dst.name+".v045.tmp")
            tmp.write_bytes(src.read_bytes())
            if sha(tmp)!=spec["new_sha256"]:
                raise RuntimeError(f"TEMP_SHA_MISMATCH {rel}")
            os.replace(tmp,dst)
            if sha(dst)!=spec["new_sha256"]:
                raise RuntimeError(f"INSTALLED_SHA_MISMATCH {rel}")

        evidence_check=run_json("scripts/run_optimus_evidence_acquisition_v04.py","check")
        if evidence_check.get("status")!="PASS_OPTIMUS_EVIDENCE_ACQUISITION_V0_4_5_CHECK":
            raise RuntimeError(f"EVIDENCE_V045_CHECK_FAILED {evidence_check}")

        v071=run_json("scripts/run_optimus_prospective_intake_v071.py","check")
        if v071.get("status")!="PASS_OPTIMUS_PROSPECTIVE_INTAKE_V0_7_1_CHECK":
            raise RuntimeError(f"V071_CHECK_FAILED {v071}")

        b8=run_json("scripts/run_optimus_external_classifier_v072b.py","check")
        if b8.get("status")!="PASS_OPTIMUS_EXTERNAL_CAPTURE_CLASSIFIER_V0_7_2B_CHECK":
            raise RuntimeError(f"B8_CHECK_FAILED {b8}")
        ded=b8.get("dedupe_contract") or {}
        if ded.get("version")!="V0_7_2B_8":
            raise RuntimeError("B8_VERSION_DRIFT")
        if ded.get("component_domain_v0_4_5_review")!="PASS_NO_CAPTURE_IDENTITY_CHANGE":
            raise RuntimeError("B8_V045_REVIEW_MISSING")
        if ded.get("historical_capture_auto_reclassification_on_parser_upgrade") is not False:
            raise RuntimeError("B8_HISTORICAL_AUTO_RECLASS_NOT_FALSE")

        c8=run_json("scripts/run_optimus_candidate_discovery_v072c.py","check")
        if c8.get("status")!="PASS_OPTIMUS_CANDIDATE_DISCOVERY_V0_7_2C_CHECK":
            raise RuntimeError(f"C8_CHECK_FAILED {c8}")

        steady=run_json("scripts/run_optimus_steady_state_v074.py","check")
        if steady.get("status")!="PASS_OPTIMUS_STEADY_STATE_V0_7_4_CHECK":
            raise RuntimeError(f"STEADY_CHECK_FAILED {steady}")
        if steady.get("baseline_sha256")!=cfg["expected_new_baseline_sha256"]:
            raise RuntimeError("STEADY_NEW_BASELINE_SHA_MISMATCH")

        reg=verify_registry_frozen(cfg)
        manifest={
            "schema_version":"OPTIMUS_COMPONENT_DOMAIN_V0_4_5_PRODUCTION_PATCH_MANIFEST",
            "patch_id":cfg["patch_id"],
            "applied_at":now_cst().isoformat(timespec="seconds"),
            "evaluation_gate":cfg["evaluation_gate"],
            "parser_contract":cfg["parser_contract"],
            "b8_review":cfg["b8_review"],
            "new_baseline_sha256":cfg["expected_new_baseline_sha256"],
            "registry_sha256":reg["registry_sha256"],
            "backup_root":str(backup),
            "evidence_check":evidence_check,
            "v071_check":v071,
            "b8_check":b8,
            "c8_check":c8,
            "steady_check":steady,
            "registry_mutation":False,"canonicalization_mutation":False,"theme_stage_mutation":False,
            "broker":False,"orders":False,"production_db":False,
        }
        manifest["manifest_payload_sha256"]=stable_sha(manifest)
        (out/"MANIFEST.json").write_bytes((json.dumps(manifest,ensure_ascii=False,indent=2)+"\n").encode("utf-8"))
        return {
            "status":"PASS_OPTIMUS_COMPONENT_DOMAIN_V0_4_5_PRODUCTION_PATCH_APPLY",
            "output":str(out),"backup_root":str(backup),
            "new_baseline_sha256":cfg["expected_new_baseline_sha256"],
            "registry_sha256":reg["registry_sha256"],
            "parser_version":cfg["parser_contract"]["new_parser_version"],
            "b8_review_result":cfg["b8_review"]["result"],
            "steady_state_status":steady["status"],
            "manifest_payload_sha256":manifest["manifest_payload_sha256"],
            "mutation":"RUNTIME_CODE_CONFIG_ONLY",
            "registry_mutation":False,"canonicalization_mutation":False,"theme_stage_mutation":False,
            "broker":False,"orders":False,"production_db":False,
        }
    except Exception as e:
        restore_errors=[]
        for rel,spec in cfg["files"].items():
            try:
                src=backup/rel; dst=PROJECT_ROOT/rel
                shutil.copy2(src,dst)
                if sha(dst)!=spec["old_sha256"]: restore_errors.append(rel)
            except Exception:
                restore_errors.append(rel)
        (out/"ROLLBACK_NOTICE.json").write_bytes(
            (json.dumps({"status":"ROLLED_BACK_OPTIMUS_COMPONENT_DOMAIN_V0_4_5_PRODUCTION_PATCH",
                         "error":repr(e),"restore_errors":restore_errors},ensure_ascii=False,indent=2)+"\n").encode("utf-8"))
        raise RuntimeError(f"PRODUCTION_PATCH_FAILED_ROLLBACK_ERRORS={restore_errors} original_error={e!r}") from e

def status(cfg):
    verify_eval(cfg); verify_payload(cfg); verify_unchanged(cfg)
    rs=runtime_state(cfg)
    base=PROJECT_ROOT/"config/research/optimus_steady_state_baseline_v074.json"
    b=json.loads(base.read_text(encoding="utf-8"))
    out={
        "status":"PASS_OPTIMUS_COMPONENT_DOMAIN_V0_4_5_PRODUCTION_PATCH_STATUS",
        "runtime_state":rs["state"],
        "baseline_id":b.get("baseline_id"),
        "baseline_sha256":sha(base),
        "registry_sha256":(b.get("registry") or {}).get("registry_sha256"),
        "parser_contract":cfg["parser_contract"],
        "b8_review":cfg["b8_review"],
        "mutation":False,
    }
    if rs["state"]=="NEW":
        out["steady_state_check"]=run_json("scripts/run_optimus_steady_state_v074.py","check")
        out["evidence_check"]=run_json("scripts/run_optimus_evidence_acquisition_v04.py","check")
    return out

def main()->int:
    ap=argparse.ArgumentParser()
    ap.add_argument("command",choices=["check","apply","status"])
    a=ap.parse_args(); cfg=load_cfg()
    try:
        out=check(cfg) if a.command=="check" else apply(cfg) if a.command=="apply" else status(cfg)
        print(json.dumps(out,ensure_ascii=False,indent=2,default=str)); return 0
    except Exception as e:
        print(json.dumps({
            "status":f"FAIL_OPTIMUS_COMPONENT_DOMAIN_V0_4_5_PRODUCTION_PATCH_{a.command.upper()}",
            "error":repr(e),"registry_mutation":False,"canonicalization_mutation":False,
            "theme_stage_mutation":False,"broker":False,"orders":False,"production_db":False
        },ensure_ascii=False,indent=2)); return 2

if __name__=="__main__":
    raise SystemExit(main())
