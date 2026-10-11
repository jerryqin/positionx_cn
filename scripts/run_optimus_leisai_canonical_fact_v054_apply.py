from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT=Path(__file__).resolve().parents[1]
CONFIG=PROJECT_ROOT/"config"/"research"/"optimus_leisai_canonical_fact_v054_apply.json"
CST=timezone(timedelta(hours=8))

def now_cst()->datetime:
    return datetime.now(CST)

def iso_now()->str:
    return now_cst().isoformat(timespec="seconds")

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

def verify_dependencies(cfg:dict[str,Any],*,allow_steady_baseline_drift:bool=False)->dict[str,str]:
    actual={}
    for key,d in cfg["dependencies"].items():
        p=resolve(d["path"])
        if not p.exists():
            raise RuntimeError(f"DEPENDENCY_MISSING {key} {p}")
        got=sha256_file(p)
        actual[key]=got
        if got!=d["sha256"]:
            if allow_steady_baseline_drift and key=="steady_baseline":
                continue
            raise RuntimeError(f"DEPENDENCY_SHA_MISMATCH {key} actual={got} expected={d['sha256']}")
    return actual

def load_runtime(cfg:dict[str,Any],*,allow_steady_baseline_drift:bool=False):
    deps=verify_dependencies(cfg,allow_steady_baseline_drift=allow_steady_baseline_drift)
    v05=import_exact(
        resolve(cfg["dependencies"]["canonicalization_script"]["path"]),
        cfg["dependencies"]["canonicalization_script"]["sha256"],
        "optimus_v05_for_leisai_guarded_apply",
    )
    reg=import_exact(
        resolve(cfg["dependencies"]["registry_script"]["path"]),
        cfg["dependencies"]["registry_script"]["sha256"],
        "optimus_registry_for_leisai_guarded_apply",
    )
    v05cfg=v05.load_json(resolve(cfg["dependencies"]["canonicalization_config"]["path"]))
    regcfg=reg.load_config(v05.resolve_path(v05cfg["registry_config"]))
    return deps,v05,reg,v05cfg,regcfg

def approved_plan(cfg:dict[str,Any])->dict[str,Any]:
    p=Path(cfg["approved_plan"]["path"])
    if not p.exists():
        raise RuntimeError(f"APPROVED_PLAN_MISSING {p}")
    got=sha256_file(p)
    exp=cfg["approved_plan"]["sha256"]
    if got!=exp:
        raise RuntimeError(f"APPROVED_PLAN_SHA_MISMATCH actual={got} expected={exp}")
    plan=json.loads(p.read_text(encoding="utf-8"))
    ap=cfg["approved_plan"]
    if plan.get("plan_id")!=ap["plan_id"]:
        raise RuntimeError("APPROVED_PLAN_ID_MISMATCH")
    facts=plan.get("facts") or []
    if len(facts)!=1:
        raise RuntimeError(f"APPROVED_PLAN_FACT_COUNT_MISMATCH actual={len(facts)}")
    f=facts[0]
    if f.get("fact_key")!=ap["fact_key"] or f.get("anchor_evidence_id")!=ap["source_anchor_evidence_id"]:
        raise RuntimeError("APPROVED_PLAN_FACT_IDENTITY_MISMATCH")
    if int(f.get("canonical_level") or -1)!=6:
        raise RuntimeError("APPROVED_PLAN_CANONICAL_LEVEL_MISMATCH")
    tv=f.get("canonical_tesla_link_level")
    if int(tv if tv is not None else -1)!=0:
        raise RuntimeError("APPROVED_PLAN_TESLA_LINK_MISMATCH")
    return plan

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

