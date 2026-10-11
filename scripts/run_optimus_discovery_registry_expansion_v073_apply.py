from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG = PROJECT_ROOT / "config" / "research" / "optimus_discovery_registry_expansion_v073_apply.json"
CST = timezone(timedelta(hours=8))

def now_cst() -> datetime:
    return datetime.now(CST)

def sha256_file(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda:f.read(1024*1024), b""):
            h.update(b)
    return h.hexdigest()

def stable_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",",":"), default=str)

def stable_sha(obj: Any) -> str:
    return hashlib.sha256(stable_json(obj).encode("utf-8")).hexdigest()

def load_cfg() -> dict[str,Any]:
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

def run_json(script: Path, *args: str) -> dict[str,Any]:
    p=subprocess.run([sys.executable,str(script),*args], cwd=PROJECT_ROOT, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"COMMAND_FAILED {script.name} {' '.join(args)} rc={p.returncode} stderr={p.stderr}")
    try:
        return json.loads(p.stdout)
    except Exception as e:
        raise RuntimeError(f"NON_JSON_OUTPUT {script.name}: {e!r}")

def latest_success_manifest(cfg: dict[str,Any]) -> dict[str,Any] | None:
    root=Path(cfg["output_root"])
    if not root.exists():
        return None
    ms=sorted(root.rglob("apply_result.json"), key=lambda p:p.stat().st_mtime, reverse=True)
    for p in ms:
        try:
            x=json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if x.get("status") in ("PASS_OPTIMUS_DISCOVERY_REGISTRY_EXPANSION_V0_7_3_APPLY",
                               "ALREADY_APPLIED_OPTIMUS_DISCOVERY_REGISTRY_EXPANSION_V0_7_3"):
            x["_path"]=str(p)
            return x
    return None

def load_registry_module_and_cfg(cfg: dict[str,Any]):
    mod=import_module(resolve(cfg["dependency_paths"]["registry_script"]),"positionx_registry_v03_for_v073_apply")
    rcfg=json.loads(resolve(cfg["dependency_paths"]["registry_config"]).read_text(encoding="utf-8"))
    return mod,rcfg

def candidate_row(con: Any, mod: Any, symbol: str) -> dict[str,Any] | None:
    df=con.execute(f"SELECT * FROM {mod.TABLE_CANDIDATE} WHERE symbol = ?", [symbol]).fetchdf()
    if df.empty:
        return None
    row=df.iloc[0].to_dict()
    return {k:(None if str(v)=="NaT" else v) for k,v in row.items()}

def evidence_rows(con: Any, mod: Any, symbols: list[str]) -> list[dict[str,Any]]:
    q=",".join(["?"]*len(symbols))
    df=con.execute(
        f"""SELECT evidence_id,symbol,company,component_domain,evidence_level,evidence_type,
                   tesla_link_level,source_type,source_ref,source_sha256,
                   initial_review_state,confidence,parser_version,interpretation
            FROM {mod.TABLE_EVIDENCE}
            WHERE symbol IN ({q})
            ORDER BY symbol,evidence_id""",
        symbols
    ).fetchdf()
    return df.to_dict(orient="records")

def registry_status_direct(mod: Any, rcfg: dict[str,Any], db_path: str | None=None) -> dict[str,Any]:
    use=dict(rcfg)
    if db_path is not None:
        use["research_db"]=db_path
    con=mod.open_db(use,read_only=True)
    try:
        mod.init_tables(con)
        return mod.status_payload(con,use)
    finally:
        con.close()

