from __future__ import annotations
import argparse, hashlib, importlib.util, json, os, re, shutil, subprocess, sys, tempfile
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

PROJECT_ROOT=Path(__file__).resolve().parents[1]
CONFIG=PROJECT_ROOT/"config"/"research"/"optimus_external_classifier_v072b.json"
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
    except Exception as e: raise RuntimeError(f"NON_JSON_OUTPUT {script.name}: {e!r} stdout={p.stdout[:1000]!r}")

def dependency_check(cfg:dict[str,Any])->dict[str,Any]:
    actual={}
    for key,rel in cfg["dependency_paths"].items():
        p=resolve(rel)
        if not p.exists(): raise RuntimeError(f"DEPENDENCY_MISSING {key} {p}")
        got=sha256_file(p); actual[key]=got
        exp=cfg["dependency_sha256"][key]
        if got!=exp: raise RuntimeError(f"DEPENDENCY_SHA_MISMATCH {key} actual={got} expected={exp}")
    a=run_json(resolve(cfg["dependency_paths"]["v072a_script"]),"check")
    if a.get("status")!="PASS_OPTIMUS_EXTERNAL_INBOX_V0_7_2A_CHECK":
        raise RuntimeError(f"V072A_NOT_PASS {a}")
    return {
        "status":"PASS_OPTIMUS_EXTERNAL_CAPTURE_CLASSIFIER_V0_7_2B_CHECK",
        "classifier_id":cfg["classifier_id"],
        "case_start":cfg["case_start"],
        "dependency_sha256":actual,
        "v072a_status":a["status"],
        "pdf_adapter":{
            "evidence_script_sha256":cfg["dependency_sha256"]["evidence_script"],
            "api":"extract_pdf_text(pdf_bytes,max_pages)->(text,pages,parse_error)",
            "select_pdf_engine_used":False,
            "empty_text_fails_closed":True,
        },
        "retrieval_contract":cfg["retrieval_contract"],
        "transport_policy":cfg.get("transport_policy",{}),
        "transport_contract":cfg.get("transport_contract",{}),
        "temporal_contract":cfg["temporal_contract"],
        "dedupe_contract":cfg.get("dedupe_contract",{}),
        "safety":cfg["safety"],
        "next":"RUN_CONTROLLED_THEME_OFFICIAL_CAPTURE",
        "mutation":False,
    }

class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts=[]
        self.skip=0
    def handle_starttag(self,tag,attrs):
        if tag.lower() in ("script","style","noscript"): self.skip+=1
    def handle_endtag(self,tag):
        if tag.lower() in ("script","style","noscript") and self.skip: self.skip-=1
    def handle_data(self,data):
        if not self.skip and data.strip(): self.parts.append(data.strip())

def html_text(data:bytes)->str:
    # Best-effort decode; raw bytes remain authoritative in V0.7.2A.
    s=data.decode("utf-8",errors="replace")
    p=TextExtractor(); p.feed(s)
    return "\n".join(p.parts)

def pdf_text(path:Path,cfg:dict[str,Any])->str:
    ev=import_module(resolve(cfg["dependency_paths"]["evidence_script"]),"positionx_v044_pdf_for_v072b")
    try:
        raw=path.read_bytes()
        max_pages=int((cfg.get("pdf") or {}).get("max_pages",260))
        text,pages,parse_error=ev.extract_pdf_text(raw,max_pages)
        if parse_error:
            raise RuntimeError(f"V044_PDF_PARSE_ERROR {parse_error}")
        if not str(text or "").strip():
            raise RuntimeError(f"V044_PDF_TEXT_EMPTY pages={pages}")
        return str(text)
    except Exception as e:
        raise RuntimeError(f"PDF_TEXT_EXTRACTION_FAILED {e!r}")

def extract_text(raw_path:Path,media_type:str|None,cfg:dict[str,Any])->str:
    mt=(media_type or "").lower()
    suf=raw_path.suffix.lower()
    if "pdf" in mt or suf==".pdf": return pdf_text(raw_path,cfg)
    data=raw_path.read_bytes()
    if "html" in mt or suf in (".html",".htm"): return html_text(data)
    return data.decode("utf-8",errors="replace")

def norm(s:str)->str:
    return re.sub(r"\s+"," ",s.lower()).strip()

THEME_CONTEXT=[
    r"(?<![A-Za-z0-9_])optimus(?![A-Za-z0-9_])",
    r"\btesla bot\b",
    r"\bhumanoid robots?\b",
    r"人形机器人",
    r"人型机器人",
]
TESLA_CONTEXT=[
    r"\btesla\b",r"(?<![A-Za-z0-9_])optimus(?![A-Za-z0-9_])",r"\btesla bot\b",
    r"特斯拉"
]
ROBOT_CONTEXT=[
    r"(?<![A-Za-z0-9_])optimus(?![A-Za-z0-9_])",r"\btesla bot\b",r"\bhumanoid(?: robots?)?\b",r"\brobotics?\b",
    r"人形机器人",r"人型机器人",r"具身智能",r"机器人"
]

