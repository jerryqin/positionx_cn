from __future__ import annotations
import argparse, hashlib, importlib.util, json, os, subprocess, sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT=Path(__file__).resolve().parents[1]
CONFIG=PROJECT_ROOT/"config"/"research"/"optimus_component_domain_v045_candidate_eval.json"
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
    mod=importlib.util.module_from_spec(spec); sys.modules[spec.name]=mod; spec.loader.exec_module(mod)
    return mod

def child_env()->dict[str,str]:
    e=os.environ.copy(); e["PYTHONIOENCODING"]="utf-8"; e["PYTHONUTF8"]="1"; return e

def run_json(rel:str,*args:str)->dict[str,Any]:
    r=subprocess.run([sys.executable,str(resolve(rel)),*args],cwd=PROJECT_ROOT,
        capture_output=True,text=True,encoding="utf-8",errors="strict",env=child_env())
    if r.returncode!=0:
        raise RuntimeError(f"COMMAND_FAILED {rel} rc={r.returncode} stdout={r.stdout[:1200]!r} stderr={r.stderr[:1200]!r}")
    i=r.stdout.find("{")
    if i<0: raise RuntimeError(f"NO_JSON_OUTPUT {rel}")
    return json.loads(r.stdout[i:])

def runtime(cfg):
    for key,d in cfg["dependencies"].items():
        p=resolve(d["path"])
        if not p.exists(): raise RuntimeError(f"DEPENDENCY_MISSING {key} {p}")
        got=sha256_file(p)
        if got!=d["sha256"]: raise RuntimeError(f"DEPENDENCY_SHA_MISMATCH {key} actual={got} expected={d['sha256']}")
    ev=import_exact(resolve(cfg["dependencies"]["evidence_script_v044"]["path"]),
                    cfg["dependencies"]["evidence_script_v044"]["sha256"],
                    "optimus_ev_v044_candidate_eval")
    reg=import_exact(resolve(cfg["dependencies"]["registry_script"]["path"]),
                     cfg["dependencies"]["registry_script"]["sha256"],
                     "optimus_reg_v03_candidate_eval")
    v071=json.loads(resolve(cfg["dependencies"]["v071_config"]["path"]).read_text(encoding="utf-8"))
    ecfg=json.loads(resolve(v071["dependency_paths"]["evidence_config"]).read_text(encoding="utf-8"))
    rcfg=reg.load_config(ev.resolve_path(ecfg["registry_config"]))
    return ev,reg,rcfg

def check(cfg):
    steady=run_json(cfg["dependencies"]["steady_checker"]["path"],"check")
    f=cfg["frozen_state"]
    checks={
        "status":steady.get("status")=="PASS_OPTIMUS_STEADY_STATE_V0_7_4_CHECK",
        "baseline":steady.get("baseline_id")==f["baseline_id"],
        "baseline_sha":steady.get("baseline_sha256")==f["baseline_sha256"],
        "registry":steady.get("registry_sha256")==f["registry_sha256"],
        "monitor":int(steady.get("current_monitor_universe_count",-1))==f["current_monitor_universe_count"],
    }
    bad=[k for k,v in checks.items() if not v]
    if bad: raise RuntimeError(f"FROZEN_STATE_MISMATCH {bad}")
    ev,reg,rcfg=runtime(cfg)
    return {
        "status":"PASS_OPTIMUS_COMPONENT_DOMAIN_V0_4_5_CANDIDATE_EVAL_CHECK",
        "baseline_id":steady["baseline_id"],
        "registry_sha256":steady["registry_sha256"],
        "v044_component_map":[{"index":i,"terms":list(t),"domain":d} for i,(t,d) in enumerate(ev.COMPONENT_MAP)],
        "candidate_policy":cfg["candidate_policy"],
        "mutation":False,
    }

