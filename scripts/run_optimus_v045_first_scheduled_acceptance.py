from __future__ import annotations
import argparse, hashlib, json, os, subprocess, sys
from pathlib import Path
from typing import Any

ROOT=Path(__file__).resolve().parents[1]
CONFIG=ROOT/"config"/"research"/"optimus_v045_first_scheduled_acceptance.json"

def sha(p:Path)->str:
    h=hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""): h.update(b)
    return h.hexdigest()

def load_cfg():
    return json.loads(CONFIG.read_text(encoding="utf-8"))

def run_json(rel:str,*args:str)->dict[str,Any]:
    e=os.environ.copy(); e["PYTHONIOENCODING"]="utf-8"; e["PYTHONUTF8"]="1"
    r=subprocess.run([sys.executable,str(ROOT/rel),*args],cwd=ROOT,
        capture_output=True,text=True,encoding="utf-8",errors="strict",env=e)
    if r.returncode!=0:
        raise RuntimeError(f"COMMAND_FAILED {rel} {' '.join(args)} rc={r.returncode} stdout={r.stdout[:1400]!r} stderr={r.stderr[:1400]!r}")
    i=r.stdout.find("{")
    if i<0: raise RuntimeError(f"NO_JSON_OUTPUT {rel}")
    return json.loads(r.stdout[i:])

def verify_deps(cfg):
    out={}
    for k,d in cfg["dependencies"].items():
        p=ROOT/d["path"]
        if not p.exists(): raise RuntimeError(f"DEPENDENCY_MISSING {k} {p}")
        got=sha(p); out[k]=got
        if got!=d["sha256"]:
            raise RuntimeError(f"DEPENDENCY_SHA_MISMATCH {k} actual={got} expected={d['sha256']}")
    return out

def step(audit,name):
    for x in audit.get("steps") or []:
        if x.get("name")==name:
            return x.get("result") or {}
    return {}

def mutation_flags_false(x):
    keys=("registry_mutation","canonicalization_mutation","theme_stage_mutation","broker","orders","production_db")
    return all(x.get(k) is False for k in keys if k in x)

def check(cfg):
    deps=verify_deps(cfg)
    steady=run_json("scripts/run_optimus_steady_state_v074.py","check")
    evidence=run_json("scripts/run_optimus_evidence_acquisition_v04.py","check")
    f=cfg["frozen_contract"]
    checks={
        "steady_status":steady.get("status")=="PASS_OPTIMUS_STEADY_STATE_V0_7_4_CHECK",
        "baseline_id":steady.get("baseline_id")==f["baseline_id"],
        "baseline_sha":steady.get("baseline_sha256")==f["baseline_sha256"],
        "registry_sha":steady.get("registry_sha256")==f["registry_sha256"],
        "candidate_count":int(steady.get("registry_candidate_count",-1))==f["candidate_count"],
        "monitor_count":int(steady.get("current_monitor_universe_count",-1))==f["current_monitor_universe_count"],
        "active_canonical":int(steady.get("active_canonical",-1))==f["active_canonical"],
        "discovery_only":int(steady.get("discovery_only",-1))==f["discovery_only"],
        "b8":steady.get("b8_dedupe_version")==f["b8_dedupe_version"],
        "evidence_v045":evidence.get("status")=="PASS_OPTIMUS_EVIDENCE_ACQUISITION_V0_4_5_CHECK",
    }
    bad=[k for k,v in checks.items() if not v]
    if bad: raise RuntimeError(f"FROZEN_CONTRACT_MISMATCH {bad}")
    return {"status":"PASS_OPTIMUS_V0_4_5_FIRST_SCHEDULED_ACCEPTANCE_CHECK",
            "dependency_sha256":deps,"checks":checks,"mutation":False}