TAG_PATTERNS={
 "SUPPLIER_SOURCING":[
    r"\bsupplier nomination\b",r"\bnominated supplier\b",r"\bsourcing\b",
    r"\bsupply agreement\b",r"\bsupplier agreement\b",
    r"供应商定点",r"获得.{0,24}定点",r"进入.{0,24}供应链",r"供应协议"
 ],
 "PURCHASE_ORDER":[
    r"\bpurchase order\b",r"\bproduction order\b",r"\bfirm order\b",
    r"采购订单",r"订单",r"中标"
 ],
 "TOOLING_CAPACITY":[
    r"\bproduction lines?\b",r"\btooling\b",r"\bcapacity expansion\b",
    r"\bcapacity build\b",r"\bmanufacturing line\b",
    r"产能建设",r"生产基地建设",r"产线建设",r"智能化产线",r"扩充.{0,24}产能"
 ],
 "MASS_PRODUCTION":[
    r"\bvolume production\b",r"\bmass production\b",r"\bseries production\b",
    r"\bscaled production\b",
    r"规模化批量供货",r"规模化生产",r"批量供应",r"批量供货",r"量产"
 ],
 "BUILD_RAMP":[
    r"\bramp(?:ing|ed)?\b",r"\bproduction ramp\b",r"\bbuild ramp\b",
    r"量产爬坡",r"产能爬坡",r"产业化放量"
 ],
 "INTERNAL_DEPLOYMENT":[
    r"\binternal deployment\b",r"\bdeployed in (?:our|tesla) factor(?:y|ies)\b",
    r"\bworking in (?:our|tesla) factor(?:y|ies)\b",
    r"工厂内部部署",r"厂内部署",r"工厂应用"
 ],
 "EXTERNAL_DEPLOYMENT":[
    r"\bexternal deployment\b",r"\bcustomer deployment\b",
    r"\bdeployed to customers\b",r"\bcommercial deployment\b",
    r"客户部署",r"交付客户",r"正式推向市场",r"商业化落地"
 ],
 "SMALL_BATCH_DELIVERY":[
    r"\bsmall[- ]batch deliver(?:y|ies|ed)\b",r"\bpilot[- ]production deliver(?:y|ies|ed)\b",
    r"小批量供货",r"小批量交付",r"小批量供应"
 ],
 "BATCH_SUPPLY":[
    r"\bbatch supply\b",r"\bbatch deliver(?:y|ies|ed)\b",r"\bproduction supply\b",
    r"批量供货",r"批量供应",r"批量交付"
 ],
 "CUSTOMER_VALIDATION":[
    r"\bcustomer validation\b",r"\bcustomer verification\b",r"\bvalidation with customers?\b",
    r"客户验证",r"客户认证",r"产品验证",r"验证和导入",r"验证及导入",r"新产品验证"
 ],
 "ORDER_SCHEDULING":[
    r"\bproduction according to orders?\b",r"\border[- ]linked production\b",
    r"\bscheduling production\b",
    r"按订单排产",r"根据订单排产",r"订单排产"
 ],
 "ROBOT_REVENUE_SHIPMENT":[
    r"\brobot(?:ics)? (?:revenue|shipments?)\b",r"\bhumanoid robot(?:ics)? (?:revenue|shipments?)\b",
    r"机器人.{0,24}(?:营收|收入|出货|交付)",r"人形机器人.{0,24}(?:营收|收入|出货|交付)"
 ],
}
CONTRADICTION_PATTERNS=[
    r"\boptimus\b.{0,120}\b(?:delay(?:ed|s)?|postpon(?:ed|ement)|cancel(?:led|ed|lation)|halt(?:ed|s)?|suspend(?:ed|s)?)\b",
    r"\b(?:delay(?:ed|s)?|postpon(?:ed|ement)|cancel(?:led|ed|lation)|halt(?:ed|s)?|suspend(?:ed|s)?)\b.{0,120}\boptimus\b",
    r"\bnot (?:a )?(?:supplier|vendor) (?:to|for) tesla\b",
    r"\bnot supplying tesla\b",
    r"(?:人形机器人|optimus).{0,60}(?:延期|推迟|取消|终止|暂停)",
    r"(?:延期|推迟|取消|终止|暂停).{0,60}(?:人形机器人|optimus)",
    r"不是.{0,20}特斯拉.{0,20}供应商",
    r"未向.{0,20}特斯拉.{0,20}(?:供货|供应)"
]

def has_any(text:str,patterns:list[str])->bool:
    return any(re.search(p,text,re.I|re.S) for p in patterns)

def signal_tags(text:str)->list[str]:
    t=norm(text)
    return [tag for tag,pats in TAG_PATTERNS.items() if has_any(t,pats)]

def context_blocks(text:str)->list[str]:
    # Keep HTML/PDF line structure whenever possible, but also split long prose into
    # sentences. This prevents a keyword in one document section from borrowing a
    # milestone from an unrelated section.
    raw=[]
    for line in re.split(r"[\r\n]+",text):
        line=line.strip()
        if not line:
            continue
        parts=re.split(r"(?<=[.!?。！？])\s+",line)
        raw.extend(x.strip() for x in parts if x.strip())
    return raw

def qa_answer_blocks(text:str)->list[str]:
    # PDF extractors often insert a hard newline every visual line. For investor Q&A,
    # those line breaks are layout artifacts, not semantic boundaries. Build an answer
    # block only from explicit company-answer markers and stop at the next numbered
    # question / question marker. This is deliberately narrower than paragraph joining.
    lines=str(text or "").replace("\r\n","\n").replace("\r","\n").split("\n")
    out=[]
    cur=[]
    in_answer=False

    def flush():
        nonlocal cur,in_answer
        if cur:
            joined=" ".join(x.strip() for x in cur if x.strip()).strip()
            if joined:
                out.append(joined)
        cur=[]
        in_answer=False

    answer_start=re.compile(r"^\s*(?:答|回复)\s*[:：]")
    # Handles "2、...", "2....", "问题2：", "问：".
    question_start=re.compile(r"^\s*(?:(?:\d{1,3})\s*[、.．]|问题\s*\d{1,3}\s*[:：]?|问\s*[:：])")

    for raw in lines:
        line=raw.strip()
        if not line:
            continue
        if answer_start.search(line):
            flush()
            cur=[line]
            in_answer=True
            continue
        if in_answer and question_start.search(line):
            flush()
            continue
        if in_answer:
            cur.append(line)
    flush()
    return out

def qa_local_signal_tags(text:str,context_patterns:list[str],radius:int=180)->list[str]:
    found=set()
    for answer in qa_answer_blocks(text):
        a=norm(answer)
        for pat in context_patterns:
            for m in re.finditer(pat,a,flags=re.I):
                lo=max(0,m.start()-radius)
                hi=min(len(a),m.end()+radius)
                found.update(signal_tags(a[lo:hi]))
    return [tag for tag in TAG_PATTERNS if tag in found]

