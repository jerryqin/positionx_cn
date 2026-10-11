from __future__ import annotations
import argparse, hashlib, json, os, subprocess, sys
from pathlib import Path
from typing import Any

PROJECT_ROOT=Path(__file__).resolve().parents[1]
BASELINE=PROJECT_ROOT/"config"/"research"/"optimus_steady_state_baseline_v074.json"

def sha(path:Path)->str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""): h.update(b)
    return h.hexdigest()

def load()->dict[str,Any]:
    return json.loads(BASELINE.read_text(encoding="utf-8"))

def run_json(rel:str,*args:str)->dict[str,Any]:
    p=PROJECT_ROOT/rel
    # Windows PowerShell can run in a GBK console. When a child Python process is
    # captured via PIPE, it may inherit a legacy stdout encoding and fail before the
    # parent sees JSON if the payload contains Unicode such as "↔".
    #
    # Force the child Python process and the parent-side decoder to UTF-8. This is an
    # observability/transport fix only; no research or Registry semantics change.
    env=os.environ.copy()
    env["PYTHONIOENCODING"]="utf-8"
    env["PYTHONUTF8"]="1"
    r=subprocess.run(
        [sys.executable,str(p),*args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        env=env,
    )
    if r.returncode!=0:
        raise RuntimeError(f"COMMAND_FAILED {rel} {' '.join(args)} rc={r.returncode} stderr={r.stderr}")
    try:
        return json.loads(r.stdout)
    except Exception as e:
        raise RuntimeError(f"NON_JSON_OUTPUT {rel}: {e!r}")

def code_check(cfg:dict[str,Any])->dict[str,str]:
    actual={}
    for rel,expected in cfg["code_freeze"].items():
        p=PROJECT_ROOT/rel
        if not p.exists():
            raise RuntimeError(f"FROZEN_FILE_MISSING {rel}")
        got=sha(p); actual[rel]=got
        if got!=expected:
            raise RuntimeError(f"FROZEN_SHA_MISMATCH {rel} actual={got} expected={expected}")
    return actual

def check()->dict[str,Any]:
    cfg=load()
    hashes=code_check(cfg)

    reg=run_json("scripts/run_optimus_candidate_registry_v03.py","status")
    mq=run_json("scripts/run_optimus_candidate_registry_v03.py","maintenance-queue")
    v07=run_json("scripts/run_optimus_prospective_monitor_v07.py","check")
    v071=run_json("scripts/run_optimus_prospective_intake_v071.py","check")
    v072=run_json("scripts/run_optimus_global_discovery_monitor_v072.py","check")
    a=run_json("scripts/run_optimus_external_inbox_v072a.py","check")
    b=run_json("scripts/run_optimus_external_classifier_v072b.py","check")
    c=run_json("scripts/run_optimus_candidate_discovery_v072c.py","check")

    exp=cfg["registry"]
    if reg.get("registry_sha256")!=exp["registry_sha256"]:
        raise RuntimeError(
            f"REGISTRY_SHA_DRIFT actual={reg.get('registry_sha256')} expected={exp['registry_sha256']}. "
            "If this followed an approved Registry APPLY, create a new steady-state baseline revision."
        )
    if int(reg.get("candidate_count",-1))!=exp["candidate_count"]:
        raise RuntimeError("REGISTRY_CANDIDATE_COUNT_DRIFT")
    lc=reg.get("lifecycle_counts") or {}
    if int(lc.get("ACTIVE_CANONICAL",0))!=exp["active_canonical"]:
        raise RuntimeError("ACTIVE_CANONICAL_DRIFT")
    if int(lc.get("DISCOVERY_ONLY",0))!=exp["discovery_only"]:
        raise RuntimeError("DISCOVERY_ONLY_DRIFT")
    if int(lc.get("ACTIVE_SEED",0))!=exp["active_seed"]:
        raise RuntimeError("ACTIVE_SEED_DRIFT")
    if int(reg.get("canonical_candidates",-1))!=exp["canonical_candidates"]:
        raise RuntimeError("CANONICAL_CANDIDATE_COUNT_DRIFT")
    if int(reg.get("provisional_evidence_count",-1))!=exp["provisional_evidence_count"]:
        raise RuntimeError("PROVISIONAL_EVIDENCE_DRIFT")

    summary=mq.get("summary") or {}
    for k,v in exp["maintenance_counts"].items():
        if int(summary.get(k,0))!=v:
            raise RuntimeError(f"MAINTENANCE_COUNT_DRIFT {k} actual={summary.get(k)} expected={v}")

    if int(v07.get("monitor_universe_count",-1))!=22 or int(v071.get("monitor_universe_count",-1))!=22:
        raise RuntimeError("CURRENT22_MONITOR_UNIVERSE_DRIFT")
    if int(v07.get("candidate_count",-1))!=24 or int(v071.get("candidate_count",-1))!=24:
        raise RuntimeError("REGISTRY24_MONITOR_PARTITION_DRIFT")
    if int(c.get("registry_candidate_count",-1))!=24 or int(c.get("current_universe_count",-1))!=22:
        raise RuntimeError("DISCOVERY_PARTITION_DRIFT")

    dedupe=b.get("dedupe_contract") or {}
    if dedupe.get("version")!="V0_7_2B_8":
        raise RuntimeError("B8_DEDUPE_CONTRACT_MISSING")
    if not dedupe.get("all_successful_classifications_are_processed"):
        raise RuntimeError("B8_SUCCESS_DEDUPE_DISABLED")
    if not dedupe.get("error_is_not_processed") or not dedupe.get("manual_waiting_is_not_processed"):
        raise RuntimeError("B8_FAILURE_DEDUPE_SEMANTICS_DRIFT")

    return {
        "status":"PASS_OPTIMUS_STEADY_STATE_V0_7_4_CHECK",
        "baseline_id":cfg["baseline_id"],
        "baseline_sha256":sha(BASELINE),
        "registry_sha256":reg["registry_sha256"],
        "registry_candidate_count":reg["candidate_count"],
        "current_monitor_universe_count":v071["monitor_universe_count"],
        "active_canonical":lc.get("ACTIVE_CANONICAL",0),
        "discovery_only":lc.get("DISCOVERY_ONLY",0),
        "provisional_evidence_count":reg["provisional_evidence_count"],
        "maintenance_counts":summary,
        "b8_dedupe_version":dedupe.get("version"),
        "c8_discovery_only_symbols_excluded":c.get("discovery_only_symbols_excluded"),
        "frozen_code_sha256":hashes,
        "mutation":False,
        "registry_mutation":False,
        "canonicalization_mutation":False,
        "theme_stage_mutation":False,
        "broker":False,
        "orders":False,
        "production_db":False
    }

def status()->dict[str,Any]:
    cfg=load()
    out={"status":"PASS_OPTIMUS_STEADY_STATE_V0_7_4_STATUS","baseline_id":cfg["baseline_id"]}
    for key,rel,args in [
        ("registry","scripts/run_optimus_candidate_registry_v03.py",("status",)),
        ("current22","scripts/run_optimus_prospective_intake_v071.py",("status",)),
        ("external_classifier","scripts/run_optimus_external_classifier_v072b.py",("status",)),
        ("candidate_discovery","scripts/run_optimus_candidate_discovery_v072c.py",("status",)),
    ]:
        try:
            out[key]=run_json(rel,*args)
        except Exception as e:
            out[key]={"status":"STATUS_READ_FAILED","error":repr(e)}
    out.update({
        "mutation":False,
        "registry_mutation":False,
        "canonicalization_mutation":False,
        "theme_stage_mutation":False,
        "broker":False,
        "orders":False,
        "production_db":False
    })
    return out

def main()->int:
    ap=argparse.ArgumentParser()
    ap.add_argument("command",choices=["check","status"])
    a=ap.parse_args()
    try:
        out=check() if a.command=="check" else status()
        print(json.dumps(out,ensure_ascii=False,indent=2,default=str))
        return 0
    except Exception as e:
        print(json.dumps({
            "status":f"FAIL_OPTIMUS_STEADY_STATE_V0_7_4_{a.command.upper()}",
            "error":repr(e),
            "mutation":False,
            "registry_mutation":False,
            "canonicalization_mutation":False,
            "theme_stage_mutation":False,
            "broker":False,
            "orders":False,
            "production_db":False
        },ensure_ascii=False,indent=2))
        return 2

if __name__=="__main__":
    raise SystemExit(main())
