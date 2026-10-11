from __future__ import annotations
import argparse, hashlib, json, shutil, subprocess, sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

PROJECT_ROOT=Path(__file__).resolve().parents[1]
CONFIG=PROJECT_ROOT/"config"/"research"/"optimus_external_inbox_v072a.json"
CONFIG_SHA256="dc26aef5c3c68f50ecd934498ba67a79ccda84999a9776aef2df2c4fbe016a65"
SOURCE_REGISTRY_SHA256="e14163ef3023a82431451e21e157516a06c0ebaeae086069633f54a0bfcd65d2"

def sha256_file(path:Path)->str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""): h.update(b)
    return h.hexdigest()

def stable_json(x:Any)->str:
    return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(",",":"),default=str)

def sha256_text(s:str)->str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def load_config()->dict[str,Any]:
    if not CONFIG.exists():
        raise RuntimeError(f"CONFIG_MISSING {CONFIG}")
    got=sha256_file(CONFIG)
    if got!=CONFIG_SHA256:
        raise RuntimeError(f"CONFIG_SHA_MISMATCH actual={got} expected={CONFIG_SHA256}")
    return json.loads(CONFIG.read_text(encoding="utf-8"))

def load_registry(cfg:dict[str,Any])->dict[str,Any]:
    p=PROJECT_ROOT/cfg["source_registry_path"]
    if not p.exists():
        raise RuntimeError(f"SOURCE_REGISTRY_MISSING {p}")
    got=sha256_file(p)
    if got!=SOURCE_REGISTRY_SHA256:
        raise RuntimeError(f"SOURCE_REGISTRY_SHA_MISMATCH actual={got} expected={SOURCE_REGISTRY_SHA256}")
    reg=json.loads(p.read_text(encoding="utf-8"))
    ids=[x["source_id"] for x in reg["entries"]]
    if len(ids)!=len(set(ids)):
        raise RuntimeError("DUPLICATE_SOURCE_ID")
    return reg

def run_json(script:Path,command:str)->dict[str,Any]:
    p=subprocess.run([sys.executable,str(script),command],cwd=PROJECT_ROOT,capture_output=True,text=True)
    if p.returncode!=0:
        raise RuntimeError(f"COMMAND_FAILED {script.name} {command} rc={p.returncode} stderr={p.stderr}")
    try:
        return json.loads(p.stdout)
    except Exception as e:
        raise RuntimeError(f"NON_JSON_OUTPUT {script.name} {command} {e!r}")

def dependency_check(cfg:dict[str,Any])->dict[str,Any]:
    actual={}
    for key,rel in cfg["dependency_paths"].items():
        p=PROJECT_ROOT/rel
        if not p.exists(): raise RuntimeError(f"DEPENDENCY_MISSING {key} {p}")
        got=sha256_file(p); actual[key]=got
        expected=cfg["dependency_sha256"][key]
        if got!=expected:
            raise RuntimeError(f"DEPENDENCY_SHA_MISMATCH {key} actual={got} expected={expected}")
    v072=run_json(PROJECT_ROOT/cfg["dependency_paths"]["v072_script"],"check")
    if v072.get("status")!="PASS_OPTIMUS_GLOBAL_DISCOVERY_MONITOR_V0_7_2_CHECK":
        raise RuntimeError(f"V072_CONTRACT_NOT_PASS {v072}")
    reg=load_registry(cfg)
    return {
        "status":"PASS_OPTIMUS_EXTERNAL_INBOX_V0_7_2A_CHECK",
        "inbox_id":cfg["inbox_id"],
        "config_sha256":CONFIG_SHA256,
        "source_registry_sha256":SOURCE_REGISTRY_SHA256,
        "registered_source_count":len(reg["entries"]),
        "dependency_sha256":actual,
        "v072_status":v072["status"],
        "safety":cfg["safety"],
        "next":"VALIDATE_OR_INGEST_EXTERNAL_CAPTURE",
        "mutation":False,
    }

