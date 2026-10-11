from __future__ import annotations

"""
PositionX Optimus Seed Debt Resolution V0.6
Phase: authoritative source freeze + APPLY preview

Safety:
- source-freeze performs public HTTP GET only
- no Registry open/write/reconcile
- no canonicalization mutation
- no evidence inbox write
- no broker/order/production DB
- preview-apply writes preview artifacts only
"""

import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CST = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path(r"D:\positionx_data")
OPTIMUS_ROOT = DATA_ROOT / "research" / "big_opportunity" / "optimus"
PLAN_DIR = OPTIMUS_ROOT / "plans" / "seed_debt_resolution_v0_6" / "20261009"
FREEZE_ROOT = OPTIMUS_ROOT / "source_freeze_v0_6"
PREVIEW_ROOT = OPTIMUS_ROOT / "seed_debt_resolution_v0_6" / "apply_preview"

PLAN_JSON = PLAN_DIR / "OPTIMUS_SEED_DEBT_RESOLUTION_V0_6_PLAN.json"
PLAN_MD = PLAN_DIR / "OPTIMUS_SEED_DEBT_RESOLUTION_V0_6_PLAN.md"

SOURCES = [
    {
        "source_id": "JOYSON_20260717_ROBOT_CONTROLLER_SUPPLY",
        "company": "均胜电子",
        "authority": "COMPANY_OFFICIAL",
        "url": "https://www.joyson.com/index.php/esg/info/1426.html",
        "kind": "html",
        "required_all": [
            "头部机器人",
            "量产供货",
            "控制器",
        ],
        "supports_fact_ids": ["JOYSON_2026_ROBOT_CONTROLLER_MASS_PRODUCTION_SUPPLY"],
    },
    {
        "source_id": "JOYSON_20260825_ROBOT_CONTROLLER_DELIVERY",
        "company": "均胜电子",
        "authority": "COMPANY_OFFICIAL",
        "url": "https://www.joyson.com/index.php/news/info/1436.html",
        "kind": "html",
        "required_all": [
            "北美",
            "中央控制器",
            "交付",
        ],
        "supports_fact_ids": ["JOYSON_2026_ROBOT_CONTROLLER_MASS_PRODUCTION_SUPPLY"],
    },
    {
        "source_id": "HENGLI_IPA_HUMANOID_APPLICATION",
        "company": "恒立液压",
        "authority": "COMPANY_OFFICIAL",
        "url": "https://www.henglihydraulics.com/col1196/1215",
        "kind": "html",
        "required_all": [
            "行星滚柱丝杠",
            "人形机器人",
        ],
        "supports_fact_ids": ["HENGLI_IPA_HUMANOID_APPLICATION"],
    },
    {
        "source_id": "RONGTAI_IR_2026_003",
        "company": "浙江荣泰",
        "authority": "SSEINFO_COMPANY_IR_RECORD",
        "url": "https://sns.sseinfo.com/resources/images/upload/202608/2026082513350431512152539.pdf",
        "kind": "pdf",
        "required_all": [
            "已向国内外客户交付机器人零部件相关产品",
            "订单要求积极排产",
            "客户相关信息涉及商业保密",
        ],
        "supports_fact_ids": ["RONGTAI_2026H1_ROBOT_COMPONENT_DELIVERY_AND_ORDER_SCHEDULING"],
    },
]