def validate_new_symbol_rows(mod: Any, rcfg: dict[str,Any], expected: dict[str,Any], db_path: str | None=None) -> dict[str,Any]:
    use=dict(rcfg)
    if db_path is not None:
        use["research_db"]=db_path
    con=mod.open_db(use,read_only=True)
    try:
        out={}
        for sym,exp in expected.items():
            row=candidate_row(con,mod,sym)
            if row is None:
                raise RuntimeError(f"POST_SYMBOL_MISSING {sym}")
            for k,v in exp.items():
                got=row.get(k)
                if isinstance(v,bool):
                    got=bool(got)
                elif isinstance(v,int) and got is not None:
                    got=int(got)
                elif isinstance(v,str) and got is not None:
                    got=str(got)
                if got != v:
                    raise RuntimeError(f"POST_SYMBOL_FIELD_MISMATCH {sym}.{k} actual={got!r} expected={v!r}")
            out[sym]={k:row.get(k) for k in exp}
        return out
    finally:
        con.close()

def plan_assets(cfg: dict[str,Any]) -> tuple[dict[str,Any],Path,Path]:
    p=Path(cfg["approved_plan"]["manifest_path"])
    if not p.exists():
        raise RuntimeError(f"APPROVED_PLAN_MANIFEST_MISSING {p}")
    manifest=json.loads(p.read_text(encoding="utf-8"))
    if manifest.get("manifest_payload_sha256")!=cfg["approved_plan"]["manifest_payload_sha256"]:
        raise RuntimeError(
            f"PLAN_PAYLOAD_SHA_MISMATCH actual={manifest.get('manifest_payload_sha256')} "
            f"expected={cfg['approved_plan']['manifest_payload_sha256']}"
        )
    ev=Path(manifest["evidence_file"])
    hr=Path(manifest["human_review_file"])
    if not ev.exists() or not hr.exists():
        raise RuntimeError("APPROVED_PLAN_ASSET_MISSING")
    if sha256_file(ev)!=cfg["approved_plan"]["planned_evidence_sha256"]:
        raise RuntimeError(f"EVIDENCE_FILE_SHA_MISMATCH actual={sha256_file(ev)}")
    if sha256_file(hr)!=cfg["approved_plan"]["human_review_sha256"]:
        raise RuntimeError(f"HUMAN_REVIEW_FILE_SHA_MISMATCH actual={sha256_file(hr)}")
    if manifest.get("planned_symbols")!=cfg["approved_plan"]["symbols"]:
        raise RuntimeError(f"PLAN_SYMBOLS_MISMATCH actual={manifest.get('planned_symbols')}")
    if int(manifest.get("planned_evidence_count",-1))!=cfg["approved_plan"]["evidence_count"]:
        raise RuntimeError("PLAN_EVIDENCE_COUNT_MISMATCH")
    if manifest.get("planned_candidate_lifecycle")!=cfg["approved_plan"]["lifecycle"]:
        raise RuntimeError("PLAN_LIFECYCLE_MISMATCH")
    if manifest.get("planned_f_status")!=cfg["approved_plan"]["f_status"]:
        raise RuntimeError("PLAN_F_STATUS_MISMATCH")
    if manifest.get("planned_review_state")!=cfg["approved_plan"]["review_state"]:
        raise RuntimeError("PLAN_REVIEW_STATE_MISMATCH")
    if int(manifest.get("planned_tesla_link_level",-1))!=cfg["approved_plan"]["tesla_link_level"]:
        raise RuntimeError("PLAN_TESLA_LEVEL_MISMATCH")
    return manifest,ev,hr

def validate_evidence_file_content(ev: Path, cfg: dict[str,Any], mod: Any) -> list[dict[str,Any]]:
    rows=[json.loads(line) for line in ev.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows)!=2:
        raise RuntimeError(f"EVIDENCE_ROW_COUNT_MISMATCH actual={len(rows)}")
    if [x.get("symbol") for x in rows] != cfg["approved_plan"]["symbols"]:
        raise RuntimeError(f"EVIDENCE_SYMBOL_ORDER_MISMATCH actual={[x.get('symbol') for x in rows]}")
    normalized=[]
    for row in rows:
        n=mod.validate_evidence_row(row,allow_canonical_input=False)
        if n["initial_review_state"]!="PROVISIONAL":
            raise RuntimeError(f"EVIDENCE_NOT_PROVISIONAL {n['symbol']}")
        if int(n["evidence_level"])!=1:
            raise RuntimeError(f"EVIDENCE_LEVEL_NOT_1 {n['symbol']}")
        if n["evidence_type"]!="APPROVED_DISCOVERY_ONLY":
            raise RuntimeError(f"EVIDENCE_TYPE_MISMATCH {n['symbol']}")
        if int(n["tesla_link_level"])!=0:
            raise RuntimeError(f"TESLA_LINK_NOT_ZERO {n['symbol']}")
        if n["contradiction_kind"] is not None or n["contradiction_severity"] is not None:
            raise RuntimeError(f"UNEXPECTED_CONTRADICTION {n['symbol']}")
        normalized.append(n)
    return normalized

