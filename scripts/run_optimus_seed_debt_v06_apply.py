from __future__ import annotations

"""
PositionX Optimus Seed Debt Resolution V0.6 — Guarded APPLY

This is the only mutation step for the approved V0.6 Seed Debt Resolution.

Guards
------
- exact user-approved V0.6 decision plan SHA
- exact Source Freeze manifest SHA
- exact APPLY_PREVIEW file/payload SHA
- exact PRECONDITION file/payload SHA
- exact frozen Registry script SHA
- exact installed Canonicalization V0.5.1 script SHA
- exact frozen V0.4.4 evidence script SHA
- exact pre-APPLY Registry data SHA and Batch3 result SHA
- exact 5-symbol ACTIVE_SEED scope

Mutation scope
--------------
Research DB only:
- stage the 7 exact frozen V0.4.4 bridge proposals (idempotent insert)
- stage 5 explicitly human-reviewed source-normalization evidence rows
- append human review decisions
- insert 6 Canonical Facts
- materialize 6 independent Canonical Evidence rows using frozen V0.5.1 materializer
- reconcile exactly the 5 approved Seed symbols
- append one Canonicalization batch row

Never touches broker/order/production DB.
"""

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CST = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]
OPTIMUS_ROOT = Path(r"D:\positionx_data\research\big_opportunity\optimus")

V05_SCRIPT = ROOT / "scripts" / "run_optimus_canonicalization_v05.py"
V05_SHA256 = "3e07d79ad34b5171618a59af50b3efda7b1836b1c9d697b76fcabbc63c988a1f"
REGISTRY_SCRIPT = ROOT / "scripts" / "run_optimus_candidate_registry_v03.py"
REGISTRY_SCRIPT_SHA256 = "3955c6deb314bee4bf6821ae9cc0d2f09c606d0fbd695f34877afa3d4b782884"
EVIDENCE_SCRIPT = ROOT / "scripts" / "run_optimus_evidence_acquisition_v04.py"
EVIDENCE_SCRIPT_SHA256 = "10e5a8288177c76e207fa264f74e18c3a878d95cf191585ca8a576643475a691"
V05_CONFIG = ROOT / "config" / "research" / "optimus_canonicalization_v0_5.json"

DECISION_PLAN = OPTIMUS_ROOT / "plans" / "seed_debt_resolution_v0_6" / "20261009" / "OPTIMUS_SEED_DEBT_RESOLUTION_V0_6_PLAN.json"
DECISION_PLAN_SHA256 = "2c164036dd7dd44ded7211bf6ec33e9e69628e9ca8aa1fe88cfa195144d5a9a5"
APPLY_PLAN = OPTIMUS_ROOT / "plans" / "seed_debt_resolution_v0_6" / "20261009" / "OPTIMUS_SEED_DEBT_V0_6_APPLY_PLAN.json"
APPLY_PLAN_SHA256 = "f2c3ab4ed4e65eb2c3ba29d1f5effa920ee697e9eb5512f8fa91444612cf0313"

APPLY_PREVIEW = OPTIMUS_ROOT / "seed_debt_resolution_v0_6" / "apply_preview" / "20261009T220159+0800" / "APPLY_PREVIEW.json"
APPLY_PREVIEW_FILE_SHA256 = "cee5f13eaf01d86ba046c3d3e85d39f2655afc5d8ec309ec50ceca1f2b2fbdea"
APPLY_PREVIEW_PAYLOAD_SHA256 = "1f890d7ea1ed12cf49919b937b8d502f4be60be73644d925bdd7e5e93e43a007"
SOURCE_FREEZE_MANIFEST_SHA256 = "5a26a75e93375cdd50d050c349450027edb01748393a6307354527201004adca"

PRECONDITION_ROOT = OPTIMUS_ROOT / "seed_debt_resolution_v0_6" / "precondition"
PRECONDITION_FILE_SHA256 = "cb84976e642d0b700ebecac22e12e920a3f51ae6955825b02204d59d0990b3ec"
PRECONDITION_PAYLOAD_SHA256 = "573d37c5f501a0915c4097fc2dd0bfabf512c7e78e0cb30bd342aa10a5b00b02"

BRIDGE_ROOT = OPTIMUS_ROOT / "classifier_bridge_v0_4_6"
REVIEW_QUEUE_SHA256 = "4034e941be34986d1add9b3d01b96a7d0e09577520b9a4dd210843b2cb2ed712"
SOURCE_NORMALIZATION_PARSER = "OPTIMUS_SEED_DEBT_V0_6_HUMAN_REVIEW_SOURCE"

EXPECTED_REGISTRY_DATA_SHA256 = "8e52129b22a2791908d63f4566c2b90651f42f311176238c9dc04b1a390bb6cd"
EXPECTED_LATEST_BATCH_ID = "CB_41b0746b2bdb0a3658e01f70"
EXPECTED_LATEST_BATCH_RESULT_SHA256 = "46818ccec9df9c62e98d54cd26557ac6bb1694ef0ab791299a03a61b7640bf3f"
SEED_SYMBOLS = {"601100.SH","603009.SH","603119.SH","601689.SH","600699.SH"}

OUTPUT_ROOT = OPTIMUS_ROOT / "seed_debt_resolution_v0_6" / "apply"

CUSTOM_IDS = {
    "HENGLI_IPA": "V06E_628b41d85e1cee8bfd37b0e5",
    "BEITE_L2": "V06E_3d308c152afb102c4452b98d",
    "RONGTAI_IR": "V06E_a92341432c5c324581eb193e",
    "JOYSON_0717": "V06E_1fa6e7cfbc24f4a192fd5bf1",
    "JOYSON_0825": "V06E_692ab6609f5d815b08616586",
}

RAW_EXPECTED = {
    "EACQ_9931abd2a8f08db316d33427": {"symbol":"600699.SH","level":1,"source_sha256":"845fac6fdd50bd806b38470802d4c95f820d1effeac998d8df89cb74d638d634"},
    "EACQ_c5d32152aeda39d87272417d": {"symbol":"601100.SH","level":1,"source_sha256":"4333bee797c3e72339738a1378c009c10dcf5f6279a4709637cf252a9935bfdc"},
    "EACQ_f080d6bbcf410060d116ead6": {"symbol":"601689.SH","level":9,"source_sha256":"762f31df5ffde5a346ebf946c17f34d8e3a33777fc43f71d521e5a434ec03416"},
    "EACQ_c0be286111b64a60dae0aaea": {"symbol":"603009.SH","level":8,"source_sha256":"5eb3a5b04b66fa950231ca72698596a5e5832fde19927cb2806d0df77c58c03f"},
    "EACQ_96aca1c5e08345d9e49d034f": {"symbol":"603009.SH","level":1,"source_sha256":"d887989cec66d3c0d2018d3b0a7fcd0f6f548d612e3cae44ca8e07d517021fca"},
    "EACQ_ec91f094cbc84503a9928c69": {"symbol":"603009.SH","level":1,"source_sha256":"34bebd59dd3cd63b2998a90234d522d5caabf3ba3707d7039e6ad1a69a1175f5"},
    "EACQ_61de46ae995f91507104e3f2": {"symbol":"603119.SH","level":2,"source_sha256":"85d265772cfa3466c275b5d7e0fc3cb6f7d40b35a5326084011bad00e912cbdd"},
}

