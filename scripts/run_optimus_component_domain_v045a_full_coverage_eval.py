from __future__ import annotations
import argparse, hashlib, json, os, subprocess, sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT=Path(__file__).resolve().parents[1]
CONFIG=PROJECT_ROOT/"config"/"research"/"optimus_component_domain_v045a_full_coverage_eval.json"
CST=timezone(timedelta(hours=8))

def now_cst():
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

def load_cfg():
    return json.loads(CONFIG.read_text(encoding="utf-8"))

def resolve(rel:str)->Path:
    p=Path(rel)
    return p if p.is_absolute() else PROJECT_ROOT/p

def child_env():
    e=os.environ.copy(); e["PYTHONIOENCODING"]="utf-8"; e["PYTHONUTF8"]="1"; return e

def run_json(rel:str,*args):
    r=subprocess.run([sys.executable,str(resolve(rel)),*args],cwd=PROJECT_ROOT,
        capture_output=True,text=True,encoding="utf-8",errors="strict",env=child_env())
    if r.returncode!=0:
        raise RuntimeError(f"COMMAND_FAILED {rel} rc={r.returncode} stdout={r.stdout[:1200]!r} stderr={r.stderr[:1200]!r}")
    i=r.stdout.find("{")
    if i<0: raise RuntimeError(f"NO_JSON_OUTPUT {rel}")
    return json.loads(r.stdout[i:])

def verify_dependencies(cfg):
    for key,d in cfg["dependencies"].items():
        p=resolve(d["path"])
        if not p.exists(): raise RuntimeError(f"DEPENDENCY_MISSING {key} {p}")
        got=sha256_file(p)
        if got!=d["sha256"]:
            raise RuntimeError(f"DEPENDENCY_SHA_MISMATCH {key} actual={got} expected={d['sha256']}")

def verify_frozen_state(cfg):
    verify_dependencies(cfg)
    steady=run_json(cfg["dependencies"]["steady_checker"]["path"],"check")
    f=cfg["frozen_state"]
    checks={
        "status":steady.get("status")=="PASS_OPTIMUS_STEADY_STATE_V0_7_4_CHECK",
        "baseline_id":steady.get("baseline_id")==f["baseline_id"],
        "baseline_sha":steady.get("baseline_sha256")==f["baseline_sha256"],
        "registry_sha":steady.get("registry_sha256")==f["registry_sha256"],
    }
    bad=[k for k,v in checks.items() if not v]
    if bad: raise RuntimeError(f"FROZEN_STATE_MISMATCH {bad}")
    return steady

def load_audit_asset(cfg):
    a=cfg["audit_asset"]
    sp=Path(a["latest_summary_path"])
    if not sp.exists():
        raise RuntimeError(f"AUDIT_SUMMARY_MISSING {sp}")
    summary=json.loads(sp.read_text(encoding="utf-8"))
    if summary.get("audit_id")!=a["audit_id"]:
        raise RuntimeError(f"AUDIT_ID_MISMATCH actual={summary.get('audit_id')}")
    supplied=summary.get("manifest_payload_sha256")
    core=dict(summary); core.pop("manifest_payload_sha256",None)
    recomputed_manifest=stable_sha(core)
    if supplied!=a["manifest_payload_sha256"] or recomputed_manifest!=a["manifest_payload_sha256"]:
        raise RuntimeError(
            f"AUDIT_MANIFEST_MISMATCH supplied={supplied} recomputed={recomputed_manifest} expected={a['manifest_payload_sha256']}"
        )
    counts=summary.get("counts") or {}
    if counts!=a["expected_counts"]:
        raise RuntimeError(f"AUDIT_COUNTS_MISMATCH actual={counts} expected={a['expected_counts']}")
    rp=Path(str(summary.get("rows_file") or ""))
    if not rp.exists():
        raise RuntimeError(f"AUDIT_ROWS_MISSING {rp}")
    got=sha256_file(rp)
    if got!=a["rows_sha256"] or summary.get("rows_sha256")!=a["rows_sha256"]:
        raise RuntimeError(f"AUDIT_ROWS_SHA_MISMATCH actual={got} summary={summary.get('rows_sha256')} expected={a['rows_sha256']}")
    rows=[json.loads(line) for line in rp.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows)!=a["expected_counts"]["evidence_rows_considered"]:
        raise RuntimeError(f"AUDIT_ROW_COUNT_MISMATCH actual={len(rows)}")
    recomputed=[r for r in rows if r.get("recomputed")]
    if len(recomputed)!=a["expected_counts"]["recomputed_rows"]:
        raise RuntimeError(f"AUDIT_RECOMPUTED_COUNT_MISMATCH actual={len(recomputed)}")
    return summary,rows

