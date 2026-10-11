from __future__ import annotations
import argparse, hashlib, importlib.util, json, os, subprocess, sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT=Path(__file__).resolve().parents[1]
CONFIG=PROJECT_ROOT/"config"/"research"/"optimus_component_domain_audit_v075.json"
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

def child_env()->dict[str,str]:
    e=os.environ.copy()
    e["PYTHONIOENCODING"]="utf-8"
    e["PYTHONUTF8"]="1"
    return e

def run_json(rel:str,*args:str)->dict[str,Any]:
    p=resolve(rel)
    r=subprocess.run(
        [sys.executable,str(p),*args],cwd=PROJECT_ROOT,capture_output=True,
        text=True,encoding="utf-8",errors="strict",env=child_env()
    )
    if r.returncode!=0:
        raise RuntimeError(f"COMMAND_FAILED {rel} {' '.join(args)} rc={r.returncode} stdout={r.stdout[:1200]!r} stderr={r.stderr[:1200]!r}")
    i=r.stdout.find("{")
    if i<0:
        raise RuntimeError(f"NO_JSON_OUTPUT {rel} stdout={r.stdout[:1200]!r}")
    return json.loads(r.stdout[i:])

def verify_dependencies(cfg:dict[str,Any])->dict[str,str]:
    actual={}
    for key,d in cfg["dependencies"].items():
        p=resolve(d["path"])
        if not p.exists():
            raise RuntimeError(f"DEPENDENCY_MISSING {key} {p}")
        got=sha256_file(p); actual[key]=got
        if got!=d["sha256"]:
            raise RuntimeError(f"DEPENDENCY_SHA_MISMATCH {key} actual={got} expected={d['sha256']}")
    return actual

def runtime(cfg:dict[str,Any]):
    deps=verify_dependencies(cfg)
    ev=import_exact(
        resolve(cfg["dependencies"]["evidence_script"]["path"]),
        cfg["dependencies"]["evidence_script"]["sha256"],
        "optimus_evidence_v044_component_domain_audit"
    )
    reg=import_exact(
        resolve(cfg["dependencies"]["registry_script"]["path"]),
        cfg["dependencies"]["registry_script"]["sha256"],
        "optimus_registry_v03_component_domain_audit"
    )
    v071=json.loads(resolve(cfg["dependencies"]["v071_config"]["path"]).read_text(encoding="utf-8"))
    ecfg_path=resolve(v071["dependency_paths"]["evidence_config"])
    if not ecfg_path.exists():
        raise RuntimeError(f"EVIDENCE_CONFIG_MISSING {ecfg_path}")
    ecfg=json.loads(ecfg_path.read_text(encoding="utf-8"))
    rcfg=reg.load_config(ev.resolve_path(ecfg["registry_config"]))
    return deps,ev,reg,ecfg,rcfg

def check(cfg:dict[str,Any])->dict[str,Any]:
    deps,ev,reg,ecfg,rcfg=runtime(cfg)
    steady=run_json(cfg["dependencies"]["steady_checker"]["path"],"check")
    b=cfg["post_rebase_contract"]
    checks={
        "steady_status":steady.get("status")=="PASS_OPTIMUS_STEADY_STATE_V0_7_4_CHECK",
        "baseline_id":steady.get("baseline_id")==b["baseline_id"],
        "baseline_sha":steady.get("baseline_sha256")==b["baseline_sha256"],
        "registry_sha":steady.get("registry_sha256")==b["registry_sha256"],
        "candidate_count":int(steady.get("registry_candidate_count",-1))==b["candidate_count"],
        "monitor_count":int(steady.get("current_monitor_universe_count",-1))==b["current_monitor_universe_count"],
        "active_canonical":int(steady.get("active_canonical",-1))==b["active_canonical"],
        "discovery_only":int(steady.get("discovery_only",-1))==b["discovery_only"],
    }
    bad=[k for k,v in checks.items() if not v]
    if bad:
        raise RuntimeError(f"POST_REBASE_CONTRACT_MISMATCH {bad}")
    return {
        "status":"PASS_OPTIMUS_COMPONENT_DOMAIN_AUDIT_V0_7_5_CHECK",
        "baseline_id":steady["baseline_id"],
        "baseline_sha256":steady["baseline_sha256"],
        "registry_sha256":steady["registry_sha256"],
        "candidate_count":steady["registry_candidate_count"],
        "monitor_count":steady["current_monitor_universe_count"],
        "parser_version":cfg["scope"]["parser_version"],
        "component_map_order":[{"terms":list(terms),"domain":domain} for terms,domain in ev.COMPONENT_MAP],
        "dependency_sha256":deps,
        "mutation":False,
        "registry_mutation":False,
        "canonicalization_mutation":False,
        "theme_stage_mutation":False,
    }

