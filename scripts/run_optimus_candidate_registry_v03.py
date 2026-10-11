from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

try:
    import duckdb
except ImportError as e:
    raise RuntimeError("duckdb is required for Optimus Candidate Registry V0.3") from e

CST = timezone(timedelta(hours=8))
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "config/research/optimus_candidate_registry_v0_3.json"
SYMBOL_RE = re.compile(r"^\d{6}\.(?:SH|SZ)$")

TABLE_META = "optimus_registry_meta_v03"
TABLE_EVIDENCE = "optimus_evidence_registry_v03"
TABLE_REVIEW = "optimus_evidence_review_v03"
TABLE_CANDIDATE = "optimus_candidate_registry_v03"
TABLE_HISTORY = "optimus_candidate_state_history_v03"
TABLE_DISCOVERY = "optimus_discovery_events_v03"
TABLE_FILE_INGEST = "optimus_evidence_file_ingest_v03"

ALLOWED_REVIEW = {"PROVISIONAL", "CANONICAL", "REJECTED"}
ALLOWED_F = {"SPECULATIVE", "POSSIBLE", "PROBABLE", "CONFIRMED", "REMOVED"}
ALLOWED_LIFECYCLE = {"DISCOVERY_ONLY", "ACTIVE_SEED", "ACTIVE_CANONICAL", "REMOVED"}


def now_cst() -> datetime:
    return datetime.now(CST)


def iso_now() -> str:
    return now_cst().isoformat()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def stable_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def stable_id(prefix: str, obj: Any, n: int = 24) -> str:
    return f"{prefix}_{sha256_bytes(stable_json(obj).encode('utf-8'))[:n]}"


def load_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_project_path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else PROJECT_ROOT / p


def normalize_date(x: str | None) -> str | None:
    if x is None or str(x).strip() == "":
        return None
    s = str(x).strip().replace("/", "-")
    if re.fullmatch(r"\d{8}", s):
        s = f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return pd.Timestamp(s).strftime("%Y-%m-%d")


def normalize_ts(x: str | None) -> str | None:
    if x is None or str(x).strip() == "":
        return None
    ts = pd.Timestamp(str(x))
    if ts.tzinfo is None:
        ts = ts.tz_localize(CST)
    else:
        ts = ts.tz_convert(CST)
    return ts.isoformat()


def normalize_seed_tesla_link(raw: str) -> int:
    """Map legacy seed labels conservatively. Never infer an explicit Optimus link.

    0 NONE, 1 ADJACENT, 2 UNNAMED/IMPLIED, 3 EXPLICIT_NAMED_RELATIONSHIP,
    4 EXPLICIT_NAMED_COMMERCIALIZATION.
    """
    s = str(raw or "").upper()
    if "NO_PUBLIC_TESLA" in s:
        return 0
    if "LEGACY_TESLA_AUTO_RELATION" in s:
        return 1
    if "UNNAMED" in s or "WORLD_KNOWN" in s or "HEAD_" in s:
        return 2
    return 0


def source_is_primary(source_type: str) -> bool:
    s = str(source_type or "").upper()
    return any(k in s for k in ("PRIMARY", "CNINFO", "TESLA_OFFICIAL", "SEC", "COMPANY_IR"))


def derive_f_status(
    *,
    max_level: int,
    max_tesla_link: int,
    has_primary: bool,
    hard_negative: bool,
    has_component_relevance: bool,
    rules: dict[str, Any],
) -> tuple[str, str]:
    """Deterministic F-state rule; returns (lifecycle, f_status)."""
    if hard_negative:
        return "REMOVED", "REMOVED"
    if not has_component_relevance:
        return "DISCOVERY_ONLY", "SPECULATIVE"
    if (
        max_level >= int(rules["confirmed_min_level"])
        and max_tesla_link >= int(rules["confirmed_min_tesla_link_level"])
        and has_primary
    ):
        return "ACTIVE_CANONICAL", "CONFIRMED"
    if (
        max_level >= int(rules["probable_min_level"])
        and max_tesla_link >= int(rules["probable_min_tesla_link_level"])
    ):
        return "ACTIVE_CANONICAL", "PROBABLE"
    if max_level >= int(rules["possible_min_level"]):
        return "ACTIVE_CANONICAL", "POSSIBLE"
    return "ACTIVE_CANONICAL", "SPECULATIVE"


KEYWORD_DOMAIN = {
    "滚柱丝杠": "planetary roller screws",
    "谐波减速器": "harmonic reducers",
    "无框力矩电机": "frameless torque motors",
    "六维力传感器": "6-axis force/torque sensing",
    "灵巧手": "dexterous hand",
    "关节模组": "joint modules",
    "机器人执行器": "actuators",
    "人形机器人": "humanoid robotics",
    "具身智能": "embodied intelligence",
    "Optimus": "Optimus",
    "特斯拉机器人": "Optimus/Tesla robotics",
}


def classify_discovery_title(title: str, query_keyword: str) -> dict[str, Any]:
    """Conservative title/query classifier. Output is always PROVISIONAL."""
    text = f"{title or ''} {query_keyword or ''}"
    level = 1
    etype = "DISCOVERY_MENTION"
    confidence = 0.45
    rules = [
        (9, "RECURRING_SHIPMENT_REVENUE", r"持续供货|持续交付|批量交付|收入|营收|销售收入"),
        (8, "MASS_PRODUCTION_ORDER", r"量产订单|批量订单|大批量订单"),
        (7, "PURCHASE_ORDER", r"采购订单|订单|中标|合同"),
        (6, "TOOLING_CAPACITY", r"专用产线|生产线|扩产|产能"),
        (5, "SAMPLE_VALIDATION", r"验证|送样|样品|测试"),
        (4, "SUPPLIER_NOMINATION", r"定点|指定供应商|供应商资格"),
        (3, "TESLA_EXPLICIT_LINK", r"Optimus|特斯拉.{0,6}(机器人|人形)"),
        (2, "ROBOT_CUSTOMER", r"机器人客户|客户导入|客户认证"),
    ]
    for lv, tp, pat in rules:
        if re.search(pat, text, flags=re.I):
            level, etype, confidence = lv, tp, min(0.92, 0.50 + lv * 0.045)
            break
    tesla_link = 3 if re.search(r"Optimus|特斯拉.{0,6}(机器人|人形)", text, flags=re.I) else 0
    domain = KEYWORD_DOMAIN.get(query_keyword, "UNKNOWN_PENDING_REVIEW")
    return {
        "evidence_level": level,
        "evidence_type": etype,
        "tesla_link_level": tesla_link,
        "component_domain": domain,
        "confidence": confidence,
        "review_state": "PROVISIONAL",
        "parser_version": "CNINFO_TITLE_RULE_V0_3",
    }


