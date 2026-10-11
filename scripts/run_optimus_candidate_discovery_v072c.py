from __future__ import annotations
import argparse, hashlib, importlib.util, json, re, subprocess, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT=Path(__file__).resolve().parents[1]
CONFIG=PROJECT_ROOT/"config"/"research"/"optimus_candidate_discovery_v072c.json"
CST=timezone(timedelta(hours=8))

def sha256_file(path:Path)->str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""): h.update(b)
    return h.hexdigest()

def stable_json(x:Any)->str:
    return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(",",":"),default=str)

def sha256_json(x:Any)->str:
    return hashlib.sha256(stable_json(x).encode("utf-8")).hexdigest()

def now_cst()->datetime:
    return datetime.now(CST)

def load_config()->dict[str,Any]:
    if not CONFIG.exists(): raise RuntimeError(f"CONFIG_MISSING {CONFIG}")
    return json.loads(CONFIG.read_text(encoding="utf-8"))

def resolve(rel:str)->Path:
    p=Path(rel)
    return p if p.is_absolute() else PROJECT_ROOT/p

def import_module(path:Path,name:str):
    spec=importlib.util.spec_from_file_location(name,path)
    if spec is None or spec.loader is None: raise RuntimeError(f"CANNOT_IMPORT {path}")
    mod=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=mod
    spec.loader.exec_module(mod)
    return mod

def run_json(script:Path,*args:str)->dict[str,Any]:
    p=subprocess.run([sys.executable,str(script),*args],cwd=PROJECT_ROOT,capture_output=True,text=True)
    if p.returncode!=0:
        raise RuntimeError(f"COMMAND_FAILED {script.name} {' '.join(args)} rc={p.returncode} stderr={p.stderr}")
    try: return json.loads(p.stdout)
    except Exception as e: raise RuntimeError(f"NON_JSON_OUTPUT {script.name}: {e!r}")

def universe(cfg:dict[str,Any])->tuple[set[str],set[str]]:
    v=json.loads(resolve(cfg["dependency_paths"]["v07_config"]).read_text(encoding="utf-8"))
    rows=v.get("universe") or []
    syms={str(x["symbol"]).upper().strip() for x in rows}
    names={normalize_name(str(x["company"])) for x in rows}
    if len(syms)!=22:
        raise RuntimeError(f"V07_UNIVERSE_NOT_22 actual={len(syms)}")
    return syms,names

def normalize_name(s:str)->str:
    s=s.lower().strip()
    s=re.sub(r"[\s\u3000]+","",s)
    s=re.sub(r"[（(].*?[）)]","",s)
    s=re.sub(r"(股份有限公司|有限公司|inc\.?|corp\.?|corporation|ltd\.?|limited)$","",s)
    return s

