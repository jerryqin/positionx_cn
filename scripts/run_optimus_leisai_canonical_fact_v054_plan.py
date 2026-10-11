from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT=Path(__file__).resolve().parents[1]
CONFIG=PROJECT_ROOT/"config"/"research"/"optimus_leisai_canonical_fact_v054_plan.json"
CST=timezone(timedelta(hours=8))

def now_cst()->datetime:
    return datetime.now(CST)

def sha256_file(p:Path)->str:
    h=hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""): h.update(b)
    return h.hexdigest()

def stable_json(x:Any)->str:
    return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(",",":"),default=str)

def stable_sha(x:Any)->str:
    return hashlib.sha256(stable_json(x).encode("utf-8")).hexdigest()

def load_cfg()->dict[str,Any]:
    return json.loads(CONFIG.read_text(encoding="utf-8"))

def resolve(rel:str)->Path:
    p=Path(rel)
    return p if p.is_absolute() else PROJECT_ROOT/p

def import_exact(path:Path,expected_sha:str,name:str):
    if not path.exists():
        raise RuntimeError(f"DEPENDENCY_MISSING {path}")
    got=sha256_file(path)
    if got!=expected_sha:
        raise RuntimeError(f"DEPENDENCY_SHA_MISMATCH {path} actual={got} expected={expected_sha}")
    spec=importlib.util.spec_from_file_location(name,path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"CANNOT_IMPORT {path}")
    mod=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=mod
    spec.loader.exec_module(mod)
    return mod

def run_json(rel:str,*args:str)->dict[str,Any]:
    p=resolve(rel)
    r=subprocess.run([sys.executable,str(p),*args],cwd=PROJECT_ROOT,capture_output=True,text=True,encoding="utf-8")
    if r.returncode!=0:
        raise RuntimeError(f"COMMAND_FAILED {rel} {' '.join(args)} rc={r.returncode} stderr={r.stderr}")
    i=r.stdout.find("{")
    if i<0:
        raise RuntimeError(f"NO_JSON_OUTPUT {rel}")
    return json.loads(r.stdout[i:])

def verify_dependencies(cfg:dict[str,Any])->dict[str,str]:
    actual={}
    for key,rel in cfg["dependency_paths"].items():
        p=resolve(rel)
        if not p.exists():
            raise RuntimeError(f"DEPENDENCY_MISSING {key} {p}")
        got=sha256_file(p); actual[key]=got
        exp=cfg["dependency_sha256"].get(key)
        if exp and got!=exp:
            raise RuntimeError(f"DEPENDENCY_SHA_MISMATCH {key} actual={got} expected={exp}")
    return actual

def proposal_signature(x:dict[str,Any])->dict[str,Any]:
    return {
        "evidence_id":x.get("evidence_id"),
        "queue_state":x.get("queue_state"),
        "symbol":x.get("symbol"),
        "company":x.get("company"),
        "component_domain":x.get("component_domain"),
        "evidence_level":x.get("evidence_level"),
        "tesla_link_level":x.get("tesla_link_level"),
        "contradiction_kind":x.get("contradiction_kind"),
        "contradiction_severity":x.get("contradiction_severity"),
        "event_time":x.get("event_time"),
        "observed_at":x.get("observed_at"),
        "source_type":x.get("source_type"),
        "source_ref":x.get("source_ref"),
        "source_sha256":x.get("source_sha256"),
        "parser_version":x.get("parser_version"),
        "interpretation":x.get("interpretation"),
        "acquisition":x.get("acquisition"),
    }

def find_proposal(cfg:dict[str,Any])->tuple[dict[str,Any],list[str]]:
    root=Path(cfg["proposal_search_root"])
    eid=cfg["target"]["evidence_id"]
    hits=[]
    for p in root.rglob("proposal_queue.jsonl") if root.exists() else []:
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            x=json.loads(line)
            if str(x.get("evidence_id"))==eid:
                hits.append((p,x))
    if not hits:
        raise RuntimeError(f"TARGET_PROPOSAL_NOT_FOUND {eid}")
    sigs={stable_json(proposal_signature(x)) for _,x in hits}
    if len(sigs)!=1:
        raise RuntimeError(f"TARGET_PROPOSAL_CONFLICT copies={len(hits)} signatures={len(sigs)}")
    hits.sort(key=lambda z:str(z[0]))
    return hits[0][1],[str(p) for p,_ in hits]