def init_tables(con: Any) -> None:
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE_META} (
            registry_id VARCHAR PRIMARY KEY,
            schema_version VARCHAR NOT NULL,
            seed_sha256 VARCHAR,
            bootstrapped_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ
        )
    """)
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE_EVIDENCE} (
            evidence_id VARCHAR PRIMARY KEY,
            symbol VARCHAR NOT NULL,
            company VARCHAR,
            component_domain VARCHAR,
            evidence_level INTEGER NOT NULL,
            evidence_type VARCHAR NOT NULL,
            tesla_link_level INTEGER NOT NULL,
            source_type VARCHAR NOT NULL,
            source_ref VARCHAR NOT NULL,
            source_sha256 VARCHAR,
            event_time TIMESTAMPTZ,
            observed_at TIMESTAMPTZ NOT NULL,
            initial_review_state VARCHAR NOT NULL,
            confidence DOUBLE,
            contradiction_kind VARCHAR,
            contradiction_severity VARCHAR,
            parser_version VARCHAR,
            interpretation VARCHAR,
            raw_payload_json VARCHAR NOT NULL,
            ingested_at TIMESTAMPTZ NOT NULL
        )
    """)
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE_REVIEW} (
            decision_id VARCHAR PRIMARY KEY,
            evidence_id VARCHAR NOT NULL,
            decision VARCHAR NOT NULL,
            reason VARCHAR NOT NULL,
            actor VARCHAR NOT NULL,
            decided_at TIMESTAMPTZ NOT NULL
        )
    """)
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE_CANDIDATE} (
            symbol VARCHAR PRIMARY KEY,
            company VARCHAR,
            component_domain VARCHAR,
            lifecycle_state VARCHAR NOT NULL,
            f_status VARCHAR NOT NULL,
            canonical BOOLEAN NOT NULL,
            needs_review BOOLEAN NOT NULL,
            freshness_state VARCHAR NOT NULL,
            origin VARCHAR NOT NULL,
            state_basis VARCHAR NOT NULL,
            max_evidence_level INTEGER NOT NULL,
            max_tesla_link_level INTEGER NOT NULL,
            positive_evidence_count INTEGER NOT NULL,
            provisional_evidence_count INTEGER NOT NULL,
            contradiction_count INTEGER NOT NULL,
            first_observed_at TIMESTAMPTZ,
            last_evidence_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL
        )
    """)
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE_HISTORY} (
            history_id VARCHAR PRIMARY KEY,
            symbol VARCHAR NOT NULL,
            effective_at TIMESTAMPTZ NOT NULL,
            prior_lifecycle_state VARCHAR,
            new_lifecycle_state VARCHAR NOT NULL,
            prior_f_status VARCHAR,
            new_f_status VARCHAR NOT NULL,
            canonical BOOLEAN NOT NULL,
            reason VARCHAR NOT NULL,
            evidence_ids_json VARCHAR NOT NULL,
            state_hash VARCHAR NOT NULL,
            created_at TIMESTAMPTZ NOT NULL
        )
    """)
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE_DISCOVERY} (
            discovery_id VARCHAR PRIMARY KEY,
            announcement_id VARCHAR,
            symbol VARCHAR,
            company VARCHAR,
            announcement_title VARCHAR,
            event_time TIMESTAMPTZ,
            observed_at TIMESTAMPTZ NOT NULL,
            query_keyword VARCHAR,
            pdf_url VARCHAR,
            payload_hash VARCHAR NOT NULL,
            raw_json VARCHAR NOT NULL,
            proposed_evidence_id VARCHAR,
            created_at TIMESTAMPTZ NOT NULL
        )
    """)
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE_FILE_INGEST} (
            file_sha256 VARCHAR PRIMARY KEY,
            source_path VARCHAR NOT NULL,
            archive_path VARCHAR NOT NULL,
            rows_seen INTEGER NOT NULL,
            rows_inserted INTEGER NOT NULL,
            ingested_at TIMESTAMPTZ NOT NULL
        )
    """)


def table_exists(con: Any, name: str) -> bool:
    return bool(con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [name]
    ).fetchone()[0])


def read_candidate(con: Any, symbol: str) -> dict[str, Any] | None:
    rows = con.execute(f"SELECT * FROM {TABLE_CANDIDATE} WHERE symbol = ?", [symbol]).fetchdf()
    if rows.empty:
        return None
    return rows.iloc[0].to_dict()


def insert_history(con: Any, prior: dict[str, Any] | None, current: dict[str, Any], reason: str, evidence_ids: list[str]) -> None:
    state_payload = {
        "symbol": current["symbol"],
        "lifecycle_state": current["lifecycle_state"],
        "f_status": current["f_status"],
        "canonical": bool(current["canonical"]),
        "state_basis": current["state_basis"],
        "evidence_ids": sorted(evidence_ids),
    }
    state_hash = sha256_bytes(stable_json(state_payload).encode("utf-8"))
    effective_at = iso_now()
    hist = {
        "symbol": current["symbol"],
        "effective_at": effective_at,
        "prior_lifecycle_state": None if prior is None else prior.get("lifecycle_state"),
        "new_lifecycle_state": current["lifecycle_state"],
        "prior_f_status": None if prior is None else prior.get("f_status"),
        "new_f_status": current["f_status"],
        "canonical": bool(current["canonical"]),
        "reason": reason,
        "evidence_ids": sorted(evidence_ids),
        "state_hash": state_hash,
    }
    history_id = stable_id("H", hist)
    con.execute(
        f"""INSERT INTO {TABLE_HISTORY}
        (history_id,symbol,effective_at,prior_lifecycle_state,new_lifecycle_state,prior_f_status,new_f_status,canonical,reason,evidence_ids_json,state_hash,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        [history_id, current["symbol"], effective_at,
         hist["prior_lifecycle_state"], hist["new_lifecycle_state"], hist["prior_f_status"], hist["new_f_status"],
         bool(current["canonical"]), reason, json.dumps(sorted(evidence_ids), ensure_ascii=False), state_hash, iso_now()]
    )