def contextual_signal_tags(text:str,context_patterns:list[str])->list[str]:
    blocks=context_blocks(text)
    found=set()
    for i,block in enumerate(blocks):
        b=norm(block)
        if has_any(b,context_patterns):
            # Same block always qualifies.
            found.update(signal_tags(b))
            # A short heading such as "Tesla Optimus" may carry context into exactly
            # the next block, but never across another heading/section. A question is
            # not a heading and must never lend its robot/Tesla context to an unrelated
            # company answer.
            question_like=bool(re.search(r"[?？]\s*$",b)) or bool(re.search(r"(?:请问|是否|怎么样|如何|什么|吗)[^。！？?]{0,80}[?？]?\s*$",b))
            if len(b)<=120 and not question_like and i+1<len(blocks):
                found.update(signal_tags(blocks[i+1]))

    # Investor-relations PDFs frequently hard-wrap one semantic answer across many
    # visual lines. Recover only a bounded local window inside an explicit answer block.
    found.update(qa_local_signal_tags(text,context_patterns,radius=180))
    return [tag for tag in TAG_PATTERNS if tag in found]

def dual_context_signal_tags(
    text:str,
    left_patterns:list[str],
    right_patterns:list[str],
    radius:int=180
)->list[str]:
    """
    Return milestone tags only when *both* semantic contexts bind to the same local
    milestone. This is stricter than intersecting two document-level tag sets.

    Example that must NOT bind:
      Q1 humanoid robot batch supply
      Q2 automotive Tesla orders

    Example that DOES bind:
      "Tesla Optimus humanoid robot project has entered batch supply."
    """
    found=set()
    blocks=context_blocks(text)

    # Same semantic block / heading carry.
    for i,block in enumerate(blocks):
        b=norm(block)
        both=has_any(b,left_patterns) and has_any(b,right_patterns)
        if both:
            found.update(signal_tags(b))
            question_like=bool(re.search(r"[?？]\s*$",b)) or bool(
                re.search(r"(?:请问|是否|怎么样|如何|什么|吗)[^。！？?]{0,80}[?？]?\s*$",b)
            )
            if len(b)<=120 and not question_like and i+1<len(blocks):
                found.update(signal_tags(blocks[i+1]))

    # Q&A local binding: the same milestone window must contain both contexts.
    for answer in qa_answer_blocks(text):
        a=norm(answer)
        for tag,pats in TAG_PATTERNS.items():
            matched=False
            for pat in pats:
                for m in re.finditer(pat,a,flags=re.I):
                    lo=max(0,m.start()-radius)
                    hi=min(len(a),m.end()+radius)
                    w=a[lo:hi]
                    if has_any(w,left_patterns) and has_any(w,right_patterns):
                        found.add(tag)
                        matched=True
                        break
                if matched:
                    break

    return [tag for tag in TAG_PATTERNS if tag in found]

def parse_time(x:Any)->datetime|None:
    if x is None: return None
    s=str(x).strip()
    if not s: return None
    d=datetime.fromisoformat(s.replace("Z","+00:00"))
    if d.tzinfo is None or d.utcoffset() is None:
        raise RuntimeError(f"NAIVE_CAPTURE_TIMESTAMP {s}")
    return d

def effective_time(capture:dict[str,Any])->tuple[datetime|None,str]:
    for key in ("event_time","published_at"):
        d=parse_time(capture.get(key))
        if d is not None: return d,key
    return None,"UNKNOWN"

def classify_capture(capture:dict[str,Any],text:str,cfg:dict[str,Any])->dict[str,Any]:
    t=norm(text)
    global_tags=signal_tags(t)
    theme_tags=contextual_signal_tags(text,THEME_CONTEXT)
    tesla_tags=contextual_signal_tags(text,TESLA_CONTEXT)
    robot_tags=contextual_signal_tags(text,ROBOT_CONTEXT)
    tesla_robot_tags=dual_context_signal_tags(text,TESLA_CONTEXT,ROBOT_CONTEXT,radius=180)
    has_theme=has_any(t,THEME_CONTEXT)
    has_tesla=has_any(t,TESLA_CONTEXT)
    contradiction=has_any(t,CONTRADICTION_PATTERNS)
    lane=str(capture["lane"])
    eff,eff_source=effective_time(capture)
    case_start=parse_time(cfg["case_start"])
    assert case_start is not None
    temporal_state="UNKNOWN_EFFECTIVE_TIME"
    if eff is not None:
        temporal_state="PROSPECTIVE_VERIFIED" if eff>=case_start else "HISTORICAL_PRE_CASE_START"

    strong=set(cfg["classification_contract"]["strong_milestone_tags"])
    supplier_com=set(cfg["classification_contract"]["supplier_commercial_tags"])

    if lane=="THEME_OFFICIAL":
        decision_tags=set(theme_tags)
    else:
        decision_tags=set(robot_tags)

    material=bool(decision_tags & (strong|supplier_com)) or contradiction
    cross_section_tags=[x for x in global_tags if x not in decision_tags]

    queue="NO_NEW_MATERIAL_EVIDENCE"
    reason="NO_CONTEXTUAL_STRONG_MILESTONE_OR_CONTRADICTION"

    if contradiction:
        if temporal_state=="HISTORICAL_PRE_CASE_START":
            queue="HISTORICAL_BASELINE_ONLY"; reason="PRE_CASE_START_CONTRADICTION_CONTEXT"
        elif temporal_state=="UNKNOWN_EFFECTIVE_TIME":
            queue="HUMAN_REVIEW_REQUIRED"; reason="CONTRADICTION_WITH_UNKNOWN_EFFECTIVE_TIME"
        else:
            queue="CONTRADICTION_ALERT"; reason="PROSPECTIVE_OFFICIAL_CONTRADICTION"
    elif not material and cross_section_tags:
        queue="NO_NEW_MATERIAL_EVIDENCE"; reason="CROSS_SECTION_SIGNAL_REJECTED"
    elif material and temporal_state=="HISTORICAL_PRE_CASE_START":
        queue="HISTORICAL_BASELINE_ONLY"; reason="NO_HINDSIGHT_PRE_CASE_START_SIGNAL"
    elif material and temporal_state=="UNKNOWN_EFFECTIVE_TIME":
        queue="HUMAN_REVIEW_REQUIRED"; reason="MATERIAL_SIGNAL_BUT_EVENT_TIME_UNKNOWN"
    elif lane=="THEME_OFFICIAL" and has_theme and bool(set(theme_tags) & strong) and temporal_state=="PROSPECTIVE_VERIFIED":
        queue="STAGE2_GATE_CANDIDATE"; reason="THEME_OFFICIAL_PROSPECTIVE_STRONG_MILESTONE"
    elif lane in ("SUPPLIER_GLOBAL_OFFICIAL","NEW_CANDIDATE_DISCOVERY") and bool(set(tesla_robot_tags) & supplier_com) and temporal_state=="PROSPECTIVE_VERIFIED":
        queue="STAGE2_GATE_CANDIDATE"; reason="SUPPLIER_OFFICIAL_NAMED_TESLA_ROBOT_BOUND_PROSPECTIVE_COMMERCIAL_MILESTONE"
    elif bool(set(robot_tags) & supplier_com) and temporal_state=="PROSPECTIVE_VERIFIED":
        queue="EVIDENCE_PROPOSAL"; reason="PROSPECTIVE_SUPPLIER_COMMERCIALIZATION_WITHOUT_NAMED_TESLA_GATE"
    elif material:
        queue="HUMAN_REVIEW_REQUIRED"; reason="MATERIAL_SIGNAL_NOT_ELIGIBLE_FOR_AUTOMATIC_QUEUE_CLASS"

    return {
        "schema_version":"OPTIMUS_EXTERNAL_CLASSIFICATION_V0_7_2B_2",
        "capture_identity":capture["capture_identity"],
        "source_id":capture["source_id"],
        "source_role":capture["source_role"],
        "lane":lane,
        "entity_name":capture["entity_name"],
        "symbol":capture.get("symbol"),
        "source_url":capture["source_url"],
        "document_title":capture["document_title"],
        "event_time":capture.get("event_time"),
        "published_at":capture.get("published_at"),
        "observed_at":capture["observed_at"],
        "effective_time":eff.isoformat() if eff else None,
        "effective_time_source":eff_source,
        "temporal_state":temporal_state,
        "has_optimus_or_humanoid_context":has_theme,
        "has_tesla_named_context":has_tesla,
        "signal_tags":list(decision_tags),
        "global_signal_tags":global_tags,
        "theme_context_signal_tags":theme_tags,
        "tesla_context_signal_tags":tesla_tags,
        "tesla_robot_context_signal_tags":tesla_robot_tags,
        "tesla_robot_link_eligible":bool(set(tesla_robot_tags) & supplier_com),
        "robot_context_signal_tags":robot_tags,
        "cross_section_signal_tags":cross_section_tags,
        "context_scope_rule":"SAME_BLOCK_OR_QA_LOCAL_WINDOW_180",
        "contradiction_match":contradiction,
        "queue_state":queue,
        "reason_code":reason,
        "human_review_required":queue not in ("NO_NEW_MATERIAL_EVIDENCE","HISTORICAL_BASELINE_ONLY"),
        "fact_inference":False,
        "evidence_level_inference":False,
        "tesla_link_level_inference":False,
        "registry_mutation":False,
        "canonicalization_mutation":False,
        "theme_stage_mutation":False,
    }