QUEUE_DECISIONS = [
    {
        "evidence_id":"EACQ_9931abd2a8f08db316d33427",
        "company":"均胜电子",
        "decision":"REJECTED",
        "reason":"RELATED_PARTY_LEASE_NAME_LIST_NOT_COMPANY_ROBOT_MILESTONE",
    },
    {
        "evidence_id":"EACQ_c5d32152aeda39d87272417d",
        "company":"恒立液压",
        "decision":"APPROVED_AS_CANONICAL_SOURCE",
        "reason":"GENUINE_LINEAR_DRIVE_PLANETARY_SCREW_OPERATIONAL_MILESTONE",
    },
    {
        "evidence_id":"EACQ_f080d6bbcf410060d116ead6",
        "company":"拓普集团",
        "decision":"APPROVED_AS_CANONICAL_SOURCE",
        "reason":"ROBOT_ACTUATORS_AND_DEXTEROUS_HAND_MOTOR_SMALL_BATCH_DELIVERY",
    },
    {
        "evidence_id":"EACQ_c0be286111b64a60dae0aaea",
        "company":"北特科技",
        "decision":"REJECTED",
        "reason":"LEVEL_OVERCLASSIFICATION_BASIC_MASS_PRODUCTION_CONDITIONS_NOT_REALIZED_MASS_PRODUCTION",
    },
    {
        "evidence_id":"EACQ_96aca1c5e08345d9e49d034f",
        "company":"北特科技",
        "decision":"APPROVED_SUPPORTING_SOURCE",
        "reason":"CORROBORATES_ROBOT_SCREW_APPLICATION_AND_UNCERTAINTY",
    },
    {
        "evidence_id":"EACQ_ec91f094cbc84503a9928c69",
        "company":"北特科技",
        "decision":"APPROVED_SUPPORTING_SOURCE",
        "reason":"CORROBORATES_ROBOT_SCREW_APPLICATION_AND_UNCERTAINTY",
    },
    {
        "evidence_id":"EACQ_61de46ae995f91507104e3f2",
        "company":"浙江荣泰",
        "decision":"REJECTED",
        "reason":"INDUSTRY_BACKGROUND_NOT_COMPANY_SPECIFIC_MILESTONE",
    },
]

CANONICAL_FACTS = [
    {
        "fact_id":"HENGLI_2025_PLANETARY_ROLLER_SCREW_INITIAL_MASS_PRODUCTION",
        "symbol":"601100.SH","company":"恒立液压","level":"L1","tesla_link":"T0",
        "source_kind":"CNINFO_ALREADY_FROZEN",
        "source_ref":"CNINFO:1225127026",
        "fact":"线性驱动项目正式投入生产运营；行星滚柱丝杠已完成送样和初步量产。",
        "anti_inference":"不得表述为‘人形机器人已量产供货’。"
    },
    {
        "fact_id":"HENGLI_IPA_HUMANOID_APPLICATION",
        "symbol":"601100.SH","company":"恒立液压","level":"L2","tesla_link":"T0",
        "source_kind":"SOURCE_FREEZE_REQUIRED",
        "source_id":"HENGLI_IPA_HUMANOID_APPLICATION",
        "fact":"IPA系列电动缸采用行星滚柱丝杠，并列示用于人形机器人等自动化机械。",
        "anti_inference":"仅证明人形应用相关性，不证明人形客户商业交付。"
    },
    {
        "fact_id":"BEITE_ROBOT_SCREW_CUSTOMER_DEVELOPMENT_AND_PILOT_PRODUCTION",
        "symbol":"603009.SH","company":"北特科技","level":"L2","tesla_link":"T0",
        "source_kind":"CNINFO_ALREADY_FROZEN",
        "source_ref":"CNINFO:1225144307;1225144315;1225501025",
        "fact":"公司与客户持续开发用于机器人肢体关节执行器、灵巧手等场景的丝杠产品；2025年有小批量试制；公司称具备规模化量产基本条件，但具体量产时间和规模仍不确定。",
        "anti_inference":"不得保留原V0.4.4 L8；‘具备量产条件’不等于‘已实现量产’。"
    },
    {
        "fact_id":"RONGTAI_2026H1_ROBOT_COMPONENT_DELIVERY_AND_ORDER_SCHEDULING",
        "symbol":"603119.SH","company":"浙江荣泰","level":"L9","tesla_link":"T0",
        "source_kind":"SOURCE_FREEZE_REQUIRED",
        "source_id":"RONGTAI_IR_2026_003",
        "fact":"2026H1公司已向国内外客户交付机器人零部件相关产品，并根据国内外客户订单要求积极排产；当前销售额仍较低。",
        "anti_inference":"投资者问题提到Tesla，但公司未确认Tesla客户关系。"
    },
    {
        "fact_id":"TOP_2026H1_ROBOT_ACTUATORS_SMALL_BATCH_DELIVERY",
        "symbol":"601689.SH","company":"拓普集团","level":"L9","tesla_link":"T0",
        "source_kind":"CNINFO_ALREADY_FROZEN",
        "source_ref":"CNINFO:1225521853",
        "fact":"直线执行器、旋转执行器及灵巧手电机已进入小批量交付阶段；机器人部件产业基地首期建设基本完成。",
        "anti_inference":"不得把汽车业务中的美国创新车企A客户映射为Optimus机器人客户。"
    },
    {
        "fact_id":"JOYSON_2026_ROBOT_CONTROLLER_MASS_PRODUCTION_SUPPLY",
        "symbol":"600699.SH","company":"均胜电子","level":"L9","tesla_link":"T0",
        "source_kind":"SOURCE_FREEZE_REQUIRED",
        "source_ids":[
            "JOYSON_20260717_ROBOT_CONTROLLER_SUPPLY",
            "JOYSON_20260825_ROBOT_CONTROLLER_DELIVERY",
        ],
        "fact":"公司官方披露控制器等产品已为头部机器人客户量产/规模化量产供货，并向北美及国内头部客户交付高性能机身机甲、中央控制器等产品。",
        "anti_inference":"未披露客户名称，不得推断为Tesla/Optimus。"
    },
]