def proposal_rows(cfg:dict[str,Any])->dict[str,dict[str,Any]]:
    root=Path(cfg["scope"]["proposal_search_root"])
    out={}
    if not root.exists():
        return out
    for p in root.rglob("proposal_queue.jsonl"):
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip(): continue
            x=json.loads(line)
            eid=str(x.get("evidence_id") or "")
            if not eid: continue
            if eid in out and stable_json(out[eid])!=stable_json(x):
                raise RuntimeError(f"PROPOSAL_EVIDENCE_CONFLICT {eid}")
            out[eid]=x
    return out

def registry_rows(reg,rcfg)->dict[str,dict[str,Any]]:
    con=reg.open_db(rcfg,read_only=True)
    try:
        df=reg.latest_effective_evidence(con)
        if df.empty:
            return {}
        rows={}
        for _,r in df.iterrows():
            x={k:(None if (hasattr(v,"isna") and v.isna()) else v) for k,v in r.to_dict().items()}
            # pandas/numpy values become JSON-friendly through default=str later.
            eid=str(x.get("evidence_id") or "")
            if not eid: continue
            raw=x.get("raw_payload_json")
            if raw:
                try: x["_raw_payload"]=json.loads(str(raw))
                except Exception: x["_raw_payload"]={}
            else:
                x["_raw_payload"]={}
            rows[eid]=x
        return rows
    finally:
        con.close()

def merge_source_row(eid:str,regrow:dict[str,Any]|None,proposal:dict[str,Any]|None)->dict[str,Any]:
    base={}
    if regrow:
        base.update(regrow)
    if proposal:
        # Proposal contains acquisition/pdf details missing from normalized registry columns.
        for k,v in proposal.items():
            if k not in base or base.get(k) in (None,"",[],{}):
                base[k]=v
        base["_proposal"]=proposal
    raw=(regrow or {}).get("_raw_payload") or {}
    for k,v in raw.items():
        if k not in base or base.get(k) in (None,"",[],{}):
            base[k]=v
    # Prefer acquisition from proposal, then raw payload.
    if proposal and proposal.get("acquisition"):
        base["acquisition"]=proposal["acquisition"]
    elif raw.get("acquisition"):
        base["acquisition"]=raw["acquisition"]
    base["evidence_id"]=eid
    return base

def domain_stats(ev,text:str)->list[dict[str,Any]]:
    low=text.lower()
    rows=[]
    for idx,(terms,domain) in enumerate(ev.COMPONENT_MAP):
        term_counts={}
        total=0
        for t in terms:
            c=low.count(str(t).lower())
            if c:
                term_counts[str(t)]=c
                total+=c
        if total:
            rows.append({
                "map_index":idx,
                "domain":domain,
                "term_counts":term_counts,
                "occurrence_count":total,
            })
    return rows

def domain_window_stats(ev,windows:list[str])->list[dict[str,Any]]:
    accum={}
    for idx,(terms,domain) in enumerate(ev.COMPONENT_MAP):
        accum[domain]={
            "map_index":idx,"domain":domain,"window_hit_count":0,
            "occurrence_count":0,"term_counts":Counter()
        }
    for w in windows:
        low=w.lower()
        for terms,domain in ev.COMPONENT_MAP:
            hit=False
            for t in terms:
                c=low.count(str(t).lower())
                if c:
                    hit=True
                    accum[domain]["occurrence_count"]+=c
                    accum[domain]["term_counts"][str(t)]+=c
            if hit:
                accum[domain]["window_hit_count"]+=1
    rows=[]
    for d,x in accum.items():
        if x["window_hit_count"] or x["occurrence_count"]:
            rows.append({
                "map_index":x["map_index"],"domain":d,
                "window_hit_count":x["window_hit_count"],
                "occurrence_count":x["occurrence_count"],
                "term_counts":dict(x["term_counts"])
            })
    rows.sort(key=lambda x:x["map_index"])
    return rows