def canonical_url(url:str)->str:
    u=urlsplit(url.strip())
    if u.scheme.lower() not in ("http","https"):
        raise RuntimeError("SOURCE_URL_MUST_BE_HTTP_OR_HTTPS")
    host=(u.hostname or "").lower().rstrip(".")
    if not host:
        raise RuntimeError("SOURCE_URL_HOST_MISSING")
    port=f":{u.port}" if u.port and u.port not in (80,443) else ""
    netloc=host+port
    path=u.path or "/"
    # Sort query parameters only; never drop them because CIK/entity constraints may live there.
    query=urlencode(sorted(parse_qsl(u.query,keep_blank_values=True)))
    return urlunsplit((u.scheme.lower(),netloc,path,query,""))

def source_entry(reg:dict[str,Any],source_id:str)->dict[str,Any]:
    for x in reg["entries"]:
        if x["source_id"]==source_id:
            return x
    raise RuntimeError(f"SOURCE_REGISTRATION_REQUIRED source_id={source_id}")

def host_allowed(entry:dict[str,Any],url:str)->bool:
    host=(urlsplit(url).hostname or "").lower().rstrip(".")
    for allowed in entry["allowed_hosts"]:
        a=allowed.lower().rstrip(".")
        if host==a: return True
        if entry.get("allow_subdomains") and host.endswith("."+a): return True
    return False

def parse_aware_iso(value:str,field:str)->str:
    try:
        d=datetime.fromisoformat(value.replace("Z","+00:00"))
    except Exception:
        raise RuntimeError(f"INVALID_ISO_TIMESTAMP {field}={value!r}")
    if d.tzinfo is None or d.utcoffset() is None:
        raise RuntimeError(f"TIMESTAMP_MUST_BE_TIMEZONE_AWARE {field}={value!r}")
    return d.isoformat()

def optional_time(value:Any,field:str)->str|None:
    if value is None: return None
    s=str(value).strip()
    if not s: return None
    return parse_aware_iso(s,field)

def normalize_capture(input_obj:dict[str,Any],cfg:dict[str,Any],reg:dict[str,Any],check_raw:bool=True)->dict[str,Any]:
    contract=cfg["input_contract"]
    missing=[k for k in contract["required_fields"] if k not in input_obj or input_obj[k] in (None,"")]
    if missing: raise RuntimeError("MISSING_REQUIRED_FIELDS "+",".join(missing))
    bad=[k for k in contract["reserved_inference_fields_rejected"] if k in input_obj]
    if bad: raise RuntimeError("INFERENCE_FIELDS_NOT_ALLOWED_IN_V072A "+",".join(sorted(bad)))

    entry=source_entry(reg,str(input_obj["source_id"]))
    if not entry.get("enabled"): raise RuntimeError("SOURCE_DISABLED")
    if not entry.get("external_inbox_allowed"):
        raise RuntimeError(f"SOURCE_NOT_ALLOWED_IN_EXTERNAL_INBOX reason={entry.get('reason')}")

    lane=str(input_obj["lane"])
    if lane not in entry["lanes"]:
        raise RuntimeError(f"SOURCE_LANE_MISMATCH source_id={entry['source_id']} lane={lane}")

    url=canonical_url(str(input_obj["source_url"]))
    if not host_allowed(entry,url):
        raise RuntimeError(f"SOURCE_HOST_NOT_ALLOWED host={urlsplit(url).hostname} source_id={entry['source_id']}")
    required=entry.get("required_url_substrings_any") or []
    if required and not any(x in url for x in required):
        raise RuntimeError(f"SOURCE_URL_ENTITY_CONSTRAINT_FAILED source_id={entry['source_id']}")

    observed=parse_aware_iso(str(input_obj["observed_at"]),"observed_at")
    published=optional_time(input_obj.get("published_at"),"published_at")
    event_time=optional_time(input_obj.get("event_time"),"event_time")

    raw=Path(str(input_obj["raw_file_path"]))
    if check_raw and (not raw.exists() or not raw.is_file()):
        raise RuntimeError(f"RAW_FILE_MISSING {raw}")
    raw_sha=sha256_file(raw) if check_raw else str(input_obj.get("raw_sha256") or "")
    if check_raw and input_obj.get("raw_sha256") and str(input_obj["raw_sha256"]).lower()!=raw_sha:
        raise RuntimeError(f"RAW_SHA256_MISMATCH actual={raw_sha} expected={input_obj['raw_sha256']}")

    identity=sha256_text(entry["source_id"]+"\n"+url+"\n"+raw_sha)
    normalized={
        "schema_version":"OPTIMUS_NORMALIZED_EXTERNAL_CAPTURE_V0_7_2A",
        "capture_identity":identity,
        "source_registry_id":reg["registry_id"],
        "source_registry_sha256":SOURCE_REGISTRY_SHA256,
        "source_id":entry["source_id"],
        "source_name":entry["name"],
        "source_role":entry["source_role"],
        "authority_tier":entry["authority_tier"],
        "lane":lane,
        "entity_name":str(input_obj["entity_name"]),
        "symbol":input_obj.get("symbol"),
        "entity_identifier":input_obj.get("entity_identifier"),
        "source_url":url,
        "source_host":urlsplit(url).hostname,
        "document_title":str(input_obj["document_title"]),
        "published_at":published,
        "event_time":event_time,
        "observed_at":observed,
        "capture_method":str(input_obj.get("capture_method") or "MANUAL_OR_EXTERNAL_CAPTURE"),
        "media_type":input_obj.get("media_type"),
        "language":input_obj.get("language"),
        "raw_original_path":str(raw),
        "raw_sha256":raw_sha,
        "raw_size_bytes":raw.stat().st_size if check_raw else None,
        "search_recall":input_obj.get("search_recall"),
        "inference_state":"NOT_PERFORMED",
        "fact_inference":False,
        "registry_mutation":False,
        "canonicalization_mutation":False,
        "theme_stage_mutation":False,
    }
    # Critical PIT invariant.
    if input_obj.get("event_time") in (None,"") and normalized["event_time"] is not None:
        raise RuntimeError("EVENT_TIME_SILENT_BACKFILL_FORBIDDEN")
    return normalized