def upsert_candidate(con: Any, current: dict[str, Any], reason: str, evidence_ids: list[str]) -> bool:
    prior = read_candidate(con, current["symbol"])
    changed = prior is None or any(
        str(prior.get(k)) != str(current.get(k))
        for k in ("company", "component_domain", "lifecycle_state", "f_status", "canonical", "needs_review", "freshness_state", "state_basis", "max_evidence_level", "max_tesla_link_level", "positive_evidence_count", "provisional_evidence_count", "contradiction_count")
    )
    con.execute(f"DELETE FROM {TABLE_CANDIDATE} WHERE symbol = ?", [current["symbol"]])
    con.execute(
        f"""INSERT INTO {TABLE_CANDIDATE}
        (symbol,company,component_domain,lifecycle_state,f_status,canonical,needs_review,freshness_state,origin,state_basis,max_evidence_level,max_tesla_link_level,positive_evidence_count,provisional_evidence_count,contradiction_count,first_observed_at,last_evidence_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [current.get("symbol"), current.get("company"), current.get("component_domain"), current.get("lifecycle_state"), current.get("f_status"),
         bool(current.get("canonical")), bool(current.get("needs_review")), current.get("freshness_state"), current.get("origin"), current.get("state_basis"),
         int(current.get("max_evidence_level",0)), int(current.get("max_tesla_link_level",0)), int(current.get("positive_evidence_count",0)),
         int(current.get("provisional_evidence_count",0)), int(current.get("contradiction_count",0)), current.get("first_observed_at"), current.get("last_evidence_at"), iso_now()]
    )
    if changed:
        insert_history(con, prior, current, reason, evidence_ids)
    return changed


def seed_bootstrap(con: Any, cfg: dict[str, Any]) -> dict[str, Any]:
    seed_path = resolve_project_path(cfg["seed_universe"])
    if not seed_path.exists():
        raise RuntimeError(f"seed universe missing: {seed_path}")
    seed_sha = sha256_file(seed_path)
    already = con.execute(f"SELECT count(*) FROM {TABLE_META} WHERE registry_id = ?", [cfg["registry_id"]]).fetchone()[0]
    if already:
        return {"status": "ALREADY_BOOTSTRAPPED", "seed_sha256": seed_sha, "inserted": 0}
    df = pd.read_csv(seed_path, dtype=str).fillna("")
    required = {"symbol","company","f_status","component_domain","tesla_link_strength","source_date","source_type","source_title","evidence_note"}
    missing = sorted(required - set(df.columns))
    if missing:
        raise RuntimeError(f"seed schema missing: {missing}")
    if df["symbol"].duplicated().any():
        raise RuntimeError("seed duplicate symbols")
    con.execute("BEGIN TRANSACTION")
    try:
        inserted = 0
        for r in df.to_dict(orient="records"):
            symbol = r["symbol"]
            if not SYMBOL_RE.match(symbol):
                raise RuntimeError(f"invalid seed symbol: {symbol}")
            f_status = r["f_status"] if r["f_status"] in ALLOWED_F else "SPECULATIVE"
            current = {
                "symbol": symbol,
                "company": r["company"],
                "component_domain": r["component_domain"],
                "lifecycle_state": "ACTIVE_SEED",
                "f_status": f_status,
                "canonical": False,
                "needs_review": True,
                "freshness_state": "SEED_NEEDS_CANONICAL_EVIDENCE",
                "origin": "SEED_V0_2",
                "state_basis": "SEED_V0_2_BOOTSTRAP_UNVERIFIED",
                "max_evidence_level": 0,
                "max_tesla_link_level": normalize_seed_tesla_link(r["tesla_link_strength"]),
                "positive_evidence_count": 0,
                "provisional_evidence_count": 0,
                "contradiction_count": 0,
                "first_observed_at": normalize_ts(r.get("source_date")) if r.get("source_date") else iso_now(),
                "last_evidence_at": normalize_ts(r.get("source_date")) if r.get("source_date") else None,
            }
            upsert_candidate(con, current, "BOOTSTRAP_SEED_V0_2", [])
            inserted += 1
        con.execute(
            f"INSERT INTO {TABLE_META}(registry_id,schema_version,seed_sha256,bootstrapped_at,updated_at) VALUES (?,?,?,?,?)",
            [cfg["registry_id"], "0.3", seed_sha, iso_now(), iso_now()]
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return {"status": "BOOTSTRAPPED", "seed_sha256": seed_sha, "inserted": inserted}


def validate_evidence_row(row: dict[str, Any], allow_canonical_input: bool = False) -> dict[str, Any]:
    symbol = str(row.get("symbol") or "").strip().upper()
    if not SYMBOL_RE.match(symbol):
        raise ValueError(f"invalid symbol: {symbol}")
    level = int(row.get("evidence_level") or 0)
    if not 1 <= level <= 9:
        raise ValueError(f"evidence_level must be 1..9: {level}")
    tesla = int(row.get("tesla_link_level") or 0)
    if not 0 <= tesla <= 4:
        raise ValueError(f"tesla_link_level must be 0..4: {tesla}")
    review = str(row.get("review_state") or row.get("initial_review_state") or "PROVISIONAL").upper()
    if review not in ALLOWED_REVIEW:
        raise ValueError(f"invalid review_state: {review}")
    if review == "CANONICAL" and not allow_canonical_input:
        raise ValueError("canonical input requires --allow-canonical-input")
    source_ref = str(row.get("source_ref") or "").strip()
    source_type = str(row.get("source_type") or "").strip()
    if not source_ref or not source_type:
        raise ValueError("source_ref and source_type are required")
    observed_at = normalize_ts(row.get("observed_at")) or iso_now()
    event_time = normalize_ts(row.get("event_time"))
    normalized = {
        "symbol": symbol,
        "company": str(row.get("company") or "").strip(),
        "component_domain": str(row.get("component_domain") or "UNKNOWN_PENDING_REVIEW").strip(),
        "evidence_level": level,
        "evidence_type": str(row.get("evidence_type") or "UNCLASSIFIED").strip().upper(),
        "tesla_link_level": tesla,
        "source_type": source_type,
        "source_ref": source_ref,
        "source_sha256": str(row.get("source_sha256") or "").strip() or None,
        "event_time": event_time,
        "observed_at": observed_at,
        "initial_review_state": review,
        "confidence": float(row.get("confidence") or 0.5),
        "contradiction_kind": str(row.get("contradiction_kind") or "").strip().upper() or None,
        "contradiction_severity": str(row.get("contradiction_severity") or "").strip().upper() or None,
        "parser_version": str(row.get("parser_version") or "EXTERNAL_STRUCTURED_V0_3").strip(),
        "interpretation": str(row.get("interpretation") or "").strip(),
    }
    payload_for_id = dict(normalized)
    payload_for_id.pop("initial_review_state", None)
    evidence_id = str(row.get("evidence_id") or "").strip() or stable_id("E", payload_for_id)
    normalized["evidence_id"] = evidence_id
    normalized["raw_payload_json"] = stable_json(row)
    return normalized


def insert_evidence(con: Any, e: dict[str, Any]) -> bool:
    exists = con.execute(f"SELECT count(*) FROM {TABLE_EVIDENCE} WHERE evidence_id = ?", [e["evidence_id"]]).fetchone()[0]
    if exists:
        return False
    con.execute(
        f"""INSERT INTO {TABLE_EVIDENCE}
        (evidence_id,symbol,company,component_domain,evidence_level,evidence_type,tesla_link_level,source_type,source_ref,source_sha256,event_time,observed_at,initial_review_state,confidence,contradiction_kind,contradiction_severity,parser_version,interpretation,raw_payload_json,ingested_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [e["evidence_id"],e["symbol"],e["company"],e["component_domain"],e["evidence_level"],e["evidence_type"],e["tesla_link_level"],e["source_type"],e["source_ref"],e["source_sha256"],e["event_time"],e["observed_at"],e["initial_review_state"],e["confidence"],e["contradiction_kind"],e["contradiction_severity"],e["parser_version"],e["interpretation"],e["raw_payload_json"],iso_now()]
    )
    return True


def ingest_jsonl(con: Any, cfg: dict[str, Any], path: Path, allow_canonical_input: bool = False) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(f"evidence file missing: {path}")
    file_sha = sha256_file(path)
    old = con.execute(f"SELECT count(*) FROM {TABLE_FILE_INGEST} WHERE file_sha256 = ?", [file_sha]).fetchone()[0]
    if old:
        return {"status":"ALREADY_INGESTED","file_sha256":file_sha,"rows_seen":0,"rows_inserted":0}
    archive_root = Path(cfg["evidence_archive_root"])
    stamp = now_cst().strftime("%Y%m%d")
    archive_dir = archive_root / stamp
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive = archive_dir / f"{file_sha[:16]}_{path.name}"
    if archive.exists() and sha256_file(archive) != file_sha:
        raise RuntimeError(f"archive hash conflict: {archive}")
    if not archive.exists():
        shutil.copy2(path, archive)
    rows_seen = 0
    inserted = 0
    con.execute("BEGIN TRANSACTION")
    try:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            rows_seen += 1
            try:
                raw = json.loads(line)
                e = validate_evidence_row(raw, allow_canonical_input=allow_canonical_input)
                inserted += int(insert_evidence(con, e))
            except Exception as exc:
                raise RuntimeError(f"{path}:{lineno}: {exc}") from exc
        con.execute(
            f"INSERT INTO {TABLE_FILE_INGEST}(file_sha256,source_path,archive_path,rows_seen,rows_inserted,ingested_at) VALUES (?,?,?,?,?,?)",
            [file_sha,str(path),str(archive),rows_seen,inserted,iso_now()]
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return {"status":"INGESTED","file_sha256":file_sha,"archive":str(archive),"rows_seen":rows_seen,"rows_inserted":inserted}


def add_review_decision(con: Any, evidence_id: str, decision: str, reason: str, actor: str) -> dict[str, Any]:
    decision = decision.upper()
    if decision not in {"CANONICAL","REJECTED"}:
        raise ValueError("decision must be CANONICAL or REJECTED")
    if not reason.strip():
        raise ValueError("reason is required")
    exists = con.execute(f"SELECT count(*) FROM {TABLE_EVIDENCE} WHERE evidence_id = ?", [evidence_id]).fetchone()[0]
    if not exists:
        raise ValueError(f"unknown evidence_id: {evidence_id}")
    obj = {"evidence_id":evidence_id,"decision":decision,"reason":reason.strip(),"actor":actor,"decided_at":iso_now()}
    decision_id = stable_id("R", obj)
    con.execute(
        f"INSERT INTO {TABLE_REVIEW}(decision_id,evidence_id,decision,reason,actor,decided_at) VALUES (?,?,?,?,?,?)",
        [decision_id,evidence_id,decision,reason.strip(),actor,obj["decided_at"]]
    )
    return {"decision_id":decision_id,**obj}


def latest_effective_evidence(con: Any, symbol: str | None = None) -> pd.DataFrame:
    where = "WHERE e.symbol = ?" if symbol else ""
    params = [symbol] if symbol else []
    return con.execute(f"""
        WITH latest_review AS (
            SELECT evidence_id, decision,
                   row_number() OVER (PARTITION BY evidence_id ORDER BY decided_at DESC, decision_id DESC) AS rn
            FROM {TABLE_REVIEW}
        )
        SELECT e.*, COALESCE(r.decision, e.initial_review_state) AS effective_review_state
        FROM {TABLE_EVIDENCE} e
        LEFT JOIN latest_review r
          ON r.evidence_id = e.evidence_id AND r.rn = 1
        {where}
        ORDER BY e.symbol, e.observed_at, e.evidence_id
    """, params).fetchdf()

def reconcile_symbol(con: Any, cfg: dict[str, Any], symbol: str) -> dict[str, Any]:
    prior = read_candidate(con, symbol)
    ev = latest_effective_evidence(con, symbol)
    if ev.empty:
        return {"symbol":symbol,"changed":False,"status":"NO_EVIDENCE"}
    canon = ev[ev["effective_review_state"] == "CANONICAL"].copy()
    provisional = ev[ev["effective_review_state"] == "PROVISIONAL"].copy()
    rejected = ev[ev["effective_review_state"] == "REJECTED"].copy()
    evidence_ids = canon["evidence_id"].astype(str).tolist()
    company = ""
    for frame in (canon, provisional, ev):
        vals = [str(x) for x in frame.get("company", pd.Series(dtype=str)).tolist() if str(x).strip()]
        if vals:
            company = vals[-1]
            break
    domain = "UNKNOWN_PENDING_REVIEW"
    for frame in (canon, provisional, ev):
        vals = [str(x) for x in frame.get("component_domain", pd.Series(dtype=str)).tolist() if str(x).strip() and str(x) != "UNKNOWN_PENDING_REVIEW"]
        if vals:
            domain = vals[-1]
            break
    if canon.empty:
        if prior and prior.get("lifecycle_state") == "ACTIVE_SEED":
            current = dict(prior)
            current["needs_review"] = not provisional.empty or True
            current["provisional_evidence_count"] = int(len(provisional))
            current["last_evidence_at"] = None if ev.empty else str(ev["observed_at"].max())
            changed = upsert_candidate(con, current, "SEED_RECONCILE_NO_CANONICAL_EVIDENCE", [])
            return {"symbol":symbol,"changed":changed,"lifecycle_state":current["lifecycle_state"],"f_status":current["f_status"]}
        current = {
            "symbol":symbol,"company":company,"component_domain":domain,
            "lifecycle_state":"DISCOVERY_ONLY","f_status":"SPECULATIVE","canonical":False,
            "needs_review":True,"freshness_state":"PROVISIONAL_ONLY","origin":"EVIDENCE_DISCOVERY",
            "state_basis":"PROVISIONAL_EVIDENCE_ONLY","max_evidence_level":0,"max_tesla_link_level":0,
            "positive_evidence_count":0,"provisional_evidence_count":int(len(provisional)),"contradiction_count":0,
            "first_observed_at":str(ev["observed_at"].min()),"last_evidence_at":str(ev["observed_at"].max())
        }
        changed = upsert_candidate(con, current, "PROVISIONAL_DISCOVERY_ONLY", [])
        return {"symbol":symbol,"changed":changed,"lifecycle_state":current["lifecycle_state"],"f_status":current["f_status"]}

    positive = canon[canon["contradiction_kind"].isna() | (canon["contradiction_kind"].astype(str).str.strip() == "")].copy()
    neg = canon[~(canon["contradiction_kind"].isna() | (canon["contradiction_kind"].astype(str).str.strip() == ""))].copy()
    hard_sev = set(str(x).upper() for x in cfg["rules"].get("hard_removal_severities", ["HARD"]))
    hard_negative = any(str(x).upper() in hard_sev for x in neg.get("contradiction_severity", pd.Series(dtype=str)).fillna("").tolist())
    max_level = int(pd.to_numeric(positive.get("evidence_level", pd.Series(dtype=float)), errors="coerce").fillna(0).max()) if not positive.empty else 0
    max_tesla = int(pd.to_numeric(positive.get("tesla_link_level", pd.Series(dtype=float)), errors="coerce").fillna(0).max()) if not positive.empty else 0
    has_primary = any(source_is_primary(x) for x in positive.get("source_type", pd.Series(dtype=str)).tolist())
    has_component = any(
        str(x).strip() and str(x) != "UNKNOWN_PENDING_REVIEW"
        for x in positive.get("component_domain", pd.Series(dtype=str)).tolist()
    )
    lifecycle, f_status = derive_f_status(
        max_level=max_level,max_tesla_link=max_tesla,has_primary=has_primary,hard_negative=hard_negative,
        has_component_relevance=has_component,rules=cfg["rules"]
    )
    last_canon = pd.to_datetime(canon["observed_at"], errors="coerce", utc=True).max()
    stale_days = int(cfg.get("stale_review_days",180))
    freshness = "CANONICAL_CURRENT"
    if pd.notna(last_canon):
        age = (pd.Timestamp.now(tz="UTC") - last_canon).days
        if age > stale_days:
            freshness = "STALE_REVIEW"
    current = {
        "symbol":symbol,"company":company,"component_domain":domain,"lifecycle_state":lifecycle,"f_status":f_status,
        "canonical":True,"needs_review":not provisional.empty,"freshness_state":freshness,
        "origin":"CANONICAL_EVIDENCE","state_basis":"DETERMINISTIC_RULE_V0_3",
        "max_evidence_level":max_level,"max_tesla_link_level":max_tesla,"positive_evidence_count":int(len(positive)),
        "provisional_evidence_count":int(len(provisional)),"contradiction_count":int(len(neg)),
        "first_observed_at":str(canon["observed_at"].min()),"last_evidence_at":str(canon["observed_at"].max())
    }
    changed = upsert_candidate(con, current, "CANONICAL_EVIDENCE_RECONCILE", evidence_ids)
    return {"symbol":symbol,"changed":changed,"lifecycle_state":lifecycle,"f_status":f_status,"canonical_evidence":len(canon),"provisional_evidence":len(provisional),"rejected_evidence":len(rejected)}


def reconcile_all(con: Any, cfg: dict[str, Any]) -> dict[str, Any]:
    syms = set(x[0] for x in con.execute(f"SELECT symbol FROM {TABLE_CANDIDATE}").fetchall())
    syms.update(x[0] for x in con.execute(f"SELECT DISTINCT symbol FROM {TABLE_EVIDENCE}").fetchall())
    results = []
    con.execute("BEGIN TRANSACTION")
    try:
        for s in sorted(syms):
            results.append(reconcile_symbol(con,cfg,s))
        con.execute(f"UPDATE {TABLE_META} SET updated_at = ? WHERE registry_id = ?", [iso_now(),cfg["registry_id"]])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return {"symbols":len(results),"changed":sum(int(r.get("changed",False)) for r in results),"results":results}


def cninfo_post(cfg: dict[str, Any], se_date: str, page_num: int, searchkey: str) -> dict[str, Any]:
    ccfg = cfg["cninfo"]
    body = urllib.parse.urlencode({
        "pageNum": page_num,
        "pageSize": int(ccfg.get("page_size",30)),
        "column":"","tabName":"fulltext","plate":"","stock":"","searchkey":searchkey,
        "secid":"","category":"","trade":"","seDate":se_date,"sortName":"","sortType":"","isHLtitle":"true",
    }).encode()
    req = urllib.request.Request(
        ccfg["api"], data=body, method="POST",
        headers={
            "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Content-Type":"application/x-www-form-urlencoded; charset=UTF-8",
            "Referer":"http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
            "X-Requested-With":"XMLHttpRequest",
        }
    )
    last = None
    for i in range(int(ccfg.get("tries",4))):
        try:
            with urllib.request.urlopen(req, timeout=int(ccfg.get("timeout_seconds",30))) as r:
                raw = r.read()
            return json.loads(raw.decode("utf-8","replace"))
        except Exception as exc:
            last = exc
            time.sleep(1.2*(i+1))
    raise RuntimeError(f"cninfo failed date={se_date} page={page_num} keyword={searchkey}: {last!r}")


def symbol_from_cninfo(code: str) -> str | None:
    c = str(code or "").strip()
    if not re.fullmatch(r"\d{6}", c):
        return None
    if c.startswith(("0","2","3")):
        return c + ".SZ"
    if c.startswith(("6","9")):
        return c + ".SH"
    return None


def discover_cninfo(con: Any, cfg: dict[str, Any], start_date: str, end_date: str) -> dict[str, Any]:
    start = pd.Timestamp(start_date).date()
    end = pd.Timestamp(end_date).date()
    observed = iso_now()
    raw_root = Path(cfg["output_root"]) / "cninfo_discovery_raw" / end.strftime("%Y%m%d") / now_cst().strftime("%Y%m%dT%H%M%S+0800")
    raw_root.mkdir(parents=True, exist_ok=False)
    inserted_events = 0
    inserted_evidence = 0
    errors: list[str] = []
    total_rows = 0
    valid_a_share_rows = 0
    dropped_invalid_or_out_of_scope_symbol = 0
    duplicate_events = 0
    new_symbol_hits = 0
    existing_symbol_hits = 0
    invalid_symbol_examples: list[dict[str, Any]] = []
    by_keyword: dict[str, dict[str, int]] = {}
    con.execute("BEGIN TRANSACTION")
    try:
        d = start
        while d <= end:
            ds = d.strftime("%Y-%m-%d")
            for kw in cfg["cninfo"]["keywords"]:
                kstats = by_keyword.setdefault(kw, {"rows_seen": 0, "valid_a_share_rows": 0, "inserted_events": 0})
                try:
                    first = cninfo_post(cfg, ds, 1, kw)
                    total_pages = int(first.get("totalpages") or 0)
                    pages = [first]
                    for pageno in range(2,total_pages+1):
                        time.sleep(float(cfg["cninfo"].get("delay_seconds",0.5)))
                        pages.append(cninfo_post(cfg,ds,pageno,kw))
                    raw_path = raw_root / f"{d.strftime('%Y%m%d')}_{hashlib.sha1(kw.encode('utf-8')).hexdigest()[:8]}.json"
                    raw_path.write_text(json.dumps(pages,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
                    for payload in pages:
                        for row in payload.get("announcements") or []:
                            total_rows += 1
                            kstats["rows_seen"] += 1
                            raw_code = str(row.get("secCode") or "").strip()
                            symbol = symbol_from_cninfo(raw_code)
                            title = re.sub(r"</?em>","",str(row.get("announcementTitle") or ""))
                            if not symbol:
                                dropped_invalid_or_out_of_scope_symbol += 1
                                if len(invalid_symbol_examples) < 10:
                                    invalid_symbol_examples.append({
                                        "secCode": raw_code or None,
                                        "secName": str(row.get("secName") or ""),
                                        "announcementTitle": title,
                                        "query_keyword": kw,
                                        "reason": "NOT_SH_SZ_A_SHARE_SYMBOL_V0_3_1",
                                    })
                                continue
                            valid_a_share_rows += 1
                            kstats["valid_a_share_rows"] += 1
                            ann_id = str(row.get("announcementId") or "")
                            adjunct = str(row.get("adjunctUrl") or "")
                            pdf_url = cfg["cninfo"].get("static_root","") + adjunct
                            et = pd.to_datetime(pd.to_numeric(row.get("announcementTime"),errors="coerce"),unit="ms",utc=True,errors="coerce")
                            event_time = None if pd.isna(et) else et.tz_convert(CST).isoformat()
                            raw_json = stable_json(row)
                            payload_hash = sha256_bytes(raw_json.encode("utf-8"))
                            disc_obj = {"announcement_id":ann_id,"symbol":symbol,"payload_hash":payload_hash}
                            discovery_id = stable_id("D",disc_obj)
                            exists = con.execute(f"SELECT count(*) FROM {TABLE_DISCOVERY} WHERE discovery_id = ?",[discovery_id]).fetchone()[0]
                            if exists:
                                duplicate_events += 1
                                continue
                            candidate_exists = bool(con.execute(f"SELECT count(*) FROM {TABLE_CANDIDATE} WHERE symbol = ?", [symbol]).fetchone()[0])
                            if candidate_exists:
                                existing_symbol_hits += 1
                            else:
                                new_symbol_hits += 1
                            proposal = classify_discovery_title(title,kw)
                            evidence_raw = {
                                "symbol":symbol,
                                "company":str(row.get("secName") or ""),
                                **proposal,
                                "source_type":"CNINFO_ANNOUNCEMENT_DISCOVERY",
                                "source_ref":pdf_url or f"CNINFO:{ann_id}",
                                "source_sha256":None,
                                "event_time":event_time,
                                "observed_at":observed,
                                "interpretation":f"CNINFO discovery hit keyword={kw}; title={title}",
                            }
                            e = validate_evidence_row(evidence_raw,allow_canonical_input=False)
                            if insert_evidence(con,e):
                                inserted_evidence += 1
                            con.execute(
                                f"""INSERT INTO {TABLE_DISCOVERY}
                                (discovery_id,announcement_id,symbol,company,announcement_title,event_time,observed_at,query_keyword,pdf_url,payload_hash,raw_json,proposed_evidence_id,created_at)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                [discovery_id,ann_id,symbol,str(row.get("secName") or ""),title,event_time,observed,kw,pdf_url,payload_hash,raw_json,e["evidence_id"],iso_now()]
                            )
                            inserted_events += 1
                            kstats["inserted_events"] += 1
                except Exception as exc:
                    errors.append(f"{ds} {kw}: {exc!r}")
                time.sleep(float(cfg["cninfo"].get("delay_seconds",0.5)))
            d += timedelta(days=1)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return {
        "status":"SUCCESS" if not errors else "PARTIAL",
        "raw_root":str(raw_root),
        "rows_seen":total_rows,
        "valid_a_share_rows":valid_a_share_rows,
        "dropped_invalid_or_out_of_scope_symbol":dropped_invalid_or_out_of_scope_symbol,
        "duplicate_events":duplicate_events,
        "new_symbol_hits":new_symbol_hits,
        "existing_symbol_hits":existing_symbol_hits,
        "inserted_events":inserted_events,
        "inserted_provisional_evidence":inserted_evidence,
        "invalid_symbol_examples":invalid_symbol_examples,
        "by_keyword":by_keyword,
        "errors":errors,
    }

def ingest_inbox(con: Any, cfg: dict[str, Any]) -> dict[str, Any]:
    root = Path(cfg["evidence_inbox"])
    root.mkdir(parents=True, exist_ok=True)
    results=[]
    for p in sorted(root.glob("*.jsonl")):
        results.append(ingest_jsonl(con,cfg,p,allow_canonical_input=False))
    return {"files":len(results),"results":results}


def registry_rows(con: Any) -> pd.DataFrame:
    return con.execute(f"SELECT * FROM {TABLE_CANDIDATE} ORDER BY lifecycle_state, f_status, symbol").fetchdf()


def registry_digest(df: pd.DataFrame) -> str:
    if df.empty:
        return sha256_bytes(b"[]")
    cols = [
        "symbol","company","component_domain","lifecycle_state","f_status","canonical","needs_review","freshness_state",
        "origin","state_basis","max_evidence_level","max_tesla_link_level","positive_evidence_count","provisional_evidence_count","contradiction_count"
    ]
    records=[]
    for _,r in df[cols].sort_values("symbol").iterrows():
        records.append({k:(None if pd.isna(r[k]) else r[k]) for k in cols})
    return sha256_bytes(stable_json(records).encode("utf-8"))


def snapshot(con: Any, cfg: dict[str, Any], as_of: str | None = None) -> dict[str, Any]:
    rows = registry_rows(con)
    stamp = now_cst().strftime("%Y%m%dT%H%M%S+0800")
    day = (normalize_date(as_of) or now_cst().strftime("%Y-%m-%d")).replace("-","")
    root = Path(cfg["output_root"]) / "snapshots" / day / stamp
    root.mkdir(parents=True,exist_ok=False)
    csv_path=root/"optimus_candidate_registry_v03.csv"
    json_path=root/"optimus_candidate_registry_v03.json"
    md_path=root/"OPTIMUS_CANDIDATE_REGISTRY_V03.md"
    manifest_path=root/"MANIFEST.json"
    rows.to_csv(csv_path,index=False,encoding="utf-8-sig")
    digest=registry_digest(rows)
    payload={
        "schema_version":"OPTIMUS_CANDIDATE_REGISTRY_V0_3",
        "registry_id":cfg["registry_id"],
        "capture_id":stamp,"as_of":day,"observed_at":iso_now(),"registry_sha256":digest,
        "counts":{
            "total":int(len(rows)),
            "lifecycle":rows["lifecycle_state"].value_counts(dropna=False).to_dict() if not rows.empty else {},
            "f_status":rows["f_status"].value_counts(dropna=False).to_dict() if not rows.empty else {},
            "canonical":int(rows["canonical"].fillna(False).sum()) if not rows.empty else 0,
            "needs_review":int(rows["needs_review"].fillna(False).sum()) if not rows.empty else 0,
        },
        "rules":"candidate count is endogenous; no fixed 12/22/32 cap",
        "safety":{"research_only":True,"broker":False,"orders":False,"production_db":False},
    }
    json_path.write_text(json.dumps(payload,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    lines=[
        "# Optimus Candidate Registry V0.3", "",
        f"- Capture: `{stamp}`",f"- Registry SHA256: `{digest}`",f"- Total rows: `{len(rows)}`","",
        "Candidate count is evidence-driven. There is no fixed 12/22/32 target.","",
        "## Current Registry","",
        "| symbol | company | lifecycle | f_status | canonical | needs_review | freshness | component_domain | max_level | tesla_link | basis |",
        "|---|---|---|---|---|---|---|---|---:|---:|---|",
    ]
    for _,r in rows.iterrows():
        lines.append(f"| {r['symbol']} | {r['company']} | {r['lifecycle_state']} | {r['f_status']} | {r['canonical']} | {r['needs_review']} | {r['freshness_state']} | {r['component_domain']} | {r['max_evidence_level']} | {r['max_tesla_link_level']} | {r['state_basis']} |")
    md_path.write_text("\n".join(lines),encoding="utf-8")
    manifest={
        "schema_version":"OPTIMUS_CANDIDATE_REGISTRY_MANIFEST_V0_3","capture_id":stamp,"immutable":True,
        "registry_sha256":digest,
        "files":{p.name:sha256_file(p) for p in (csv_path,json_path,md_path)},
    }
    manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    return {"status":"PASS_OPTIMUS_CANDIDATE_REGISTRY_V0_3_SNAPSHOT","output":str(root),**payload,"manifest_sha256":sha256_file(manifest_path)}


def maintenance_bucket_for_candidate(row: dict[str, Any]) -> str | None:
    lifecycle = str(row.get("lifecycle_state") or "")
    canonical = bool(row.get("canonical"))
    freshness = str(row.get("freshness_state") or "")
    if lifecycle == "ACTIVE_SEED" and not canonical:
        return "SEED_AUDIT_DEBT"
    if lifecycle == "DISCOVERY_ONLY":
        return "DISCOVERY_ONLY"
    if canonical and freshness == "STALE_REVIEW":
        return "STALE_CANONICAL"
    return None


def maintenance_queue_payload(con: Any, cfg: dict[str, Any]) -> dict[str, Any]:
    rows = registry_rows(con)
    ev = latest_effective_evidence(con)
    provisional = ev[ev["effective_review_state"] == "PROVISIONAL"].copy() if not ev.empty else pd.DataFrame()

    seed = rows[(rows["lifecycle_state"] == "ACTIVE_SEED") & (~rows["canonical"].fillna(False))].copy() if not rows.empty else pd.DataFrame()
    stale = rows[(rows["canonical"].fillna(False)) & (rows["freshness_state"] == "STALE_REVIEW")].copy() if not rows.empty else pd.DataFrame()
    discovery_only = rows[rows["lifecycle_state"] == "DISCOVERY_ONLY"].copy() if not rows.empty else pd.DataFrame()

    pcols = ["evidence_id","symbol","company","evidence_level","evidence_type","tesla_link_level","source_type","source_ref","observed_at","confidence","interpretation"]
    ccols = ["symbol","company","component_domain","lifecycle_state","f_status","freshness_state","origin","state_basis","last_evidence_at"]
    hist = con.execute(f"""SELECT symbol,effective_at,prior_lifecycle_state,new_lifecycle_state,prior_f_status,new_f_status,reason,state_hash
                           FROM {TABLE_HISTORY} ORDER BY effective_at DESC LIMIT 50""").fetchdf()

    return {
        "status":"PASS_OPTIMUS_MAINTENANCE_QUEUE_V0_3_1",
        "summary":{
            "provisional_evidence":int(len(provisional)),
            "seed_audit_debt":int(len(seed)),
            "stale_canonical":int(len(stale)),
            "discovery_only":int(len(discovery_only)),
            "recent_state_changes_shown":int(len(hist)),
        },
        "provisional_evidence": provisional[pcols].to_dict(orient="records") if not provisional.empty else [],
        "seed_audit_debt": seed[ccols].to_dict(orient="records") if not seed.empty else [],
        "stale_canonical": stale[ccols].to_dict(orient="records") if not stale.empty else [],
        "discovery_only": discovery_only[ccols].to_dict(orient="records") if not discovery_only.empty else [],
        "recent_state_changes": hist.to_dict(orient="records") if not hist.empty else [],
        "policy":{
            "candidate_count_fixed":False,
            "seed_rows_are_bootstrap_debt_not_canonical_truth":True,
            "provisional_evidence_never_auto_promotes":True,
            "stale_review_days":int(cfg.get("stale_review_days",180)),
        },
        "research_only":True,"broker":False,"orders":False,"production_db":False,
    }

def status_payload(con: Any, cfg: dict[str, Any]) -> dict[str, Any]:
    rows=registry_rows(con)
    ev_count=con.execute(f"SELECT count(*) FROM {TABLE_EVIDENCE}").fetchone()[0]
    review_count=con.execute(f"SELECT count(*) FROM {TABLE_REVIEW}").fetchone()[0]
    disc_count=con.execute(f"SELECT count(*) FROM {TABLE_DISCOVERY}").fetchone()[0]
    ev=latest_effective_evidence(con)
    provisional=int((ev["effective_review_state"]=="PROVISIONAL").sum()) if not ev.empty else 0
    seed_debt=int(((rows["lifecycle_state"]=="ACTIVE_SEED") & (~rows["canonical"].fillna(False))).sum()) if not rows.empty else 0
    stale_canonical=int(((rows["canonical"].fillna(False)) & (rows["freshness_state"]=="STALE_REVIEW")).sum()) if not rows.empty else 0
    discovery_only=int((rows["lifecycle_state"]=="DISCOVERY_ONLY").sum()) if not rows.empty else 0
    return {
        "status":"PASS_OPTIMUS_CANDIDATE_REGISTRY_V0_3_STATUS",
        "registry_id":cfg["registry_id"],
        "candidate_count":int(len(rows)),
        "candidate_count_is_fixed":False,
        "registry_sha256":registry_digest(rows),
        "lifecycle_counts":rows["lifecycle_state"].value_counts(dropna=False).to_dict() if not rows.empty else {},
        "f_status_counts":rows["f_status"].value_counts(dropna=False).to_dict() if not rows.empty else {},
        "canonical_candidates":int(rows["canonical"].fillna(False).sum()) if not rows.empty else 0,
        "needs_review_candidates":int(rows["needs_review"].fillna(False).sum()) if not rows.empty else 0,
        "evidence_count":int(ev_count),"review_decision_count":int(review_count),"provisional_evidence_count":int(provisional),"discovery_event_count":int(disc_count),
        "maintenance_counts":{"seed_audit_debt":seed_debt,"stale_canonical":stale_canonical,"discovery_only":discovery_only,"provisional_evidence":int(provisional)},
        "research_only":True,"broker":False,"production_db":False,
    }


def open_db(cfg: dict[str, Any], read_only: bool=False) -> Any:
    p=Path(cfg["research_db"])
    if not read_only:
        p.parent.mkdir(parents=True,exist_ok=True)
    if read_only and not p.exists():
        raise RuntimeError(f"research db missing: {p}")
    return duckdb.connect(str(p),read_only=read_only)


def run_check(cfg: dict[str, Any]) -> int:
    seed=resolve_project_path(cfg["seed_universe"])
    out={
        "status":"PASS_OPTIMUS_CANDIDATE_REGISTRY_V0_3_1_CHECK" if seed.exists() else "BLOCKED_SEED_MISSING",
        "registry_id":cfg["registry_id"],"research_db":cfg["research_db"],"seed_universe":str(seed),
        "seed_sha256":sha256_file(seed) if seed.exists() else None,"fixed_candidate_count":False,
        "lifecycle_contract":sorted(ALLOWED_LIFECYCLE),"f_status_contract":sorted(ALLOWED_F),
        "evidence_review_contract":["PROVISIONAL","CANONICAL","REJECTED"],
        "auto_discovery":"CNINFO fulltext metadata/title -> PROVISIONAL only",
        "auto_canonical_upgrade":False,
        "research_only":True,"broker":False,"production_db":False,
    }
    print(json.dumps(out,ensure_ascii=False,indent=2))
    return 0 if seed.exists() else 2


def main() -> int:
    ap=argparse.ArgumentParser(description="PositionX Optimus evidence-driven Candidate Registry V0.3.1 Ops Hardening")
    ap.add_argument("--config",default=str(DEFAULT_CONFIG))
    sp=ap.add_subparsers(dest="command",required=True)
    sp.add_parser("check")
    sp.add_parser("bootstrap")
    sp.add_parser("status")
    s=sp.add_parser("ingest"); s.add_argument("--file",required=True); s.add_argument("--allow-canonical-input",action="store_true")
    s=sp.add_parser("review"); s.add_argument("--evidence-id",required=True); s.add_argument("--decision",choices=["CANONICAL","REJECTED"],required=True); s.add_argument("--reason",required=True); s.add_argument("--actor",default="MANUAL_REVIEW")
    sp.add_parser("reconcile")
    s=sp.add_parser("discover-cninfo"); s.add_argument("--start-date"); s.add_argument("--end-date"); s.add_argument("--days",type=int,default=1)
    s=sp.add_parser("snapshot"); s.add_argument("--as-of")
    s=sp.add_parser("daily"); s.add_argument("--days",type=int,default=1); s.add_argument("--as-of")
    sp.add_parser("review-queue")
    sp.add_parser("maintenance-queue")
    args=ap.parse_args()
    cfg=load_config(Path(args.config))
    if args.command=="check":
        return run_check(cfg)
    con=open_db(cfg,read_only=False)
    try:
        init_tables(con)
        if args.command=="bootstrap":
            out=seed_bootstrap(con,cfg); print(json.dumps({"status":"PASS_OPTIMUS_CANDIDATE_REGISTRY_V0_3_BOOTSTRAP",**out},ensure_ascii=False,indent=2)); return 0
        if args.command=="status":
            print(json.dumps(status_payload(con,cfg),ensure_ascii=False,indent=2,default=str)); return 0
        if args.command=="ingest":
            out=ingest_jsonl(con,cfg,Path(args.file),allow_canonical_input=args.allow_canonical_input)
            rec=reconcile_all(con,cfg)
            print(json.dumps({"status":"PASS_OPTIMUS_EVIDENCE_INGEST_V0_3","ingest":out,"reconcile":{"symbols":rec["symbols"],"changed":rec["changed"]}},ensure_ascii=False,indent=2)); return 0
        if args.command=="review":
            con.execute("BEGIN TRANSACTION")
            try:
                dec=add_review_decision(con,args.evidence_id,args.decision,args.reason,args.actor); con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK"); raise
            rec=reconcile_all(con,cfg)
            print(json.dumps({"status":"PASS_OPTIMUS_EVIDENCE_REVIEW_V0_3","decision":dec,"reconcile":{"symbols":rec["symbols"],"changed":rec["changed"]}},ensure_ascii=False,indent=2)); return 0
        if args.command=="reconcile":
            rec=reconcile_all(con,cfg); print(json.dumps({"status":"PASS_OPTIMUS_CANDIDATE_RECONCILE_V0_3","symbols":rec["symbols"],"changed":rec["changed"]},ensure_ascii=False,indent=2)); return 0
        if args.command=="discover-cninfo":
            end=pd.Timestamp(args.end_date).date() if args.end_date else now_cst().date()
            start=pd.Timestamp(args.start_date).date() if args.start_date else end-timedelta(days=max(0,args.days-1))
            out=discover_cninfo(con,cfg,str(start),str(end)); rec=reconcile_all(con,cfg)
            print(json.dumps({"status":"PASS_OPTIMUS_CNINFO_DISCOVERY_V0_3" if out["status"]=="SUCCESS" else "PARTIAL_OPTIMUS_CNINFO_DISCOVERY_V0_3","discovery":out,"reconcile":{"symbols":rec["symbols"],"changed":rec["changed"]}},ensure_ascii=False,indent=2)); return 0 if out["status"]=="SUCCESS" else 2
        if args.command=="snapshot":
            print(json.dumps(snapshot(con,cfg,args.as_of),ensure_ascii=False,indent=2,default=str)); return 0
        if args.command=="review-queue":
            q=latest_effective_evidence(con); q=q[q["effective_review_state"]=="PROVISIONAL"]
            cols=["evidence_id","symbol","company","evidence_level","evidence_type","tesla_link_level","source_type","source_ref","observed_at","confidence","interpretation"]
            print(json.dumps({"status":"PASS_OPTIMUS_REVIEW_QUEUE_V0_3","count":int(len(q)),"items":q[cols].to_dict(orient="records")},ensure_ascii=False,indent=2,default=str)); return 0
        if args.command=="maintenance-queue":
            print(json.dumps(maintenance_queue_payload(con,cfg),ensure_ascii=False,indent=2,default=str)); return 0
        if args.command=="daily":
            end=now_cst().date(); start=end-timedelta(days=max(0,args.days-1))
            disc=discover_cninfo(con,cfg,str(start),str(end))
            inbox=ingest_inbox(con,cfg)
            rec=reconcile_all(con,cfg)
            snap=snapshot(con,cfg,args.as_of)
            maint=maintenance_queue_payload(con,cfg)
            out={"status":"PASS_OPTIMUS_CANDIDATE_DAILY_V0_3_1" if disc["status"]=="SUCCESS" else "PARTIAL_OPTIMUS_CANDIDATE_DAILY_V0_3_1","discovery":disc,"inbox":inbox,"reconcile":{"symbols":rec["symbols"],"changed":rec["changed"]},"maintenance":maint["summary"],"snapshot":snap}
            print(json.dumps(out,ensure_ascii=False,indent=2,default=str)); return 0 if disc["status"]=="SUCCESS" else 2
        raise RuntimeError("unknown command")
    finally:
        con.close()


if __name__=="__main__":
    raise SystemExit(main())