def prior_classified_ids(root:Path)->set[str]:
    """
    B.8 routine dedupe: every successful classification is processed, even when it
    produced NO_NEW_MATERIAL_EVIDENCE and therefore never entered classification_queue.
    Errors / WAITING_MANUAL_CAPTURE are deliberately excluded.
    """
    out=set()
    if not root.exists():
        return out

    # B.8 authoritative append-only ledgers.
    for p in root.rglob("processed_capture_identities.jsonl"):
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            x=json.loads(line)
            if x.get("status")!="CLASSIFIED_SUCCESS":
                continue
            cid=str(x.get("capture_identity") or "")
            if cid:
                out.add(cid)

    # Migration path for all pre-B.8 runs: audit contains CLASSIFIED for both queued
    # and NO_NEW/HISTORICAL outcomes. Never import ERROR / WAITING rows.
    for p in root.rglob("audit.jsonl"):
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            x=json.loads(line)
            if x.get("status")!="CLASSIFIED":
                continue
            cid=str(x.get("capture_identity") or "")
            if cid:
                out.add(cid)

    # Legacy queue fallback for very old runs whose audit may be incomplete.
    for p in root.rglob("classification_queue.jsonl"):
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                x=json.loads(line)
                cid=str(x.get("capture_identity") or "")
                if cid:
                    out.add(cid)
    return out

def classify_normalized_file(path:Path,cfg:dict[str,Any])->dict[str,Any]:
    cap=json.loads(path.read_text(encoding="utf-8"))
    raw=Path(str(cap.get("raw_copied_path") or ""))
    if not raw.exists(): raise RuntimeError(f"CAPTURE_RAW_FILE_MISSING {raw}")
    text=extract_text(raw,cap.get("media_type"),cfg)
    out=classify_capture(cap,text,cfg)
    out["text_sha256"]=hashlib.sha256(text.encode("utf-8",errors="replace")).hexdigest()
    out["text_char_count"]=len(text)
    return out

def source_entry(source_id:str,cfg:dict[str,Any])->dict[str,Any]:
    p=resolve(cfg["dependency_paths"]["v072a_source_registry"])
    reg=json.loads(p.read_text(encoding="utf-8"))
    for x in reg["entries"]:
        if x["source_id"]==source_id: return x
    raise RuntimeError(f"SOURCE_REGISTRATION_REQUIRED source_id={source_id}")

def transport_mode(source_id:str,cfg:dict[str,Any])->str:
    return str((cfg.get("transport_policy") or {}).get(source_id) or "DIRECT_HTTP")

def media_type_from_path(path:Path)->str:
    suf=path.suffix.lower()
    if suf in (".html",".htm"): return "text/html"
    if suf==".pdf": return "application/pdf"
    if suf in (".txt",".md"): return "text/plain"
    return "application/octet-stream"