def inspect(cfg,date_override=None):
    check(cfg)
    target=date_override or cfg["target_date"]
    root=Path(cfg["scheduled_data_root"])
    latest=root/"latest_scheduled.json"
    if not latest.exists():
        return {"status":"WAITING_OPTIMUS_V0_4_5_FIRST_SCHEDULED_RUN",
                "reason":"latest_scheduled.json not found","target_date":target,"mutation":False}
    x=json.loads(latest.read_text(encoding="utf-8"))
    started=str(x.get("started_at") or "")
    if not started.startswith(target):
        return {"status":"WAITING_OPTIMUS_V0_4_5_FIRST_SCHEDULED_RUN",
                "reason":"latest scheduled run is not target date",
                "target_date":target,"latest_started_at":started,
                "latest_status":x.get("status"),"mutation":False}

    audit=x.get("audit") or {}
    steady=step(audit,"steady_state_check")
    intake=step(audit,"current22_intake")
    queue=step(audit,"intake_queue")

    errors=[]
    if x.get("status")!=cfg["acceptance_policy"]["scheduled_status"]: errors.append("SCHEDULED_STATUS")
    if steady.get("status")!=cfg["acceptance_policy"]["steady_status"]: errors.append("STEADY_STATUS")
    if steady.get("baseline_sha256")!=cfg["frozen_contract"]["baseline_sha256"]: errors.append("BASELINE_SHA")
    if intake.get("status")!=cfg["acceptance_policy"]["intake_status"]: errors.append("INTAKE_STATUS")
    if intake.get("quality")!=cfg["acceptance_policy"]["intake_quality"]: errors.append("INTAKE_QUALITY")
    if intake.get("query_errors"): errors.append("QUERY_ERRORS")
    if "skipped_existing_source_across_parser" not in intake: errors.append("MISSING_EXISTING_SOURCE_SKIP_COUNTER")
    if "skipped_prior_source_across_parser" not in intake: errors.append("MISSING_PRIOR_SOURCE_SKIP_COUNTER")

    proposals=queue.get("proposals") or []
    bad_parser=[]
    bad_ambiguous=[]
    for p in proposals:
        if p.get("parser_version")!=cfg["frozen_contract"]["parser_version"]:
            bad_parser.append(p.get("evidence_id"))
        if p.get("component_domain")=="AMBIGUOUS_MULTI_DOMAIN_PENDING_REVIEW":
            if p.get("queue_state")!="HUMAN_REVIEW_REQUIRED":
                bad_ambiguous.append(p.get("evidence_id"))
    if bad_parser: errors.append("PROPOSAL_NOT_V045")
    if bad_ambiguous: errors.append("AMBIGUOUS_NOT_HUMAN_REVIEW")

    for obj,name in [(x,"scheduled"),(steady,"steady"),(intake,"intake")]:
        if not mutation_flags_false(obj):
            errors.append(f"MUTATION_FLAG_{name.upper()}")

    out={
        "status":"PASS_OPTIMUS_V0_4_5_FIRST_SCHEDULED_RUN_ACCEPTANCE" if not errors else "FAIL_OPTIMUS_V0_4_5_FIRST_SCHEDULED_RUN_ACCEPTANCE",
        "target_date":target,
        "started_at":started,
        "scheduled_status":x.get("status"),
        "catchup":x.get("catchup"),
        "announcement_count":x.get("announcement_count"),
        "proposal_count":x.get("proposal_count"),
        "pending_attention_count":x.get("pending_attention_count"),
        "intake":{
            "date_min":intake.get("date_min"),
            "date_max":intake.get("date_max"),
            "quality":intake.get("quality"),
            "announcement_count":intake.get("announcement_count"),
            "proposal_count":intake.get("proposal_count"),
            "queue_state_counts":intake.get("queue_state_counts"),
            "skipped_known_registry_evidence":intake.get("skipped_known_registry_evidence"),
            "skipped_already_queued":intake.get("skipped_already_queued"),
            "skipped_existing_source_across_parser":intake.get("skipped_existing_source_across_parser"),
            "skipped_prior_source_across_parser":intake.get("skipped_prior_source_across_parser"),
            "scanned_not_relevant":intake.get("scanned_not_relevant"),
            "query_errors":intake.get("query_errors"),
        },
        "proposal_parser_versions":sorted({str(p.get("parser_version")) for p in proposals}),
        "ambiguous_human_review_count":sum(
            1 for p in proposals
            if p.get("component_domain")=="AMBIGUOUS_MULTI_DOMAIN_PENDING_REVIEW"
            and p.get("queue_state")=="HUMAN_REVIEW_REQUIRED"
        ),
        "bad_parser_evidence_ids":bad_parser,
        "bad_ambiguous_evidence_ids":bad_ambiguous,
        "errors":errors,
        "scheduled_log":x.get("scheduled_log") or str(latest),
        "mutation":False,
        "registry_mutation":False,
        "canonicalization_mutation":False,
        "theme_stage_mutation":False,
        "broker":False,
        "orders":False,
        "production_db":False,
    }
    return out

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("command",choices=["check","inspect"])
    ap.add_argument("--date",default=None)
    a=ap.parse_args(); cfg=load_cfg()
    try:
        out=check(cfg) if a.command=="check" else inspect(cfg,a.date)
        print(json.dumps(out,ensure_ascii=False,indent=2,default=str))
        return 0 if not str(out.get("status","")).startswith("FAIL_") else 2
    except Exception as e:
        print(json.dumps({"status":"FAIL_OPTIMUS_V0_4_5_FIRST_SCHEDULED_ACCEPTANCE_TOOL",
                          "error":repr(e),"mutation":False},ensure_ascii=False,indent=2))
        return 2

if __name__=="__main__":
    raise SystemExit(main())