def proposal_rows(cfg)->dict[str,dict]:
    root=Path(cfg["proposal_search_root"]); out={}
    if not root.exists(): return out
    for p in root.rglob("proposal_queue.jsonl"):
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip(): continue
            x=json.loads(line); eid=str(x.get("evidence_id") or "")
            if not eid: continue
            if eid in out and stable_json(out[eid])!=stable_json(x):
                raise RuntimeError(f"PROPOSAL_CONFLICT {eid}")
            out[eid]=x
    return out

def registry_rows(reg,rcfg)->dict[str,dict]:
    con=reg.open_db(rcfg,read_only=True)
    try:
        df=reg.latest_effective_evidence(con)
        out={}
        for _,row in df.iterrows():
            x=row.to_dict()
            eid=str(x.get("evidence_id") or "")
            if eid: out[eid]=x
        return out
    finally:
        con.close()

def merged(eid,rr,pr):
    x={}
    if rr: x.update(rr)
    if pr:
        for k,v in pr.items():
            if k not in x or x.get(k) in (None,"",[],{}): x[k]=v
        x["_proposal"]=pr
    if pr and pr.get("acquisition"): x["acquisition"]=pr["acquisition"]
    x["evidence_id"]=eid
    return x

def score_domains(ev,windows):
    rows=[]
    for idx,(terms,domain) in enumerate(ev.COMPONENT_MAP):
        wh=0; occ=0; tc=Counter()
        for w in windows:
            low=w.lower(); hit=False
            for t in terms:
                c=low.count(str(t).lower())
                if c:
                    hit=True; occ+=c; tc[str(t)]+=c
            if hit: wh+=1
        if wh or occ:
            rows.append({"map_index":idx,"domain":domain,"window_hit_count":wh,
                         "occurrence_count":occ,"term_counts":dict(tc)})
    rows.sort(key=lambda z:(-z["window_hit_count"],-z["occurrence_count"],z["map_index"]))
    return rows

def candidate_domain(cfg,old_domain,rows):
    pol=cfg["candidate_policy"]; amb=pol["ambiguous_domain"]; unk=pol["unknown_domain"]
    if not rows:
        return {"candidate_domain":unk,"decision":"NO_HITS","top":None,"second":None}
    if len(rows)==1:
        return {"candidate_domain":rows[0]["domain"],"decision":"SINGLE_DOMAIN","top":rows[0],"second":None}
    top,second=rows[0],rows[1]
    top_unique=top["window_hit_count"]>second["window_hit_count"]
    if top_unique and top["domain"]==old_domain:
        return {"candidate_domain":old_domain,"decision":"OLD_DOMAIN_IS_UNIQUE_TOP","top":top,"second":second}
    margin=top["window_hit_count"]-second["window_hit_count"]
    ratio=(top["window_hit_count"]/second["window_hit_count"]) if second["window_hit_count"]>0 else 999.0
    if top_unique and margin>=int(pol["minimum_window_margin_for_changed_domain"]) and ratio>=float(pol["minimum_ratio_for_changed_domain"]):
        return {"candidate_domain":top["domain"],"decision":"NEW_UNIQUE_DOMINANT_DOMAIN",
                "top":top,"second":second,"margin":margin,"ratio":ratio}
    return {"candidate_domain":amb,"decision":"AMBIGUOUS_MULTI_DOMAIN_PENDING_REVIEW",
            "top":top,"second":second,"margin":margin,"ratio":ratio}