def find_source_proposal(cfg:dict[str,Any])->dict[str,Any]:
    root=Path(cfg["proposal_search_root"])
    eid=cfg["approved_plan"]["source_anchor_evidence_id"]
    hits=[]
    if root.exists():
        for p in root.rglob("proposal_queue.jsonl"):
            for line in p.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                x=json.loads(line)
                if str(x.get("evidence_id"))==eid:
                    hits.append((p,x))
    if not hits:
        raise RuntimeError(f"SOURCE_PROPOSAL_NOT_FOUND {eid}")
    sigs={stable_json(proposal_signature(x)) for _,x in hits}
    if len(sigs)!=1:
        raise RuntimeError(f"SOURCE_PROPOSAL_CONFLICT copies={len(hits)} signatures={len(sigs)}")
    q=hits[0][1]
    ap=cfg["approved_plan"]
    if q.get("queue_state")!="EVIDENCE_PROPOSAL":
        raise RuntimeError(f"SOURCE_QUEUE_STATE_MISMATCH actual={q.get('queue_state')}")
    if q.get("symbol")!="002979.SZ" or q.get("company")!="雷赛智能":
        raise RuntimeError("SOURCE_SYMBOL_COMPANY_MISMATCH")
    if int(q.get("evidence_level") or -1)!=6:
        raise RuntimeError("SOURCE_EVIDENCE_LEVEL_MISMATCH")
    tv=q.get("tesla_link_level")
    if int(tv if tv is not None else -1)!=0:
        raise RuntimeError("SOURCE_TESLA_LINK_MISMATCH")
    if q.get("source_sha256")!=ap["source_anchor_sha256"]:
        raise RuntimeError("SOURCE_SHA_MISMATCH")
    if q.get("source_ref")!="http://static.cninfo.com.cn/finalpage/2026-10-09/1225596738.PDF":
        raise RuntimeError("SOURCE_REF_MISMATCH")
    if q.get("contradiction_kind") is not None:
        raise RuntimeError("UNEXPECTED_SOURCE_CONTRADICTION")
    return q

def source_to_anchor(q:dict[str,Any])->dict[str,Any]:
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

def table_columns(con:Any,table:str)->set[str]:
    rows=con.execute(f"PRAGMA table_info('{table}')").fetchall()
    return {str(r[1]) for r in rows}

def assert_v05_schema(con:Any,v05:Any,reg:Any,*,allow_init:bool=False)->None:
    # Read-only status/check must never call CREATE TABLE through v05.init_tables().
    # All guarded APPLY paths require the frozen schema to exist already, so default
    # behavior is inspection-only and fail-closed on missing columns/tables.
    if allow_init:
        v05.init_tables(con)
    required={
        v05.TABLE_FACT:{
            "fact_id","batch_id","symbol","company","fact_key","fact_type","fact_text",
            "component_domain","canonical_level","canonical_tesla_link_level","anchor_evidence_id",
            "policy_version","actor","canonicalized_at","plan_sha256"
        },
        v05.TABLE_FACT_SOURCE:{"fact_id","evidence_id","source_role","review_action"},
        v05.TABLE_BATCH:{
            "batch_id","plan_id","plan_sha256","actor","applied_at","fact_count",
            "canonical_symbol_count","covered_source_count","reject_only_count",
            "affected_symbols_json","output_dir","result_manifest_sha256"
        },
    }
    for table,cols in required.items():
        actual=table_columns(con,table)
        missing=cols-actual
        if missing:
            raise RuntimeError(f"V05_SCHEMA_MISSING_COLUMNS table={table} missing={sorted(missing)}")
    for table in (reg.TABLE_EVIDENCE,reg.TABLE_REVIEW,reg.TABLE_CANDIDATE):
        if not table_columns(con,table):
            raise RuntimeError(f"REGISTRY_TABLE_MISSING {table}")

def state_payload(con:Any,v05:Any,reg:Any,regcfg:dict[str,Any],symbol:str)->dict[str,Any]:
    rs=reg.status_payload(con,regcfg)
    c=reg.read_candidate(con,symbol) or {}
    facts=con.execute(f"SELECT count(*),count(DISTINCT symbol) FROM {v05.TABLE_FACT}").fetchone()
    mats=con.execute(
        f"SELECT count(*) FROM {reg.TABLE_EVIDENCE} WHERE parser_version = ?",
        [v05.FACT_MATERIALIZATION_PARSER]
    ).fetchone()[0]
    return {
        "registry":rs,
        "candidate":c,
        "canonical_fact_count":int(facts[0]),
        "canonical_symbol_count":int(facts[1]),
        "materialized_fact_evidence_count":int(mats),
    }