def validate_target_proposal(cfg:dict[str,Any],q:dict[str,Any])->None:
    t=cfg["target"]
    checks={
        "evidence_id":q.get("evidence_id")==t["evidence_id"],
        "queue_state":q.get("queue_state")==t["required_queue_state"],
        "symbol":q.get("symbol")==t["symbol"],
        "company":q.get("company")==t["company"],
        "source_ref":q.get("source_ref")==t["source_ref"],
        "event_time":q.get("event_time")==t["event_time"],
        "evidence_level":int(q.get("evidence_level") or -1)==t["required_evidence_level"],
        "tesla_link_level":int(q.get("tesla_link_level") if q.get("tesla_link_level") is not None else -1)==t["required_tesla_link_level"],
        "contradiction_kind":q.get("contradiction_kind")==t["required_contradiction_kind"],
        "parser_version":q.get("parser_version")==t["expected_parser_version"],
        "source_sha256_present":bool(str(q.get("source_sha256") or "").strip()),
    }
    bad=[k for k,v in checks.items() if not v]
    if bad:
        raise RuntimeError(f"TARGET_PROPOSAL_MISMATCH {bad}")
    acq=q.get("acquisition") or {}
    level_rule=str(acq.get("level_rule") or "")
    if not level_rule.startswith("L6:"):
        raise RuntimeError(f"EXPECTED_L6_LEVEL_RULE actual={level_rule!r}")
    pdf_path=acq.get("pdf_path")
    pdf_sha=acq.get("pdf_sha256")
    if pdf_path and Path(pdf_path).exists() and pdf_sha:
        got=sha256_file(Path(pdf_path))
        if got!=str(pdf_sha):
            raise RuntimeError(f"LOCAL_PDF_SHA_MISMATCH actual={got} expected={pdf_sha}")

def proposal_to_anchor(q:dict[str,Any])->dict[str,Any]:
    acq=q.get("acquisition") or {}
    confidence=0.82 if q.get("source_sha256") and acq.get("pdf_path") else (0.68 if q.get("source_sha256") else 0.55)
    return {
        "evidence_id":q["evidence_id"],
        "symbol":q["symbol"],
        "company":q["company"],
        "component_domain":q.get("component_domain") or "UNKNOWN_PENDING_REVIEW",
        "evidence_level":int(q["evidence_level"]),
        "evidence_type":"OFFICIAL_DISCLOSURE_FULLTEXT_HIT",
        "tesla_link_level":int(q["tesla_link_level"]),
        "source_type":q["source_type"],
        "source_ref":q["source_ref"],
        "source_sha256":q["source_sha256"],
        "event_time":q.get("event_time"),
        "observed_at":q.get("observed_at"),
        "review_state":"PROVISIONAL",
        "confidence":confidence,
        "contradiction_kind":q.get("contradiction_kind"),
        "contradiction_severity":q.get("contradiction_severity"),
        "parser_version":q.get("parser_version"),
        "interpretation":q.get("interpretation"),
        "acquisition":acq,
    }

def runtime(cfg:dict[str,Any]):
    deps=verify_dependencies(cfg)
    v05=import_exact(resolve(cfg["dependency_paths"]["canonicalization_script"]),
                     cfg["dependency_sha256"]["canonicalization_script"],
                     "optimus_v05_for_leisai_plan")
    v05cfg=v05.load_json(resolve(cfg["dependency_paths"]["canonicalization_config"]))
    reg=import_exact(resolve(cfg["dependency_paths"]["registry_script"]),
                     cfg["dependency_sha256"]["registry_script"],
                     "optimus_registry_for_leisai_plan")
    regcfg=reg.load_config(v05.resolve_path(v05cfg["registry_config"]))
    return deps,v05,v05cfg,reg,regcfg