def evaluate_row(cfg,ev,row):
    acq=row.get("acquisition") or {}
    pth=str(acq.get("pdf_path") or "")
    out={"evidence_id":str(row.get("evidence_id") or ""),"symbol":str(row.get("symbol") or ""),
         "company":str(row.get("company") or ""),"old_domain":str(row.get("component_domain") or ""),
         "pdf_path":pth or None,"recomputed":False}
    if not pth or not Path(pth).exists():
        out["skip_reason"]="LOCAL_PDF_MISSING"; return out
    p=Path(pth); text,pages,err=ev.extract_pdf_text(p.read_bytes(),260)
    if err or not text.strip():
        out["skip_reason"]="PDF_TEXT_UNAVAILABLE"; out["pdf_error"]=err; return out
    title=str(acq.get("announcement_title") or row.get("title") or "")
    kws=acq.get("matched_keywords") or row.get("matched_keywords") or []
    if not isinstance(kws,list): kws=[str(kws)]
    evidence_text="\n".join([title," ".join(str(x) for x in kws),text])
    windows=ev.context_windows(evidence_text)
    if not windows: windows=[evidence_text]
    rows=score_domains(ev,windows)
    old_first=ev.component_domain("\n".join(windows) or evidence_text)
    cand=candidate_domain(cfg,out["old_domain"],rows)
    out.update({
        "recomputed":True,"old_first_match_recomputed":old_first,
        "domain_scores":rows,"matched_domain_count":len(rows),
        **cand,
    })
    out["changes_old_domain"]=out["candidate_domain"]!=out["old_domain"]
    out["becomes_ambiguous"]=out["candidate_domain"]==cfg["candidate_policy"]["ambiguous_domain"]
    out["minority_risk_old"]=False
    if rows:
        oldrow=next((x for x in rows if x["domain"]==out["old_domain"]),None)
        if oldrow:
            out["minority_risk_old"]=oldrow["window_hit_count"]<rows[0]["window_hit_count"]
    return out