def validate_state(state:dict[str,Any],expected:dict[str,Any],*,pre:bool)->None:
    r=state["registry"]; c=state["candidate"]
    checks={
        "candidate_count":int(r.get("candidate_count",-1))==expected["candidate_count"],
        "active_canonical":int((r.get("lifecycle_counts") or {}).get("ACTIVE_CANONICAL",0))==expected["active_canonical"],
        "discovery_only":int((r.get("lifecycle_counts") or {}).get("DISCOVERY_ONLY",0))==expected["discovery_only"],
        "active_seed":int((r.get("lifecycle_counts") or {}).get("ACTIVE_SEED",0))==expected["active_seed"],
        "canonical_candidates":int(r.get("canonical_candidates",-1))==expected["canonical_candidates"],
        "evidence_count":int(r.get("evidence_count",-1))==expected["evidence_count"],
        "review_decision_count":int(r.get("review_decision_count",-1))==expected["review_decision_count"],
        "provisional_evidence_count":int(r.get("provisional_evidence_count",-1))==expected["provisional_evidence_count"],
        "canonical_fact_count":state["canonical_fact_count"]==expected["canonical_fact_count"],
        "canonical_symbol_count":state["canonical_symbol_count"]==expected["canonical_symbol_count"],
        "materialized_fact_evidence_count":state["materialized_fact_evidence_count"]==expected["materialized_fact_evidence_count"],
        "target_lifecycle_state":c.get("lifecycle_state")==expected["target_lifecycle_state"],
        "target_f_status":c.get("f_status")==expected["target_f_status"],
        "target_max_evidence_level":int(c.get("max_evidence_level",-1))==expected["target_max_evidence_level"],
        "target_max_tesla_link_level":int(c.get("max_tesla_link_level",-1))==expected["target_max_tesla_link_level"],
    }
    bad=[k for k,v in checks.items() if not v]
    if bad:
        raise RuntimeError(f"{'PRE' if pre else 'POST'}_STATE_MISMATCH {bad} state={state}")
    if pre:
        exp_sha=expected.get("registry_sha256")
        if exp_sha and r.get("registry_sha256")!=exp_sha:
            raise RuntimeError(f"PRE_REGISTRY_SHA_MISMATCH actual={r.get('registry_sha256')} expected={exp_sha}")

def normalize_fact(plan:dict[str,Any],v05:Any,cfg:dict[str,Any])->dict[str,Any]:
    f=dict(plan["facts"][0])
    fid=v05.fact_id_for(f)
    if fid!=cfg["approved_plan"]["fact_id"]:
        raise RuntimeError(f"FACT_ID_MISMATCH actual={fid}")
    if f["fact_key"]!=cfg["approved_plan"]["fact_key"]:
        raise RuntimeError("FACT_KEY_MISMATCH")
    return f