LIFECYCLE_TRANSITIONS = [
    {"symbol":"601100.SH","company":"恒立液压","from":"ACTIVE_SEED","to":"ACTIVE_CANONICAL","f_status":"POSSIBLE","tesla_link":"T0"},
    {"symbol":"603009.SH","company":"北特科技","from":"ACTIVE_SEED","to":"ACTIVE_CANONICAL","f_status":"POSSIBLE","tesla_link":"T0"},
    {"symbol":"603119.SH","company":"浙江荣泰","from":"ACTIVE_SEED","to":"ACTIVE_CANONICAL","f_status":"POSSIBLE","tesla_link":"T0"},
    {"symbol":"601689.SH","company":"拓普集团","from":"ACTIVE_SEED","to":"ACTIVE_CANONICAL","f_status":"POSSIBLE","tesla_link":"T0"},
    {"symbol":"600699.SH","company":"均胜电子","from":"ACTIVE_SEED","to":"ACTIVE_CANONICAL","f_status":"POSSIBLE","tesla_link":"T0"},
]


def now_cst() -> datetime:
    return datetime.now(CST)


def iso_now() -> str:
    return now_cst().isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024*1024), b""):
            h.update(chunk)
    return h.hexdigest()


def stable_json(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",",":"), default=str)


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) + "\n" for r in rows)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


def load_plan() -> dict[str, Any]:
    if not PLAN_JSON.exists():
        raise RuntimeError(f"plan not found: {PLAN_JSON}")
    obj = json.loads(PLAN_JSON.read_text(encoding="utf-8"))
    if obj.get("plan_id") != "OPTIMUS_SEED_DEBT_RESOLUTION_V0_6_PLAN_20261009":
        raise RuntimeError(f"unexpected plan_id: {obj.get('plan_id')}")
    if obj.get("plan_only") is not True or obj.get("apply_blocked") is not True:
        raise RuntimeError("plan safety flags changed")
    return obj


def decode_html(data: bytes) -> str:
    for enc in ("utf-8","gb18030","gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8","replace")


def pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except Exception as e:
        raise RuntimeError(f"PYPDF_REQUIRED:{e!r}")
    reader = PdfReader(io.BytesIO(data), strict=False)
    return "\n".join((p.extract_text() or "") for p in reader.pages)


def fetch(url: str, tries: int = 5, timeout: int = 45) -> tuple[bytes, dict[str, Any]]:
    last = None
    for i in range(tries):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) PositionX-Research/1.0",
                "Accept":"*/*",
                "Accept-Encoding":"identity",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
                return data, {
                    "http_status": int(getattr(r,"status",r.getcode())),
                    "content_type": r.headers.get("Content-Type"),
                    "content_length": r.headers.get("Content-Length"),
                }
        except Exception as e:
            last = e
            time.sleep(1.5*(i+1))
    raise RuntimeError(f"HTTP_FETCH_FAILED url={url} last={last!r}")


def normalize_for_match(text: str) -> str:
    return re.sub(r"\s+","", text).lower()


def validate_source_text(source: dict[str, Any], text: str) -> list[dict[str, Any]]:
    norm = normalize_for_match(text)
    checks=[]
    for phrase in source["required_all"]:
        ok = normalize_for_match(phrase) in norm
        checks.append({"phrase":phrase,"pass":ok})
    return checks