def run_eval(cfg):
    check(cfg); ev,reg,rcfg=runtime(cfg)
    rr=registry_rows(reg,rcfg); pr=proposal_rows(cfg)
    ids=set()
    for eid,x in rr.items():
        if str(x.get("parser_version") or "")=="OPTIMUS_EVIDENCE_ACQUISITION_V0_4_4": ids.add(eid)
    for eid,x in pr.items():
        if str(x.get("parser_version") or "")=="OPTIMUS_EVIDENCE_ACQUISITION_V0_4_4": ids.add(eid)
    rows=[evaluate_row(cfg,ev,merged(eid,rr.get(eid),pr.get(eid))) for eid in sorted(ids)]
    recomputed=[x for x in rows if x.get("recomputed")]
    target=next((x for x in recomputed if x["evidence_id"]==cfg["audit_basis"]["target_evidence_id"]),None)
    if not target: raise RuntimeError("LEISAI_TARGET_NOT_RECOMPUTED")
    if target["candidate_domain"]!="control / encoders":
        raise RuntimeError(f"LEISAI_CANDIDATE_NOT_CONTROL actual={target['candidate_domain']}")

    changed=[x for x in recomputed if x.get("changes_old_domain")]
    ambiguous=[x for x in recomputed if x.get("becomes_ambiguous")]
    dominant_changes=[x for x in changed if not x.get("becomes_ambiguous")]
    minority=[x for x in recomputed if x.get("minority_risk_old")]
    minority_resolved=[x for x in minority if x.get("candidate_domain")!=x.get("old_domain")]

    by_company={}
    for x in changed:
        by_company.setdefault(x["symbol"],{"symbol":x["symbol"],"company":x["company"],"rows":0,
                                           "dominant_changes":0,"ambiguous":0,"transitions":Counter()})
        z=by_company[x["symbol"]]; z["rows"]+=1
        if x["becomes_ambiguous"]: z["ambiguous"]+=1
        else: z["dominant_changes"]+=1
        z["transitions"][f"{x['old_domain']} -> {x['candidate_domain']}"]+=1
    company_rows=[]
    for z in by_company.values():
        z=dict(z); z["transitions"]=dict(z["transitions"]); company_rows.append(z)
    company_rows.sort(key=lambda x:(-x["rows"],x["symbol"]))

    summary={
        "schema_version":"OPTIMUS_COMPONENT_DOMAIN_V0_4_5_CANDIDATE_EVALUATION",
        "evaluation_id":cfg["evaluation_id"],
        "created_at":now_cst().isoformat(timespec="seconds"),
        "candidate_policy":cfg["candidate_policy"],
        "counts":{
            "rows_considered":len(rows),
            "recomputed_rows":len(recomputed),
            "old_minority_risk_rows":len(minority),
            "candidate_changed_rows":len(changed),
            "candidate_dominant_domain_changes":len(dominant_changes),
            "candidate_ambiguous_rows":len(ambiguous),
            "minority_risk_rows_no_longer_kept_as_old_domain":len(minority_resolved),
        },
        "leisai_target":{
            "evidence_id":target["evidence_id"],"old_domain":target["old_domain"],
            "candidate_domain":target["candidate_domain"],"decision":target["decision"],
            "top":target.get("top"),"second":target.get("second"),
            "changes_old_domain":target["changes_old_domain"],
        },
        "changed_rows":[{
            "evidence_id":x["evidence_id"],"symbol":x["symbol"],"company":x["company"],
            "old_domain":x["old_domain"],"candidate_domain":x["candidate_domain"],
            "decision":x["decision"],"top":x.get("top"),"second":x.get("second"),
            "minority_risk_old":x.get("minority_risk_old"),
        } for x in changed],
        "company_impact":company_rows,
        "adoption_gate":{
            "historical_reclassification":False,
            "future_only":True,
            "parser_version_if_adopted":"OPTIMUS_EVIDENCE_ACQUISITION_V0_4_5",
            "requires_human_approval":True,
            "requires_downstream_hash_rebase":True,
            "requires_B8_dedupe_contract_review":True,
            "recommended_next":"HUMAN_REVIEW_CHANGED_ROWS_THEN_DECIDE_V0_4_5_PATCH"
        },
        "mutation":False,"registry_mutation":False,"canonicalization_mutation":False,
        "theme_stage_mutation":False,"broker":False,"orders":False,"production_db":False,
    }
    stamp=now_cst().strftime("%Y%m%dT%H%M%S+0800")
    out=Path(cfg["output_root"])/stamp; out.mkdir(parents=True,exist_ok=False)
    rows_path=out/"row_evaluation.jsonl"
    rows_path.write_text("".join(json.dumps(x,ensure_ascii=False,sort_keys=True,default=str)+"\n" for x in rows),encoding="utf-8")
    summary["rows_file"]=str(rows_path); summary["rows_sha256"]=sha256_file(rows_path)
    summary["manifest_payload_sha256"]=stable_sha(summary)
    (out/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    (Path(cfg["output_root"])/"latest_summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    return {"status":"PASS_OPTIMUS_COMPONENT_DOMAIN_V0_4_5_CANDIDATE_EVALUATION","output":str(out),**summary}

def status(cfg):
    p=Path(cfg["output_root"])/"latest_summary.json"
    if not p.exists(): return {"status":"ARMED_OPTIMUS_COMPONENT_DOMAIN_V0_4_5_CANDIDATE_EVALUATION_NOT_RUN","mutation":False}
    x=json.loads(p.read_text(encoding="utf-8"))
    return {"status":"PASS_OPTIMUS_COMPONENT_DOMAIN_V0_4_5_CANDIDATE_EVALUATION_STATUS",
            "counts":x.get("counts"),"leisai_target":x.get("leisai_target"),
            "company_impact":x.get("company_impact"),"adoption_gate":x.get("adoption_gate"),
            "manifest_payload_sha256":x.get("manifest_payload_sha256"),"mutation":False}

def main()->int:
    ap=argparse.ArgumentParser(); ap.add_argument("command",choices=["check","run","status"]); a=ap.parse_args()
    cfg=load_cfg()
    try:
        out=check(cfg) if a.command=="check" else run_eval(cfg) if a.command=="run" else status(cfg)
        print(json.dumps(out,ensure_ascii=False,indent=2,default=str)); return 0
    except Exception as e:
        print(json.dumps({"status":f"FAIL_OPTIMUS_COMPONENT_DOMAIN_V0_4_5_CANDIDATE_EVAL_{a.command.upper()}",
                         "error":repr(e),"mutation":False,"registry_mutation":False,
                         "canonicalization_mutation":False,"theme_stage_mutation":False,
                         "broker":False,"orders":False,"production_db":False},
                        ensure_ascii=False,indent=2)); return 2

if __name__=="__main__":
    raise SystemExit(main())