EXTERNAL_EVENT_TIME = {
    "JOYSON_20260717_ROBOT_CONTROLLER_SUPPLY": "2026-07-17T00:00:00+08:00",
    "JOYSON_20260825_ROBOT_CONTROLLER_DELIVERY": "2026-08-25T00:00:00+08:00",
    "RONGTAI_IR_2026_003": "2026-08-25T00:00:00+08:00",
    "HENGLI_IPA_HUMANOID_APPLICATION": None,
}

EXTERNAL_LEVEL = {
    "JOYSON_20260717_ROBOT_CONTROLLER_SUPPLY": 9,
    "JOYSON_20260825_ROBOT_CONTROLLER_DELIVERY": 9,
    "RONGTAI_IR_2026_003": 9,
    "HENGLI_IPA_HUMANOID_APPLICATION": 2,
}

EXTERNAL_ID = {
    "JOYSON_20260717_ROBOT_CONTROLLER_SUPPLY": CUSTOM_IDS["JOYSON_0717"],
    "JOYSON_20260825_ROBOT_CONTROLLER_DELIVERY": CUSTOM_IDS["JOYSON_0825"],
    "RONGTAI_IR_2026_003": CUSTOM_IDS["RONGTAI_IR"],
    "HENGLI_IPA_HUMANOID_APPLICATION": CUSTOM_IDS["HENGLI_IPA"],
}

EXTERNAL_DOMAIN = {
    "JOYSON_20260717_ROBOT_CONTROLLER_SUPPLY": "controllers / sensing / joints / automotive electronics",
    "JOYSON_20260825_ROBOT_CONTROLLER_DELIVERY": "controllers / sensing / joints / automotive electronics",
    "RONGTAI_IR_2026_003": "precision transmission / robot parts",
    "HENGLI_IPA_HUMANOID_APPLICATION": "planetary roller screws / linear drive",
}


def now_cst() -> datetime:
    return datetime.now(CST)


def iso_now() -> str:
    return now_cst().isoformat(timespec="seconds")


def sha256_file(p: Path) -> str:
    h=hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda:f.read(1024*1024), b""):
            h.update(b)
    return h.hexdigest()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def stable_json(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",",":"), default=str)


def payload_sha(x: dict[str, Any]) -> str:
    y=dict(x)
    y.pop("manifest_payload_sha256",None)
    return sha256_bytes(stable_json(y).encode("utf-8"))


def load_json(p: Path) -> dict[str, Any]:
    return json.loads(p.read_text(encoding="utf-8"))