def diagnose_text(ev,evidence_text:str,stored_domain:str|None)->dict[str,Any]:
    windows=ev.context_windows(evidence_text)
    scoped="\n".join(windows) or evidence_text
    recomputed=ev.component_domain(scoped)
    stats=domain_window_stats(ev,windows if windows else [evidence_text])
    matched=[x["domain"] for x in stats]
    max_windows=max([x["window_hit_count"] for x in stats],default=0)
    max_occ=max([x["occurrence_count"] for x in stats],default=0)
    dominant_windows=[x["domain"] for x in stats if x["window_hit_count"]==max_windows and max_windows>0]
    dominant_occ=[x["domain"] for x in stats if x["occurrence_count"]==max_occ and max_occ>0]
    selected=stored_domain or recomputed
    selected_row=next((x for x in stats if x["domain"]==selected),None)
    first_match_minority=False
    if selected_row and len(stats)>=2:
        first_match_minority=selected_row["window_hit_count"]<max_windows
    return {
        "stored_domain":stored_domain,
        "recomputed_first_match_domain":recomputed,
        "recomputed_matches_stored":stored_domain in (None,"") or recomputed==stored_domain,
        "context_window_count":len(windows),
        "matched_domain_count":len(stats),
        "matched_domains":matched,
        "domain_stats":stats,
        "dominant_by_window_count":dominant_windows,
        "dominant_by_occurrence_count":dominant_occ,
        "order_sensitive":len(stats)>=2,
        "first_match_minority_risk":first_match_minority,
    }

def audit_one(ev,row:dict[str,Any])->dict[str,Any]:
    acq=row.get("acquisition") or {}
    pdf_path=str(acq.get("pdf_path") or "")
    expected_pdf_sha=str(acq.get("pdf_sha256") or row.get("source_sha256") or "")
    title=str(acq.get("announcement_title") or row.get("title") or "")
    kws=acq.get("matched_keywords") or row.get("matched_keywords") or []
    if not isinstance(kws,list):
        kws=[str(kws)]
    out={
        "evidence_id":str(row.get("evidence_id") or ""),
        "symbol":str(row.get("symbol") or ""),
        "company":str(row.get("company") or ""),
        "parser_version":str(row.get("parser_version") or ""),
        "stored_domain":str(row.get("component_domain") or ""),
        "source_ref":str(row.get("source_ref") or ""),
        "effective_review_state":str(row.get("effective_review_state") or row.get("review_state") or row.get("initial_review_state") or ""),
        "pdf_path":pdf_path or None,
        "pdf_available":False,
        "pdf_sha_match":None,
        "recomputed":False,
    }
    if not pdf_path:
        out["skip_reason"]="NO_LOCAL_PDF_PATH"
        return out
    p=Path(pdf_path)
    if not p.exists():
        out["skip_reason"]="LOCAL_PDF_MISSING"
        return out
    out["pdf_available"]=True
    got=sha256_file(p)
    out["pdf_sha256"]=got
    if expected_pdf_sha:
        out["pdf_sha_match"]=got==expected_pdf_sha
        if not out["pdf_sha_match"]:
            out["skip_reason"]="LOCAL_PDF_SHA_MISMATCH"
            return out
    text,pages,err=ev.extract_pdf_text(p.read_bytes(),260)
    out["pdf_pages_parsed"]=pages
    out["pdf_parse_error"]=err
    if err or not text.strip():
        out["skip_reason"]="PDF_TEXT_UNAVAILABLE"
        return out
    evidence_text="\n".join([title," ".join(str(x) for x in kws),text])
    d=diagnose_text(ev,evidence_text,out["stored_domain"])
    out.update(d)
    out["recomputed"]=True
    return out