def apply_transaction(con:Any,*,v05:Any,reg:Any,regcfg:dict[str,Any],cfg:dict[str,Any],
                      plan:dict[str,Any],source:dict[str,Any],actor:str,commit:bool)->dict[str,Any]:
    assert_v05_schema(con,v05,reg)
    pre=state_payload(con,v05,reg,regcfg,cfg["precondition"]["target_symbol"])
    validate_state(pre,cfg["precondition"],pre=True)

    ap=cfg["approved_plan"]
    batch_id=v05.stable_id("CB",{"plan_id":plan["plan_id"],"plan_sha256":ap["sha256"]})
    if batch_id!=ap["batch_id"]:
        raise RuntimeError(f"BATCH_ID_MISMATCH actual={batch_id} expected={ap['batch_id']}")

    already=con.execute(f"SELECT count(*) FROM {v05.TABLE_BATCH} WHERE plan_sha256 = ?",[ap["sha256"]]).fetchone()[0]
    if already:
        return {"status":"ALREADY_APPLIED","pre":pre,"batch_id":batch_id,"mutation":False}

    fact=normalize_fact(plan,v05,cfg)
    anchor=reg.validate_evidence_row(source_to_anchor(source),allow_canonical_input=False)
    if anchor["evidence_id"]!=ap["source_anchor_evidence_id"]:
        raise RuntimeError("NORMALIZED_ANCHOR_ID_MISMATCH")

    existing_anchor=con.execute(
        f"SELECT count(*) FROM {reg.TABLE_EVIDENCE} WHERE evidence_id = ?",
        [anchor["evidence_id"]]
    ).fetchone()[0]
    if existing_anchor:
        raise RuntimeError("SOURCE_ANCHOR_ALREADY_IN_REGISTRY_BEFORE_APPLY")

    existing_fact=con.execute(
        f"SELECT count(*) FROM {v05.TABLE_FACT} WHERE fact_id = ? OR fact_key = ?",
        [ap["fact_id"],ap["fact_key"]]
    ).fetchone()[0]
    if existing_fact:
        raise RuntimeError("FACT_ALREADY_EXISTS_BEFORE_APPLY")

    con.execute("BEGIN TRANSACTION")
    try:
        inserted_anchor=reg.insert_evidence(con,anchor)
        if not inserted_anchor:
            raise RuntimeError("FAILED_TO_INSERT_SOURCE_ANCHOR")

        canonicalized_at=iso_now()
        con.execute(
            f"""INSERT INTO {v05.TABLE_FACT}
            (fact_id,batch_id,symbol,company,fact_key,fact_type,fact_text,component_domain,
             canonical_level,canonical_tesla_link_level,anchor_evidence_id,policy_version,
             actor,canonicalized_at,plan_sha256)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                ap["fact_id"],batch_id,fact["symbol"],fact["company"],fact["fact_key"],
                fact["fact_type"],fact["fact_text"],fact["component_domain"],
                int(fact["canonical_level"]),int(fact["canonical_tesla_link_level"]),
                fact["anchor_evidence_id"],"OPTIMUS_CANONICALIZATION_V0_5_1",
                actor,canonicalized_at,ap["sha256"]
            ]
        )
        fact_db={
            "fact_id":ap["fact_id"],"batch_id":batch_id,"symbol":fact["symbol"],
            "company":fact["company"],"fact_key":fact["fact_key"],"fact_type":fact["fact_type"],
            "fact_text":fact["fact_text"],"component_domain":fact["component_domain"],
            "canonical_level":int(fact["canonical_level"]),
            "canonical_tesla_link_level":int(fact["canonical_tesla_link_level"]),
            "anchor_evidence_id":fact["anchor_evidence_id"],
            "canonicalized_at":canonicalized_at,
        }
        mat=v05.materialize_fact_evidence(
            con,reg,batch_id=batch_id,fact_row=fact_db,actor=actor,append_repair_record=False
        )
        if mat["materialized_evidence_id"]!=ap["materialized_evidence_id"]:
            raise RuntimeError(
                f"MATERIALIZED_EVIDENCE_ID_MISMATCH actual={mat['materialized_evidence_id']} "
                f"expected={ap['materialized_evidence_id']}"
            )
        if not mat.get("evidence_inserted"):
            raise RuntimeError("MATERIALIZED_EVIDENCE_NOT_INSERTED")
        if not mat.get("anchor_review_rejected"):
            raise RuntimeError("ANCHOR_REJECT_REVIEW_NOT_ADDED")

        con.execute(
            f"INSERT INTO {v05.TABLE_FACT_SOURCE}(fact_id,evidence_id,source_role,review_action) VALUES (?,?,?,?)",
            [ap["fact_id"],ap["source_anchor_evidence_id"],"ANCHOR","REJECTED_AS_SOURCE_ABSORBED_BY_CANONICAL_FACT"]
        )

        rec=reg.reconcile_symbol(con,regcfg,cfg["precondition"]["target_symbol"])

        con.execute(
            f"""INSERT INTO {v05.TABLE_BATCH}
            (batch_id,plan_id,plan_sha256,actor,applied_at,fact_count,canonical_symbol_count,
             covered_source_count,reject_only_count,affected_symbols_json,output_dir,result_manifest_sha256)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                batch_id,plan["plan_id"],ap["sha256"],actor,iso_now(),1,1,1,0,
                json.dumps([cfg["precondition"]["target_symbol"]],ensure_ascii=False),None,None
            ]
        )

        post=state_payload(con,v05,reg,regcfg,cfg["expected_post"]["target_symbol"])
        validate_state(post,cfg["expected_post"],pre=False)

        audit=v05.audit_fact_materialization(con,reg,regcfg,batch_id)
        if audit.get("repair_required") or int(audit.get("anomaly_count",0))!=0:
            raise RuntimeError(f"MATERIALIZATION_AUDIT_FAILED {audit}")

        # Exact source/fact semantics.
        source_state=con.execute(
            f"""SELECT COALESCE(r.decision,e.initial_review_state),r.reason
            FROM {reg.TABLE_EVIDENCE} e
            LEFT JOIN (
              SELECT evidence_id,decision,reason,
                     row_number() OVER (PARTITION BY evidence_id ORDER BY decided_at DESC,decision_id DESC) rn
              FROM {reg.TABLE_REVIEW}
            ) r ON r.evidence_id=e.evidence_id AND r.rn=1
            WHERE e.evidence_id=?""",
            [ap["source_anchor_evidence_id"]]
        ).fetchone()
        desired_reason=f"SOURCE_ABSORBED_BY_CANONICAL_FACT_V0_5_1:{ap['fact_id']}"
        if not source_state or str(source_state[0])!="REJECTED" or str(source_state[1])!=desired_reason:
            raise RuntimeError(f"SOURCE_ANCHOR_FINAL_STATE_MISMATCH actual={source_state}")

        cfe=con.execute(
            f"""SELECT evidence_level,tesla_link_level,initial_review_state,parser_version
            FROM {reg.TABLE_EVIDENCE} WHERE evidence_id=?""",
            [ap["materialized_evidence_id"]]
        ).fetchone()
        if not cfe or (int(cfe[0]),int(cfe[1]),str(cfe[2]),str(cfe[3])) != (
            6,0,"CANONICAL",v05.FACT_MATERIALIZATION_PARSER
        ):
            raise RuntimeError(f"MATERIALIZED_CFE_SEMANTICS_MISMATCH actual={cfe}")

        result={
            "batch_id":batch_id,
            "fact_id":ap["fact_id"],
            "materialized_evidence_id":ap["materialized_evidence_id"],
            "source_anchor_evidence_id":ap["source_anchor_evidence_id"],
            "pre":pre,
            "post":post,
            "reconcile":rec,
            "materialization":mat,
            "audit":audit,
        }
        if commit:
            con.execute("COMMIT")
            result["mutation"]=True
        else:
            con.execute("ROLLBACK")
            result["mutation"]=False
        return result
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise

def simulate(cfg:dict[str,Any],actor:str)->dict[str,Any]:
    deps,v05,reg,v05cfg,regcfg=load_runtime(cfg)
    plan=approved_plan(cfg)
    source=find_source_proposal(cfg)
    db=Path(v05.resolve_path(v05cfg["research_db"]))
    if not db.exists():
        raise RuntimeError(f"RESEARCH_DB_MISSING {db}")
    with tempfile.TemporaryDirectory(prefix="optimus_leisai_v054_sim_") as td:
        tmp=Path(td)/db.name
        shutil.copy2(db,tmp)
        import duckdb
        con=duckdb.connect(str(tmp),read_only=False)
        try:
            res=apply_transaction(
                con,v05=v05,reg=reg,regcfg=regcfg,cfg=cfg,plan=plan,source=source,
                actor=actor,commit=False
            )
        finally:
            con.close()
    return {
        "status":"PASS_OPTIMUS_LEISAI_CANONICAL_FACT_V0_5_4_SIMULATION",
        "approved_plan_sha256":cfg["approved_plan"]["sha256"],
        "batch_id":cfg["approved_plan"]["batch_id"],
        "fact_id":cfg["approved_plan"]["fact_id"],
        "materialized_evidence_id":cfg["approved_plan"]["materialized_evidence_id"],
        "simulation":res,
        "dependency_sha256":deps,
        "mutation":False,
        "registry_mutation":False,
        "canonicalization_mutation":False,
        "theme_stage_mutation":False,
    }

