from __future__ import annotations
import hashlib

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = HERE / "config" / "foundation" / "positionx_doctor_v01.json"

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
SKIP = "SKIP"

def run(cmd, cwd, timeout=20):
    try:
        r = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout
        )
        return {"returncode": r.returncode, "stdout": r.stdout, "stderr": r.stderr}
    except subprocess.TimeoutExpired as e:
        return {"returncode": 124, "stdout": e.stdout or "", "stderr": "TIMEOUT"}

def result(domain, status, summary, blocking=False, evidence=None, warnings=None):
    return {
        "domain": domain,
        "status": status,
        "blocking": bool(blocking),
        "summary": summary,
        "evidence": evidence or {},
        "warnings": warnings or []
    }

def git_tracked(root):
    r = subprocess.run(["git","ls-files","-z"], cwd=root, capture_output=True)
    if r.returncode != 0:
        return []
    return sorted(x.decode("utf-8","replace") for x in r.stdout.split(b"\0") if x)

def check_git(root, mode, cfg):
    r1 = run(["git","rev-parse","--is-inside-work-tree"], root)
    if r1["returncode"] != 0 or r1["stdout"].strip() != "true":
        return result("git", FAIL, "Not inside a Git worktree.", True, {"returncode":r1["returncode"]})
    head = run(["git","rev-parse","HEAD"], root)
    tree = run(["git","show","-s","--format=%T","HEAD"], root)
    branch = run(["git","branch","--show-current"], root)
    status = run(["git","status","--porcelain=v1"], root)
    dirty = bool(status["stdout"].strip())
    return result(
        "git", WARN if dirty else PASS,
        "Git repository is readable; worktree is dirty." if dirty else "Git repository is readable and worktree is clean.",
        False,
        {"head":head["stdout"].strip(),"tree":tree["stdout"].strip(),
         "branch":branch["stdout"].strip(),"dirty":dirty}
    )

def parse_lock(path):
    rows = {}
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line=line.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name,ver=line.split("==",1)
        rows[name.strip().lower().replace("_","-")] = ver.strip()
    return rows

def installed_versions():
    try:
        from importlib.metadata import distributions
        out={}
        for d in distributions():
            name=(d.metadata.get("Name") or "").strip().lower().replace("_","-")
            if name:
                out[name]=d.version
        return out
    except Exception:
        return {}

def check_python_environment(root, mode, cfg):
    expected=cfg["python_version"]
    actual=".".join(map(str,sys.version_info[:3]))
    lock_path=root/cfg["requirements_lock"]
    lock=parse_lock(lock_path)
    installed=installed_versions()
    mismatches=[]
    missing=[]
    for name,ver in lock.items():
        if name not in installed:
            missing.append(name)
        elif installed[name] != ver:
            mismatches.append({"package":name,"expected":ver,"actual":installed[name]})
    pip = run([sys.executable,"-m","pip","check"], root, cfg["subprocess_timeout_seconds"])
    blocking = actual != expected or not lock or bool(missing) or bool(mismatches) or pip["returncode"] != 0
    return result(
        "python_environment", FAIL if blocking else PASS,
        "Python/runtime dependency contract failed." if blocking else "Python/runtime dependency contract is healthy.",
        blocking,
        {"python_actual":actual,"python_expected":expected,"lock_entries":len(lock),
         "missing":missing,"version_mismatches":mismatches,
         "pip_check_returncode":pip["returncode"],"pip_check_stdout":pip["stdout"].strip()}
    )

def settings_file(root,cfg):
    for rel in cfg["settings_candidates"]:
        p=root/rel
        if p.exists():
            return p
    return None

def check_configuration(root, mode, cfg):
    p=settings_file(root,cfg)
    if p is None:
        return result("configuration", FAIL, "No settings YAML found in configured candidates.", True,
                      {"candidates":cfg["settings_candidates"]})
    try:
        text=p.read_text(encoding="utf-8")
    except Exception as e:
        return result("configuration", FAIL, "Settings file is not readable.", True, {"path":str(p),"error":repr(e)})
    return result("configuration", PASS, "Primary settings file is readable.", False,
                  {"path":str(p.relative_to(root)),"bytes":len(text.encode("utf-8"))})