def sorted_scores(row):
    stats=list(row.get("domain_stats") or [])
    stats.sort(key=lambda z:(-int(z.get("window_hit_count",0)),
                             -int(z.get("occurrence_count",0)),
                             int(z.get("map_index",999))))
    return stats

def candidate_domain(cfg,old_domain,rows):
    pol=cfg["candidate_policy"]
    if not rows:
        return {"candidate_domain":pol["unknown_domain"],"decision":"NO_HITS","top":None,"second":None}
    if len(rows)==1:
        return {"candidate_domain":rows[0]["domain"],"decision":"SINGLE_DOMAIN","top":rows[0],"second":None}
    top,second=rows[0],rows[1]
    top_unique=int(top["window_hit_count"])>int(second["window_hit_count"])
    if top_unique and top["domain"]==old_domain:
        return {"candidate_domain":old_domain,"decision":"OLD_DOMAIN_IS_UNIQUE_TOP","top":top,"second":second}
    margin=int(top["window_hit_count"])-int(second["window_hit_count"])
    ratio=(int(top["window_hit_count"])/int(second["window_hit_count"])) if int(second["window_hit_count"])>0 else 999.0
    if top_unique and margin>=int(pol["minimum_window_margin_for_changed_domain"]) and ratio>=float(pol["minimum_ratio_for_changed_domain"]):
        return {"candidate_domain":top["domain"],"decision":"NEW_UNIQUE_DOMINANT_DOMAIN",
                "top":top,"second":second,"margin":margin,"ratio":ratio}
    return {"candidate_domain":pol["ambiguous_domain"],"decision":"AMBIGUOUS_MULTI_DOMAIN_PENDING_REVIEW",
            "top":top,"second":second,"margin":margin,"ratio":ratio}

def evaluate(cfg,row):
    old=str(row.get("stored_domain") or "")
    scores=sorted_scores(row)
    cand=candidate_domain(cfg,old,scores)
    out={
        "evidence_id":row.get("evidence_id"),
        "symbol":row.get("symbol"),
        "company":row.get("company"),
        "old_domain":old,
        "old_recomputed_first_match_domain":row.get("recomputed_first_match_domain"),
        "old_order_sensitive":bool(row.get("order_sensitive")),
        "old_first_match_minority_risk":bool(row.get("first_match_minority_risk")),
        "scores":scores,
        **cand,
    }
    out["changes_old_domain"]=out["candidate_domain"]!=old
    out["becomes_ambiguous"]=out["candidate_domain"]==cfg["candidate_policy"]["ambiguous_domain"]
    return out

def check(cfg):
    steady=verify_frozen_state(cfg)
    summary,rows=load_audit_asset(cfg)
    return {
        "status":"PASS_OPTIMUS_COMPONENT_DOMAIN_V0_4_5A_FULL_COVERAGE_EVAL_CHECK",
        "baseline_id":steady["baseline_id"],
        "registry_sha256":steady["registry_sha256"],
        "audit_manifest_payload_sha256":summary["manifest_payload_sha256"],
        "audit_rows_sha256":summary["rows_sha256"],
        "audit_counts":summary["counts"],
        "candidate_policy":cfg["candidate_policy"],
        "mutation":False,
    }