def write_apply_artifacts(cfg:dict[str,Any],result:dict[str,Any],backup:Path)->tuple[Path,str]:
    stamp=now_cst().strftime("%Y%m%dT%H%M%S+0800")
    out=Path(cfg["output_root"])/stamp
    out.mkdir(parents=True,exist_ok=False)
    rp=out/"result.json"
    rp.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    ap=Path(cfg["approved_plan"]["path"])
    shutil.copy2(ap,out/"approved_plan.json")
    manifest={
        "schema_version":"OPTIMUS_LEISAI_CANONICAL_FACT_V0_5_4_APPLY_MANIFEST",
        "immutable":True,
        "approved_plan_sha256":cfg["approved_plan"]["sha256"],
        "batch_id":cfg["approved_plan"]["batch_id"],
        "fact_id":cfg["approved_plan"]["fact_id"],
        "materialized_evidence_id":cfg["approved_plan"]["materialized_evidence_id"],
        "backup_path":str(backup),
        "files":{
            "result.json":sha256_file(rp),
            "approved_plan.json":sha256_file(out/"approved_plan.json"),
        },
    }
    manifest["manifest_payload_sha256"]=stable_sha(manifest)
    mp=out/"MANIFEST.json"
    mp.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    return out,sha256_file(mp)

def apply_real(cfg:dict[str,Any],actor:str)->dict[str,Any]:
    # Simulation is mandatory immediately before real mutation.
    sim=simulate(cfg,actor)

    deps,v05,reg,v05cfg,regcfg=load_runtime(cfg)
    plan=approved_plan(cfg)
    source=find_source_proposal(cfg)
    db=Path(v05.resolve_path(v05cfg["research_db"]))
    if not db.exists():
        raise RuntimeError(f"RESEARCH_DB_MISSING {db}")

    backup_root=Path(cfg["backup_root"])
    backup_root.mkdir(parents=True,exist_ok=True)
    stamp=now_cst().strftime("%Y%m%dT%H%M%S+0800")
    backup=backup_root/f"{db.stem}_{stamp}{db.suffix}.bak"

    # Ensure no open connection from simulation, then create byte-for-byte backup.
    shutil.copy2(db,backup)
    backup_sha=sha256_file(backup)
    pre_db_sha=sha256_file(db)
    if backup_sha!=pre_db_sha:
        raise RuntimeError("DB_BACKUP_SHA_MISMATCH")

    created_output=None
    import duckdb
    try:
        con=duckdb.connect(str(db),read_only=False)
        try:
            result=apply_transaction(
                con,v05=v05,reg=reg,regcfg=regcfg,cfg=cfg,plan=plan,source=source,
                actor=actor,commit=True
            )
        finally:
            con.close()

        if result.get("status")=="ALREADY_APPLIED":
            return {
                "status":"ALREADY_APPLIED_OPTIMUS_LEISAI_CANONICAL_FACT_V0_5_4",
                "approved_plan_sha256":cfg["approved_plan"]["sha256"],
                "batch_id":cfg["approved_plan"]["batch_id"],
                "mutation":False,
            }

        # Snapshot only after successful committed state.
        con=duckdb.connect(str(db),read_only=False)
        try:
            snap=reg.snapshot(con,regcfg,as_of=now_cst().strftime("%Y%m%d"))
        finally:
            con.close()

        final={
            "status":"PASS_OPTIMUS_LEISAI_CANONICAL_FACT_V0_5_4_APPLY",
            "approved_plan_sha256":cfg["approved_plan"]["sha256"],
            "actor":actor,
            "simulation_status":sim["status"],
            "batch_id":cfg["approved_plan"]["batch_id"],
            "fact_id":cfg["approved_plan"]["fact_id"],
            "source_anchor_evidence_id":cfg["approved_plan"]["source_anchor_evidence_id"],
            "materialized_evidence_id":cfg["approved_plan"]["materialized_evidence_id"],
            "backup_path":str(backup),
            "backup_sha256":backup_sha,
            "pre_db_sha256":pre_db_sha,
            "post_db_sha256":sha256_file(db),
            "registry_snapshot":snap,
            "transaction_result":result,
            "expected_post":cfg["expected_post"],
            "steady_state_baseline_rebase_required":True,
            "theme_stage_change":False,
            "tesla_supplier_claim":False,
            "registry_mutation":True,
            "canonicalization_mutation":True,
            "broker":False,
            "orders":False,
            "production_db":False,
            "rollback_restored":False,
        }
        out,manifest_sha=write_apply_artifacts(cfg,final,backup)
        created_output=out

        # Update batch artifact pointers only after files are durable.
        con=duckdb.connect(str(db),read_only=False)
        try:
            con.execute(
                f"UPDATE {v05.TABLE_BATCH} SET output_dir=?, result_manifest_sha256=? WHERE batch_id=?",
                [str(out),manifest_sha,cfg["approved_plan"]["batch_id"]]
            )
        finally:
            con.close()

        final["output"]=str(out)
        final["result_manifest_sha256"]=manifest_sha
        # Rewrite result with final output fields; manifest remains a manifest of the
        # first immutable result payload plus approved plan, matching audit convention.
        (out/"final_summary.json").write_text(
            json.dumps(final,ensure_ascii=False,indent=2,default=str),encoding="utf-8"
        )
        return final
    except Exception as e:
        # Close handles before replacing DB.
        try:
            shutil.copy2(backup,db)
            restored=sha256_file(db)==backup_sha
        except Exception as restore_exc:
            raise RuntimeError(
                f"APPLY_FAILED_AND_RESTORE_FAILED apply_error={e!r} restore_error={restore_exc!r}"
            ) from restore_exc
        if created_output and created_output.exists():
            fail=(created_output/"ROLLBACK_NOTICE.json")
            fail.write_text(json.dumps({
                "status":"ROLLED_BACK_OPTIMUS_LEISAI_CANONICAL_FACT_V0_5_4",
                "apply_error":repr(e),
                "backup_path":str(backup),
                "backup_sha256":backup_sha,
                "restored":restored,
            },ensure_ascii=False,indent=2),encoding="utf-8")
        raise RuntimeError(
            f"APPLY_FAILED_ROLLBACK_RESTORED={restored} original_error={e!r}"
        ) from e