def check_data_root(data_root, mode, cfg):
    if not data_root.exists() or not data_root.is_dir():
        return result("data_root", FAIL, "Data root is missing.", True, {"path":str(data_root)})
    readable=os.access(data_root, os.R_OK | os.X_OK)
    usage=shutil.disk_usage(data_root)
    blocking=not readable
    return result("data_root", FAIL if blocking else PASS,
                  "Data root is not readable." if blocking else "Data root exists and is readable.",
                  blocking,
                  {"path":str(data_root),"free_bytes":usage.free,"total_bytes":usage.total})

def bounded_data_inventory(data_root, max_files):
    counts={"duckdb":0,"db":0,"json":0,"csv":0,"parquet":0,"manifest_like":0}
    visited=0
    for base,dirs,files in os.walk(data_root):
        dirs[:] = [d for d in dirs if d not in {".git",".venv","__pycache__"}]
        for name in files:
            visited += 1
            low=name.lower()
            ext=Path(name).suffix.lower()
            if ext==".duckdb": counts["duckdb"]+=1
            elif ext in {".db",".sqlite",".sqlite3"}: counts["db"]+=1
            elif ext==".json": counts["json"]+=1
            elif ext==".csv": counts["csv"]+=1
            elif ext==".parquet": counts["parquet"]+=1
            if "manifest" in low or "sha256" in low:
                counts["manifest_like"]+=1
            if visited >= max_files:
                return counts,visited,True
    return counts,visited,False

def check_local_data_assets(root, data_root, mode, cfg):
    counts,visited,capped=bounded_data_inventory(data_root,cfg["data_scan_max_files"])
    has_assets=(counts["duckdb"]+counts["db"]+counts["manifest_like"])>0
    return result("local_data_assets", PASS if has_assets else WARN,
                  "Local durable/audit assets were found." if has_assets else "No durable DB/manifest evidence found in bounded scan.",
                  False, {"counts":counts,"visited_files":visited,"scan_capped":capped})

def check_qmt_xtquant(root, mode, cfg):
    spec=importlib.util.find_spec("xtquant")
    tracked=git_tracked(root)
    hits=[p for p in tracked if any(t in p.lower() for t in cfg["domain_terms"]["qmt_xtquant"])]
    blocking=(mode=="operational" and spec is None)
    return result("qmt_xtquant", FAIL if blocking else (PASS if spec else WARN),
                  "xtquant is import-discoverable." if spec else "xtquant is not import-discoverable.",
                  blocking, {"xtquant_spec_found":spec is not None,"tracked_surface_count":len(hits),"sample":hits[:20]})

def _sha256_path(path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""):
            h.update(chunk)
    return h.hexdigest()

def validate_m2_read_only_evidence(root,cfg):
    rel=str(cfg.get("m2_read_only_evidence_file") or "").strip()
    if not rel:
        return {"valid":False,"reason":"EVIDENCE_FILE_NOT_CONFIGURED"}
    path=root/rel
    if not path.is_file():
        return {"valid":False,"reason":"EVIDENCE_FILE_MISSING","path":rel}
    try:
        evidence=json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"valid":False,"reason":"EVIDENCE_FILE_INVALID","error":f"{type(e).__name__}: {e}"}
    required_domains=("market_data","account","positions","orders","trades")
    facts_ok=(
        evidence.get("schema")=="POSITIONX_CN_QMT_M2_READ_ONLY_EVIDENCE_V1"
        and evidence.get("live_validation_status")=="PASS_POSITIONX_CN_QMT_M2B_FULL_QMT_LIVE_READ_ONLY_VALIDATION_V2"
        and evidence.get("actual_connections",{}).get("qmt_market_connected") is True
        and evidence.get("actual_connections",{}).get("broker_connected") is True
        and all(evidence.get("domains",{}).get(k) is True for k in required_domains)
        and evidence.get("order_actions_executed") is False
        and evidence.get("privacy_redaction_proven") is True
    )
    bindings=evidence.get("source_bindings",{})
    mismatches=[]
    if not isinstance(bindings,dict) or not bindings:
        mismatches.append({"path":"<bindings>","reason":"EMPTY_SOURCE_BINDINGS"})
    else:
        for item,expected in sorted(bindings.items()):
            p=root/item
            actual=_sha256_path(p) if p.is_file() else None
            if actual!=expected:
                mismatches.append({"path":item,"expected_sha256":expected,"actual_sha256":actual})
    valid=bool(facts_ok and not mismatches)
    return {"valid":valid,"reason":"OK" if valid else "STALE_OR_INVALID_M2_EVIDENCE",
            "evidence_file":rel,"m2b_artifact_sha256":evidence.get("m2b_artifact_sha256"),
            "source_binding_count":len(bindings) if isinstance(bindings,dict) else 0,
            "source_mismatches":mismatches,"live_probe_performed_now":False}

