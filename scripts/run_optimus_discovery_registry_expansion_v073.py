from __future__ import annotations
import argparse, hashlib, json, subprocess, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT=Path(__file__).resolve().parents[1]
CONFIG=PROJECT_ROOT/"config"/"research"/"optimus_discovery_registry_expansion_v073_plan.json"
CST=timezone(timedelta(hours=8))

def sha(path:Path)->str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""): h.update(b)
    return h.hexdigest()

def stable(obj:Any)->str:
    return json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(",",":"),default=str)

def stable_sha(obj:Any)->str:
    return hashlib.sha256(stable(obj).encode("utf-8")).hexdigest()

def resolve(rel:str)->Path:
    p=Path(rel)
    return p if p.is_absolute() else PROJECT_ROOT/p

def load_cfg()->dict[str,Any]:
    return json.loads(CONFIG.read_text(encoding="utf-8"))

def run_json(script:Path,*args:str)->dict[str,Any]:
    p=subprocess.run([sys.executable,str(script),*args],cwd=PROJECT_ROOT,capture_output=True,text=True)
    if p.returncode!=0:
        raise RuntimeError(f"COMMAND_FAILED {script.name} {' '.join(args)} rc={p.returncode} stderr={p.stderr}")
    try: return json.loads(p.stdout)
    except Exception as e: raise RuntimeError(f"NON_JSON_OUTPUT {script}: {e!r}")

def dependency_check(cfg:dict[str,Any])->dict[str,Any]:
    actual={}
    for k,rel in cfg["dependency_paths"].items():
        p=resolve(rel)
        if not p.exists(): raise RuntimeError(f"DEPENDENCY_MISSING {k} {p}")
        got=sha(p); actual[k]=got
        exp=cfg["dependency_sha256"][k]
        if got!=exp: raise RuntimeError(f"DEPENDENCY_SHA_MISMATCH {k} actual={got} expected={exp}")

    reg=run_json(resolve(cfg["dependency_paths"]["registry_script"]),"status")
    rb=cfg["registry_baseline"]
    if reg.get("registry_sha256")!=rb["registry_sha256"]:
        raise RuntimeError(f"REGISTRY_SHA_DRIFT actual={reg.get('registry_sha256')}")
    if int(reg.get("candidate_count",-1))!=rb["candidate_count"]:
        raise RuntimeError(f"REGISTRY_CANDIDATE_COUNT_DRIFT actual={reg.get('candidate_count')}")
    lc=reg.get("lifecycle_counts") or {}
    if int(lc.get("ACTIVE_CANONICAL",0))!=rb["active_canonical"]:
        raise RuntimeError("REGISTRY_ACTIVE_CANONICAL_DRIFT")
    if int(lc.get("DISCOVERY_ONLY",0))!=rb["discovery_only"]:
        raise RuntimeError("REGISTRY_DISCOVERY_ONLY_DRIFT")
    if int(lc.get("ACTIVE_SEED",0))!=rb["active_seed"]:
        raise RuntimeError("REGISTRY_ACTIVE_SEED_DRIFT")

    cstat=run_json(resolve(cfg["dependency_paths"]["c6_script"]),"status")
    sb=cfg["discovery_scan_baseline"]
    if cstat.get("manifest_payload_sha256")!=sb["manifest_payload_sha256"]:
        raise RuntimeError(f"DISCOVERY_SCAN_MANIFEST_DRIFT actual={cstat.get('manifest_payload_sha256')}")
    if cstat.get("queue_sha256")!=sb["queue_sha256"]:
        raise RuntimeError(f"DISCOVERY_QUEUE_SHA_DRIFT actual={cstat.get('queue_sha256')}")
    if int(cstat.get("proposal_count",-1))!=sb["proposal_count"]:
        raise RuntimeError("DISCOVERY_PROPOSAL_COUNT_DRIFT")
    if int(cstat.get("waiting_capture_repair_count",-1))!=sb["waiting_capture_repair_count"]:
        raise RuntimeError("WAITING_CAPTURE_REPAIR_COUNT_DRIFT")

    queue=run_json(resolve(cfg["dependency_paths"]["c6_script"]),"queue")
    props={x["discovery_proposal_id"]:x for x in queue.get("proposals",[])}
    expected={x["discovery_proposal_id"]:x for x in cfg["human_review_recommendations"]}
    if set(props)!=set(expected):
        raise RuntimeError(f"DISCOVERY_PROPOSAL_SET_DRIFT actual={sorted(props)} expected={sorted(expected)}")

    for pid,exp in expected.items():
        p=props[pid]
        if p.get("symbol")!=exp["symbol"] or p.get("entity_name")!=exp["company"]:
            raise RuntimeError(f"PROPOSAL_IDENTITY_DRIFT {pid}")
        if p.get("classifier_queue_state")!=exp["required_classifier_state"]:
            raise RuntimeError(f"PROPOSAL_CLASSIFIER_STATE_DRIFT {pid}")
        if bool(p.get("historical_context_only"))!=bool(exp["required_historical_context_only"]):
            raise RuntimeError(f"PROPOSAL_TEMPORAL_DRIFT {pid}")
        if bool(p.get("tesla_robot_link_eligible"))!=bool(exp["required_tesla_robot_link_eligible"]):
            raise RuntimeError(f"PROPOSAL_TESLA_ROBOT_BINDING_DRIFT {pid}")

    return {
        "status":"PASS_OPTIMUS_DISCOVERY_REGISTRY_EXPANSION_V0_7_3_CHECK",
        "plan_id":cfg["plan_id"],
        "registry_sha256":reg["registry_sha256"],
        "candidate_count":reg["candidate_count"],
        "discovery_manifest_payload_sha256":cstat["manifest_payload_sha256"],
        "discovery_queue_sha256":cstat["queue_sha256"],
        "proposal_count":len(props),
        "waiting_capture_repair_count":cstat["waiting_capture_repair_count"],
        "dependency_sha256":actual,
        "safety":cfg["safety"],
        "mutation":False,
    }