def write_json(p: Path, x: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp=p.with_suffix(p.suffix+".tmp")
    tmp.write_text(json.dumps(x,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    os.replace(tmp,p)


def write_jsonl(p: Path, rows: list[dict[str, Any]]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    content="".join(json.dumps(r,ensure_ascii=False,sort_keys=True,default=str)+"\n" for r in rows)
    tmp=p.with_suffix(p.suffix+".tmp")
    tmp.write_text(content,encoding="utf-8")
    os.replace(tmp,p)


def import_exact(path: Path, expected_sha: str, name: str):
    if not path.exists():
        raise RuntimeError(f"MISSING_DEPENDENCY: {path}")
    got=sha256_file(path)
    if got!=expected_sha:
        raise RuntimeError(f"DEPENDENCY_SHA_MISMATCH {path} actual={got} expected={expected_sha}")
    spec=importlib.util.spec_from_file_location(name,path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"CANNOT_IMPORT: {path}")
    mod=importlib.util.module_from_spec(spec)
    sys.modules[name]=mod
    spec.loader.exec_module(mod)
    return mod


def extract_json(stdout: str) -> dict[str, Any]:
    s=stdout.strip()
    try:
        x=json.loads(s)
        if isinstance(x,dict): return x
    except Exception:
        pass
    dec=json.JSONDecoder()
    out=[]
    for i,ch in enumerate(s):
        if ch!="{": continue
        try:
            x,end=dec.raw_decode(s[i:])
            if isinstance(x,dict): out.append(x)
        except Exception:
            pass
    if not out:
        raise RuntimeError(f"NO_JSON_IN_STDOUT: {s[-1000:]}")
    return out[-1]


def run_json(cmd: list[str]) -> dict[str, Any]:
    p=subprocess.run(cmd,cwd=ROOT,capture_output=True,text=True,encoding="utf-8",errors="replace")
    if p.returncode!=0:
        raise RuntimeError(f"READ_ONLY_COMMAND_FAILED rc={p.returncode} cmd={cmd} stderr={p.stderr[-1200:]}")
    return extract_json(p.stdout)


def exact_file(path: Path, expected: str, label: str) -> None:
    if not path.exists():
        raise RuntimeError(f"{label}_MISSING: {path}")
    got=sha256_file(path)
    if got!=expected:
        raise RuntimeError(f"{label}_SHA_MISMATCH actual={got} expected={expected} path={path}")


def find_exact_precondition() -> Path:
    if not PRECONDITION_ROOT.exists():
        raise RuntimeError(f"PRECONDITION_ROOT_MISSING: {PRECONDITION_ROOT}")
    for p in sorted(PRECONDITION_ROOT.glob("*/PRECONDITION.json"), key=lambda x:x.stat().st_mtime, reverse=True):
        if sha256_file(p)==PRECONDITION_FILE_SHA256:
            x=load_json(p)
            if x.get("status")=="PASS_OPTIMUS_SEED_DEBT_V0_6_PRECONDITION" and x.get("manifest_payload_sha256")==PRECONDITION_PAYLOAD_SHA256:
                return p
    raise RuntimeError("EXACT_APPROVED_PRECONDITION_NOT_FOUND")


def find_bridge() -> tuple[Path, dict[str,Any], list[dict[str,Any]]]:
    if not BRIDGE_ROOT.exists():
        raise RuntimeError(f"BRIDGE_ROOT_MISSING: {BRIDGE_ROOT}")
    for mp in sorted(BRIDGE_ROOT.glob("*/manifest.json"), key=lambda x:x.stat().st_mtime, reverse=True):
        try:
            m=load_json(mp)
        except Exception:
            continue
        if m.get("status")!="PASS_OPTIMUS_CLASSIFIER_BRIDGE_V0_4_6":
            continue
        if int(m.get("proposal_count", -1))!=7 or int(m.get("parse_error_count", -1))!=0 or int(m.get("local_pdf_read_error_count", -1))!=0:
            continue
        if m.get("classifier_script_sha256")!=EVIDENCE_SCRIPT_SHA256:
            continue
        rq=mp.parent/"review_queue.json"
        ep=mp.parent/"evidence_proposals_v0_4_4.jsonl"
        if not rq.exists() or not ep.exists():
            continue
        if sha256_file(rq)!=REVIEW_QUEUE_SHA256:
            continue
        rows=[json.loads(line) for line in ep.read_text(encoding="utf-8").splitlines() if line.strip()]
        by={str(r.get("evidence_id")):r for r in rows}
        if set(by)!=set(RAW_EXPECTED):
            continue
        ok=True
        for eid,exp in RAW_EXPECTED.items():
            r=by[eid]
            ok &= str(r.get("symbol"))==exp["symbol"]
            ok &= int(r.get("evidence_level") or 0)==exp["level"]
            ok &= int(r.get("tesla_link_level") or 0)==0
            ok &= str(r.get("source_sha256") or "")==exp["source_sha256"]
            ok &= str(r.get("parser_version") or "")=="OPTIMUS_EVIDENCE_ACQUISITION_V0_4_4"
            ok &= str(r.get("source_type") or "")=="CNINFO_PRIMARY_PDF"
        if ok:
            return mp.parent,m,rows
    raise RuntimeError("EXACT_SEVEN_PROPOSAL_BRIDGE_NOT_FOUND")


def source_freeze_from_preview() -> tuple[Path,dict[str,Any]]:
    exact_file(APPLY_PREVIEW,APPLY_PREVIEW_FILE_SHA256,"APPLY_PREVIEW")
    p=load_json(APPLY_PREVIEW)
    if p.get("preview_payload_sha256")!=APPLY_PREVIEW_PAYLOAD_SHA256:
        raise RuntimeError("APPLY_PREVIEW_PAYLOAD_SHA_MISMATCH")
    freeze=Path(str(p["source_freeze"]))
    mp=freeze/"manifest.json"
    exact_file(mp,SOURCE_FREEZE_MANIFEST_SHA256,"SOURCE_FREEZE_MANIFEST")
    m=load_json(mp)
    if m.get("status")!="PASS_OPTIMUS_SEED_DEBT_V0_6_SOURCE_FREEZE":
        raise RuntimeError(f"SOURCE_FREEZE_STATUS_NOT_PASS: {m.get('status')}")
    by={str(x.get("source_id")):x for x in m.get("sources",[])}
    if set(EXTERNAL_ID)-set(by):
        raise RuntimeError(f"SOURCE_FREEZE_MISSING_IDS: {sorted(set(EXTERNAL_ID)-set(by))}")
    for sid in EXTERNAL_ID:
        s=by[sid]
        if s.get("content_validation_pass") is not True:
            raise RuntimeError(f"SOURCE_FREEZE_CONTENT_VALIDATION_FAIL: {sid}")
        raw=Path(str(s["path"]))
        if not raw.exists():
            raise RuntimeError(f"SOURCE_FREEZE_RAW_MISSING: {raw}")
        if sha256_file(raw)!=str(s.get("sha256")):
            raise RuntimeError(f"SOURCE_FREEZE_RAW_SHA_MISMATCH: {sid}")
    return freeze,m


def build_external_rows(freeze_manifest: dict[str,Any]) -> list[dict[str,Any]]:
    by={str(x["source_id"]):x for x in freeze_manifest["sources"]}
    rows=[]
    for sid,eid in EXTERNAL_ID.items():
        s=by[sid]
        authority=str(s.get("authority") or "")
        source_type = "COMPANY_OFFICIAL_PRIMARY_WEB" if authority=="COMPANY_OFFICIAL" else "SSEINFO_PRIMARY_IR_PDF"
        rows.append({
            "evidence_id":eid,
            "symbol": {
                "HENGLI_IPA_HUMANOID_APPLICATION":"601100.SH",
                "RONGTAI_IR_2026_003":"603119.SH",
                "JOYSON_20260717_ROBOT_CONTROLLER_SUPPLY":"600699.SH",
                "JOYSON_20260825_ROBOT_CONTROLLER_DELIVERY":"600699.SH",
            }[sid],
            "company":str(s.get("company") or ""),
            "component_domain":EXTERNAL_DOMAIN[sid],
            "evidence_level":EXTERNAL_LEVEL[sid],
            "evidence_type":"HUMAN_REVIEWED_AUTHORITATIVE_SOURCE",
            "tesla_link_level":0,
            "source_type":source_type,
            "source_ref":str(s["url"]),
            "source_sha256":str(s["sha256"]),
            "event_time":EXTERNAL_EVENT_TIME[sid],
            "observed_at":str(s.get("frozen_at") or freeze_manifest.get("observed_at") or ""),
            "review_state":"PROVISIONAL",
            "confidence":1.0,
            "contradiction_kind":None,
            "contradiction_severity":None,
            "parser_version":SOURCE_NORMALIZATION_PARSER,
            "interpretation":(
                f"V0.6 human-reviewed authoritative source normalization; source_id={sid}; "
                "Tesla/Optimus link intentionally remains T0 unless the company names it."
            ),
            "acquisition":{
                "source_id":sid,
                "authority":authority,
                "frozen_path":str(s["path"]),
                "source_freeze_sha256":str(s["sha256"]),
                "content_validation_pass":True,
                "auto_canonical":False,
            },
        })
    return rows


def build_beite_curated(raw_rows: list[dict[str,Any]]) -> dict[str,Any]:
    by={str(x["evidence_id"]):x for x in raw_rows}
    src=by["EACQ_c0be286111b64a60dae0aaea"]
    support_ids=["EACQ_96aca1c5e08345d9e49d034f","EACQ_ec91f094cbc84503a9928c69"]
    provenance=[src["source_sha256"]]+[by[x]["source_sha256"] for x in support_ids]
    synthesis_sha=sha256_bytes(stable_json(sorted(provenance)).encode("utf-8"))
    return {
        "evidence_id":CUSTOM_IDS["BEITE_L2"],
        "symbol":"603009.SH",
        "company":"北特科技",
        "component_domain":"planetary roller screws / ball screws",
        "evidence_level":2,
        "evidence_type":"HUMAN_REVIEWED_SOURCE_NORMALIZATION",
        "tesla_link_level":0,
        "source_type":"CNINFO_PRIMARY_PDF_HUMAN_REVIEW",
        "source_ref":str(src["source_ref"]),
        "source_sha256":str(src["source_sha256"]),
        "event_time":src.get("event_time"),
        "observed_at":src.get("observed_at"),
        "review_state":"PROVISIONAL",
        "confidence":1.0,
        "contradiction_kind":None,
        "contradiction_severity":None,
        "parser_version":SOURCE_NORMALIZATION_PARSER,
        "interpretation":(
            "V0.6 human review correction: source says 2025 small-batch trial production and "
            "basic conditions for scaled production; this supports L2 operational/customer-development relevance, "
            "not realized L8 mass production. No Tesla inference."
        ),
        "acquisition":{
            "derived_from_raw_evidence_id":"EACQ_c0be286111b64a60dae0aaea",
            "corroborating_raw_evidence_ids":support_ids,
            "provenance_source_sha256s":provenance,
            "provenance_bundle_sha256":synthesis_sha,
            "human_override_reason":"BASIC_MASS_PRODUCTION_CONDITIONS_NOT_REALIZED_MASS_PRODUCTION",
            "auto_canonical":False,
        },
    }


def all_stage_rows(raw_rows:list[dict[str,Any]], freeze_manifest:dict[str,Any]) -> list[dict[str,Any]]:
    return raw_rows + [build_beite_curated(raw_rows)] + build_external_rows(freeze_manifest)


def read_state() -> dict[str,Any]:
    py=sys.executable
    reg=run_json([py,str(REGISTRY_SCRIPT),"status"])
    mq=run_json([py,str(REGISTRY_SCRIPT),"maintenance-queue"])
    can=run_json([py,str(V05_SCRIPT),"status"])
    return {"registry":reg,"maintenance":mq,"canonicalization":can}


def assert_pre_state(state:dict[str,Any]) -> None:
    r=state["registry"]; m=state["maintenance"]; c=state["canonicalization"]
    errors=[]
    if r.get("registry_sha256")!=EXPECTED_REGISTRY_DATA_SHA256: errors.append(("registry_sha",r.get("registry_sha256")))
    if r.get("candidate_count")!=22: errors.append(("candidate_count",r.get("candidate_count")))
    lc=r.get("lifecycle_counts") or {}
    if lc.get("ACTIVE_CANONICAL")!=17 or lc.get("ACTIVE_SEED")!=5: errors.append(("lifecycle",lc))
    sm=m.get("summary") or {}
    for k,v in {"provisional_evidence":0,"seed_audit_debt":5,"stale_canonical":0,"discovery_only":0}.items():
        if sm.get(k)!=v: errors.append((f"maintenance.{k}",sm.get(k)))
    got={str(x.get("symbol")) for x in m.get("seed_audit_debt",[])}
    if got!=SEED_SYMBOLS: errors.append(("seed_symbols",sorted(got)))
    if c.get("fact_count")!=18 or c.get("canonical_symbol_count")!=17 or c.get("materialized_fact_evidence_count")!=18:
        errors.append(("canonical_counts",(c.get("fact_count"),c.get("canonical_symbol_count"),c.get("materialized_fact_evidence_count"))))
    latest=c.get("latest_batch") or {}
    if latest.get("batch_id")!=EXPECTED_LATEST_BATCH_ID: errors.append(("latest_batch",latest.get("batch_id")))
    if latest.get("result_manifest_sha256")!=EXPECTED_LATEST_BATCH_RESULT_SHA256: errors.append(("latest_result_sha",latest.get("result_manifest_sha256")))
    if errors: raise RuntimeError(f"PRE_STATE_DRIFT: {errors}")


def dependency_guard() -> dict[str,Any]:
    exact_file(V05_SCRIPT,V05_SHA256,"V05_SCRIPT")
    exact_file(REGISTRY_SCRIPT,REGISTRY_SCRIPT_SHA256,"REGISTRY_SCRIPT")
    exact_file(EVIDENCE_SCRIPT,EVIDENCE_SCRIPT_SHA256,"EVIDENCE_SCRIPT")
    exact_file(DECISION_PLAN,DECISION_PLAN_SHA256,"DECISION_PLAN")
    exact_file(APPLY_PLAN,APPLY_PLAN_SHA256,"APPLY_PLAN")
    pre=find_exact_precondition()
    bridge,bm,raw=find_bridge()
    freeze,fm=source_freeze_from_preview()
    return {
        "precondition":pre,
        "bridge":bridge,
        "bridge_manifest":bm,
        "raw_rows":raw,
        "source_freeze":freeze,
        "source_freeze_manifest":fm,
    }


def import_runtime():
    v05=import_exact(V05_SCRIPT,V05_SHA256,"optimus_v05_frozen_for_v06")
    cfg=v05.load_json(V05_CONFIG)
    reg=v05.import_registry(cfg)
    return v05,cfg,reg


def batch_identity(v05,plan:dict[str,Any]) -> tuple[str,str]:
    plan_sha=sha256_file(APPLY_PLAN)
    return v05.stable_id("CB",{"plan_id":plan["plan_id"],"plan_sha256":plan_sha}),plan_sha


def existing_batch(v05,cfg,plan_sha:str) -> dict[str,Any]|None:
    con=v05.connect_db(cfg,read_only=True)
    try:
        try:
            df=con.execute(f"SELECT * FROM {v05.TABLE_BATCH} WHERE plan_sha256 = ?",[plan_sha]).fetchdf()
        except Exception:
            return None
        if df.empty:return None
        return df.iloc[0].to_dict()
    finally:
        con.close()


def normalized_stage_rows(reg,rows:list[dict[str,Any]]) -> list[dict[str,Any]]:
    out=[]
    for r in rows:
        try:
            out.append(reg.validate_evidence_row(r,allow_canonical_input=False))
        except Exception as e:
            raise RuntimeError(f"EVIDENCE_VALIDATION_FAILED evidence_id={r.get('evidence_id')} error={e!r}") from e
    ids=[str(x["evidence_id"]) for x in out]
    if len(ids)!=len(set(ids)):
        raise RuntimeError("STAGED_EVIDENCE_ID_DUPLICATE")
    return out


def row_signature(row:dict[str,Any]) -> dict[str,Any]:
    return {
        "symbol":str(row.get("symbol") or ""),
        "evidence_level":int(row.get("evidence_level") or 0),
        "tesla_link_level":int(row.get("tesla_link_level") or 0),
        "source_type":str(row.get("source_type") or ""),
        "source_ref":str(row.get("source_ref") or ""),
        "source_sha256":str(row.get("source_sha256") or ""),
        "parser_version":str(row.get("parser_version") or ""),
    }


def insert_or_verify_evidence(con,reg,row:dict[str,Any]) -> bool:
    eid=str(row["evidence_id"])
    df=con.execute(f"SELECT * FROM {reg.TABLE_EVIDENCE} WHERE evidence_id = ?",[eid]).fetchdf()
    if not df.empty:
        old=df.iloc[0].to_dict()
        # Database row and JSON normalization use the same primary audit fields.
        a=row_signature(old); b=row_signature(row)
        if a!=b:
            raise RuntimeError(f"EXISTING_EVIDENCE_CONFLICT {eid} old={a} new={b}")
        return False
    return bool(reg.insert_evidence(con,row))


def nullable_timestamp_value(x: Any) -> Any:
    """Preserve a genuinely unknown event_time as SQL NULL.

    DuckDB->pandas fetchdf() represents a NULL TIMESTAMPTZ as pandas.NaT.
    The frozen Registry V0.3.1 normalize_ts() turns pandas.NaT into the literal
    string "NaT", which DuckDB correctly rejects on a subsequent TIMESTAMPTZ
    insert. V0.6 must not fabricate an event time from observed_at, so normalize
    only missing/NaT sentinels back to None.
    """
    if x is None:
        return None
    s = str(x).strip()
    if s.lower() in {"", "nat", "nan", "none", "null"}:
        return None
    return x


def materialize_fact_evidence_v06_safe(v05, con, reg, *, batch_id: str, fact_row: dict[str,Any], actor: str) -> dict[str,Any]:
    """V0.5.1-equivalent materialization with one nullable-event_time fix.

    This intentionally does not alter the frozen V0.5.1 or Registry V0.3.1
    modules. It preserves all V0.5.1 review/materialization semantics and only
    converts pandas.NaT from a nullable source event_time back to Python None.
    """
    anchor_id = str(fact_row["anchor_evidence_id"])
    anchor = v05._load_anchor_row(con, reg, anchor_id)
    payload = v05.build_materialized_fact_evidence(
        batch_id=batch_id,
        fact_row=fact_row,
        anchor_row=anchor,
    )
    payload["event_time"] = nullable_timestamp_value(payload.get("event_time"))
    normalized = reg.validate_evidence_row(payload, allow_canonical_input=True)
    # Defensive check: Registry normalization must never emit literal NaT.
    if str(normalized.get("event_time")).strip().lower() == "nat":
        raise RuntimeError(
            f"MATERIALIZED_EVENT_TIME_NAT_NOT_ALLOWED fact_id={fact_row['fact_id']} anchor={anchor_id}"
        )
    inserted = reg.insert_evidence(con, normalized)

    state, reason = v05._latest_review_state(con, reg, anchor_id)
    desired_reason = f"SOURCE_ABSORBED_BY_CANONICAL_FACT_V0_5_1:{fact_row['fact_id']}"
    review_added = False
    if state != "REJECTED" or reason != desired_reason:
        reg.add_review_decision(con, anchor_id, "REJECTED", desired_reason, actor)
        review_added = True

    return {
        "fact_id": fact_row["fact_id"],
        "symbol": fact_row["symbol"],
        "anchor_evidence_id": anchor_id,
        "materialized_evidence_id": normalized["evidence_id"],
        "evidence_inserted": bool(inserted),
        "anchor_review_rejected": bool(review_added),
        "repair_record_inserted": False,
        "nullable_event_time_normalized": payload.get("event_time") is None,
    }


def latest_review(v05,con,reg,eid:str)->tuple[str|None,str|None]:
    return v05._latest_review_state(con,reg,eid)


def add_review_if_needed(v05,con,reg,eid:str,decision:str,reason:str,actor:str)->bool:
    state,old_reason=latest_review(v05,con,reg,eid)
    if state==decision and old_reason==reason:
        return False
    reg.add_review_decision(con,eid,decision,reason,actor)
    return True


def validate_plan_sources(con,v05,reg,plan:dict[str,Any]) -> dict[str,Any]:
    errors=[]
    facts=[]
    for fact in plan["facts"]:
        ids=[fact["anchor_evidence_id"]]+list(fact.get("support_evidence_ids",[]))
        local=[]
        for eid in ids:
            df=con.execute(f"SELECT * FROM {reg.TABLE_EVIDENCE} WHERE evidence_id = ?",[eid]).fetchdf()
            if df.empty:
                local.append(f"missing {eid}"); continue
            row=df.iloc[0].to_dict()
            state,_=latest_review(v05,con,reg,eid)
            if state!="PROVISIONAL":
                local.append(f"{eid} effective state {state} != PROVISIONAL")
            if str(row.get("symbol"))!=fact["symbol"]:
                local.append(f"{eid} symbol mismatch")
        adf=con.execute(f"SELECT * FROM {reg.TABLE_EVIDENCE} WHERE evidence_id = ?",[fact["anchor_evidence_id"]]).fetchdf()
        if not adf.empty:
            a=adf.iloc[0].to_dict()
            if int(a.get("evidence_level") or 0)!=int(fact["canonical_level"]):
                local.append(f"anchor level {a.get('evidence_level')} != canonical {fact['canonical_level']}")
            if int(a.get("tesla_link_level") or 0)!=0:
                local.append("anchor Tesla link != 0")
            if not str(a.get("source_sha256") or "").strip():
                local.append("anchor source_sha256 missing")
        if local: errors.extend([f"{fact['fact_key']}: {x}" for x in local])
        facts.append({"fact_key":fact["fact_key"],"symbol":fact["symbol"],"source_ids":ids,"ok":not local})
    for rr in plan.get("reject_only",[]):
        eid=rr["evidence_id"]
        df=con.execute(f"SELECT * FROM {reg.TABLE_EVIDENCE} WHERE evidence_id = ?",[eid]).fetchdf()
        if df.empty: errors.append(f"reject_only missing {eid}")
    return {"ok":not errors,"errors":errors,"fact_checks":facts}


def final_state_payload(con,v05,reg,reg_cfg,batch_id:str) -> dict[str,Any]:
    maint=reg.maintenance_queue_payload(con,reg_cfg)["summary"]
    rows=con.execute(f"SELECT lifecycle_state,count(*) n FROM {reg.TABLE_CANDIDATE} GROUP BY 1").fetchall()
    lifecycle={str(k):int(v) for k,v in rows}
    five=[]
    for sym in sorted(SEED_SYMBOLS):
        c=reg.read_candidate(con,sym) or {}
        five.append({"symbol":sym,"lifecycle_state":c.get("lifecycle_state"),"f_status":c.get("f_status")})
    facts=con.execute(f"SELECT count(*),count(DISTINCT symbol) FROM {v05.TABLE_FACT}").fetchone()
    mats=con.execute(f"SELECT count(*) FROM {reg.TABLE_EVIDENCE} WHERE parser_version = ?",[v05.FACT_MATERIALIZATION_PARSER]).fetchone()[0]
    audit=v05.audit_fact_materialization(con,reg,reg_cfg,batch_id)
    return {
        "maintenance":maint,
        "lifecycle_counts":lifecycle,
        "five":five,
        "fact_count":int(facts[0]),
        "canonical_symbol_count":int(facts[1]),
        "materialized_fact_evidence_count":int(mats),
        "batch_materialization_audit":audit,
    }


def assert_final_state(x:dict[str,Any])->None:
    errors=[]
    m=x["maintenance"]
    for k,v in {"provisional_evidence":0,"seed_audit_debt":0,"stale_canonical":0,"discovery_only":0}.items():
        if int(m.get(k,-1))!=v: errors.append((k,m.get(k)))
    if x["lifecycle_counts"].get("ACTIVE_CANONICAL")!=22 or x["lifecycle_counts"].get("ACTIVE_SEED",0)!=0:
        errors.append(("lifecycle_counts",x["lifecycle_counts"]))
    if x["fact_count"]!=24 or x["canonical_symbol_count"]!=22 or x["materialized_fact_evidence_count"]!=24:
        errors.append(("canonical_counts",(x["fact_count"],x["canonical_symbol_count"],x["materialized_fact_evidence_count"])))
    for r in x["five"]:
        if r["lifecycle_state"]!="ACTIVE_CANONICAL" or r["f_status"]!="POSSIBLE":
            errors.append(("candidate",r))
    a=x["batch_materialization_audit"]
    if a.get("anomaly_count")!=0 or a.get("repair_required"):
        errors.append(("materialization_audit",a.get("anomalies")))
    if errors: raise RuntimeError(f"POST_STATE_VALIDATION_FAILED: {errors}")


def cmd_check() -> int:
    try:
        art=dependency_guard()
        v05,cfg,reg=import_runtime()
        plan=load_json(APPLY_PLAN)
        batch_id,plan_sha=batch_identity(v05,plan)
        old=existing_batch(v05,cfg,plan_sha)
        if old:
            status="ALREADY_APPLIED_OPTIMUS_SEED_DEBT_V0_6"
            state=read_state()
        else:
            state=read_state()
            assert_pre_state(state)
            rows=normalized_stage_rows(reg,all_stage_rows(art["raw_rows"],art["source_freeze_manifest"]))
            status="PASS_OPTIMUS_SEED_DEBT_V0_6_GUARDED_APPLY_CHECK"
        out={
            "status":status,
            "plan_sha256":plan_sha,
            "batch_id":batch_id,
            "existing_batch":old,
            "staged_evidence_count":12,
            "canonical_fact_count":6,
            "seed_symbol_count":5,
            "next":"RUN_APPLY" if not old else "VERIFY_ONLY",
            "mutation":False,
            "broker":False,"orders":False,"production_db":False,
        }
        print(json.dumps(out,ensure_ascii=False,indent=2,default=str))
        return 0
    except Exception as e:
        print(json.dumps({"status":"FAIL_OPTIMUS_SEED_DEBT_V0_6_GUARDED_APPLY_CHECK","error":repr(e),"mutation":False},ensure_ascii=False,indent=2))
        return 2


def cmd_plan() -> int:
    try:
        art=dependency_guard()
        v05,cfg,reg=import_runtime()
        plan=load_json(APPLY_PLAN)
        batch_id,plan_sha=batch_identity(v05,plan)
        old=existing_batch(v05,cfg,plan_sha)
        state=read_state()
        if not old: assert_pre_state(state)
        rows=normalized_stage_rows(reg,all_stage_rows(art["raw_rows"],art["source_freeze_manifest"]))
        derived=[{
            "fact_id":v05.fact_id_for(f),
            "fact_key":f["fact_key"],"symbol":f["symbol"],
            "level":f["canonical_level"],"tesla":f["canonical_tesla_link_level"],
            "anchor":f["anchor_evidence_id"],"supports":f.get("support_evidence_ids",[])
        } for f in plan["facts"]]
        out={
            "status":"PASS_OPTIMUS_SEED_DEBT_V0_6_GUARDED_APPLY_PLAN" if not old else "ALREADY_APPLIED_OPTIMUS_SEED_DEBT_V0_6",
            "plan_id":plan["plan_id"],"plan_sha256":plan_sha,"batch_id":batch_id,
            "staged_evidence_count":len(rows),"raw_bridge_proposal_count":7,"human_reviewed_source_row_count":5,
            "facts":derived,"reject_only":plan["reject_only"],
            "target":plan["expected_post_state"],
            "requires_exact_precondition_payload_sha256":PRECONDITION_PAYLOAD_SHA256,
            "requires_exact_plan_sha256":APPLY_PLAN_SHA256,
            "mutation":False,
        }
        print(json.dumps(out,ensure_ascii=False,indent=2,default=str))
        return 0
    except Exception as e:
        print(json.dumps({"status":"FAIL_OPTIMUS_SEED_DEBT_V0_6_GUARDED_APPLY_PLAN","error":repr(e),"mutation":False},ensure_ascii=False,indent=2))
        return 2


def write_apply_artifacts(outdir:Path,plan:dict[str,Any],stage_rows:list[dict[str,Any]],result:dict[str,Any])->str:
    outdir.mkdir(parents=True,exist_ok=True)
    shutil.copy2(APPLY_PLAN,outdir/"approved_apply_plan.json")
    write_jsonl(outdir/"staged_source_evidence.jsonl",stage_rows)
    write_json(outdir/"result.json",result)
    with (outdir/"canonical_facts.csv").open("w",encoding="utf-8-sig",newline="") as f:
        cols=["fact_id","symbol","company","fact_key","fact_type","canonical_level","canonical_tesla_link_level","anchor_evidence_id","fact_text"]
        w=csv.DictWriter(f,fieldnames=cols);w.writeheader()
        for r in result["canonical_facts"]: w.writerow({k:r.get(k) for k in cols})
    md=[
        "# Optimus Seed Debt Resolution V0.6 APPLY","",
        f"- Batch: `{result['batch_id']}`",
        f"- Apply plan SHA256: `{result['plan_sha256']}`",
        f"- Precondition payload SHA256: `{PRECONDITION_PAYLOAD_SHA256}`",
        f"- Actor: `{result['actor']}`",
        f"- Facts: `{len(result['canonical_facts'])}`",
        f"- ACTIVE_CANONICAL: `17 -> 22`",
        f"- ACTIVE_SEED: `5 -> 0`",
        "- Tesla/Optimus link for all five: `T0`",
        "- Theme stage change: `false`","",
        "Canonical unit remains the reviewed Fact; raw/source evidence is source-only after materialization.",
    ]
    (outdir/"APPLY_REPORT.md").write_text("\n".join(md),encoding="utf-8")
    files=[outdir/"approved_apply_plan.json",outdir/"staged_source_evidence.jsonl",outdir/"result.json",outdir/"canonical_facts.csv",outdir/"APPLY_REPORT.md"]
    man={
        "schema_version":"OPTIMUS_SEED_DEBT_V0_6_APPLY_MANIFEST",
        "batch_id":result["batch_id"],
        "immutable":True,
        "upstream":{
            "decision_plan_sha256":DECISION_PLAN_SHA256,
            "source_freeze_manifest_sha256":SOURCE_FREEZE_MANIFEST_SHA256,
            "apply_preview_file_sha256":APPLY_PREVIEW_FILE_SHA256,
            "apply_preview_payload_sha256":APPLY_PREVIEW_PAYLOAD_SHA256,
            "precondition_file_sha256":PRECONDITION_FILE_SHA256,
            "precondition_payload_sha256":PRECONDITION_PAYLOAD_SHA256,
            "pre_apply_registry_sha256":EXPECTED_REGISTRY_DATA_SHA256,
        },
        "files":{p.name:sha256_file(p) for p in files},
        "safety":{"research_only":True,"broker":False,"orders":False,"production_db":False},
    }
    mp=outdir/"MANIFEST.json"
    write_json(mp,man)
    return sha256_file(mp)


def cmd_apply(args:argparse.Namespace)->int:
    # Approval is conversationally granted, but runtime still requires exact frozen hashes.
    if args.approve_precondition_payload_sha256!=PRECONDITION_PAYLOAD_SHA256:
        print(json.dumps({"status":"FAIL_OPTIMUS_SEED_DEBT_V0_6_APPLY","error":"PRECONDITION_APPROVAL_HASH_MISMATCH","mutation":False},ensure_ascii=False,indent=2)); return 2
    if args.approve_plan_sha256!=APPLY_PLAN_SHA256:
        print(json.dumps({"status":"FAIL_OPTIMUS_SEED_DEBT_V0_6_APPLY","error":"PLAN_APPROVAL_HASH_MISMATCH","mutation":False},ensure_ascii=False,indent=2)); return 2
    if not str(args.actor or "").strip():
        print(json.dumps({"status":"FAIL_OPTIMUS_SEED_DEBT_V0_6_APPLY","error":"ACTOR_REQUIRED","mutation":False},ensure_ascii=False,indent=2)); return 2

    attempt=None
    con=None
    try:
        art=dependency_guard()
        v05,cfg,reg=import_runtime()
        reg_cfg=reg.load_config(v05.resolve_path(cfg["registry_config"]))
        plan=load_json(APPLY_PLAN)
        batch_id,plan_sha=batch_identity(v05,plan)

        old=existing_batch(v05,cfg,plan_sha)
        if old:
            print(json.dumps({
                "status":"ALREADY_APPLIED_OPTIMUS_SEED_DEBT_V0_6",
                "batch_id":old.get("batch_id"),"plan_sha256":plan_sha,
                "message":"No mutation performed; exact apply plan already recorded.",
                "mutation":False
            },ensure_ascii=False,indent=2,default=str))
            return 0

        # Re-read live pre-state immediately before opening the write transaction.
        assert_pre_state(read_state())

        stage_rows=normalized_stage_rows(reg,all_stage_rows(art["raw_rows"],art["source_freeze_manifest"]))
        stamp=now_cst().strftime("%Y%m%dT%H%M%S+0800")
        attempt=OUTPUT_ROOT/stamp
        attempt.mkdir(parents=True,exist_ok=False)
        shutil.copy2(APPLY_PLAN,attempt/"approved_apply_plan.precommit.json")
        write_jsonl(attempt/"staged_source_evidence.precommit.jsonl",stage_rows)
        write_json(attempt/"UPSTREAM_GUARD.json",{
            "status":"PASS_PRECOMMIT_GUARD",
            "observed_at":iso_now(),
            "plan_sha256":plan_sha,
            "precondition_payload_sha256":PRECONDITION_PAYLOAD_SHA256,
            "source_freeze_manifest_sha256":SOURCE_FREEZE_MANIFEST_SHA256,
            "apply_preview_payload_sha256":APPLY_PREVIEW_PAYLOAD_SHA256,
            "registry_data_sha256":EXPECTED_REGISTRY_DATA_SHA256,
            "v05_script_sha256":V05_SHA256,
            "registry_script_sha256":REGISTRY_SCRIPT_SHA256,
            "evidence_script_sha256":EVIDENCE_SCRIPT_SHA256,
        })

        con=v05.connect_db(cfg,read_only=False)
        # Tables already exist after 3 successful batches; don't alter schema here.
        for table in (v05.TABLE_FACT,v05.TABLE_FACT_SOURCE,v05.TABLE_BATCH,reg.TABLE_EVIDENCE,reg.TABLE_REVIEW,reg.TABLE_CANDIDATE):
            try:
                con.execute(f"SELECT 1 FROM {table} LIMIT 1")
            except Exception as e:
                raise RuntimeError(f"REQUIRED_TABLE_MISSING_OR_UNREADABLE {table}: {e!r}")

        # Guard duplicate fact keys/ids before transaction.
        for fact in plan["facts"]:
            fid=v05.fact_id_for(fact)
            n=con.execute(f"SELECT count(*) FROM {v05.TABLE_FACT} WHERE fact_id = ? OR fact_key = ?",[fid,fact["fact_key"]]).fetchone()[0]
            if n:
                raise RuntimeError(f"FACT_ALREADY_EXISTS_WITHOUT_BATCH {fact['fact_key']} {fid}")

        actor=str(args.actor).strip()
        applied_at=iso_now()
        maintenance_before=reg.maintenance_queue_payload(con,reg_cfg)["summary"]
        inserted_stage=[]
        review_log=[]
        fact_rows=[]
        materialization=[]
        reconcile=[]

        con.execute("BEGIN TRANSACTION")
        try:
            # Stage exact source evidence inside the same transaction as canonicalization.
            for row in stage_rows:
                ins=insert_or_verify_evidence(con,reg,row)
                inserted_stage.append({"evidence_id":row["evidence_id"],"inserted":bool(ins)})

            src_validation=validate_plan_sources(con,v05,reg,plan)
            if not src_validation["ok"]:
                raise RuntimeError(f"PLAN_SOURCE_VALIDATION_FAILED {src_validation['errors']}")

            for fact in plan["facts"]:
                fid=v05.fact_id_for(fact)
                anchor=str(fact["anchor_evidence_id"])
                canonicalized_at=applied_at

                # Supporting source rows remain provenance but do not independently
                # contribute evidence strength after the reviewed Fact is materialized.
                source_rows=[(anchor,"ANCHOR","REJECTED_AS_SOURCE_ABSORBED_BY_CANONICAL_FACT")]
                for evid in fact.get("support_evidence_ids",[]):
                    evid=str(evid)
                    reason=f"DUPLICATE_CORROBORATION_SUPERSEDED_BY_CANONICAL_FACT_V0_6:{fid}"
                    added=add_review_if_needed(v05,con,reg,evid,"REJECTED",reason,actor)
                    review_log.append({"evidence_id":evid,"decision":"REJECTED","reason":reason,"added":added})
                    source_rows.append((evid,"SUPPORT","REJECTED_AS_DUPLICATE_CORROBORATION"))

                con.execute(
                    f"""INSERT INTO {v05.TABLE_FACT}
                    (fact_id,batch_id,symbol,company,fact_key,fact_type,fact_text,component_domain,canonical_level,canonical_tesla_link_level,anchor_evidence_id,policy_version,actor,canonicalized_at,plan_sha256)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [fid,batch_id,fact["symbol"],fact["company"],fact["fact_key"],fact["fact_type"],fact["fact_text"],fact["component_domain"],
                     int(fact["canonical_level"]),int(fact["canonical_tesla_link_level"]),anchor,"OPTIMUS_SEED_DEBT_RESOLUTION_V0_6",actor,canonicalized_at,plan_sha]
                )
                fact_db={
                    "fact_id":fid,"batch_id":batch_id,"symbol":fact["symbol"],"company":fact["company"],
                    "fact_key":fact["fact_key"],"fact_type":fact["fact_type"],"fact_text":fact["fact_text"],
                    "component_domain":fact["component_domain"],"canonical_level":int(fact["canonical_level"]),
                    "canonical_tesla_link_level":int(fact["canonical_tesla_link_level"]),
                    "anchor_evidence_id":anchor,"canonicalized_at":canonicalized_at,
                }
                mat=materialize_fact_evidence_v06_safe(v05,con,reg,batch_id=batch_id,fact_row=fact_db,actor=actor)
                materialization.append(mat)
                if mat["anchor_review_rejected"]:
                    review_log.append({"evidence_id":anchor,"decision":"REJECTED","reason":f"SOURCE_ABSORBED_BY_CANONICAL_FACT_V0_5_1:{fid}","added":True})

                for evid,role,action in source_rows:
                    con.execute(f"INSERT INTO {v05.TABLE_FACT_SOURCE}(fact_id,evidence_id,source_role,review_action) VALUES (?,?,?,?)",[fid,evid,role,action])

                fact_rows.append({
                    "fact_id":fid,"symbol":fact["symbol"],"company":fact["company"],"fact_key":fact["fact_key"],
                    "fact_type":fact["fact_type"],"fact_text":fact["fact_text"],"component_domain":fact["component_domain"],
                    "canonical_level":int(fact["canonical_level"]),"canonical_tesla_link_level":0,
                    "anchor_evidence_id":anchor,"support_evidence_ids":list(fact.get("support_evidence_ids",[])),
                    "materialized_evidence_id":mat["materialized_evidence_id"],
                })

            for row in plan.get("reject_only",[]):
                eid=str(row["evidence_id"])
                reason=f"{row['reason_code']}:OPTIMUS_SEED_DEBT_RESOLUTION_V0_6"
                added=add_review_if_needed(v05,con,reg,eid,"REJECTED",reason,actor)
                review_log.append({"evidence_id":eid,"decision":"REJECTED","reason":reason,"added":added})

            for sym in sorted(SEED_SYMBOLS):
                reconcile.append(reg.reconcile_symbol(con,reg_cfg,sym))

            # Validate the complete target while still inside the transaction.
            final_precommit=final_state_payload(con,v05,reg,reg_cfg,batch_id)
            # audit_fact_materialization needs batch facts only and works before TABLE_BATCH row.
            assert_final_state(final_precommit)

            con.execute(
                f"""INSERT INTO {v05.TABLE_BATCH}
                (batch_id,plan_id,plan_sha256,actor,applied_at,fact_count,canonical_symbol_count,covered_source_count,reject_only_count,affected_symbols_json,output_dir,result_manifest_sha256)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                [batch_id,plan["plan_id"],plan_sha,actor,applied_at,len(plan["facts"]),len(SEED_SYMBOLS),
                 len({x for f in plan["facts"] for x in [f["anchor_evidence_id"],*f.get("support_evidence_ids",[])]} | {r["evidence_id"] for r in plan.get("reject_only",[])}),
                 len(plan.get("reject_only",[])),json.dumps(sorted(SEED_SYMBOLS),ensure_ascii=False),str(attempt),None]
            )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

        maintenance_after=reg.maintenance_queue_payload(con,reg_cfg)["summary"]
        result={
            "status":"PASS_OPTIMUS_SEED_DEBT_RESOLUTION_V0_6_APPLY",
            "batch_id":batch_id,"plan_id":plan["plan_id"],"plan_sha256":plan_sha,
            "actor":actor,"applied_at":applied_at,
            "staged_source_evidence_count":len(stage_rows),
            "staged_source_insert_results":inserted_stage,
            "canonical_facts":fact_rows,
            "canonical_symbols":sorted(SEED_SYMBOLS),
            "source_review_decisions":review_log,
            "source_review_decision_count":len(review_log),
            "fact_materialization":materialization,
            "maintenance_before":maintenance_before,
            "maintenance_after":maintenance_after,
            "reconcile":{"symbols":len(reconcile),"changed":sum(int(bool(x.get("changed"))) for x in reconcile),"results":reconcile},
            "post_state":final_precommit,
            "theme_stage_change":False,
            "all_five_tesla_link":"T0",
            "research_only":True,"broker":False,"orders":False,"production_db":False,
        }

        # Preserve the precommit guard/staging artifacts and add the final immutable set.
        manifest_sha=write_apply_artifacts(attempt,plan,stage_rows,result)

        # Metadata-only append to the already-committed batch row.
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute(f"UPDATE {v05.TABLE_BATCH} SET output_dir = ?, result_manifest_sha256 = ? WHERE batch_id = ?",[str(attempt),manifest_sha,batch_id])
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

        snap=reg.snapshot(con,reg_cfg,as_of=now_cst().strftime("%Y%m%d"))
        con.close();con=None

        result["output"]=str(attempt)
        result["result_manifest_sha256"]=manifest_sha
        result["registry_snapshot"]={"status":snap.get("status"),"output":snap.get("output"),"registry_sha256":snap.get("registry_sha256")}
        write_json(attempt/"result.final.json",result)

        print(json.dumps({
            "status":result["status"],"batch_id":batch_id,"plan_sha256":plan_sha,
            "canonical_fact_count":6,"canonical_symbol_count":5,
            "maintenance_before":maintenance_before,"maintenance_after":maintenance_after,
            "target_active_canonical":22,"target_active_seed":0,
            "theme_stage_change":False,"all_five_tesla_link":"T0",
            "output":str(attempt),"result_manifest_sha256":manifest_sha,
            "registry_snapshot":result["registry_snapshot"],
            "broker":False,"orders":False,"production_db":False,
        },ensure_ascii=False,indent=2,default=str))
        return 0
    except Exception as e:
        if con is not None:
            try: con.close()
            except Exception: pass
        if attempt is not None and attempt.exists():
            try:
                write_json(attempt/"FAILURE.json",{"status":"FAIL_OPTIMUS_SEED_DEBT_RESOLUTION_V0_6_APPLY","error":repr(e),"observed_at":iso_now()})
            except Exception:
                pass
        print(json.dumps({"status":"FAIL_OPTIMUS_SEED_DEBT_RESOLUTION_V0_6_APPLY","error":repr(e),"broker":False,"orders":False,"production_db":False},ensure_ascii=False,indent=2))
        return 4


def cmd_verify()->int:
    try:
        art=dependency_guard()
        v05,cfg,reg=import_runtime()
        reg_cfg=reg.load_config(v05.resolve_path(cfg["registry_config"]))
        plan=load_json(APPLY_PLAN)
        batch_id,plan_sha=batch_identity(v05,plan)
        con=v05.connect_db(cfg,read_only=True)
        try:
            n=con.execute(f"SELECT count(*) FROM {v05.TABLE_BATCH} WHERE batch_id = ? AND plan_sha256 = ?",[batch_id,plan_sha]).fetchone()[0]
            if n!=1: raise RuntimeError("V0_6_BATCH_NOT_FOUND")
            x=final_state_payload(con,v05,reg,reg_cfg,batch_id)
            assert_final_state(x)
            b=con.execute(f"SELECT result_manifest_sha256,output_dir FROM {v05.TABLE_BATCH} WHERE batch_id = ?",[batch_id]).fetchone()
        finally:
            con.close()
        out={"status":"PASS_OPTIMUS_SEED_DEBT_RESOLUTION_V0_6_VERIFY","batch_id":batch_id,"plan_sha256":plan_sha,
             "post_state":x,"result_manifest_sha256":b[0],"output":b[1],
             "theme_stage_change":False,"all_five_tesla_link":"T0","mutation":False}
        print(json.dumps(out,ensure_ascii=False,indent=2,default=str)); return 0
    except Exception as e:
        print(json.dumps({"status":"FAIL_OPTIMUS_SEED_DEBT_RESOLUTION_V0_6_VERIFY","error":repr(e),"mutation":False},ensure_ascii=False,indent=2)); return 3


def build_parser()->argparse.ArgumentParser:
    p=argparse.ArgumentParser(description="PositionX Optimus Seed Debt Resolution V0.6 guarded APPLY")
    sp=p.add_subparsers(dest="cmd",required=True)
    sp.add_parser("check")
    sp.add_parser("plan")
    a=sp.add_parser("apply")
    a.add_argument("--approve-precondition-payload-sha256",required=True)
    a.add_argument("--approve-plan-sha256",required=True)
    a.add_argument("--actor",required=True)
    sp.add_parser("verify")
    return p


def main()->int:
    a=build_parser().parse_args()
    if a.cmd=="check":return cmd_check()
    if a.cmd=="plan":return cmd_plan()
    if a.cmd=="apply":return cmd_apply(a)
    if a.cmd=="verify":return cmd_verify()
    return 2


if __name__=="__main__":
    raise SystemExit(main())