def write_manual_capture_request_generic(
    stage:Path,rec:dict[str,Any],cfg:dict[str,Any],
    reason:str,http_status:int|None=None,error_text:str|None=None
)->Path:
    req={
        "schema_version":"OPTIMUS_MANUAL_CAPTURE_REQUEST_V0_7_2B_4",
        "reason":reason,
        "http_status":http_status,
        "error":error_text,
        "source_id":rec.get("source_id"),
        "lane":rec.get("lane"),
        "entity_name":rec.get("entity_name"),
        "symbol":rec.get("symbol"),
        "source_url":rec.get("source_url"),
        "document_title":rec.get("document_title"),
        "published_at":rec.get("published_at"),
        "event_time":rec.get("event_time"),
        "transport_mode":transport_mode(str(rec.get("source_id") or ""),cfg),
        "instructions":[
            "Open the exact source_url in a normal browser.",
            "For dynamic HTML pages, use Print -> Save as PDF after the official content is visibly rendered.",
            "For an official PDF source, download/save the original official PDF bytes.",
            "Do not edit the saved source file.",
            "Run the V0.7.2B manual-capture command using a single-record manifest and the saved raw file.",
        ],
        "event_time_backfill_forbidden":True,
        "classifier_result":None,
        "registry_mutation":False,
        "canonicalization_mutation":False,
        "theme_stage_mutation":False,
    }
    p=stage/"manual_capture_request.json"
    p.write_text(json.dumps(req,ensure_ascii=False,indent=2),encoding="utf-8")
    return p

def write_manual_capture_request(stage:Path,rec:dict[str,Any],error:HTTPError,cfg:dict[str,Any])->Path:
    # Backward-compatible wrapper retained for B.1 regression tests.
    return write_manual_capture_request_generic(
        stage,rec,cfg,
        reason="HTTP_403_FROM_REGISTERED_OFFICIAL_SOURCE",
        http_status=int(error.code),
        error_text=repr(error),
    )

def is_manual_network_fallback(source_id:str,exc:BaseException,cfg:dict[str,Any])->bool:
    mode=transport_mode(source_id,cfg)
    if mode!="MANUAL_BROWSER_FALLBACK_ON_NETWORK_OR_DYNAMIC_SHELL":
        return False
    return isinstance(exc,(ConnectionError,TimeoutError,URLError))

def dynamic_shell_reason(raw_path:Path,final_url:str,media_type:str|None,rec:dict[str,Any],cfg:dict[str,Any])->str|None:
    dc=cfg.get("dynamic_shell_detection") or {}
    if not dc.get("enabled"):
        return None
    host=(urlsplit(final_url).hostname or "").lower().rstrip(".")
    if host not in {str(x).lower().rstrip(".") for x in dc.get("hosts",[])}:
        return None
    mt=(media_type or "").lower()
    if "html" not in mt and raw_path.suffix.lower() not in (".html",".htm"):
        return None
    size=raw_path.stat().st_size
    try:
        visible=html_text(raw_path.read_bytes())
    except Exception:
        visible=""
    visible_chars=len(norm(visible))
    if size<=int(dc.get("html_max_bytes",12000)) and visible_chars<=int(dc.get("visible_text_max_chars",1200)):
        return f"DYNAMIC_HTML_SHELL bytes={size} visible_chars={visible_chars}"
    return None


def host_allowed(entry:dict[str,Any],url:str)->bool:
    host=(urlsplit(url).hostname or "").lower().rstrip(".")
    for allowed in entry["allowed_hosts"]:
        a=allowed.lower().rstrip(".")
        if host==a or (entry.get("allow_subdomains") and host.endswith("."+a)): return True
    return False

class FinalHostHandler(HTTPRedirectHandler):
    pass

def fetch_url(rec:dict[str,Any],cfg:dict[str,Any],stage:Path)->tuple[Path,str,str]:
    source_id=str(rec["source_id"])
    entry=source_entry(source_id,cfg)
    url=str(rec["source_url"])
    if not entry.get("external_inbox_allowed"):
        raise RuntimeError(f"SOURCE_NOT_ALLOWED_EXTERNAL_INBOX {source_id}")
    if not host_allowed(entry,url):
        raise RuntimeError(f"INITIAL_HOST_NOT_ALLOWED {url}")

    host=(urlsplit(url).hostname or "").lower()
    ua="PositionX-Optimus-Research/0.7.2B"
    if host.endswith("sec.gov"):
        email=os.environ.get(cfg["retrieval_contract"]["sec_contact_email_env"],"").strip()
        if not email:
            raise RuntimeError("SEC_CONTACT_EMAIL_REQUIRED set POSITIONX_SEC_CONTACT_EMAIL")
        ua=f"PositionX-Optimus-Research/0.7.2B {email}"

    req=Request(url,headers={"User-Agent":ua,"Accept":"text/html,application/pdf,text/plain,*/*;q=0.5"})
    opener=build_opener(FinalHostHandler())
    with opener.open(req,timeout=int(cfg["retrieval_contract"]["timeout_seconds"])) as r:
        final_url=r.geturl()
        if not host_allowed(entry,final_url):
            raise RuntimeError(f"REDIRECT_FINAL_HOST_NOT_ALLOWED {final_url}")
        maxb=int(cfg["retrieval_contract"]["max_bytes"])
        data=r.read(maxb+1)
        if len(data)>maxb: raise RuntimeError(f"DOWNLOAD_TOO_LARGE >{maxb}")
        ctype=(r.headers.get("Content-Type") or "").split(";")[0].strip().lower()

    ext=".bin"
    if "html" in ctype: ext=".html"
    elif "pdf" in ctype: ext=".pdf"
    elif "text" in ctype: ext=".txt"
    else:
        p=Path(urlsplit(final_url).path)
        if p.suffix.lower() in (".pdf",".html",".htm",".txt"): ext=p.suffix.lower()

    raw=stage/f"raw{ext}"
    raw.write_bytes(data)
    return raw,final_url,ctype