def find_capture(cfg:dict[str,Any],source_url:str,symbol:str)->dict[str,Any]:
    a_cfg=json.loads(resolve(cfg["dependency_paths"]["v072a_config"]).read_text(encoding="utf-8"))
    root=Path(a_cfg["output_root"])
    matches=[]
    for p in root.rglob("capture.normalized.json") if root.exists() else []:
        try: x=json.loads(p.read_text(encoding="utf-8"))
        except Exception: continue
        if str(x.get("source_url"))==source_url and str(x.get("symbol"))==symbol:
            matches.append((p.stat().st_mtime,p,x))
    if not matches:
        raise RuntimeError(f"OFFICIAL_CAPTURE_NOT_FOUND {symbol} {source_url}")
    matches.sort(reverse=True,key=lambda z:z[0])
    _,p,x=matches[0]
    if not x.get("raw_sha256"):
        raise RuntimeError(f"CAPTURE_RAW_SHA_MISSING {symbol}")
    x["_normalized_file"]=str(p)
    return x

def evidence_rows(cfg:dict[str,Any])->list[dict[str,Any]]:
    rows=[]
    bridge=cfg["registry_bridge_contract"]
    for x in cfg["human_review_recommendations"]:
        cap=find_capture(cfg,x["source_url"],x["symbol"])
        row={
            "symbol":x["symbol"],
            "company":x["company"],
            "component_domain":x["component_domain"],
            "evidence_level":bridge["evidence_level"],
            "evidence_type":bridge["evidence_type"],
            "tesla_link_level":bridge["tesla_link_level"],
            "source_type":bridge["source_type"],
            "source_ref":x["source_url"],
            "source_sha256":cap["raw_sha256"],
            "event_time":cap.get("event_time"),
            "observed_at":cap.get("observed_at"),
            "review_state":bridge["review_state"],
            "confidence":0.99,
            "parser_version":"OPTIMUS_DISCOVERY_REGISTRY_BRIDGE_V0_7_3_PLAN",
            "interpretation":(
                f"{x['recommended_decision']} / {x['strength']}. "
                f"Historical official discovery evidence only. Tesla robot link level fixed at 0. "
                f"No canonicalization and no Theme Stage effect. {x['reason']}"
            ),
            "discovery_proposal_id":x["discovery_proposal_id"],
            "registry_intent":"DISCOVERY_ONLY",
            "human_apply_approval_required":True,
        }
        rows.append(row)
    return rows