def preflight(cfg:dict[str,Any])->dict[str,Any]:
    deps,v05,v05cfg,reg,regcfg=runtime(cfg)

    steady=run_json(cfg["dependency_paths"]["steady_checker"],"check")
    if steady.get("status")!="PASS_OPTIMUS_STEADY_STATE_V0_7_4_CHECK":
        raise RuntimeError(f"STEADY_STATE_NOT_PASS {steady.get('status')}")

    q,copies=find_proposal(cfg)
    validate_target_proposal(cfg,q)
    anchor=proposal_to_anchor(q)
    normalized=reg.validate_evidence_row(anchor,allow_canonical_input=False)
    if normalized["evidence_id"]!=cfg["target"]["evidence_id"]:
        raise RuntimeError("ANCHOR_EVIDENCE_ID_CHANGED")

    con=v05.connect_db(v05cfg,read_only=True)
    try:
        # Current Registry/canonical state.
        status=reg.status_payload(con,regcfg)
        pre=cfg["precondition"]
        if status.get("registry_sha256")!=pre["registry_sha256"]:
            raise RuntimeError(f"REGISTRY_SHA_DRIFT actual={status.get('registry_sha256')}")
        if int(status.get("candidate_count",-1))!=pre["registry_candidate_count"]:
            raise RuntimeError("REGISTRY_CANDIDATE_COUNT_DRIFT")
        lc=status.get("lifecycle_counts") or {}
        if int(lc.get("ACTIVE_CANONICAL",0))!=pre["active_canonical"] or int(lc.get("DISCOVERY_ONLY",0))!=pre["discovery_only"]:
            raise RuntimeError(f"REGISTRY_LIFECYCLE_DRIFT {lc}")
        if int(status.get("evidence_count",-1))!=pre["registry_evidence_count"]:
            raise RuntimeError(f"REGISTRY_EVIDENCE_COUNT_DRIFT {status.get('evidence_count')}")
        if int(status.get("review_decision_count",-1))!=pre["registry_review_decision_count"]:
            raise RuntimeError("REGISTRY_REVIEW_COUNT_DRIFT")
        if int(status.get("provisional_evidence_count",-1))!=pre["registry_provisional_evidence_count"]:
            raise RuntimeError("REGISTRY_PROVISIONAL_COUNT_DRIFT")

        c=reg.read_candidate(con,cfg["target"]["symbol"]) or {}
        for field,expected in [
            ("lifecycle_state",pre["target_lifecycle_state"]),
            ("f_status",pre["target_f_status"]),
            ("canonical",pre["target_canonical"]),
            ("max_evidence_level",pre["target_max_evidence_level"]),
            ("max_tesla_link_level",pre["target_max_tesla_link_level"]),
        ]:
            got=c.get(field)
            if isinstance(expected,bool): got=bool(got)
            elif isinstance(expected,int) and got is not None: got=int(got)
            if got!=expected:
                raise RuntimeError(f"TARGET_CANDIDATE_DRIFT {field} actual={got!r} expected={expected!r}")

        exists=con.execute(f"SELECT count(*) FROM {reg.TABLE_EVIDENCE} WHERE evidence_id = ?",
                           [cfg["target"]["evidence_id"]]).fetchone()[0]
        if exists:
            raise RuntimeError("TARGET_ANCHOR_ALREADY_IN_REGISTRY")

        facts=con.execute(f"SELECT count(*),count(DISTINCT symbol) FROM {v05.TABLE_FACT}").fetchone()
        if int(facts[0])!=pre["canonical_fact_count"] or int(facts[1])!=pre["canonical_symbol_count"]:
            raise RuntimeError(f"CANONICAL_FACT_COUNT_DRIFT actual={facts}")
        mats=con.execute(f"SELECT count(*) FROM {reg.TABLE_EVIDENCE} WHERE parser_version = ?",
                         [v05.FACT_MATERIALIZATION_PARSER]).fetchone()[0]
        if int(mats)!=pre["materialized_fact_evidence_count"]:
            raise RuntimeError(f"MATERIALIZED_FACT_COUNT_DRIFT actual={mats}")

        fk=cfg["fact"]["fact_key"]
        dup=con.execute(f"SELECT count(*) FROM {v05.TABLE_FACT} WHERE fact_key = ?",[fk]).fetchone()[0]
        if dup:
            raise RuntimeError(f"FACT_KEY_ALREADY_EXISTS {fk}")
    finally:
        con.close()

    return {
        "status":"PASS_OPTIMUS_LEISAI_CANONICAL_FACT_V0_5_4_PLAN_CHECK",
        "plan_id":cfg["plan_id"],
        "target_evidence_id":cfg["target"]["evidence_id"],
        "proposal_copies":len(copies),
        "proposal_files":copies,
        "source_sha256":q["source_sha256"],
        "source_ref":q["source_ref"],
        "evidence_level":q["evidence_level"],
        "tesla_link_level":q["tesla_link_level"],
        "anchor_normalized_id":normalized["evidence_id"],
        "registry_sha256":steady["registry_sha256"],
        "dependency_sha256":deps,
        "safety":cfg["safety"],
        "mutation":False,
    }