def dependency_check(cfg:dict[str,Any])->dict[str,Any]:
    actual={}
    for key,rel in cfg["dependency_paths"].items():
        p=resolve(rel)
        if not p.exists(): raise RuntimeError(f"DEPENDENCY_MISSING {key} {p}")
        got=sha256_file(p); actual[key]=got
        exp=cfg["dependency_sha256"][key]
        if got!=exp:
            raise RuntimeError(f"DEPENDENCY_SHA_MISMATCH {key} actual={got} expected={exp}")

    reg=run_json(resolve(cfg["dependency_paths"]["registry_script"]),"status")
    if reg.get("registry_sha256")!=cfg["baseline"]["registry_sha256"]:
        raise RuntimeError(f"REGISTRY_BASELINE_DRIFT actual={reg.get('registry_sha256')}")
    lc=reg.get("lifecycle_counts") or {}
    if reg.get("candidate_count")!=24 or lc.get("ACTIVE_CANONICAL")!=22:
        raise RuntimeError("REGISTRY_BASELINE_SHAPE_DRIFT")
    if int(lc.get("DISCOVERY_ONLY") or 0)!=2:
        raise RuntimeError("REGISTRY_DISCOVERY_ONLY_SHAPE_DRIFT")
    if int(reg.get("provisional_evidence_count") or 0)!=2:
        raise RuntimeError("REGISTRY_PROVISIONAL_EVIDENCE_SHAPE_DRIFT")

    b=run_json(resolve(cfg["dependency_paths"]["v072b_script"]),"status")
    if b.get("status") not in (
        "PASS_OPTIMUS_EXTERNAL_CAPTURE_CLASSIFIER_V0_7_2B_STATUS",
        "ARMED_OPTIMUS_EXTERNAL_CAPTURE_CLASSIFIER_V0_7_2B_NO_RUNS",
    ):
        raise RuntimeError(f"B2_STATUS_NOT_PASS {b}")
    # The original C activation anchored one clean B.2 manifest. Operationally,
    # B must be allowed to produce later genuine queues. Freeze code/config hashes,
    # not the latest research result. A latest run with transport/classification
    # errors still fails closed.
    if int(b.get("error_count",0) or 0)!=0:
        raise RuntimeError(f"B2_LATEST_RUN_HAS_ERRORS error_count={b.get('error_count')}")

    syms,names=universe(cfg)
    return {
        "status":"PASS_OPTIMUS_CANDIDATE_DISCOVERY_V0_7_2C_CHECK",
        "discovery_id":cfg["discovery_id"],
        "registry_sha256":reg["registry_sha256"],
        "registry_candidate_count":reg.get("candidate_count"),
        "current_universe_count":len(syms),
        "registry_discovery_only_count":int((reg.get("lifecycle_counts") or {}).get("DISCOVERY_ONLY") or 0),
        "discovery_only_symbols_excluded":cfg.get("baseline",{}).get("discovery_only_symbols",[]),
        "b2_latest_manifest_payload_sha256":b.get("manifest_payload_sha256"),
        "b2_queue_count":b.get("queue_count",0),
        "b2_error_count":b.get("error_count",0),
        "b2_runtime_guard":"CODE_CONFIG_HASH_FROZEN; LATEST_ERROR_COUNT_MUST_BE_ZERO; QUEUE_MAY_BE_NONZERO",
        "dependency_sha256":actual,
        "safety":cfg["safety"],
        "next":"RUN_DISCOVERY_RECALL_OR_SCAN_DISCOVERY_INBOX",
        "mutation":False,
    }

def parse_aware(value:str,field:str)->str:
    try: d=datetime.fromisoformat(value.replace("Z","+00:00"))
    except Exception: raise RuntimeError(f"INVALID_ISO_TIMESTAMP {field}={value!r}")
    if d.tzinfo is None or d.utcoffset() is None:
        raise RuntimeError(f"TIMESTAMP_MUST_BE_TIMEZONE_AWARE {field}={value!r}")
    return d.isoformat()

def current_member(entity_name:str,symbol:Any,cfg:dict[str,Any])->tuple[bool,str|None]:
    syms,names=universe(cfg)
    if symbol is not None and str(symbol).upper().strip() in syms:
        return True,"SYMBOL_ALREADY_IN_CURRENT_22"
    if normalize_name(entity_name) in names:
        return True,"COMPANY_NAME_ALREADY_IN_CURRENT_22"

    baseline=cfg.get("baseline",{})
    discovery_syms={str(x).upper().strip() for x in (baseline.get("discovery_only_symbols") or [])}
    discovery_names={normalize_name(str(x.get("company") or "")) for x in (baseline.get("discovery_only_entities") or [])}
    s=str(symbol).upper().strip() if symbol is not None else ""
    if s and s in discovery_syms:
        return True,"SYMBOL_ALREADY_IN_REGISTRY_DISCOVERY_ONLY"
    if normalize_name(entity_name) in discovery_names:
        return True,"COMPANY_NAME_ALREADY_IN_REGISTRY_DISCOVERY_ONLY"
    return False,None


def preclassification_registry_skip(cap:dict[str,Any],cfg:dict[str,Any])->tuple[bool,str|None]:
    """
    Existing Registry members are not NEW_CANDIDATE_DISCOVERY work anymore.
    Skip them before parsing/classification so obsolete unreadable captures cannot
    degrade discovery scan completeness.
    """
    if cap.get("lane")!="NEW_CANDIDATE_DISCOVERY":
        return False,None
    return current_member(str(cap.get("entity_name") or ""),cap.get("symbol"),cfg)