def plan(cfg:dict[str,Any])->dict[str,Any]:
    check=dependency_check(cfg)
    rows=evidence_rows(cfg)
    stamp=datetime.now(CST).strftime("%Y%m%dT%H%M%S+0800")
    out=Path(cfg["output_root"])/datetime.now(CST).strftime("%Y%m%d")/(stamp+"_plan")
    out.mkdir(parents=True,exist_ok=False)

    evidence_file=out/"planned_registry_provisional_evidence.jsonl"
    evidence_file.write_text("".join(json.dumps(x,ensure_ascii=False,sort_keys=True)+"\n" for x in rows),encoding="utf-8")

    approval={
        "schema_version":"OPTIMUS_DISCOVERY_REGISTRY_APPROVAL_V0_7_3",
        "plan_id":cfg["plan_id"],
        "decision_required":"EXPLICIT_HUMAN_APPROVAL_BEFORE_ANY_REGISTRY_APPLY",
        "recommended":[
            {
                "symbol":x["symbol"],"company":x["company"],
                "decision":x["recommended_decision"],"strength":x["strength"],
                "tesla_link_level":0,"theme_stage_effect":False
            } for x in cfg["human_review_recommendations"]
        ],
        "deferred":cfg["deferred_candidates"],
        "registry_apply":False,
    }
    approval_file=out/"human_review_recommendation.json"
    approval_file.write_text(json.dumps(approval,ensure_ascii=False,indent=2),encoding="utf-8")

    manifest={
        "schema_version":"OPTIMUS_DISCOVERY_REGISTRY_EXPANSION_PLAN_V0_7_3",
        "plan_id":cfg["plan_id"],
        "created_at":datetime.now(CST).isoformat(timespec="seconds"),
        "registry_precondition_sha256":cfg["registry_baseline"]["registry_sha256"],
        "discovery_scan_manifest_payload_sha256":cfg["discovery_scan_baseline"]["manifest_payload_sha256"],
        "discovery_queue_sha256":cfg["discovery_scan_baseline"]["queue_sha256"],
        "planned_symbols":[x["symbol"] for x in rows],
        "planned_candidate_lifecycle":"DISCOVERY_ONLY",
        "planned_f_status":"SPECULATIVE",
        "planned_review_state":"PROVISIONAL",
        "planned_tesla_link_level":0,
        "planned_evidence_count":len(rows),
        "expected_post_apply_preview":cfg["expected_post_apply_preview"],
        "evidence_file":str(evidence_file),
        "evidence_file_sha256":sha(evidence_file),
        "human_review_file":str(approval_file),
        "human_review_file_sha256":sha(approval_file),
        "apply_implemented":False,
        "explicit_human_apply_approval_required":True,
        "registry_mutation":False,
        "canonicalization_mutation":False,
        "theme_stage_mutation":False,
    }
    manifest["manifest_payload_sha256"]=stable_sha(manifest)
    (out/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    return {
        "status":"PASS_OPTIMUS_DISCOVERY_REGISTRY_EXPANSION_V0_7_3_PLAN",
        "output":str(out),
        **manifest,
        "human_review_recommendations":cfg["human_review_recommendations"],
        "deferred_candidates":cfg["deferred_candidates"],
        "mutation":False,
    }

def status(cfg:dict[str,Any])->dict[str,Any]:
    root=Path(cfg["output_root"])
    ms=sorted(root.rglob("manifest.json"),key=lambda p:p.stat().st_mtime,reverse=True) if root.exists() else []
    if not ms:
        return {"status":"ARMED_OPTIMUS_DISCOVERY_REGISTRY_EXPANSION_V0_7_3_PLAN_ONLY","mutation":False}
    m=json.loads(ms[0].read_text(encoding="utf-8"))
    return {"status":"PASS_OPTIMUS_DISCOVERY_REGISTRY_EXPANSION_V0_7_3_STATUS","latest_manifest":str(ms[0]),**m,"mutation":False}

def main()->int:
    ap=argparse.ArgumentParser()
    sp=ap.add_subparsers(dest="command",required=True)
    sp.add_parser("check"); sp.add_parser("plan"); sp.add_parser("status")
    a=ap.parse_args(); cfg=load_cfg()
    try:
        out=dependency_check(cfg) if a.command=="check" else plan(cfg) if a.command=="plan" else status(cfg)
        print(json.dumps(out,ensure_ascii=False,indent=2,default=str))
        return 0
    except Exception as e:
        print(json.dumps({
            "status":f"FAIL_OPTIMUS_DISCOVERY_REGISTRY_EXPANSION_V0_7_3_{a.command.upper()}",
            "error":repr(e),
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