def preflight(cfg:dict[str,Any])->dict[str,Any]:
    deps,v05,reg,v05cfg,regcfg=load_runtime(cfg)
    plan=approved_plan(cfg)
    source=find_source_proposal(cfg)
    db=Path(v05.resolve_path(v05cfg["research_db"]))
    import duckdb
    con=duckdb.connect(str(db),read_only=True)
    try:
        assert_v05_schema(con,v05,reg)
        already=con.execute(
            f"SELECT count(*) FROM {v05.TABLE_BATCH} WHERE plan_sha256=?",
            [cfg["approved_plan"]["sha256"]]
        ).fetchone()[0]
        state=state_payload(con,v05,reg,regcfg,cfg["precondition"]["target_symbol"])
        if already:
            validate_state(state,cfg["expected_post"],pre=False)
            audit=v05.audit_fact_materialization(
                con,reg,regcfg,cfg["approved_plan"]["batch_id"]
            )
            if audit.get("repair_required") or int(audit.get("anomaly_count",0))!=0:
                raise RuntimeError(f"POST_APPLY_MATERIALIZATION_AUDIT_FAILED {audit}")
            return {
                "status":"PASS_OPTIMUS_LEISAI_CANONICAL_FACT_V0_5_4_APPLY_CHECK_ALREADY_APPLIED",
                "applied":True,
                "approved_plan_sha256":cfg["approved_plan"]["sha256"],
                "batch_id":cfg["approved_plan"]["batch_id"],
                "fact_id":cfg["approved_plan"]["fact_id"],
                "materialized_evidence_id":cfg["approved_plan"]["materialized_evidence_id"],
                "registry_sha256":state["registry"]["registry_sha256"],
                "post_state":state,
                "materialization_audit":audit,
                "dependency_sha256":deps,
                "registry_mutation":False,
                "canonicalization_mutation":False,
                "theme_stage_mutation":False,
            }
        validate_state(state,cfg["precondition"],pre=True)
        anchor_exists=con.execute(
            f"SELECT count(*) FROM {reg.TABLE_EVIDENCE} WHERE evidence_id=?",
            [cfg["approved_plan"]["source_anchor_evidence_id"]]
        ).fetchone()[0]
    finally:
        con.close()
    if anchor_exists:
        raise RuntimeError("SOURCE_ANCHOR_ALREADY_PRESENT_BEFORE_APPLY")
    f=normalize_fact(plan,v05,cfg)
    return {
        "status":"PASS_OPTIMUS_LEISAI_CANONICAL_FACT_V0_5_4_APPLY_CHECK",
        "approved_plan_sha256":cfg["approved_plan"]["sha256"],
        "approved_plan_path":cfg["approved_plan"]["path"],
        "batch_id":cfg["approved_plan"]["batch_id"],
        "fact_id":cfg["approved_plan"]["fact_id"],
        "fact_key":cfg["approved_plan"]["fact_key"],
        "source_anchor_evidence_id":cfg["approved_plan"]["source_anchor_evidence_id"],
        "source_anchor_sha256":source["source_sha256"],
        "materialized_evidence_id":cfg["approved_plan"]["materialized_evidence_id"],
        "canonical_level":int(f["canonical_level"]),
        "canonical_tesla_link_level":int(f["canonical_tesla_link_level"]),
        "registry_sha256":state["registry"]["registry_sha256"],
        "dependency_sha256":deps,
        "simulation_required":True,
        "backup_required":True,
        "rollback_restore_on_failure":True,
        "registry_mutation":False,
        "canonicalization_mutation":False,
        "theme_stage_mutation":False,
    }