def check_broker_read_only(root, mode, cfg):
    tracked=git_tracked(root)
    hits=[p for p in tracked if any(t in p.lower() for t in cfg["domain_terms"]["broker_read_only"])]
    sample=hits[:20]
    if not hits:
        return result("broker_read_only", FAIL, "No broker/reconcile code surface discovered.", True,
                      {"tracked_surface_count":0,"sample":[],"live_connectivity_checked":False},[])
    evidence=validate_m2_read_only_evidence(root,cfg)
    base={"tracked_surface_count":len(hits),"sample":sample,"live_connectivity_checked":False,
          "live_evidence_valid":bool(evidence.get("valid")),"live_evidence":evidence}
    if evidence.get("valid"):
        return result("broker_read_only", PASS,
                      "Broker read-only surface exists; baseline-bound M2 live evidence is valid. Doctor did not perform a new live probe.",
                      False,base,[])
    return result("broker_read_only", WARN,
                  "Broker code surface exists, but durable M2 live read-only evidence is missing, invalid, or stale.",
                  False,base,["BROKER_M2_READ_ONLY_EVIDENCE_MISSING_OR_STALE"])


def check_scheduler(root, mode, cfg):
    evidence={"platform":sys.platform}
    if os.name=="nt":
        r=run(["schtasks","/Query","/FO","CSV","/NH"], root, cfg["subprocess_timeout_seconds"])
        evidence["schtasks_returncode"]=r["returncode"]
        if r["returncode"]==0:
            lines=[x for x in r["stdout"].splitlines() if "positionx" in x.lower() or "optimus" in x.lower()]
            evidence["matching_task_lines"]=lines[:20]
            return result("scheduler", PASS if lines else WARN,
                          "Relevant scheduled tasks found." if lines else "Task Scheduler is readable; no PositionX/Optimus task name matched.",
                          False,evidence)
        return result("scheduler", WARN, "Task Scheduler query failed.", False,evidence)
    return result("scheduler", SKIP, "Windows Task Scheduler check skipped on non-Windows platform.", False,evidence)

def source_and_data_evidence(root,data_root,domain,cfg):
    tracked=git_tracked(root)
    terms=cfg["domain_terms"][domain]
    source_hits=[p for p in tracked if any(t in p.lower() for t in terms)]
    data_terms=cfg["data_evidence_terms"].get(domain,terms)
    data_hits=[]
    visited=0
    cap=cfg["data_scan_max_files"]
    for base,dirs,files in os.walk(data_root):
        dirs[:] = [d for d in dirs if d not in {".git",".venv","__pycache__"}]
        for name in files:
            visited += 1
            full=(Path(base)/name)
            low=str(full).lower()
            if any(t in low for t in data_terms):
                try:
                    rel=str(full.relative_to(data_root))
                except Exception:
                    rel=str(full)
                data_hits.append(rel)
                if len(data_hits)>=30:
                    break
            if visited>=cap:
                break
        if len(data_hits)>=30 or visited>=cap:
            break
    return source_hits,data_hits,visited>=cap

def check_optional_domain(root,data_root,domain,cfg):
    src,data,capped=source_and_data_evidence(root,data_root,domain,cfg)
    if src and data:
        st=PASS; summary="Source surface and data evidence found."
    elif src:
        st=WARN; summary="Source surface found but no data evidence in bounded scan."
    else:
        st=WARN; summary="No source surface discovered for optional subsystem."
    return result(domain,st,summary,False,
                  {"source_count":len(src),"source_sample":src[:20],
                   "data_evidence_count":len(data),"data_evidence_sample":data[:20],
                   "data_scan_capped":capped})

def parse_boolean_key(text,key):
    pat=re.compile(r'(?im)^\s*'+re.escape(key)+r'\s*:\s*(true|false|1|0|yes|no|on|off)\s*(?:#.*)?$')
    vals=[]
    for m in pat.finditer(text):
        raw=m.group(1).lower()
        vals.append(raw in {"true","1","yes","on"})
    return vals