def freeze_sources() -> int:
    load_plan()
    cap = FREEZE_ROOT / now_cst().strftime("%Y%m%dT%H%M%S+0800")
    raw = cap / "raw"
    raw.mkdir(parents=True, exist_ok=False)

    rows=[]
    failures=[]
    for i, s in enumerate(SOURCES,1):
        print(f"[SOURCE-FREEZE] {i}/{len(SOURCES)} {s['source_id']}", file=sys.stderr, flush=True)
        try:
            data, transport = fetch(s["url"])
            ext = ".pdf" if s["kind"]=="pdf" else ".html"
            path = raw / f"{s['source_id']}{ext}"
            path.write_bytes(data)

            text = pdf_text(data) if s["kind"]=="pdf" else decode_html(data)
            checks = validate_source_text(s, text)
            all_pass = all(x["pass"] for x in checks)
            rec = {
                **s,
                "frozen_at": iso_now(),
                "path": str(path),
                "bytes": len(data),
                "sha256": sha256_bytes(data),
                "transport": transport,
                "required_phrase_checks": checks,
                "content_validation_pass": all_pass,
            }
            rows.append(rec)
            if not all_pass:
                failures.append({"source_id":s["source_id"],"reason":"REQUIRED_PHRASE_MISSING","checks":checks})
        except Exception as e:
            failures.append({"source_id":s["source_id"],"reason":repr(e)})

    status = "PASS_OPTIMUS_SEED_DEBT_V0_6_SOURCE_FREEZE" if not failures and len(rows)==len(SOURCES) else "FAIL_OPTIMUS_SEED_DEBT_V0_6_SOURCE_FREEZE"
    manifest = {
        "status":status,
        "capture_id":cap.name,
        "observed_at":iso_now(),
        "plan":str(PLAN_JSON),
        "plan_sha256":sha256_file(PLAN_JSON),
        "source_count_expected":len(SOURCES),
        "source_count_frozen":len(rows),
        "sources":rows,
        "failures":failures,
        "safety":{
            "public_http_get_only":True,
            "registry_opened":False,
            "registry_mutation":False,
            "canonicalization_mutation":False,
            "evidence_inbox_write":False,
            "broker":False,
            "orders":False,
            "production_db":False,
        },
    }
    manifest["manifest_payload_sha256"] = sha256_bytes(stable_json(manifest).encode("utf-8"))
    write_json(cap/"manifest.json", manifest)
    print(json.dumps({
        "status":status,
        "capture":str(cap),
        "source_count_frozen":len(rows),
        "failure_count":len(failures),
        "manifest":str(cap/"manifest.json"),
        "manifest_payload_sha256":manifest["manifest_payload_sha256"],
    }, ensure_ascii=False, indent=2))
    return 0 if status.startswith("PASS_") else 3


def latest_pass_freeze() -> Path:
    if not FREEZE_ROOT.exists():
        raise RuntimeError(f"freeze root not found: {FREEZE_ROOT}")
    mps=sorted(FREEZE_ROOT.glob("*/manifest.json"), key=lambda p:p.stat().st_mtime, reverse=True)
    for mp in mps:
        try:
            obj=json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if obj.get("status")=="PASS_OPTIMUS_SEED_DEBT_V0_6_SOURCE_FREEZE":
            return mp.parent
    raise RuntimeError("no PASS source freeze found")