def dependency_check(cfg: dict[str,Any]) -> dict[str,Any]:
    actual={}
    for k,rel in cfg["dependency_paths"].items():
        p=resolve(rel)
        if not p.exists():
            raise RuntimeError(f"DEPENDENCY_MISSING {k} {p}")
        got=sha256_file(p); actual[k]=got
        exp=cfg["dependency_sha256"][k]
        if got!=exp:
            raise RuntimeError(f"DEPENDENCY_SHA_MISMATCH {k} actual={got} expected={exp}")

    # Re-run the approved PLAN check. This guarantees its upstream discovery/Registry
    # anchors are still unchanged.
    plan_check=run_json(resolve(cfg["dependency_paths"]["plan_script"]),"check")
    if plan_check.get("status")!="PASS_OPTIMUS_DISCOVERY_REGISTRY_EXPANSION_V0_7_3_CHECK":
        raise RuntimeError(f"PLAN_CHECK_NOT_PASS {plan_check}")

    manifest,ev,hr=plan_assets(cfg)
    mod,rcfg=load_registry_module_and_cfg(cfg)
    normalized=validate_evidence_file_content(ev,cfg,mod)

    reg=run_json(resolve(cfg["dependency_paths"]["registry_script"]),"status")
    pre=cfg["registry_precondition"]
    # Allow explicit idempotent recognition after a previous successful wrapper apply.
    prior=latest_success_manifest(cfg)
    if reg.get("registry_sha256")!=pre["registry_sha256"]:
        if prior and reg.get("registry_sha256")==prior.get("post_registry_sha256"):
            return {
                "status":"ALREADY_APPLIED_OPTIMUS_DISCOVERY_REGISTRY_EXPANSION_V0_7_3",
                "prior_apply_result":prior.get("_path"),
                "post_registry_sha256":reg.get("registry_sha256"),
                "candidate_count":reg.get("candidate_count"),
                "mutation":False,
            }
        raise RuntimeError(
            f"REGISTRY_PRECONDITION_SHA_MISMATCH actual={reg.get('registry_sha256')} "
            f"expected={pre['registry_sha256']}"
        )
    if int(reg.get("candidate_count",-1))!=pre["candidate_count"]:
        raise RuntimeError("PRE_CANDIDATE_COUNT_MISMATCH")
    lc=reg.get("lifecycle_counts") or {}
    if int(lc.get("ACTIVE_CANONICAL",0))!=pre["active_canonical"]:
        raise RuntimeError("PRE_ACTIVE_CANONICAL_MISMATCH")
    if int(lc.get("DISCOVERY_ONLY",0))!=pre["discovery_only"]:
        raise RuntimeError("PRE_DISCOVERY_ONLY_MISMATCH")
    if int(lc.get("ACTIVE_SEED",0))!=pre["active_seed"]:
        raise RuntimeError("PRE_ACTIVE_SEED_MISMATCH")
    if int(reg.get("canonical_candidates",-1))!=pre["canonical_candidates"]:
        raise RuntimeError("PRE_CANONICAL_COUNT_MISMATCH")

    # Existing Registry must not already contain the approved symbols.
    con=mod.open_db(rcfg,read_only=True)
    try:
        for s in cfg["approved_plan"]["symbols"]:
            if candidate_row(con,mod,s) is not None:
                raise RuntimeError(f"APPROVED_SYMBOL_ALREADY_IN_REGISTRY {s}")
    finally:
        con.close()

    return {
        "status":"PASS_OPTIMUS_DISCOVERY_REGISTRY_EXPANSION_V0_7_3_APPLY_CHECK",
        "apply_id":cfg["apply_id"],
        "registry_sha256":reg["registry_sha256"],
        "candidate_count":reg["candidate_count"],
        "plan_manifest_payload_sha256":manifest["manifest_payload_sha256"],
        "evidence_file":str(ev),
        "evidence_file_sha256":sha256_file(ev),
        "human_review_file_sha256":sha256_file(hr),
        "normalized_evidence_ids":[x["evidence_id"] for x in normalized],
        "dependency_sha256":actual,
        "simulation_required":True,
        "database_backup_required":True,
        "explicit_human_approval_recorded":bool(cfg["approval"]["approved"]),
        "mutation":False,
    }