def write_classification_run(cfg:dict[str,Any],capture_id:str,queue:list[dict[str,Any]],audit:list[dict[str,Any]],
                             record_count:int,skipped:int=0,errors:int=0,manual_required:int=0)->dict[str,Any]:
    out_root=Path(cfg["output_root"])/now_cst().strftime("%Y%m%d")/capture_id
    out_root.mkdir(parents=True,exist_ok=False)
    qfile=out_root/"classification_queue.jsonl"
    afile=out_root/"audit.jsonl"
    pfile=out_root/"processed_capture_identities.jsonl"
    qfile.write_text("".join(json.dumps(x,ensure_ascii=False,sort_keys=True,default=str)+"\n" for x in queue),encoding="utf-8")
    afile.write_text("".join(json.dumps(x,ensure_ascii=False,sort_keys=True,default=str)+"\n" for x in audit),encoding="utf-8")
    processed=[
        {
            "schema_version":"OPTIMUS_CLASSIFIED_CAPTURE_IDENTITY_V0_7_2B_8",
            "capture_id":capture_id,
            "capture_identity":x["capture_identity"],
            "status":"CLASSIFIED_SUCCESS",
            "queue_state":x.get("queue_state"),
            "reclassification":bool(x.get("reclassification",False)),
        }
        for x in audit
        if x.get("status")=="CLASSIFIED" and x.get("capture_identity")
    ]
    pfile.write_text("".join(json.dumps(x,ensure_ascii=False,sort_keys=True,default=str)+"\n" for x in processed),encoding="utf-8")
    counts={}
    for x in queue: counts[x["queue_state"]]=counts.get(x["queue_state"],0)+1
    payload={
        "schema_version":"OPTIMUS_EXTERNAL_CAPTURE_RUN_V0_7_2B_1",
        "capture_id":capture_id,
        "record_count":record_count,
        "classified_count":sum(1 for x in audit if x.get("status")=="CLASSIFIED"),
        "queue_count":len(queue),
        "queue_state_counts":counts,
        "skipped_already_classified":skipped,
        "manual_capture_required_count":manual_required,
        "error_count":errors,
        "classification_queue":str(qfile),
        "classification_queue_sha256":sha256_file(qfile),
        "audit":str(afile),
        "audit_sha256":sha256_file(afile),
        "processed_capture_identities":str(pfile),
        "processed_capture_identities_sha256":sha256_file(pfile),
        "processed_capture_count":len(processed),
        "registry_mutation":False,
        "canonicalization_mutation":False,
        "theme_stage_mutation":False,
    }
    payload["manifest_payload_sha256"]=sha256_json(payload)
    (out_root/"manifest.json").write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    if errors:
        status="PARTIAL_OPTIMUS_EXTERNAL_CAPTURE_RUN_V0_7_2B"
    elif manual_required:
        status="WAITING_OPTIMUS_EXTERNAL_CAPTURE_MANUAL_CAPTURE_V0_7_2B_1"
    else:
        status="PASS_OPTIMUS_EXTERNAL_CAPTURE_RUN_V0_7_2B"
    return {
        "status":status,
        "output":str(out_root),
        **{k:payload[k] for k in (
            "record_count","classified_count","queue_count","queue_state_counts",
            "skipped_already_classified","manual_capture_required_count","error_count",
            "manifest_payload_sha256")},
        "mutation":False,
        "registry_mutation":False,
        "canonicalization_mutation":False,
        "theme_stage_mutation":False,
    }

def manual_capture(manifest_path:Path,raw_file:Path)->dict[str,Any]:
    cfg=load_config(); dependency_check(cfg)
    manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
    records=manifest["captures"] if isinstance(manifest,dict) else manifest
    if not isinstance(records,list) or len(records)!=1:
        raise RuntimeError("MANUAL_CAPTURE_REQUIRES_EXACTLY_ONE_MANIFEST_RECORD")
    rec=records[0]
    entry=source_entry(str(rec["source_id"]),cfg)
    if not entry.get("external_inbox_allowed"):
        raise RuntimeError(f"SOURCE_NOT_ALLOWED_EXTERNAL_INBOX {rec['source_id']}")
    if not host_allowed(entry,str(rec["source_url"])):
        raise RuntimeError(f"MANUAL_CAPTURE_SOURCE_HOST_NOT_ALLOWED {rec['source_url']}")
    if not raw_file.exists() or not raw_file.is_file():
        raise RuntimeError(f"MANUAL_RAW_FILE_MISSING {raw_file}")

    capture_id=now_cst().strftime("%Y%m%dT%H%M%S+0800")+"_manual"
    stage=Path(cfg["staging_root"])/capture_id/"001"
    stage.mkdir(parents=True,exist_ok=False)
    observed=now_cst().isoformat(timespec="seconds")
    meta={
        "source_id":rec["source_id"],
        "lane":rec["lane"],
        "entity_name":rec["entity_name"],
        "symbol":rec.get("symbol"),
        "entity_identifier":rec.get("entity_identifier"),
        "source_url":rec["source_url"],
        "document_title":rec["document_title"],
        "published_at":rec.get("published_at"),
        "event_time":rec.get("event_time"),
        "observed_at":observed,
        "capture_method":"V0_7_2B_4_MANUAL_BROWSER_RENDERED_OR_DOWNLOADED_OFFICIAL_SOURCE",
        "media_type":media_type_from_path(raw_file),
        "language":rec.get("language"),
        "raw_file_path":str(raw_file),
        "search_recall":rec.get("search_recall"),
    }
    meta_path=stage/"capture.input.json"
    meta_path.write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding="utf-8")
    ing=run_json(resolve(cfg["dependency_paths"]["v072a_script"]),"ingest","--input",str(meta_path))
    if not str(ing.get("status","")).startswith(("PASS_","ALREADY_INGESTED_")):
        raise RuntimeError(f"V072A_INGEST_NOT_PASS {ing}")
    norm_file=Path(ing["normalized_file"])
    cap=json.loads(norm_file.read_text(encoding="utf-8"))
    cid=cap["capture_identity"]
    prior=prior_classified_ids(Path(cfg["output_root"]))
    if cid in prior:
        audit=[{
            "record_index":1,
            "capture_identity":cid,
            "status":"SKIP_ALREADY_CLASSIFIED",
            "source_url":cap.get("source_url"),
            "capture_method":"MANUAL_BROWSER_RENDERED_OR_DOWNLOADED_OFFICIAL_SOURCE",
        }]
        return write_classification_run(cfg,capture_id,[],audit,1,skipped=1)

    cls=classify_normalized_file(norm_file,cfg)
    queue=[cls] if cls["queue_state"]!="NO_NEW_MATERIAL_EVIDENCE" else []
    audit=[{
        "record_index":1,
        "capture_identity":cls["capture_identity"],
        "status":"CLASSIFIED",
        "queue_state":cls["queue_state"],
        "signal_tags":cls["signal_tags"],
        "global_signal_tags":cls.get("global_signal_tags",[]),
        "cross_section_signal_tags":cls.get("cross_section_signal_tags",[]),
        "reason_code":cls.get("reason_code"),
        "temporal_state":cls["temporal_state"],
        "source_url":cls["source_url"],
        "capture_method":"MANUAL_BROWSER_RENDERED_OR_DOWNLOADED_OFFICIAL_SOURCE",
    }]
    return write_classification_run(cfg,capture_id,queue,audit,1)