def make_plan(cfg:dict[str,Any])->dict[str,Any]:
    check=preflight(cfg)
    deps,v05,v05cfg,reg,regcfg=runtime(cfg)
    q,copies=find_proposal(cfg)
    anchor=proposal_to_anchor(q)
    normalized_anchor=reg.validate_evidence_row(anchor,allow_canonical_input=False)

    fact=dict(cfg["fact"])
    fid=v05.fact_id_for(fact)
    fact_for_materializer={
        "fact_id":fid,
        "batch_id":"PENDING_PLAN_SHA_DERIVATION",
        "symbol":fact["symbol"],
        "company":fact["company"],
        "fact_key":fact["fact_key"],
        "fact_type":fact["fact_type"],
        "fact_text":fact["fact_text"],
        "component_domain":fact["component_domain"],
        "canonical_level":fact["canonical_level"],
        "canonical_tesla_link_level":fact["canonical_tesla_link_level"],
        "anchor_evidence_id":fact["anchor_evidence_id"],
        # PLAN preview needs a syntactically valid timestamp because the frozen
        # Registry validator parses observed_at. This preview timestamp is NOT the
        # future APPLY timestamp; V0.5.1 assigns canonicalized_at again at APPLY.
        "canonicalized_at":now_cst().isoformat(timespec="seconds"),
    }

    decision_plan={
        "schema_version":"OPTIMUS_PROSPECTIVE_CANONICALIZATION_PLAN_V0_5_4",
        "plan_id":cfg["plan_id"],
        "basis":{
            "proposal_evidence_id":cfg["target"]["evidence_id"],
            "proposal_source_ref":q["source_ref"],
            "proposal_source_sha256":q["source_sha256"],
            "proposal_event_time":q.get("event_time"),
            "proposal_observed_at":q.get("observed_at"),
            "proposal_level":q["evidence_level"],
            "proposal_tesla_link":q["tesla_link_level"],
            "proposal_files":copies,
        },
        "facts":[fact],
        "reject_only":[],
        "source_review_policy":cfg["source_review_policy"],
        "expected_post_apply_semantics":cfg["expected_post_apply_semantics"],
        "policy":{
            "canonical_unit":"FACT_NOT_PDF",
            "materializer":"OPTIMUS_CANONICALIZATION_V0_5_1",
            "source_anchor_does_not_contribute_strength_after_fact_materialization":True,
            "no_tesla_supplier_inference":True,
            "no_theme_stage_change":True,
            "separate_apply_approval_required":True,
        }
    }

    stamp=now_cst().strftime("%Y%m%dT%H%M%S+0800")
    out=Path(cfg["output_root"])/stamp
    out.mkdir(parents=True,exist_ok=False)
    plan_file=out/"CANONICALIZATION_PLAN.json"
    plan_file.write_text(json.dumps(decision_plan,ensure_ascii=False,indent=2),encoding="utf-8")
    plan_sha=sha256_file(plan_file)
    batch_id=v05.stable_id("CB",{"plan_id":decision_plan["plan_id"],"plan_sha256":plan_sha})

    fact_for_materializer["batch_id"]=batch_id
    # V0.5.1 materialization preview is derived from the reviewed Fact, not from raw
    # parser strength/contradiction metadata. No DB write occurs here.
    mat_payload=v05.build_materialized_fact_evidence(
        batch_id=batch_id,
        fact_row=fact_for_materializer,
        anchor_row=normalized_anchor,
    )
    mat_norm=reg.validate_evidence_row(mat_payload,allow_canonical_input=True)
    if mat_norm["initial_review_state"]!="CANONICAL":
        raise RuntimeError("MATERIALIZED_FACT_PREVIEW_NOT_CANONICAL")
    if int(mat_norm["evidence_level"])!=6 or int(mat_norm["tesla_link_level"])!=0:
        raise RuntimeError("MATERIALIZED_FACT_PREVIEW_LEVEL_DRIFT")
    if mat_norm["component_domain"]!=cfg["fact"]["component_domain"]:
        raise RuntimeError("MATERIALIZED_FACT_COMPONENT_DOMAIN_DRIFT")

    anchor_file=out/"source_anchor_preview.json"
    anchor_file.write_text(json.dumps(normalized_anchor,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    mat_file=out/"materialized_fact_preview.json"
    mat_file.write_text(json.dumps(mat_norm,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    review_file=out/"HUMAN_REVIEW_RECOMMENDATION.json"
    review={
        "decision_required":"EXPLICIT_HUMAN_APPROVAL_BEFORE_APPLY",
        "recommended_decision":"APPROVE_CANONICAL_FACT",
        "fact_id":fid,
        "fact_key":fact["fact_key"],
        "canonical_level":6,
        "canonical_tesla_link_level":0,
        "anchor_evidence_id":normalized_anchor["evidence_id"],
        "anchor_post_materialization_review":"REJECTED_AS_SOURCE_ABSORBED_BY_CANONICAL_FACT",
        "materialized_evidence_id":mat_norm["evidence_id"],
        "target_registry_state_after_apply":"ACTIVE_CANONICAL / POSSIBLE",
        "theme_stage_change":False,
        "tesla_optimus_supplier_claim":False,
    }
    review_file.write_text(json.dumps(review,ensure_ascii=False,indent=2),encoding="utf-8")

    manifest={
        "schema_version":"OPTIMUS_PROSPECTIVE_CANONICALIZATION_PLAN_MANIFEST_V0_5_4",
        "plan_id":decision_plan["plan_id"],
        "created_at":now_cst().isoformat(timespec="seconds"),
        "batch_id":batch_id,
        "plan_file":str(plan_file),
        "plan_sha256":plan_sha,
        "fact_id":fid,
        "fact_key":fact["fact_key"],
        "source_anchor_evidence_id":normalized_anchor["evidence_id"],
        "source_anchor_sha256":q["source_sha256"],
        "materialized_evidence_id":mat_norm["evidence_id"],
        "expected_post_apply_semantics":cfg["expected_post_apply_semantics"],
        "apply_implemented":False,
        "explicit_human_apply_approval_required":True,
        "registry_mutation":False,
        "canonicalization_mutation":False,
        "theme_stage_mutation":False,
    }
    manifest["manifest_payload_sha256"]=stable_sha(manifest)
    manifest_file=out/"manifest.json"
    manifest_file.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    return {
        "status":"PASS_OPTIMUS_LEISAI_CANONICAL_FACT_V0_5_4_PLAN",
        "output":str(out),
        **manifest,
        "human_review_recommendation":review,
        "mutation":False,
    }

def status(cfg:dict[str,Any])->dict[str,Any]:
    root=Path(cfg["output_root"])
    ms=sorted(root.rglob("manifest.json"),key=lambda p:p.stat().st_mtime,reverse=True) if root.exists() else []
    if not ms:
        return {"status":"ARMED_OPTIMUS_LEISAI_CANONICAL_FACT_V0_5_4_PLAN_NOT_RUN","mutation":False}
    m=json.loads(ms[0].read_text(encoding="utf-8"))
    return {
        "status":"PASS_OPTIMUS_LEISAI_CANONICAL_FACT_V0_5_4_PLAN_STATUS",
        "latest_manifest":str(ms[0]),
        **m,
        "mutation":False,
    }

def main()->int:
    ap=argparse.ArgumentParser()
    ap.add_argument("command",choices=["check","plan","status"])
    a=ap.parse_args()
    cfg=load_cfg()
    try:
        out=preflight(cfg) if a.command=="check" else make_plan(cfg) if a.command=="plan" else status(cfg)
        print(json.dumps(out,ensure_ascii=False,indent=2,default=str))
        return 0
    except Exception as e:
        print(json.dumps({
            "status":f"FAIL_OPTIMUS_LEISAI_CANONICAL_FACT_V0_5_4_{a.command.upper()}",
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