def assert_postconditions(status: dict[str,Any], pre_status: dict[str,Any], cfg: dict[str,Any]) -> None:
    post=cfg["postconditions"]
    if int(status.get("candidate_count",-1))!=post["candidate_count"]:
        raise RuntimeError(f"POST_CANDIDATE_COUNT_MISMATCH actual={status.get('candidate_count')}")
    lc=status.get("lifecycle_counts") or {}
    if int(lc.get("ACTIVE_CANONICAL",0))!=post["active_canonical"]:
        raise RuntimeError("POST_ACTIVE_CANONICAL_MISMATCH")
    if int(lc.get("DISCOVERY_ONLY",0))!=post["discovery_only"]:
        raise RuntimeError("POST_DISCOVERY_ONLY_MISMATCH")
    if int(lc.get("ACTIVE_SEED",0))!=post["active_seed"]:
        raise RuntimeError("POST_ACTIVE_SEED_MISMATCH")
    if int(status.get("canonical_candidates",-1))!=post["canonical_candidates"]:
        raise RuntimeError("POST_CANONICAL_COUNT_MISMATCH")
    if int(status.get("evidence_count",-1)) != int(pre_status["evidence_count"]) + post["evidence_count_delta"]:
        raise RuntimeError("POST_EVIDENCE_COUNT_DELTA_MISMATCH")
    if int(status.get("provisional_evidence_count",-1)) != int(pre_status["provisional_evidence_count"]) + post["provisional_evidence_count_delta"]:
        raise RuntimeError("POST_PROVISIONAL_COUNT_DELTA_MISMATCH")
    if int(status.get("review_decision_count",-1)) != int(pre_status["review_decision_count"]) + post["review_decision_count_delta"]:
        raise RuntimeError("POST_REVIEW_DECISION_DELTA_MISMATCH")

def apply_to_db(mod: Any, rcfg: dict[str,Any], evidence_file: Path, db_path: str,
                archive_root: str) -> tuple[dict[str,Any],dict[str,Any]]:
    use=dict(rcfg)
    use["research_db"]=db_path
    use["evidence_archive_root"]=archive_root
    con=mod.open_db(use,read_only=False)
    try:
        mod.init_tables(con)
        ing=mod.ingest_jsonl(con,use,evidence_file,allow_canonical_input=False)
        rec=mod.reconcile_all(con,use)
        stat=mod.status_payload(con,use)
        return {"ingest":ing,"reconcile":{"symbols":rec["symbols"],"changed":rec["changed"]}},stat
    finally:
        con.close()