def run_eval(cfg):
    check(cfg)
    summary,rows=load_audit_asset(cfg)
    evals=[evaluate(cfg,r) for r in rows if r.get("recomputed")]
    if len(evals)!=cfg["audit_asset"]["expected_counts"]["recomputed_rows"]:
        raise RuntimeError(f"FULL_COVERAGE_LOST actual={len(evals)}")

    target=next((x for x in evals if x["evidence_id"]==cfg["target"]["evidence_id"]),None)
    if not target:
        raise RuntimeError("LEISAI_TARGET_MISSING")
    if target["old_domain"]!=cfg["target"]["old_domain"]:
        raise RuntimeError(f"LEISAI_OLD_DOMAIN_MISMATCH actual={target['old_domain']}")
    if target["candidate_domain"]!=cfg["target"]["expected_candidate_domain"]:
        raise RuntimeError(f"LEISAI_CANDIDATE_MISMATCH actual={target['candidate_domain']}")

    changed=[x for x in evals if x["changes_old_domain"]]
    dominant=[x for x in changed if not x["becomes_ambiguous"]]
    ambiguous=[x for x in evals if x["becomes_ambiguous"]]
    old_minority=[x for x in evals if x["old_first_match_minority_risk"]]
    minority_fixed=[x for x in old_minority if x["candidate_domain"]!=x["old_domain"]]
    minority_still_old=[x for x in old_minority if x["candidate_domain"]==x["old_domain"]]

    company={}
    for x in changed:
        key=(x["symbol"],x["company"])
        z=company.setdefault(key,{
            "symbol":x["symbol"],"company":x["company"],"changed_rows":0,
            "dominant_changes":0,"ambiguous_rows":0,"transitions":Counter()
        })
        z["changed_rows"]+=1
        if x["becomes_ambiguous"]: z["ambiguous_rows"]+=1
        else: z["dominant_changes"]+=1
        z["transitions"][f"{x['old_domain']} -> {x['candidate_domain']}"]+=1
    company_impact=[]
    for z in company.values():
        z=dict(z); z["transitions"]=dict(z["transitions"]); company_impact.append(z)
    company_impact.sort(key=lambda z:(-z["changed_rows"],z["symbol"]))

    decision="DO_NOT_ADOPT_YET"
    rationale=[]
    if len(evals)==50 and len(old_minority)==8 and len(minority_still_old)==0:
        decision="ELIGIBLE_FOR_HUMAN_REVIEW"
        rationale.append("FULL_50_ROW_COVERAGE_RESTORED")
        rationale.append("ALL_8_FIRST_MATCH_MINORITY_RISK_ROWS_NO_LONGER_KEEP_OLD_FIRST_MATCH_DOMAIN")
    else:
        if len(evals)!=50: rationale.append("FULL_COVERAGE_NOT_50")
        if len(old_minority)!=8: rationale.append("AUDIT_MINORITY_RISK_COUNT_NOT_8")
        if minority_still_old: rationale.append("SOME_MINORITY_RISK_ROWS_STILL_KEEP_OLD_DOMAIN")

    result={
        "schema_version":"OPTIMUS_COMPONENT_DOMAIN_V0_4_5A_FULL_COVERAGE_EVALUATION",
        "evaluation_id":cfg["evaluation_id"],
        "created_at":now_cst().isoformat(timespec="seconds"),
        "source_audit_manifest_payload_sha256":summary["manifest_payload_sha256"],
        "source_audit_rows_sha256":summary["rows_sha256"],
        "candidate_policy":cfg["candidate_policy"],
        "counts":{
            "evaluated_rows":len(evals),
            "old_order_sensitive_rows":sum(x["old_order_sensitive"] for x in evals),
            "old_first_match_minority_risk_rows":len(old_minority),
            "candidate_changed_rows":len(changed),
            "candidate_dominant_domain_changes":len(dominant),
            "candidate_ambiguous_rows":len(ambiguous),
            "minority_risk_rows_no_longer_kept_as_old_domain":len(minority_fixed),
            "minority_risk_rows_still_kept_as_old_domain":len(minority_still_old),
        },
        "leisai_target":target,
        "changed_rows":changed,
        "ambiguous_rows":ambiguous,
        "minority_risk_rows_still_old":minority_still_old,
        "company_impact":company_impact,
        "adoption_decision":decision,
        "adoption_rationale":rationale,
        "adoption_gate":{
            "historical_reclassification":False,
            "future_only":True,
            "parser_version_if_adopted":"OPTIMUS_EVIDENCE_ACQUISITION_V0_4_5",
            "requires_explicit_human_approval":True,
            "requires_B8_dedupe_contract_review":True,
            "requires_downstream_hash_rebase":True,
            "no_patch_generated_by_this_evaluation":True,
        },
        "mutation":False,
        "registry_mutation":False,
        "canonicalization_mutation":False,
        "theme_stage_mutation":False,
        "broker":False,
        "orders":False,
        "production_db":False,
    }
    stamp=now_cst().strftime("%Y%m%dT%H%M%S+0800")
    out=Path(cfg["output_root"])/stamp
    out.mkdir(parents=True,exist_ok=False)
    rows_path=out/"full_coverage_evaluation.jsonl"
    rows_path.write_text("".join(json.dumps(x,ensure_ascii=False,sort_keys=True,default=str)+"\n" for x in evals),encoding="utf-8")
    result["rows_file"]=str(rows_path)
    result["rows_sha256"]=sha256_file(rows_path)
    result["manifest_payload_sha256"]=stable_sha(result)
    (out/"summary.json").write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    (Path(cfg["output_root"])/"latest_summary.json").write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    return {"status":"PASS_OPTIMUS_COMPONENT_DOMAIN_V0_4_5A_FULL_COVERAGE_EVALUATION",
            "output":str(out),**result}