def prepare_leads(input_path:Path)->dict[str,Any]:
    cfg=load_config(); dependency_check(cfg)
    obj=json.loads(input_path.read_text(encoding="utf-8"))
    rows=obj.get("leads") if isinstance(obj,dict) else obj
    if not isinstance(rows,list): raise RuntimeError("LEADS_ARRAY_REQUIRED")

    required=set(cfg["recall_contract"]["lead_required_fields"])
    reserved=set(cfg["recall_contract"]["reserved_inference_fields_rejected"])
    allowed=set(cfg["recall_contract"]["allowed_recall_source_types"])

    out=[]; audit=[]
    seen=set()
    for i,x in enumerate(rows,1):
        missing=[k for k in required if x.get(k) in (None,"")]
        if missing:
            audit.append({"index":i,"status":"REJECTED_LEAD","reason":"MISSING_REQUIRED_FIELDS","fields":missing}); continue
        bad=sorted(reserved & set(x))
        if bad:
            audit.append({"index":i,"status":"REJECTED_LEAD","reason":"INFERENCE_FIELDS_NOT_ALLOWED","fields":bad}); continue
        if x["recall_source_type"] not in allowed:
            audit.append({"index":i,"status":"REJECTED_LEAD","reason":"RECALL_SOURCE_TYPE_NOT_ALLOWED"}); continue
        try:
            observed=parse_aware(str(x["observed_at"]),"observed_at")
        except Exception as e:
            audit.append({"index":i,"status":"REJECTED_LEAD","reason":repr(e)}); continue
        member,why=current_member(str(x["entity_name"]),x.get("symbol"),cfg)
        if member:
            audit.append({"index":i,"status":"SKIP_CURRENT_22","reason":why,"entity_name":x["entity_name"],"symbol":x.get("symbol")}); continue

        key=hashlib.sha256((
            normalize_name(str(x["entity_name"]))+"\n"+
            str(x.get("symbol") or "").upper().strip()+"\n"+
            str(x["candidate_official_source_id"])+"\n"+
            str(x["candidate_official_url"]).strip()
        ).encode("utf-8")).hexdigest()
        if key in seen:
            audit.append({"index":i,"status":"SKIP_DUPLICATE_LEAD","lead_id":key}); continue
        seen.add(key)
        out.append({
            "schema_version":"OPTIMUS_DISCOVERY_RECALL_LEAD_V0_7_2C",
            "lead_id":key,
            "entity_name":str(x["entity_name"]),
            "symbol":x.get("symbol"),
            "entity_identifier":x.get("entity_identifier"),
            "recall_source_type":x["recall_source_type"],
            "lead_url":str(x["lead_url"]),
            "candidate_official_source_id":str(x["candidate_official_source_id"]),
            "candidate_official_url":str(x["candidate_official_url"]),
            "rationale":x.get("rationale"),
            "observed_at":observed,
            "state":"DISCOVERY_RECALL_ONLY",
            "lead_never_equals_evidence":True,
            "next":"CAPTURE_REGISTERED_OFFICIAL_SOURCE_VIA_V0_7_2A_OR_V0_7_2B",
        })
        audit.append({"index":i,"status":"ACCEPT_RECALL_LEAD","lead_id":key})

    run_id=now_cst().strftime("%Y%m%dT%H%M%S+0800")+"_recall"
    root=Path(cfg["output_root"])/now_cst().strftime("%Y%m%d")/run_id
    root.mkdir(parents=True,exist_ok=False)
    q=root/"discovery_recall_queue.jsonl"; a=root/"audit.jsonl"
    q.write_text("".join(json.dumps(x,ensure_ascii=False,sort_keys=True)+"\n" for x in out),encoding="utf-8")
    a.write_text("".join(json.dumps(x,ensure_ascii=False,sort_keys=True)+"\n" for x in audit),encoding="utf-8")
    manifest={
        "schema_version":"OPTIMUS_DISCOVERY_RECALL_RUN_V0_7_2C",
        "run_id":run_id,
        "input_count":len(rows),
        "accepted_lead_count":len(out),
        "current_22_skipped":sum(1 for x in audit if x["status"]=="SKIP_CURRENT_22"),
        "rejected_count":sum(1 for x in audit if x["status"]=="REJECTED_LEAD"),
        "duplicate_count":sum(1 for x in audit if x["status"]=="SKIP_DUPLICATE_LEAD"),
        "queue_file":str(q),"queue_sha256":sha256_file(q),
        "audit_file":str(a),"audit_sha256":sha256_file(a),
        "registry_mutation":False,"canonicalization_mutation":False,"theme_stage_mutation":False
    }
    manifest["manifest_payload_sha256"]=sha256_json(manifest)
    (root/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    return {"status":"PASS_OPTIMUS_DISCOVERY_RECALL_V0_7_2C","output":str(root),**manifest,"mutation":False}

def discovery_from_classification(cap:dict[str,Any],cls:dict[str,Any],cfg:dict[str,Any])->tuple[dict[str,Any]|None,dict[str,Any]]:
    if cap.get("lane")!="NEW_CANDIDATE_DISCOVERY":
        return None,{"status":"SKIP_NON_DISCOVERY_LANE","capture_identity":cap.get("capture_identity")}
    member,why=current_member(str(cap.get("entity_name") or ""),cap.get("symbol"),cfg)
    if member:
        return None,{"status":"SKIP_CURRENT_22","reason":why,"capture_identity":cap.get("capture_identity")}

    qstate=str(cls["queue_state"])
    disposition=cfg["disposition_map"].get(qstate)
    robot_tags=cls.get("robot_context_signal_tags") or []
    cross=cls.get("cross_section_signal_tags") or []

    if qstate=="NO_NEW_MATERIAL_EVIDENCE":
        reason="CROSS_SECTION_ONLY" if cross else "NO_COMPANY_SPECIFIC_ROBOT_MILESTONE"
        return None,{
            "status":"REJECTED_DISCOVERY",
            "reason":reason,
            "capture_identity":cap["capture_identity"],
            "entity_name":cap["entity_name"],
            "global_signal_tags":cls.get("global_signal_tags",[]),
            "cross_section_signal_tags":cross,
        }

    if qstate!="CONTRADICTION_ALERT" and not robot_tags:
        return None,{
            "status":"REJECTED_DISCOVERY",
            "reason":"MATERIAL_ROBOT_OR_HUMANOID_CONTEXT_REQUIRED",
            "capture_identity":cap["capture_identity"],
            "entity_name":cap["entity_name"],
        }

    did=hashlib.sha256((
        normalize_name(str(cap["entity_name"]))+"\n"+
        str(cap.get("symbol") or "").upper().strip()+"\n"+
        str(cap["capture_identity"])
    ).encode("utf-8")).hexdigest()
    proposal={
        "schema_version":"OPTIMUS_CANDIDATE_DISCOVERY_PROPOSAL_V0_7_2C",
        "discovery_proposal_id":"DCP_"+did[:24],
        "state":disposition,
        "human_review_required":True,
        "entity_name":cap["entity_name"],
        "symbol":cap.get("symbol"),
        "entity_identifier":cap.get("entity_identifier"),
        "capture_identity":cap["capture_identity"],
        "source_id":cap["source_id"],
        "source_role":cap["source_role"],
        "source_url":cap["source_url"],
        "document_title":cap["document_title"],
        "published_at":cap.get("published_at"),
        "event_time":cap.get("event_time"),
        "observed_at":cap["observed_at"],
        "classifier_queue_state":qstate,
        "classifier_reason_code":cls["reason_code"],
        "temporal_state":cls["temporal_state"],
        "robot_context_signal_tags":robot_tags,
        "tesla_context_signal_tags":cls.get("tesla_context_signal_tags",[]),
        "tesla_robot_context_signal_tags":cls.get("tesla_robot_context_signal_tags",[]),
        "tesla_robot_link_eligible":bool(cls.get("tesla_robot_link_eligible",False)),
        "global_signal_tags":cls.get("global_signal_tags",[]),
        "cross_section_signal_tags":cross,
        "historical_context_only":qstate=="HISTORICAL_BASELINE_ONLY",
        "prospective_theme_effect":False if qstate=="HISTORICAL_BASELINE_ONLY" else None,
        "next":"HUMAN_REVIEW_THEN_SEPARATE_REGISTRY_PLAN_IF_APPROVED",
        "automatic_registry_add":False,
        "automatic_active_canonical":False,
        "automatic_f_status":False,
        "automatic_theme_stage_change":False,
    }
    return proposal,{"status":"DISCOVERY_PROPOSAL_CREATED","discovery_proposal_id":proposal["discovery_proposal_id"],"capture_identity":cap["capture_identity"]}

def scan_inbox()->dict[str,Any]:
    cfg=load_config(); dependency_check(cfg)
    bmod=import_module(resolve(cfg["dependency_paths"]["v072b_script"]),"positionx_v072b2_for_v072c")
    a_cfg=json.loads(resolve(cfg["dependency_paths"]["v072a_config"]).read_text(encoding="utf-8"))
    inbox=Path(a_cfg["output_root"])
    files=sorted(inbox.rglob("capture.normalized.json")) if inbox.exists() else []

    proposals=[]; audit=[]; seen=set(); waiting_capture_repair=0; existing_registry_skipped=0
    bcfg=bmod.load_config()
    for p in files:
        cap=json.loads(p.read_text(encoding="utf-8"))
        if cap.get("lane")!="NEW_CANDIDATE_DISCOVERY":
            continue

        skip_member,skip_reason=preclassification_registry_skip(cap,cfg)
        if skip_member:
            existing_registry_skipped+=1
            audit.append({
                "status":"SKIP_EXISTING_REGISTRY_MEMBER_BEFORE_CLASSIFICATION",
                "reason":skip_reason,
                "capture_identity":cap.get("capture_identity"),
                "entity_name":cap.get("entity_name"),
                "symbol":cap.get("symbol"),
                "source_url":cap.get("source_url"),
                "normalized_file":str(p),
                "registry_mutation":False,
                "canonicalization_mutation":False,
                "theme_stage_mutation":False,
            })
            continue

        try:
            cls=bmod.classify_normalized_file(p,bcfg)
        except Exception as e:
            # External Inbox is immutable. A failed historical capture must not be
            # deleted merely to let the scan continue. Keep it visible as unresolved,
            # skip inference for that capture, and continue with other independent
            # captures.
            waiting_capture_repair+=1
            audit.append({
                "status":"WAITING_CAPTURE_REPAIR",
                "reason":"CLASSIFICATION_FAILED_FAIL_CLOSED",
                "capture_identity":cap.get("capture_identity"),
                "entity_name":cap.get("entity_name"),
                "symbol":cap.get("symbol"),
                "source_url":cap.get("source_url"),
                "normalized_file":str(p),
                "error":repr(e),
                "registry_mutation":False,
                "canonicalization_mutation":False,
                "theme_stage_mutation":False,
            })
            continue
        proposal,au=discovery_from_classification(cap,cls,cfg)
        audit.append(au)
        if proposal is not None:
            pid=proposal["discovery_proposal_id"]
            if pid in seen:
                audit.append({"status":"SKIP_DUPLICATE_DISCOVERY_PROPOSAL","discovery_proposal_id":pid})
                continue
            seen.add(pid); proposals.append(proposal)

    run_id=now_cst().strftime("%Y%m%dT%H%M%S+0800")+"_scan"
    root=Path(cfg["output_root"])/now_cst().strftime("%Y%m%d")/run_id
    root.mkdir(parents=True,exist_ok=False)
    q=root/"discovery_queue.jsonl"; a=root/"audit.jsonl"
    q.write_text("".join(json.dumps(x,ensure_ascii=False,sort_keys=True,default=str)+"\n" for x in proposals),encoding="utf-8")
    a.write_text("".join(json.dumps(x,ensure_ascii=False,sort_keys=True,default=str)+"\n" for x in audit),encoding="utf-8")
    state_counts={}
    for x in proposals: state_counts[x["state"]]=state_counts.get(x["state"],0)+1
    manifest={
        "schema_version":"OPTIMUS_CANDIDATE_DISCOVERY_RUN_V0_7_2C",
        "run_id":run_id,
        "external_inbox_capture_count":len(files),
        "discovery_lane_capture_count":sum(1 for p in files if json.loads(p.read_text(encoding="utf-8")).get("lane")=="NEW_CANDIDATE_DISCOVERY"),
        "proposal_count":len(proposals),
        "state_counts":state_counts,
        "rejected_discovery_count":sum(1 for x in audit if x["status"]=="REJECTED_DISCOVERY"),
        "waiting_capture_repair_count":waiting_capture_repair,
        "existing_registry_skipped_before_classification":existing_registry_skipped,
        "scan_completeness":"COMPLETE" if waiting_capture_repair==0 else "PARTIAL_WAITING_CAPTURE_REPAIR",
        "queue_file":str(q),"queue_sha256":sha256_file(q),
        "audit_file":str(a),"audit_sha256":sha256_file(a),
        "registry_mutation":False,"canonicalization_mutation":False,"theme_stage_mutation":False
    }
    manifest["manifest_payload_sha256"]=sha256_json(manifest)
    (root/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    return {
        "status":"PASS_OPTIMUS_CANDIDATE_DISCOVERY_SCAN_V0_7_2C" if waiting_capture_repair==0
                 else "PARTIAL_OPTIMUS_CANDIDATE_DISCOVERY_SCAN_V0_7_2C_WAITING_CAPTURE_REPAIR",
        "output":str(root),**manifest,"mutation":False
    }

def latest_status(cfg:dict[str,Any])->dict[str,Any]:
    root=Path(cfg["output_root"])
    ms=sorted(root.rglob("manifest.json"),key=lambda p:p.stat().st_mtime,reverse=True) if root.exists() else []
    if not ms:
        return {"status":"ARMED_OPTIMUS_CANDIDATE_DISCOVERY_V0_7_2C_NO_RUNS","mutation":False}
    m=json.loads(ms[0].read_text(encoding="utf-8"))
    return {"status":"PASS_OPTIMUS_CANDIDATE_DISCOVERY_V0_7_2C_STATUS","latest_manifest":str(ms[0]),**m,"mutation":False}

def latest_queue(cfg:dict[str,Any])->dict[str,Any]:
    root=Path(cfg["output_root"])
    qs=sorted(root.rglob("discovery_queue.jsonl"),key=lambda p:p.stat().st_mtime,reverse=True) if root.exists() else []
    rows=[]
    if qs:
        rows=[json.loads(x) for x in qs[0].read_text(encoding="utf-8").splitlines() if x.strip()]
    return {
        "status":"PASS_OPTIMUS_CANDIDATE_DISCOVERY_QUEUE_V0_7_2C",
        "queue_file":str(qs[0]) if qs else None,
        "proposal_count":len(rows),
        "proposals":[{
            "discovery_proposal_id":x["discovery_proposal_id"],
            "state":x["state"],
            "entity_name":x["entity_name"],
            "symbol":x["symbol"],
            "source_id":x["source_id"],
            "source_url":x["source_url"],
            "classifier_queue_state":x["classifier_queue_state"],
            "temporal_state":x["temporal_state"],
            "robot_context_signal_tags":x["robot_context_signal_tags"],
            "tesla_context_signal_tags":x["tesla_context_signal_tags"],
            "tesla_robot_context_signal_tags":x.get("tesla_robot_context_signal_tags",[]),
            "tesla_robot_link_eligible":bool(x.get("tesla_robot_link_eligible",False)),
            "historical_context_only":x["historical_context_only"],
        } for x in rows],
        "human_review_required":True,
        "registry_mutation":False,
        "canonicalization_mutation":False,
        "theme_stage_mutation":False,
        "mutation":False
    }

def plan(cfg:dict[str,Any])->dict[str,Any]:
    return {
        "status":"PASS_OPTIMUS_CANDIDATE_DISCOVERY_V0_7_2C_PLAN",
        "discovery_id":cfg["discovery_id"],
        "mode":cfg["mode"],
        "recall_contract":cfg["recall_contract"],
        "candidate_contract":cfg["candidate_contract"],
        "discovery_state_machine":cfg["discovery_state_machine"],
        "disposition_map":cfg["disposition_map"],
        "safety":cfg["safety"],
        "next_phase":"CONTROLLED DISCOVERY LEAD + OFFICIAL CAPTURE PILOT",
        "mutation":False
    }

def main()->int:
    ap=argparse.ArgumentParser()
    sp=ap.add_subparsers(dest="command",required=True)
    sp.add_parser("check"); sp.add_parser("plan"); sp.add_parser("status"); sp.add_parser("queue"); sp.add_parser("scan-inbox")
    p=sp.add_parser("prepare-leads"); p.add_argument("--input",required=True)
    a=ap.parse_args()
    cfg=load_config()
    try:
        if a.command=="check": out=dependency_check(cfg)
        elif a.command=="plan": out=plan(cfg)
        elif a.command=="status": out=latest_status(cfg)
        elif a.command=="queue": out=latest_queue(cfg)
        elif a.command=="scan-inbox": out=scan_inbox()
        elif a.command=="prepare-leads": out=prepare_leads(Path(a.input))
        else: raise RuntimeError("UNKNOWN_COMMAND")
        print(json.dumps(out,ensure_ascii=False,indent=2,default=str))
        return 0
    except Exception as e:
        print(json.dumps({
            "status":f"FAIL_OPTIMUS_CANDIDATE_DISCOVERY_V0_7_2C_{a.command.upper().replace('-','_')}",
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