def simulation(cfg: dict[str,Any], evidence_file: Path, mod: Any, rcfg: dict[str,Any],
               pre_status: dict[str,Any], out: Path) -> dict[str,Any]:
    real_db=Path(rcfg["research_db"])
    if not real_db.exists():
        raise RuntimeError(f"REGISTRY_DB_MISSING {real_db}")
    sim_dir=out/"simulation"
    sim_dir.mkdir(parents=True,exist_ok=False)
    sim_db=sim_dir/"positionx_research_sim.duckdb"
    shutil.copy2(real_db,sim_db)
    sim_archive=sim_dir/"evidence_archive"
    actions,status=apply_to_db(mod,rcfg,evidence_file,str(sim_db),str(sim_archive))
    assert_postconditions(status,pre_status,cfg)
    symbol_rows=validate_new_symbol_rows(mod,rcfg,cfg["postconditions"]["symbols"],str(sim_db))
    return {
        "status":"PASS_SIMULATION",
        "sim_db":str(sim_db),
        "sim_db_sha256":sha256_file(sim_db),
        "post_registry_sha256":status["registry_sha256"],
        "post_status":status,
        "symbol_rows":symbol_rows,
        "actions":actions,
    }

def copy_db_backup(rcfg: dict[str,Any], out: Path) -> tuple[Path,str]:
    db=Path(rcfg["research_db"])
    if not db.exists():
        raise RuntimeError(f"REGISTRY_DB_MISSING {db}")
    wal=Path(str(db)+".wal")
    if wal.exists():
        raise RuntimeError(f"REGISTRY_WAL_PRESENT_REFUSE_BACKUP {wal}")
    backup=out/"backup"/db.name
    backup.parent.mkdir(parents=True,exist_ok=False)
    shutil.copy2(db,backup)
    return backup,sha256_file(backup)

def restore_db(backup: Path, rcfg: dict[str,Any]) -> None:
    db=Path(rcfg["research_db"])
    tmp=db.with_suffix(db.suffix+".restore_tmp")
    shutil.copy2(backup,tmp)
    os.replace(tmp,db)

def apply(cfg: dict[str,Any], token: str) -> dict[str,Any]:
    if token != cfg["approval"]["approval_token"]:
        raise RuntimeError("APPROVAL_TOKEN_MISMATCH")
    if not cfg["approval"]["approved"]:
        raise RuntimeError("APPROVAL_NOT_RECORDED")

    chk=dependency_check(cfg)
    if chk.get("status")=="ALREADY_APPLIED_OPTIMUS_DISCOVERY_REGISTRY_EXPANSION_V0_7_3":
        return chk

    manifest,evidence_file,human_review_file=plan_assets(cfg)
    mod,rcfg=load_registry_module_and_cfg(cfg)
    pre_status=run_json(resolve(cfg["dependency_paths"]["registry_script"]),"status")

    run_id=now_cst().strftime("%Y%m%dT%H%M%S+0800")+"_apply"
    out=Path(cfg["output_root"])/now_cst().strftime("%Y%m%d")/run_id
    out.mkdir(parents=True,exist_ok=False)

    sim=simulation(cfg,evidence_file,mod,rcfg,pre_status,out)

    backup,backup_sha=copy_db_backup(rcfg,out)
    real_actions=None
    post_status=None
    restored=False
    try:
        real_actions,post_status=apply_to_db(
            mod,rcfg,evidence_file,rcfg["research_db"],rcfg["evidence_archive_root"]
        )
        assert_postconditions(post_status,pre_status,cfg)
        real_rows=validate_new_symbol_rows(mod,rcfg,cfg["postconditions"]["symbols"])
        if post_status["registry_sha256"] != sim["post_registry_sha256"]:
            raise RuntimeError(
                f"REAL_SIM_REGISTRY_SHA_MISMATCH real={post_status['registry_sha256']} "
                f"sim={sim['post_registry_sha256']}"
            )
    except Exception:
        restore_db(backup,rcfg)
        restored=True
        raise

    result={
        "status":"PASS_OPTIMUS_DISCOVERY_REGISTRY_EXPANSION_V0_7_3_APPLY",
        "apply_id":cfg["apply_id"],
        "run_id":run_id,
        "output":str(out),
        "approved_symbols":cfg["approved_plan"]["symbols"],
        "approval_text":cfg["approval"]["approval_text"],
        "plan_manifest_payload_sha256":manifest["manifest_payload_sha256"],
        "evidence_file_sha256":sha256_file(evidence_file),
        "human_review_file_sha256":sha256_file(human_review_file),
        "pre_registry_sha256":pre_status["registry_sha256"],
        "simulation_post_registry_sha256":sim["post_registry_sha256"],
        "post_registry_sha256":post_status["registry_sha256"],
        "pre_status":{
            "candidate_count":pre_status["candidate_count"],
            "lifecycle_counts":pre_status["lifecycle_counts"],
            "canonical_candidates":pre_status["canonical_candidates"],
            "evidence_count":pre_status["evidence_count"],
            "provisional_evidence_count":pre_status["provisional_evidence_count"],
            "review_decision_count":pre_status["review_decision_count"],
        },
        "post_status":{
            "candidate_count":post_status["candidate_count"],
            "lifecycle_counts":post_status["lifecycle_counts"],
            "f_status_counts":post_status["f_status_counts"],
            "canonical_candidates":post_status["canonical_candidates"],
            "evidence_count":post_status["evidence_count"],
            "provisional_evidence_count":post_status["provisional_evidence_count"],
            "review_decision_count":post_status["review_decision_count"],
        },
        "symbol_rows":real_rows,
        "real_actions":real_actions,
        "simulation":sim,
        "database_backup":str(backup),
        "database_backup_sha256":backup_sha,
        "restored_on_failure":restored,
        "registry_mutation":True,
        "canonicalization_mutation":False,
        "theme_stage_mutation":False,
        "broker":False,
        "orders":False,
        "production_db":False,
    }
    result["result_payload_sha256"]=stable_sha(result)
    result_file=out/"apply_result.json"
    result_file.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    return result

