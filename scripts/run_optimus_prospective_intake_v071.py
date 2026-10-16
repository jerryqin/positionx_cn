from __future__ import annotations
import argparse, hashlib, importlib.util, json, subprocess, sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG = PROJECT_ROOT / "config" / "research" / "optimus_prospective_intake_v071.json"

CST = timezone(timedelta(hours=8))

def sha256_file(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda:f.read(1024*1024), b""):
            h.update(b)
    return h.hexdigest()

def stable_json(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

def sha256_json(x: Any) -> str:
    return hashlib.sha256(stable_json(x).encode("utf-8")).hexdigest()

def now_cst() -> datetime:
    return datetime.now(CST)

def load_config() -> dict[str,Any]:
    return json.loads(CONFIG.read_text(encoding="utf-8"))

def resolve(rel: str) -> Path:
    p=Path(rel)
    return p if p.is_absolute() else PROJECT_ROOT/p

def import_module(path: Path, name: str):
    spec=importlib.util.spec_from_file_location(name,path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"CANNOT_IMPORT {path}")
    mod=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=mod
    spec.loader.exec_module(mod)
    return mod

def run_json(script: Path, command: str) -> dict[str,Any]:
    p=subprocess.run([sys.executable,str(script),command],cwd=PROJECT_ROOT,
                     capture_output=True,text=True)
    if p.returncode!=0:
        raise RuntimeError(f"COMMAND_FAILED {script.name} {command} rc={p.returncode} stderr={p.stderr}")
    try:
        return json.loads(p.stdout)
    except Exception as exc:
        raise RuntimeError(f"NON_JSON_OUTPUT {script.name} {command}: {exc!r}")

def dependency_check(cfg: dict[str,Any]) -> dict[str,Any]:
    paths=cfg["dependency_paths"]
    shas=cfg["dependency_sha256"]
    actual={}
    for key in ("v07_script","v07_config","evidence_script","registry_script"):
        p=resolve(paths[key])
        if not p.exists():
            raise RuntimeError(f"DEPENDENCY_MISSING {key} {p}")
        actual[key]=sha256_file(p)
        if actual[key]!=shas[key]:
            raise RuntimeError(f"DEPENDENCY_SHA_MISMATCH {key} actual={actual[key]} expected={shas[key]}")
    evidence_cfg=resolve(paths["evidence_config"])
    if not evidence_cfg.exists():
        raise RuntimeError(f"EVIDENCE_CONFIG_MISSING {evidence_cfg}")

    v07=run_json(resolve(paths["v07_script"]),"check")
    if v07.get("status")!="PASS_OPTIMUS_PROSPECTIVE_MONITOR_V0_7_BASELINE_CHECK":
        raise RuntimeError(f"V07_BASELINE_NOT_PASS {v07}")

    return {
        "status":"PASS_OPTIMUS_PROSPECTIVE_INTAKE_V0_7_1_CHECK",
        "intake_id":cfg["intake_id"],
        "v07_status":v07["status"],
        "baseline_registry_sha256":v07["baseline_registry_sha256"],
        "candidate_count":v07["candidate_count"],
        "active_canonical":v07["active_canonical"],
        "discovery_only":v07.get("discovery_only",0),
        "monitor_universe_count":v07.get("monitor_universe_count",22),
        "active_seed":v07["active_seed"],
        "fact_count":v07["fact_count"],
        "materialized_fact_evidence_count":v07["materialized_fact_evidence_count"],
        "dependency_sha256":actual,
        "scope":cfg["scope"],
        "safety":cfg["safety"],
        "next":"RUN_READ_ONLY_DAILY_INTAKE",
        "mutation":False,
    }

def load_v07_universe(cfg: dict[str,Any]) -> list[dict[str,str]]:
    vcfg=json.loads(resolve(cfg["dependency_paths"]["v07_config"]).read_text(encoding="utf-8"))
    rows=vcfg.get("universe") or []
    if len(rows)!=22 or len({str(x["symbol"]).upper() for x in rows})!=22:
        raise RuntimeError("V07_UNIVERSE_NOT_EXACT_22")
    return [{"symbol":str(x["symbol"]).upper(),"company":str(x["company"])} for x in rows]

def registry_existing_ids(evidence_mod, evidence_cfg: dict[str,Any]) -> set[str]:
    reg=evidence_mod.load_registry_module(evidence_cfg)
    rcfg=reg.load_config(evidence_mod.resolve_path(evidence_cfg["registry_config"]))
    con=reg.open_db(rcfg,read_only=True)
    try:
        ev=reg.latest_effective_evidence(con)
        if ev.empty or "evidence_id" not in ev.columns:
            return set()
        return set(ev["evidence_id"].dropna().astype(str).tolist())
    finally:
        con.close()

def _clean_identity_value(v: Any) -> str:
    if v is None:
        return ""
    s=str(v).strip()
    if not s or s.lower() in {"nan","none","<na>"}:
        return ""
    return s

def source_identity(row: dict[str,Any]) -> str:
    symbol=_clean_identity_value(row.get("symbol")).upper()
    source_sha=_clean_identity_value(row.get("source_sha256"))
    source_ref=_clean_identity_value(row.get("source_ref"))
    if source_sha:
        return f"{symbol}|sha256:{source_sha}"
    if source_ref:
        return f"{symbol}|ref:{source_ref}"
    return ""

def registry_existing_source_identities(evidence_mod, evidence_cfg: dict[str,Any]) -> set[str]:
    reg=evidence_mod.load_registry_module(evidence_cfg)
    rcfg=reg.load_config(evidence_mod.resolve_path(evidence_cfg["registry_config"]))
    con=reg.open_db(rcfg,read_only=True)
    try:
        ev=reg.latest_effective_evidence(con)
        if ev.empty:
            return set()
        out=set()
        for _,r in ev.iterrows():
            sid=source_identity(r.to_dict())
            if sid:
                out.add(sid)
        return out
    finally:
        con.close()

def previous_monitor_source_identities(output_root: Path) -> set[str]:
    out=set()
    if not output_root.exists():
        return out
    for p in output_root.rglob("proposal_queue.jsonl"):
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            sid=source_identity(json.loads(line))
            if sid:
                out.add(sid)
    return out

def previous_monitor_ids(output_root: Path) -> set[str]:
    ids=set()
    if not output_root.exists():
        return ids
    for p in output_root.rglob("proposal_queue.jsonl"):
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                if not line.strip(): continue
                obj=json.loads(line)
                eid=str(obj.get("evidence_id") or "")
                if eid: ids.add(eid)
        except Exception:
            # Prior artifact corruption must not be silently ignored.
            raise RuntimeError(f"PRIOR_QUEUE_READ_FAILED {p}")
    return ids

def queue_class(ev: dict[str,Any]) -> str:
    csev=str(ev.get("contradiction_severity") or "").upper()
    if csev=="HARD":
        return "CONTRADICTION_ALERT"
    if (str(ev.get("component_domain") or "")=="AMBIGUOUS_MULTI_DOMAIN_PENDING_REVIEW"
        or bool((ev.get("acquisition") or {}).get("component_domain_review_required"))):
        return "HUMAN_REVIEW_REQUIRED"
    t=int(ev.get("tesla_link_level") or 0)
    if t>=3:
        return "STAGE2_GATE_CANDIDATE"
    if t==2:
        return "HUMAN_REVIEW_REQUIRED"
    return "EVIDENCE_PROPOSAL"

def write_jsonl(path: Path, rows: list[dict[str,Any]]) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    data="".join(json.dumps(x,ensure_ascii=False,sort_keys=True,default=str)+"\n" for x in rows)
    path.write_text(data,encoding="utf-8")

def capture(cfg: dict[str,Any], dates: list[date]) -> dict[str,Any]:
    dependency_check(cfg)
    universe=load_v07_universe(cfg)
    scope={x["symbol"] for x in universe}
    company_by_symbol={x["symbol"]:x["company"] for x in universe}

    evidence_script=resolve(cfg["dependency_paths"]["evidence_script"])
    evidence_mod=import_module(evidence_script,"positionx_optimus_evidence_v045_readonly_v071")
    ecfg=json.loads(resolve(cfg["dependency_paths"]["evidence_config"]).read_text(encoding="utf-8"))

    observed=now_cst().isoformat(timespec="seconds")
    capture_id=now_cst().strftime("%Y%m%dT%H%M%S+0800")
    output_root=Path(cfg["output_root"])
    run_root=output_root/max(dates).strftime("%Y%m%d")/capture_id
    raw_root=run_root/"cninfo_raw"
    raw_root.mkdir(parents=True,exist_ok=False)

    existing_ids=registry_existing_ids(evidence_mod,ecfg)
    prior_ids=previous_monitor_ids(output_root)
    existing_source_ids=registry_existing_source_identities(evidence_mod,ecfg)
    prior_source_ids=previous_monitor_source_identities(output_root)

    by_ann,qstats=evidence_mod.query_seed_all_announcements(ecfg,dates,scope,raw_root)
    audits=[]
    proposals=[]
    skipped_known=0
    skipped_seen=0
    skipped_existing_source_across_parser=0
    skipped_prior_source_across_parser=0
    scanned_not_relevant=0

    for i,ann_id in enumerate(sorted(by_ann),1):
        item=by_ann[ann_id]
        print(f"[V0.7.1] {i}/{len(by_ann)} {item['symbol']} announcement={ann_id}",
              file=sys.stderr,flush=True)
        ev,au=evidence_mod.build_evidence(ecfg,item,run_root,observed)
        au=dict(au)
        au["monitor_scope_company"]=company_by_symbol.get(item["symbol"])
        if ev is None:
            scanned_not_relevant+=1
            au["monitor_disposition"]="SCANNED_NOT_RELEVANT"
            audits.append(au)
            continue

        sid=source_identity(ev)
        if sid and sid in existing_source_ids:
            skipped_existing_source_across_parser+=1
            au["monitor_disposition"]="SKIP_EXISTING_SOURCE_ACROSS_PARSER_VERSIONS"
            au["source_identity"]=sid
            audits.append(au)
            continue
        if sid and sid in prior_source_ids:
            skipped_prior_source_across_parser+=1
            au["monitor_disposition"]="SKIP_PRIOR_MONITOR_SOURCE_ACROSS_PARSER_VERSIONS"
            au["source_identity"]=sid
            audits.append(au)
            continue

        eid=str(ev["evidence_id"])
        if eid in existing_ids:
            skipped_known+=1
            au["monitor_disposition"]="SKIP_KNOWN_REGISTRY_EVIDENCE"
            audits.append(au)
            continue
        if eid in prior_ids:
            skipped_seen+=1
            au["monitor_disposition"]="SKIP_ALREADY_QUEUED"
            audits.append(au)
            continue

        out_class=queue_class(ev)
        qrow={
            "queue_schema":"OPTIMUS_PROSPECTIVE_PROPOSAL_V0_7_1",
            "queue_state":out_class,
            "human_review_required":True,
            "evidence_id":eid,
            "symbol":ev["symbol"],
            "company":ev["company"],
            "component_domain":ev.get("component_domain"),
            "component_domain_decision":(ev.get("acquisition") or {}).get("component_domain_decision"),
            "component_domain_review_required":bool((ev.get("acquisition") or {}).get("component_domain_review_required")),
            "evidence_level":ev.get("evidence_level"),
            "tesla_link_level":ev.get("tesla_link_level"),
            "contradiction_kind":ev.get("contradiction_kind"),
            "contradiction_severity":ev.get("contradiction_severity"),
            "event_time":ev.get("event_time"),
            "observed_at":ev.get("observed_at"),
            "source_type":ev.get("source_type"),
            "source_ref":ev.get("source_ref"),
            "source_sha256":ev.get("source_sha256"),
            "parser_version":ev.get("parser_version"),
            "interpretation":ev.get("interpretation"),
            "acquisition":ev.get("acquisition"),
            "monitor_policy":{
                "auto_canonical":False,
                "auto_candidate_state_change":False,
                "auto_theme_stage_change":False,
                "registry_mutation":False,
                "canonicalization_mutation":False,
            }
        }
        proposals.append(qrow)
        au["monitor_disposition"]=out_class
        audits.append(au)

    queue_file=run_root/"proposal_queue.jsonl"
    audit_file=run_root/"audit.jsonl"
    write_jsonl(queue_file,proposals)
    write_jsonl(audit_file,audits)

    counts={}
    for x in proposals:
        k=x["queue_state"]
        counts[k]=counts.get(k,0)+1

    manifest={
        "schema_version":"OPTIMUS_PROSPECTIVE_INTAKE_V0_7_1",
        "intake_id":cfg["intake_id"],
        "capture_id":capture_id,
        "observed_at":observed,
        "date_min":min(dates).isoformat(),
        "date_max":max(dates).isoformat(),
        "scope":cfg["scope"]["lane"],
        "scope_symbol_count":len(scope),
        "query_stats":qstats,
        "announcement_count":len(by_ann),
        "scanned_not_relevant":scanned_not_relevant,
        "skipped_known_registry_evidence":skipped_known,
        "skipped_already_queued":skipped_seen,
        "skipped_existing_source_across_parser":skipped_existing_source_across_parser,
        "skipped_prior_source_across_parser":skipped_prior_source_across_parser,
        "proposal_count":len(proposals),
        "queue_state_counts":counts,
        "proposal_queue_file":str(queue_file),
        "proposal_queue_sha256":sha256_file(queue_file),
        "audit_file":str(audit_file),
        "audit_sha256":sha256_file(audit_file),
        "theme_stage_change":False,
        "registry_mutation":False,
        "canonicalization_mutation":False,
        "evidence_inbox_write":False,
        "broker":False,
        "orders":False,
        "production_db":False,
    }
    manifest["manifest_payload_sha256"]=sha256_json(manifest)
    (run_root/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2,default=str),encoding="utf-8")

    quality="PASS" if not (qstats.get("errors") or []) else "PARTIAL"
    return {
        "status":"PASS_OPTIMUS_PROSPECTIVE_INTAKE_V0_7_1" if quality=="PASS"
                 else "PARTIAL_OPTIMUS_PROSPECTIVE_INTAKE_V0_7_1",
        "quality":quality,
        "output":str(run_root),
        "date_min":manifest["date_min"],
        "date_max":manifest["date_max"],
        "announcement_count":manifest["announcement_count"],
        "proposal_count":manifest["proposal_count"],
        "queue_state_counts":counts,
        "skipped_known_registry_evidence":skipped_known,
        "skipped_already_queued":skipped_seen,
        "skipped_existing_source_across_parser":skipped_existing_source_across_parser,
        "skipped_prior_source_across_parser":skipped_prior_source_across_parser,
        "scanned_not_relevant":scanned_not_relevant,
        "query_errors":qstats.get("errors") or [],
        "manifest_payload_sha256":manifest["manifest_payload_sha256"],
        "theme_stage_change":False,
        "registry_mutation":False,
        "canonicalization_mutation":False,
        "broker":False,
        "orders":False,
        "production_db":False,
    }