def status(cfg):
    p=Path(cfg["output_root"])/"latest_summary.json"
    if not p.exists():
        return {"status":"ARMED_OPTIMUS_COMPONENT_DOMAIN_V0_4_5A_FULL_COVERAGE_EVALUATION_NOT_RUN","mutation":False}
    x=json.loads(p.read_text(encoding="utf-8"))
    return {
        "status":"PASS_OPTIMUS_COMPONENT_DOMAIN_V0_4_5A_FULL_COVERAGE_EVALUATION_STATUS",
        "counts":x.get("counts"),
        "leisai_target":x.get("leisai_target"),
        "company_impact":x.get("company_impact"),
        "adoption_decision":x.get("adoption_decision"),
        "adoption_rationale":x.get("adoption_rationale"),
        "manifest_payload_sha256":x.get("manifest_payload_sha256"),
        "mutation":False,
    }

def main()->int:
    ap=argparse.ArgumentParser()
    ap.add_argument("command",choices=["check","run","status"])
    a=ap.parse_args()
    cfg=load_cfg()
    try:
        out=check(cfg) if a.command=="check" else run_eval(cfg) if a.command=="run" else status(cfg)
        print(json.dumps(out,ensure_ascii=False,indent=2,default=str)); return 0
    except Exception as e:
        print(json.dumps({
            "status":f"FAIL_OPTIMUS_COMPONENT_DOMAIN_V0_4_5A_FULL_COVERAGE_EVAL_{a.command.upper()}",
            "error":repr(e),"mutation":False,"registry_mutation":False,
            "canonicalization_mutation":False,"theme_stage_mutation":False,
            "broker":False,"orders":False,"production_db":False,
        },ensure_ascii=False,indent=2)); return 2

if __name__=="__main__":
    raise SystemExit(main())