def status(cfg: dict[str,Any]) -> dict[str,Any]:
    prior=latest_success_manifest(cfg)
    if not prior:
        return {
            "status":"ARMED_OPTIMUS_DISCOVERY_REGISTRY_EXPANSION_V0_7_3_APPLY_NOT_RUN",
            "approved_symbols":cfg["approved_plan"]["symbols"],
            "mutation":False,
        }
    return {
        "status":"PASS_OPTIMUS_DISCOVERY_REGISTRY_EXPANSION_V0_7_3_APPLY_STATUS",
        "apply_result":prior.get("_path"),
        "post_registry_sha256":prior.get("post_registry_sha256"),
        "post_status":prior.get("post_status"),
        "approved_symbols":prior.get("approved_symbols"),
        "registry_mutation":True,
        "canonicalization_mutation":False,
        "theme_stage_mutation":False,
    }

def main() -> int:
    ap=argparse.ArgumentParser()
    sp=ap.add_subparsers(dest="command",required=True)
    sp.add_parser("check")
    a=sp.add_parser("apply")
    a.add_argument("--approval-token",required=True)
    sp.add_parser("status")
    args=ap.parse_args()
    cfg=load_cfg()
    try:
        if args.command=="check":
            out=dependency_check(cfg)
        elif args.command=="apply":
            out=apply(cfg,args.approval_token)
        else:
            out=status(cfg)
        print(json.dumps(out,ensure_ascii=False,indent=2,default=str))
        return 0
    except Exception as e:
        print(json.dumps({
            "status":f"FAIL_OPTIMUS_DISCOVERY_REGISTRY_EXPANSION_V0_7_3_{args.command.upper()}",
            "error":repr(e),
            "registry_mutation":"UNKNOWN_IF_PROCESS_INTERRUPTED" if args.command=="apply" else False,
            "canonicalization_mutation":False,
            "theme_stage_mutation":False,
            "broker":False,
            "orders":False,
            "production_db":False,
        },ensure_ascii=False,indent=2))
        return 2

if __name__=="__main__":
    raise SystemExit(main())