def run_audit(cfg:dict[str,Any])->dict[str,Any]:
    check(cfg)
    deps,ev,reg,ecfg,rcfg=runtime(cfg)
    regrows=registry_rows(reg,rcfg)
    props=proposal_rows(cfg)
    parser=cfg["scope"]["parser_version"]
    ids=set()
    for eid,x in regrows.items():
        if str(x.get("parser_version") or "")==parser:
            ids.add(eid)
    for eid,x in props.items():
        if str(x.get("parser_version") or "")==parser:
            ids.add(eid)
    target=cfg["target"]["source_evidence_id"]
    ids.add(target)

    rows=[]
    for eid in sorted(ids):
        merged=merge_source_row(eid,regrows.get(eid),props.get(eid))
        rows.append(audit_one(ev,merged))

    # Exact target source/canonical semantics from Registry.
    con=reg.open_db(rcfg,read_only=True)
    try:
        src=con.execute(
            f"""SELECT e.evidence_id,e.symbol,e.company,e.component_domain,e.evidence_level,e.tesla_link_level,
                       COALESCE(r.decision,e.initial_review_state) AS effective_review_state
                FROM {reg.TABLE_EVIDENCE} e
                LEFT JOIN (
                  SELECT evidence_id,decision,
                         row_number() OVER (PARTITION BY evidence_id ORDER BY decided_at DESC,decision_id DESC) rn
                  FROM {reg.TABLE_REVIEW}
                ) r ON r.evidence_id=e.evidence_id AND r.rn=1
                WHERE e.evidence_id=?""",
            [cfg["target"]["source_evidence_id"]]
        ).fetchone()
        cfe=con.execute(
            f"""SELECT e.evidence_id,e.symbol,e.company,e.component_domain,e.evidence_level,e.tesla_link_level,
                       COALESCE(r.decision,e.initial_review_state) AS effective_review_state
                FROM {reg.TABLE_EVIDENCE} e
                LEFT JOIN (
                  SELECT evidence_id,decision,
                         row_number() OVER (PARTITION BY evidence_id ORDER BY decided_at DESC,decision_id DESC) rn
                  FROM {reg.TABLE_REVIEW}
                ) r ON r.evidence_id=e.evidence_id AND r.rn=1
                WHERE e.evidence_id=?""",
            [cfg["target"]["canonical_evidence_id"]]
        ).fetchone()
        cand=reg.read_candidate(con,cfg["target"]["symbol"]) or {}
    finally:
        con.close()
    if not src or not cfe:
        raise RuntimeError("TARGET_SOURCE_OR_CFE_MISSING")
    source_domain=str(src[3]); canonical_domain=str(cfe[3])
    target_diag=next((x for x in rows if x["evidence_id"]==target),None)
    if target_diag is None:
        raise RuntimeError("TARGET_DIAGNOSTIC_MISSING")

    counts={
        "evidence_rows_considered":len(rows),
        "recomputed_rows":sum(bool(x.get("recomputed")) for x in rows),
        "missing_local_pdf_rows":sum(x.get("skip_reason") in ("NO_LOCAL_PDF_PATH","LOCAL_PDF_MISSING") for x in rows),
        "order_sensitive_rows":sum(bool(x.get("order_sensitive")) for x in rows),
        "first_match_minority_risk_rows":sum(bool(x.get("first_match_minority_risk")) for x in rows),
        "stored_recompute_mismatch_rows":sum(x.get("recomputed") and not x.get("recomputed_matches_stored") for x in rows),
    }
    systemic=[x for x in rows if x.get("order_sensitive") or x.get("first_match_minority_risk") or (x.get("recomputed") and not x.get("recomputed_matches_stored"))]
    target_root_cause={
        "source_domain":source_domain,
        "canonical_domain":canonical_domain,
        "candidate_current_domain":cand.get("component_domain"),
        "source_effective_review_state":str(src[6]),
        "canonical_effective_review_state":str(cfe[6]),
        "source_canonical_divergence":source_domain!=canonical_domain,
        "component_map_first_match_policy":True,
        "target_order_sensitive":bool(target_diag.get("order_sensitive")),
        "target_first_match_minority_risk":bool(target_diag.get("first_match_minority_risk")),
        "target_recomputed_first_match_domain":target_diag.get("recomputed_first_match_domain"),
        "target_dominant_by_window_count":target_diag.get("dominant_by_window_count"),
        "target_domain_stats":target_diag.get("domain_stats"),
    }
    if source_domain!=cfg["target"]["expected_source_domain"]:
        raise RuntimeError(f"TARGET_SOURCE_DOMAIN_DRIFT actual={source_domain}")
    if canonical_domain!=cfg["target"]["expected_canonical_domain"]:
        raise RuntimeError(f"TARGET_CANONICAL_DOMAIN_DRIFT actual={canonical_domain}")
    if str(src[6])!="REJECTED" or str(cfe[6])!="CANONICAL":
        raise RuntimeError(f"TARGET_REVIEW_STATE_DRIFT source={src[6]} cfe={cfe[6]}")

    recommendation="NO_PATCH_YET"
    rationale=[]
    if target_root_cause["target_order_sensitive"]:
        rationale.append("LEISAI_SOURCE_HAS_MULTI_DOMAIN_CONTEXT_AND_FROZEN_CLASSIFIER_IS_FIRST_MATCH_ORDER_SENSITIVE")
    if counts["first_match_minority_risk_rows"]>0:
        recommendation="PATCH_WARRANTED_AFTER_READ_ONLY_REVIEW"
        rationale.append("AT_LEAST_ONE_RECOMPUTED_ROW_SELECTS_A_DOMAIN_WITH_FEWER_CONTEXT_WINDOWS_THAN_ANOTHER_DOMAIN")
    elif counts["order_sensitive_rows"]>1:
        recommendation="PATCH_WARRANTED_AFTER_READ_ONLY_REVIEW"
        rationale.append("ORDER_SENSITIVITY_IS_NOT_ISOLATED_TO_ONE_EVIDENCE_ROW")
    elif target_root_cause["source_canonical_divergence"] and target_root_cause["target_order_sensitive"]:
        recommendation="NARROW_PATCH_WARRANTED"
        rationale.append("LEISAI_CONFIRMS_FIRST_MATCH_AMBIGUITY_BUT_SYSTEMIC_SCOPE_IS_NOT_YET_LARGE")

    stamp=now_cst().strftime("%Y%m%dT%H%M%S+0800")
    out=Path(cfg["output_root"])/stamp
    out.mkdir(parents=True,exist_ok=False)
    rows_file=out/"evidence_domain_diagnostics.jsonl"
    rows_file.write_text("".join(json.dumps(x,ensure_ascii=False,sort_keys=True,default=str)+"\n" for x in rows),encoding="utf-8")
    summary={
        "schema_version":"OPTIMUS_COMPONENT_DOMAIN_AUDIT_V0_7_5",
        "audit_id":cfg["audit_id"],
        "created_at":now_cst().isoformat(timespec="seconds"),
        "scope":cfg["scope"],
        "counts":counts,
        "target_root_cause":target_root_cause,
        "systemic_flagged_evidence":[
            {
                "evidence_id":x.get("evidence_id"),"symbol":x.get("symbol"),"company":x.get("company"),
                "stored_domain":x.get("stored_domain"),
                "recomputed_first_match_domain":x.get("recomputed_first_match_domain"),
                "matched_domains":x.get("matched_domains"),
                "dominant_by_window_count":x.get("dominant_by_window_count"),
                "order_sensitive":x.get("order_sensitive"),
                "first_match_minority_risk":x.get("first_match_minority_risk"),
            } for x in systemic
        ],
        "recommendation":recommendation,
        "recommendation_rationale":rationale,
        "governance":{
            "no_auto_reclassification":True,
            "no_registry_change":True,
            "no_canonicalization_change":True,
            "no_theme_stage_change":True,
            "human_review_required_before_any_classifier_patch":True,
        },
        "mutation":False,
        "registry_mutation":False,
        "canonicalization_mutation":False,
        "theme_stage_mutation":False,
        "broker":False,
        "orders":False,
        "production_db":False,
    }
    summary["rows_file"]=str(rows_file)
    summary["rows_sha256"]=sha256_file(rows_file)
    summary["manifest_payload_sha256"]=stable_sha(summary)
    sf=out/"summary.json"
    sf.write_text(json.dumps(summary,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    latest=Path(cfg["output_root"])/"latest_summary.json"
    latest.write_text(json.dumps(summary,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    return {
        "status":"PASS_OPTIMUS_COMPONENT_DOMAIN_AUDIT_V0_7_5",
        "output":str(out),
        **summary,
    }

def status(cfg:dict[str,Any])->dict[str,Any]:
    p=Path(cfg["output_root"])/"latest_summary.json"
    if not p.exists():
        return {"status":"ARMED_OPTIMUS_COMPONENT_DOMAIN_AUDIT_V0_7_5_NOT_RUN","mutation":False}
    x=json.loads(p.read_text(encoding="utf-8"))
    return {
        "status":"PASS_OPTIMUS_COMPONENT_DOMAIN_AUDIT_V0_7_5_STATUS",
        "latest_summary":str(p),
        "counts":x.get("counts"),
        "target_root_cause":x.get("target_root_cause"),
        "recommendation":x.get("recommendation"),
        "recommendation_rationale":x.get("recommendation_rationale"),
        "manifest_payload_sha256":x.get("manifest_payload_sha256"),
        "mutation":False,
    }

def main()->int:
    ap=argparse.ArgumentParser()
    ap.add_argument("command",choices=["check","run","status"])
    a=ap.parse_args()
    cfg=load_cfg()
    try:
        out=check(cfg) if a.command=="check" else run_audit(cfg) if a.command=="run" else status(cfg)
        print(json.dumps(out,ensure_ascii=False,indent=2,default=str))
        return 0
    except Exception as e:
        print(json.dumps({
            "status":f"FAIL_OPTIMUS_COMPONENT_DOMAIN_AUDIT_V0_7_5_{a.command.upper()}",
            "error":repr(e),
            "mutation":False,
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