def run_manifest(manifest_path:Path)->dict[str,Any]:
    cfg=load_config(); dependency_check(cfg)
    manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
    records=manifest["captures"] if isinstance(manifest,dict) else manifest
    if not isinstance(records,list) or not records: raise RuntimeError("MANIFEST_CAPTURES_REQUIRED")

    capture_id=now_cst().strftime("%Y%m%dT%H%M%S+0800")
    out_root=Path(cfg["output_root"])/now_cst().strftime("%Y%m%d")/capture_id
    out_root.mkdir(parents=True,exist_ok=False)
    prior=prior_classified_ids(Path(cfg["output_root"]))
    queue=[]; audit=[]; skipped=0; errors=0; manual_required=0

    for idx,rec in enumerate(records,1):
        stage=Path(cfg["staging_root"])/capture_id/f"{idx:03d}"
        stage.mkdir(parents=True,exist_ok=False)
        try:
            raw,final_url,ctype=fetch_url(rec,cfg,stage)
            shell_reason=dynamic_shell_reason(raw,final_url,ctype,rec,cfg)
            if shell_reason:
                manual_required+=1
                req_path=write_manual_capture_request_generic(
                    stage,rec,cfg,
                    reason="DYNAMIC_HTML_SHELL_FROM_REGISTERED_OFFICIAL_SOURCE",
                    error_text=shell_reason,
                )
                audit.append({
                    "record_index":idx,
                    "status":"WAITING_MANUAL_CAPTURE",
                    "reason":"DYNAMIC_HTML_SHELL",
                    "error":shell_reason,
                    "source_url":final_url,
                    "manual_capture_request":str(req_path),
                })
                continue
            observed=now_cst().isoformat(timespec="seconds")
            meta={
                "source_id":rec["source_id"],
                "lane":rec["lane"],
                "entity_name":rec["entity_name"],
                "symbol":rec.get("symbol"),
                "entity_identifier":rec.get("entity_identifier"),
                "source_url":final_url,
                "document_title":rec["document_title"],
                "published_at":rec.get("published_at"),
                "event_time":rec.get("event_time"),
                "observed_at":observed,
                "capture_method":"V0_7_2B_EXACT_REGISTERED_URL",
                "media_type":ctype,
                "language":rec.get("language"),
                "raw_file_path":str(raw),
                "search_recall":rec.get("search_recall"),
            }
            meta_path=stage/"capture.input.json"
            meta_path.write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding="utf-8")
            ing=run_json(resolve(cfg["dependency_paths"]["v072a_script"]),"ingest","--input",str(meta_path))
            if not str(ing.get("status","")).startswith(("PASS_","ALREADY_INGESTED_")):
                raise RuntimeError(f"V072A_INGEST_NOT_PASS {ing}")
            norm_file=Path(ing["normalized_file"])
            cap=json.loads(norm_file.read_text(encoding="utf-8"))
            cid=cap["capture_identity"]
            if cid in prior:
                skipped+=1
                audit.append({"record_index":idx,"capture_identity":cid,"status":"SKIP_ALREADY_CLASSIFIED","source_url":final_url})
                continue
            cls=classify_normalized_file(norm_file,cfg)
            if cls["queue_state"]!="NO_NEW_MATERIAL_EVIDENCE":
                queue.append(cls)
            audit.append({
                "record_index":idx,
                "capture_identity":cid,
                "status":"CLASSIFIED",
                "queue_state":cls["queue_state"],
                "signal_tags":cls["signal_tags"],
                "temporal_state":cls["temporal_state"],
                "source_url":final_url,
            })
            prior.add(cid)
        except HTTPError as e:
            if int(getattr(e,"code",0) or 0)==403 and transport_mode(str(rec.get("source_id") or ""),cfg)=="MANUAL_BROWSER_FALLBACK_ON_HTTP_403":
                manual_required+=1
                req_path=write_manual_capture_request(stage,rec,e,cfg)
                audit.append({
                    "record_index":idx,
                    "status":"WAITING_MANUAL_CAPTURE",
                    "http_status":403,
                    "error":repr(e),
                    "source_url":rec.get("source_url"),
                    "manual_capture_request":str(req_path),
                })
            else:
                errors+=1
                audit.append({"record_index":idx,"status":"ERROR","error":repr(e),"source_url":rec.get("source_url")})
        except (ConnectionError,TimeoutError,URLError) as e:
            if is_manual_network_fallback(str(rec.get("source_id") or ""),e,cfg):
                manual_required+=1
                req_path=write_manual_capture_request_generic(
                    stage,rec,cfg,
                    reason="NETWORK_TRANSPORT_FAILURE_FROM_REGISTERED_OFFICIAL_SOURCE",
                    error_text=repr(e),
                )
                audit.append({
                    "record_index":idx,
                    "status":"WAITING_MANUAL_CAPTURE",
                    "reason":"NETWORK_TRANSPORT_FAILURE",
                    "error":repr(e),
                    "source_url":rec.get("source_url"),
                    "manual_capture_request":str(req_path),
                })
            else:
                errors+=1
                audit.append({"record_index":idx,"status":"ERROR","error":repr(e),"source_url":rec.get("source_url")})
        except Exception as e:
            errors+=1
            audit.append({"record_index":idx,"status":"ERROR","error":repr(e),"source_url":rec.get("source_url")})

    # Remove the temporary output directory created before retrieval; the common writer
    # creates the immutable result directory exactly once.
    try:
        out_root.rmdir()
    except OSError:
        pass
    return write_classification_run(
        cfg,capture_id,queue,audit,len(records),
        skipped=skipped,errors=errors,manual_required=manual_required
    )