def latest_manifest(cfg: dict[str,Any]) -> dict[str,Any]:
    root=Path(cfg["output_root"])
    files=sorted(root.rglob("manifest.json"),key=lambda p:p.stat().st_mtime,reverse=True) if root.exists() else []
    if not files:
        return {
            "status":"ARMED_OPTIMUS_PROSPECTIVE_INTAKE_V0_7_1_NO_RUNS",
            "intake_id":cfg["intake_id"],
            "mutation":False
        }
    m=json.loads(files[0].read_text(encoding="utf-8"))
    return {
        "status":"PASS_OPTIMUS_PROSPECTIVE_INTAKE_V0_7_1_STATUS",
        "latest_manifest":str(files[0]),
        "capture_id":m["capture_id"],
        "date_min":m["date_min"],
        "date_max":m["date_max"],
        "proposal_count":m["proposal_count"],
        "queue_state_counts":m["queue_state_counts"],
        "manifest_payload_sha256":m["manifest_payload_sha256"],
        "registry_mutation":False,
        "canonicalization_mutation":False,
        "theme_stage_change":False,
    }

def latest_queue(cfg: dict[str,Any]) -> dict[str,Any]:
    root=Path(cfg["output_root"])
    files=sorted(root.rglob("proposal_queue.jsonl"),key=lambda p:p.stat().st_mtime,reverse=True) if root.exists() else []
    if not files:
        return {
            "status":"PASS_OPTIMUS_PROSPECTIVE_QUEUE_V0_7_1",
            "proposal_count":0,
            "proposals":[],
            "mutation":False
        }
    rows=[json.loads(x) for x in files[0].read_text(encoding="utf-8").splitlines() if x.strip()]
    return {
        "status":"PASS_OPTIMUS_PROSPECTIVE_QUEUE_V0_7_1",
        "queue_file":str(files[0]),
        "proposal_count":len(rows),
        "proposals":[{
            "queue_state":x["queue_state"],
            "evidence_id":x["evidence_id"],
            "symbol":x["symbol"],
            "company":x["company"],
            "evidence_level":x["evidence_level"],
            "tesla_link_level":x["tesla_link_level"],
            "contradiction_kind":x["contradiction_kind"],
            "source_ref":x["source_ref"],
            "event_time":x["event_time"],
        } for x in rows],
        "human_review_required":True,
        "mutation":False,
    }