def destination_root(cfg:dict[str,Any],n:dict[str,Any])->Path:
    day=n["observed_at"][:10].replace("-","")
    return Path(cfg["output_root"])/day/n["capture_identity"]

def ingest(input_path:Path)->dict[str,Any]:
    cfg=load_config(); dependency_check(cfg); reg=load_registry(cfg)
    obj=json.loads(input_path.read_text(encoding="utf-8"))
    n=normalize_capture(obj,cfg,reg,check_raw=True)
    dst=destination_root(cfg,n)
    raw_src=Path(n["raw_original_path"])
    raw_name="raw"+raw_src.suffix.lower()
    norm_file=dst/"capture.normalized.json"
    raw_dst=dst/raw_name

    if dst.exists():
        if not norm_file.exists():
            raise RuntimeError(f"IMMUTABLE_CAPTURE_DIR_INCOMPLETE {dst}")
        old=json.loads(norm_file.read_text(encoding="utf-8"))
        if old.get("capture_identity")!=n["capture_identity"] or old.get("raw_sha256")!=n["raw_sha256"]:
            raise RuntimeError(f"IMMUTABLE_CAPTURE_CONFLICT {dst}")
        return {
            "status":"ALREADY_INGESTED_OPTIMUS_EXTERNAL_CAPTURE_V0_7_2A",
            "capture_identity":n["capture_identity"],
            "output":str(dst),
            "normalized_file":str(norm_file),
            "raw_file":str(raw_dst),
            "event_time":old.get("event_time"),
            "observed_at":old.get("observed_at"),
            "mutation":False,
            "registry_mutation":False,
            "canonicalization_mutation":False,
            "theme_stage_mutation":False,
        }

    dst.mkdir(parents=True,exist_ok=False)
    shutil.copy2(raw_src,raw_dst)
    if sha256_file(raw_dst)!=n["raw_sha256"]:
        raise RuntimeError("COPIED_RAW_SHA_MISMATCH")
    n["raw_copied_path"]=str(raw_dst)
    norm_file.write_text(json.dumps(n,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    return {
        "status":"PASS_OPTIMUS_EXTERNAL_CAPTURE_INGEST_V0_7_2A",
        "capture_identity":n["capture_identity"],
        "source_id":n["source_id"],
        "lane":n["lane"],
        "entity_name":n["entity_name"],
        "source_url":n["source_url"],
        "event_time":n["event_time"],
        "observed_at":n["observed_at"],
        "raw_sha256":n["raw_sha256"],
        "output":str(dst),
        "normalized_file":str(norm_file),
        "raw_file":str(raw_dst),
        "inference_state":"NOT_PERFORMED",
        "mutation":False,
        "registry_mutation":False,
        "canonicalization_mutation":False,
        "theme_stage_mutation":False,
    }

def validate(input_path:Path)->dict[str,Any]:
    cfg=load_config(); dependency_check(cfg); reg=load_registry(cfg)
    obj=json.loads(input_path.read_text(encoding="utf-8"))
    n=normalize_capture(obj,cfg,reg,check_raw=True)
    return {
        "status":"PASS_OPTIMUS_EXTERNAL_CAPTURE_VALIDATE_V0_7_2A",
        "capture_identity":n["capture_identity"],
        "source_id":n["source_id"],
        "lane":n["lane"],
        "entity_name":n["entity_name"],
        "source_host":n["source_host"],
        "event_time":n["event_time"],
        "observed_at":n["observed_at"],
        "raw_sha256":n["raw_sha256"],
        "would_output":str(destination_root(cfg,n)),
        "inference_state":"NOT_PERFORMED",
        "mutation":False,
    }

def plan()->dict[str,Any]:
    cfg=load_config()
    return {
        "status":"PASS_OPTIMUS_EXTERNAL_INBOX_V0_7_2A_PLAN",
        "inbox_id":cfg["inbox_id"],
        "mode":cfg["mode"],
        "input_contract":cfg["input_contract"],
        "storage_contract":cfg["storage_contract"],
        "safety":cfg["safety"],
        "next_phase":"V0.7.2B READ_ONLY EXTERNAL CAPTURE / CLASSIFICATION",
        "mutation":False,
    }

def sources()->dict[str,Any]:
    cfg=load_config(); reg=load_registry(cfg)
    return {
        "status":"PASS_OPTIMUS_EXTERNAL_SOURCE_REGISTRY_V0_7_2A",
        "registry_id":reg["registry_id"],
        "registry_sha256":SOURCE_REGISTRY_SHA256,
        "source_count":len(reg["entries"]),
        "sources":[{
            "source_id":x["source_id"],
            "name":x["name"],
            "authority_tier":x["authority_tier"],
            "source_role":x["source_role"],
            "lanes":x["lanes"],
            "allowed_hosts":x["allowed_hosts"],
            "external_inbox_allowed":x["external_inbox_allowed"],
        } for x in reg["entries"]],
        "extension_policy":reg["extension_policy"],
        "unknown_source_policy":reg["unknown_source_policy"],
        "mutation":False,
    }

def status()->dict[str,Any]:
    cfg=load_config()
    root=Path(cfg["output_root"])
    captures=sorted(root.rglob("capture.normalized.json")) if root.exists() else []
    by_lane={}
    for p in captures:
        try:
            x=json.loads(p.read_text(encoding="utf-8")); lane=x.get("lane","UNKNOWN")
            by_lane[lane]=by_lane.get(lane,0)+1
        except Exception:
            raise RuntimeError(f"INBOX_CORRUPT {p}")
    return {
        "status":"PASS_OPTIMUS_EXTERNAL_INBOX_V0_7_2A_STATUS",
        "output_root":str(root),
        "capture_count":len(captures),
        "lane_counts":by_lane,
        "registry_mutation":False,
        "canonicalization_mutation":False,
        "theme_stage_mutation":False,
    }

def main()->int:
    ap=argparse.ArgumentParser()
    sp=ap.add_subparsers(dest="command",required=True)
    sp.add_parser("check"); sp.add_parser("plan"); sp.add_parser("sources"); sp.add_parser("status")
    v=sp.add_parser("validate"); v.add_argument("--input",required=True)
    i=sp.add_parser("ingest"); i.add_argument("--input",required=True)
    a=ap.parse_args()
    try:
        if a.command=="check": out=dependency_check(load_config())
        elif a.command=="plan": out=plan()
        elif a.command=="sources": out=sources()
        elif a.command=="status": out=status()
        elif a.command=="validate": out=validate(Path(a.input))
        elif a.command=="ingest": out=ingest(Path(a.input))
        else: raise RuntimeError("UNKNOWN_COMMAND")
        print(json.dumps(out,ensure_ascii=False,indent=2,default=str))
        return 0
    except Exception as exc:
        print(json.dumps({
            "status":f"FAIL_OPTIMUS_EXTERNAL_INBOX_V0_7_2A_{a.command.upper()}",
            "error":repr(exc),
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