def preview_apply() -> int:
    plan = load_plan()
    freeze = latest_pass_freeze()
    fm = json.loads((freeze/"manifest.json").read_text(encoding="utf-8"))
    by_id = {x["source_id"]:x for x in fm["sources"]}

    # Require every external source used by proposed facts.
    missing=[]
    for f in CANONICAL_FACTS:
        ids=[]
        if f.get("source_id"): ids=[f["source_id"]]
        if f.get("source_ids"): ids=list(f["source_ids"])
        for sid in ids:
            if sid not in by_id or not by_id[sid].get("content_validation_pass"):
                missing.append({"fact_id":f["fact_id"],"source_id":sid})
    if missing:
        raise RuntimeError(f"SOURCE_FREEZE_PRECONDITION_FAILED: {missing}")

    cap = PREVIEW_ROOT / now_cst().strftime("%Y%m%dT%H%M%S+0800")
    cap.mkdir(parents=True, exist_ok=False)

    facts=[]
    for f in CANONICAL_FACTS:
        x=dict(f)
        ids=[]
        if x.get("source_id"): ids=[x["source_id"]]
        if x.get("source_ids"): ids=list(x["source_ids"])
        if ids:
            x["frozen_sources"]=[{
                "source_id":sid,
                "url":by_id[sid]["url"],
                "sha256":by_id[sid]["sha256"],
                "path":by_id[sid]["path"],
                "authority":by_id[sid]["authority"],
            } for sid in ids]
        facts.append(x)

    write_jsonl(cap/"HUMAN_REVIEW_DECISIONS_PREVIEW.jsonl", QUEUE_DECISIONS)
    write_jsonl(cap/"CANONICAL_FACTS_PREVIEW.jsonl", facts)
    write_jsonl(cap/"LIFECYCLE_TRANSITIONS_PREVIEW.jsonl", LIFECYCLE_TRANSITIONS)

    preview = {
        "status":"READY_FOR_USER_APPROVAL",
        "preview_only":True,
        "apply_executed":False,
        "plan_id":plan["plan_id"],
        "plan_sha256":sha256_file(PLAN_JSON),
        "source_freeze":str(freeze),
        "source_freeze_manifest_sha256":sha256_file(freeze/"manifest.json"),
        "review_decision_count":len(QUEUE_DECISIONS),
        "canonical_fact_count":len(facts),
        "lifecycle_transition_count":len(LIFECYCLE_TRANSITIONS),
        "proposed_endpoint":{
            "active_canonical":22,
            "active_seed":0,
            "discovery_only":0,
            "removed":0,
        },
        "theme_stage_change":False,
        "all_tesla_links":"T0",
        "registry_precondition_check":"DEFERRED_TO_APPLY_COMMAND",
        "required_user_action":"EXPLICIT_APPROVAL_BEFORE_APPLY",
        "files":{
            "human_review_decisions":str(cap/"HUMAN_REVIEW_DECISIONS_PREVIEW.jsonl"),
            "canonical_facts":str(cap/"CANONICAL_FACTS_PREVIEW.jsonl"),
            "lifecycle_transitions":str(cap/"LIFECYCLE_TRANSITIONS_PREVIEW.jsonl"),
        },
        "safety":{
            "registry_opened":False,
            "registry_mutation":False,
            "canonicalization_mutation":False,
            "db_opened":False,
            "broker":False,
            "orders":False,
            "production_db":False,
        },
    }
    preview["preview_payload_sha256"]=sha256_bytes(stable_json(preview).encode("utf-8"))
    write_json(cap/"APPLY_PREVIEW.json", preview)
    print(json.dumps(preview, ensure_ascii=False, indent=2))
    return 0


def check() -> int:
    errors=[]
    try:
        plan=load_plan()
    except Exception as e:
        plan=None
        errors.append(repr(e))
    result={
        "status":"PASS_OPTIMUS_SEED_DEBT_V0_6_SOURCE_FREEZE_PREVIEW_CHECK" if not errors else "FAIL_OPTIMUS_SEED_DEBT_V0_6_SOURCE_FREEZE_PREVIEW_CHECK",
        "plan_dir":str(PLAN_DIR),
        "plan_json":str(PLAN_JSON),
        "plan_json_exists":PLAN_JSON.exists(),
        "plan_md_exists":PLAN_MD.exists(),
        "plan_id":plan.get("plan_id") if plan else None,
        "source_count":len(SOURCES),
        "sources":[{"source_id":s["source_id"],"authority":s["authority"],"url":s["url"]} for s in SOURCES],
        "registry_opened":False,
        "registry_mutation":False,
        "canonicalization_mutation":False,
        "broker":False,
        "orders":False,
        "errors":errors,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


def main() -> int:
    ap=argparse.ArgumentParser()
    sp=ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("check")
    sp.add_parser("freeze-sources")
    sp.add_parser("preview-apply")
    a=ap.parse_args()
    if a.cmd=="check": return check()
    if a.cmd=="freeze-sources": return freeze_sources()
    if a.cmd=="preview-apply": return preview_apply()
    return 2

if __name__=="__main__":
    raise SystemExit(main())