def main()->int:
    ap=argparse.ArgumentParser()
    sp=ap.add_subparsers(dest="command",required=True)
    sp.add_parser("check")
    d=sp.add_parser("daily"); d.add_argument("--days",type=int,default=1)
    r=sp.add_parser("run-date"); r.add_argument("--date",required=True)
    sp.add_parser("status")
    sp.add_parser("queue")
    a=ap.parse_args()
    cfg=load_config()
    try:
        if a.command=="check":
            out=dependency_check(cfg)
        elif a.command=="daily":
            if a.days<1 or a.days>31:
                raise RuntimeError("days must be 1..31")
            end=now_cst().date()
            start=end-timedelta(days=a.days-1)
            out=capture(cfg,[start+timedelta(days=i) for i in range(a.days)])
        elif a.command=="run-date":
            d=date.fromisoformat(a.date)
            out=capture(cfg,[d])
        elif a.command=="status":
            out=latest_manifest(cfg)
        elif a.command=="queue":
            out=latest_queue(cfg)
        else:
            raise RuntimeError("unknown command")
        print(json.dumps(out,ensure_ascii=False,indent=2,default=str))
        return 0 if not str(out.get("status","")).startswith(("FAIL","PARTIAL")) else 2
    except Exception as exc:
        print(json.dumps({
            "status":f"FAIL_OPTIMUS_PROSPECTIVE_INTAKE_V0_7_1_{a.command.upper().replace('-','_')}",
            "error":repr(exc),
            "registry_mutation":False,
            "canonicalization_mutation":False,
            "broker":False,
            "orders":False,
            "production_db":False,
        },ensure_ascii=False,indent=2))
        return 2

if __name__=="__main__":
    raise SystemExit(main())