def check_runtime_safety(root, mode, cfg):
    p=settings_file(root,cfg)
    if p is None:
        return result("runtime_safety", FAIL, "Cannot verify safety flags without settings YAML.", True)
    text=p.read_text(encoding="utf-8",errors="replace")
    evidence={}
    problems=[]
    warnings=[]
    for key,expected in cfg["safety_keys"].items():
        vals=parse_boolean_key(text,key)
        evidence[key]={"values":vals,"expected":expected}
        if not vals:
            warnings.append("MISSING_SAFETY_KEY:"+key)
        elif any(v != expected for v in vals):
            problems.append(key)
    if problems:
        return result("runtime_safety", FAIL, "One or more execution safety flags are unsafe.", True,
                      evidence,warnings)
    if warnings:
        return result("runtime_safety", WARN, "No unsafe flag found, but one or more expected safety keys are missing.",
                      False,evidence,warnings)
    return result("runtime_safety", PASS, "Execution safety flags are fail-closed.", False,evidence)

def check_backup_recovery(root,data_root,mode,cfg):
    files={rel:(root/rel).exists() for rel in cfg["recovery_files"]}
    gov=data_root/"governance"
    evidence={"recovery_files":files,"governance_dir_exists":gov.exists()}
    missing=[k for k,v in files.items() if not v]
    blocking=bool(missing)
    return result("backup_recovery", FAIL if blocking else PASS,
                  "Recovery contract files are missing." if blocking else "Recovery/governance prerequisites are present.",
                  blocking,evidence,["MISSING:"+x for x in missing])

def check_foundation_files(root,cfg):
    return [rel for rel in cfg["required_foundation_files"] if not (root/rel).exists()]

def build_checks(root,data_root,mode,cfg):
    checks=[]
    missing=check_foundation_files(root,cfg)
    if missing:
        checks.append(result("foundation_contract",FAIL,"Required foundation files are missing.",True,{"missing":missing}))
    else:
        checks.append(result("foundation_contract",PASS,"Required foundation files are present.",False))
    checks.extend([
        check_git(root,mode,cfg),
        check_python_environment(root,mode,cfg),
        check_configuration(root,mode,cfg),
        check_data_root(data_root,mode,cfg),
        check_local_data_assets(root,data_root,mode,cfg),
        check_qmt_xtquant(root,mode,cfg),
        check_broker_read_only(root,mode,cfg),
        check_scheduler(root,mode,cfg),
        check_optional_domain(root,data_root,"deep_oversold",cfg),
        check_optional_domain(root,data_root,"ret120",cfg),
        check_optional_domain(root,data_root,"portfolio_shadow",cfg),
        check_optional_domain(root,data_root,"g4_g7",cfg),
        check_optional_domain(root,data_root,"optimus",cfg),
        check_runtime_safety(root,mode,cfg),
        check_backup_recovery(root,data_root,mode,cfg),
    ])
    return checks

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("command",choices=["check"])
    ap.add_argument("--project-root",default=str(HERE))
    ap.add_argument("--data-root",default="D:/positionx_data")
    ap.add_argument("--config",default=str(DEFAULT_CONFIG))
    ap.add_argument("--mode",choices=["foundation","operational"],default="operational")
    a=ap.parse_args()

    root=Path(a.project_root)
    data_root=Path(a.data_root)
    cfg=json.loads(Path(a.config).read_text(encoding="utf-8"))

    checks=build_checks(root,data_root,a.mode,cfg)
    blockers=[x for x in checks if x["status"]==FAIL and x["blocking"]]
    warnings=[x for x in checks if x["status"]==WARN]
    overall="BLOCKED" if blockers else ("DEGRADED" if warnings else "HEALTHY")
    payload={
        "status":"PASS_POSITIONX_DOCTOR" if not blockers else "FAIL_POSITIONX_DOCTOR",
        "doctor_version":cfg["version"],
        "mode":a.mode,
        "overall":overall,
        "blocking_failure_count":len(blockers),
        "warning_count":len(warnings),
        "broker_read_only":True,
        "operational_script_execution":False,
        "broker_connectivity_checked":False,
        "order_action":False,
        "production_db_mutation":False,
        "checks":checks
    }
    print(json.dumps(payload,ensure_ascii=True,indent=2))
    return 0 if not blockers else 2

if __name__=="__main__":
    raise SystemExit(main())