def reclassify_latest(cfg:dict[str,Any])->dict[str,Any]:
    dependency_check(cfg)
    inbox_cfg=json.loads(resolve(cfg["dependency_paths"]["v072a_config"]).read_text(encoding="utf-8"))
    root=Path(inbox_cfg["output_root"])
    files=sorted(root.rglob("capture.normalized.json"),key=lambda p:p.stat().st_mtime,reverse=True) if root.exists() else []
    if not files:
        raise RuntimeError("NO_EXTERNAL_INBOX_CAPTURE_TO_RECLASSIFY")
    norm_file=files[0]
    cls=classify_normalized_file(norm_file,cfg)
    capture_id=now_cst().strftime("%Y%m%dT%H%M%S+0800")+"_reclass"
    queue=[cls] if cls["queue_state"]!="NO_NEW_MATERIAL_EVIDENCE" else []
    audit=[{
        "record_index":1,
        "capture_identity":cls["capture_identity"],
        "status":"CLASSIFIED",
        "reclassification":True,
        "supersedes_prior_classifications_for_capture":True,
        "queue_state":cls["queue_state"],
        "reason_code":cls["reason_code"],
        "signal_tags":cls["signal_tags"],
        "global_signal_tags":cls.get("global_signal_tags",[]),
        "cross_section_signal_tags":cls.get("cross_section_signal_tags",[]),
        "temporal_state":cls["temporal_state"],
        "source_url":cls["source_url"],
    }]
    return write_classification_run(cfg,capture_id,queue,audit,1)

def status(cfg:dict[str,Any])->dict[str,Any]:
    root=Path(cfg["output_root"])
    ms=sorted(root.rglob("manifest.json"),key=lambda p:p.stat().st_mtime,reverse=True) if root.exists() else []
    if not ms:
        return {"status":"ARMED_OPTIMUS_EXTERNAL_CAPTURE_CLASSIFIER_V0_7_2B_NO_RUNS","mutation":False}
    m=json.loads(ms[0].read_text(encoding="utf-8"))
    return {
        "status":"PASS_OPTIMUS_EXTERNAL_CAPTURE_CLASSIFIER_V0_7_2B_STATUS",
        "latest_manifest":str(ms[0]),
        "capture_id":m["capture_id"],
        "record_count":m["record_count"],
        "queue_count":m["queue_count"],
        "queue_state_counts":m["queue_state_counts"],
        "manual_capture_required_count":m.get("manual_capture_required_count",0),
        "error_count":m["error_count"],
        "manifest_payload_sha256":m["manifest_payload_sha256"],
        "mutation":False,
    }

def queue(cfg:dict[str,Any])->dict[str,Any]:
    root=Path(cfg["output_root"])
    qs=sorted(root.rglob("classification_queue.jsonl"),key=lambda p:p.stat().st_mtime,reverse=True) if root.exists() else []
    rows=[]
    if qs:
        rows=[json.loads(x) for x in qs[0].read_text(encoding="utf-8").splitlines() if x.strip()]
    return {
        "status":"PASS_OPTIMUS_EXTERNAL_CLASSIFICATION_QUEUE_V0_7_2B",
        "queue_file":str(qs[0]) if qs else None,
        "queue_count":len(rows),
        "items":[{
            "queue_state":x["queue_state"],
            "reason_code":x["reason_code"],
            "source_id":x["source_id"],
            "entity_name":x["entity_name"],
            "source_url":x["source_url"],
            "effective_time":x["effective_time"],
            "temporal_state":x["temporal_state"],
            "signal_tags":x["signal_tags"],
            "global_signal_tags":x.get("global_signal_tags",[]),
            "cross_section_signal_tags":x.get("cross_section_signal_tags",[]),
            "context_scope_rule":x.get("context_scope_rule"),
        } for x in rows],
        "human_review_required":True,
        "mutation":False,
    }

def plan(cfg:dict[str,Any])->dict[str,Any]:
    return {
        "status":"PASS_OPTIMUS_EXTERNAL_CAPTURE_CLASSIFIER_V0_7_2B_PLAN",
        "classifier_id":cfg["classifier_id"],
        "case_id":cfg["case_id"],
        "case_start":cfg["case_start"],
        "retrieval_contract":cfg["retrieval_contract"],
        "temporal_contract":cfg["temporal_contract"],
        "classification_contract":cfg["classification_contract"],
        "safety":cfg["safety"],
        "next_phase":"CONTROLLED TESLA OFFICIAL PILOT; THEN V0.7.2C DISCOVERY QUEUE",
        "mutation":False,
    }

def main()->int:
    ap=argparse.ArgumentParser()
    sp=ap.add_subparsers(dest="command",required=True)
    sp.add_parser("check"); sp.add_parser("plan"); sp.add_parser("status"); sp.add_parser("queue"); sp.add_parser("reclassify-latest")
    r=sp.add_parser("run-manifest"); r.add_argument("--manifest",required=True)
    mc=sp.add_parser("manual-capture"); mc.add_argument("--manifest",required=True); mc.add_argument("--raw-file",required=True)
    c=sp.add_parser("classify-capture"); c.add_argument("--normalized-file",required=True)
    a=ap.parse_args()
    cfg=load_config()
    try:
        if a.command=="check": out=dependency_check(cfg)
        elif a.command=="plan": out=plan(cfg)
        elif a.command=="status": out=status(cfg)
        elif a.command=="queue": out=queue(cfg)
        elif a.command=="reclassify-latest": out=reclassify_latest(cfg)
        elif a.command=="run-manifest": out=run_manifest(Path(a.manifest))
        elif a.command=="manual-capture": out=manual_capture(Path(a.manifest),Path(a.raw_file))
        elif a.command=="classify-capture":
            dependency_check(cfg)
            out=classify_normalized_file(Path(a.normalized_file),cfg)
            out={"status":"PASS_OPTIMUS_EXTERNAL_CAPTURE_CLASSIFY_V0_7_2B",**out}
        else: raise RuntimeError("UNKNOWN_COMMAND")
        print(json.dumps(out,ensure_ascii=False,indent=2,default=str))
        return 0 if not str(out.get("status","")).startswith(("FAIL","PARTIAL")) else 2
    except Exception as e:
        print(json.dumps({
            "status":f"FAIL_OPTIMUS_EXTERNAL_CAPTURE_CLASSIFIER_V0_7_2B_{a.command.upper().replace('-','_')}",
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