def status(cfg:dict[str,Any])->dict[str,Any]:
    deps,v05,reg,v05cfg,regcfg=load_runtime(cfg,allow_steady_baseline_drift=True)
    db=Path(v05.resolve_path(v05cfg["research_db"]))
    import duckdb
    con=duckdb.connect(str(db),read_only=True)
    try:
        assert_v05_schema(con,v05,reg)
        state=state_payload(con,v05,reg,regcfg,cfg["expected_post"]["target_symbol"])
        batch=con.execute(
            f"SELECT * FROM {v05.TABLE_BATCH} WHERE plan_sha256=?",
            [cfg["approved_plan"]["sha256"]]
        ).fetchdf()
        audit=None
        if not batch.empty:
            audit=v05.audit_fact_materialization(con,reg,regcfg,cfg["approved_plan"]["batch_id"])
    finally:
        con.close()
    return {
        "status":"PASS_OPTIMUS_LEISAI_CANONICAL_FACT_V0_5_4_APPLY_STATUS",
        "applied":not batch.empty,
        "approved_plan_sha256":cfg["approved_plan"]["sha256"],
        "batch":None if batch.empty else batch.iloc[0].to_dict(),
        "state":state,
        "materialization_audit":audit,
        "steady_state_baseline_rebase_required":not batch.empty,
        "mutation":False,
    }

def main()->int:
    ap=argparse.ArgumentParser()
    ap.add_argument("command",choices=["check","simulate","apply","status"])
    ap.add_argument("--approve-plan-sha256")
    ap.add_argument("--actor",default="USER_EXPLICIT_APPROVAL_20261010")
    a=ap.parse_args()
    cfg=load_cfg()
    try:
        if a.command=="check":
            out=preflight(cfg)
        elif a.command=="simulate":
            out=simulate(cfg,a.actor)
        elif a.command=="apply":
            expected=cfg["approved_plan"]["sha256"]
            if a.approve_plan_sha256!=expected:
                raise RuntimeError(
                    f"PLAN_SHA256_APPROVAL_MISMATCH provided={a.approve_plan_sha256} expected={expected}"
                )
            out=apply_real(cfg,a.actor)
        else:
            out=status(cfg)
        print(json.dumps(out,ensure_ascii=False,indent=2,default=str))
        return 0
    except Exception as e:
        print(json.dumps({
            "status":f"FAIL_OPTIMUS_LEISAI_CANONICAL_FACT_V0_5_4_{a.command.upper()}",
            "error":repr(e),
            "registry_mutation":False if a.command!="apply" else "UNKNOWN_CHECK_ROLLBACK_FIELDS",
            "canonicalization_mutation":False if a.command!="apply" else "UNKNOWN_CHECK_ROLLBACK_FIELDS",
            "theme_stage_mutation":False,
            "broker":False,
            "orders":False,
            "production_db":False,
        },ensure_ascii=False,indent=2))
        return 2

if __name__=="__main__":
    raise SystemExit(main())
