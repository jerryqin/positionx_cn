from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CST = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config/research/optimus_canonicalization_v0_5.json"
DEFAULT_PLAN = ROOT / "config/research/optimus_canonicalization_seed_batch_v0_5_20261009.json"

REGISTRY_V031_SHA256 = "3955c6deb314bee4bf6821ae9cc0d2f09c606d0fbd695f34877afa3d4b782884"
EVIDENCE_V044_SHA256 = "ec8eb0dafd655eef8ce8475e9a0e000b3c8ff351fb4f9d6bead0ea60e35df41b"
EVIDENCE_V045_SHA256 = "10e5a8288177c76e207fa264f74e18c3a878d95cf191585ca8a576643475a691"
SUPPORTED_EVIDENCE_SCRIPT_SHA256 = {EVIDENCE_V044_SHA256, EVIDENCE_V045_SHA256}
EXPECTED_SOURCE_PARSER = "OPTIMUS_EVIDENCE_ACQUISITION_V0_4_4"

TABLE_FACT = "optimus_canonical_facts_v05"
TABLE_FACT_SOURCE = "optimus_canonical_fact_sources_v05"
TABLE_BATCH = "optimus_canonical_batches_v05"
TABLE_REPAIR = "optimus_canonical_fact_materialization_repairs_v051"
FACT_MATERIALIZATION_PARSER = "OPTIMUS_CANONICALIZATION_V0_5_1_FACT_MATERIALIZATION"


def now_cst() -> datetime:
    return datetime.now(CST)


def iso_now() -> str:
    return now_cst().isoformat()


def stable_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def stable_id(prefix: str, obj: Any, n: int = 24) -> str:
    return f"{prefix}_{sha256_bytes(stable_json(obj).encode('utf-8'))[:n]}"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else ROOT / p


def fact_id_for(fact: dict[str, Any]) -> str:
    payload = {
        "fact_key": fact["fact_key"],
        "symbol": fact["symbol"],
        "fact_text": fact["fact_text"],
        "canonical_level": int(fact["canonical_level"]),
        "canonical_tesla_link_level": int(fact["canonical_tesla_link_level"]),
    }
    return stable_id("CF", payload)


def plan_covered_evidence_ids(plan: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for fact in plan.get("facts", []):
        out.append(str(fact["anchor_evidence_id"]))
        out.extend(str(x) for x in fact.get("support_evidence_ids", []))
    out.extend(str(x["evidence_id"]) for x in plan.get("reject_only", []))
    return out


def validate_plan_schema(plan: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    if plan.get("schema_version") != "OPTIMUS_CANONICALIZATION_PLAN_V0_5":
        errors.append("schema_version must be OPTIMUS_CANONICALIZATION_PLAN_V0_5")
    if not str(plan.get("plan_id") or "").strip():
        errors.append("plan_id required")
    facts = plan.get("facts") or []
    rejects = plan.get("reject_only") or []
    if not facts:
        errors.append("facts must not be empty")

    fact_keys: set[str] = set()
    all_ids: list[str] = []
    derived_fact_ids: list[str] = []
    for i, fact in enumerate(facts, 1):
        prefix = f"facts[{i}]"
        for k in ("fact_key", "symbol", "company", "fact_type", "fact_text", "component_domain", "anchor_evidence_id"):
            if not str(fact.get(k) or "").strip():
                errors.append(f"{prefix}.{k} required")
        key = str(fact.get("fact_key") or "")
        if key in fact_keys:
            errors.append(f"duplicate fact_key: {key}")
        fact_keys.add(key)
        try:
            lvl = int(fact.get("canonical_level"))
            if not 1 <= lvl <= 9:
                errors.append(f"{prefix}.canonical_level must be 1..9")
        except Exception:
            errors.append(f"{prefix}.canonical_level invalid")
        try:
            tl = int(fact.get("canonical_tesla_link_level"))
            if not 0 <= tl <= 4:
                errors.append(f"{prefix}.canonical_tesla_link_level must be 0..4")
        except Exception:
            errors.append(f"{prefix}.canonical_tesla_link_level invalid")
        anchor = str(fact.get("anchor_evidence_id") or "")
        supports = [str(x) for x in fact.get("support_evidence_ids", [])]
        if anchor in supports:
            errors.append(f"{prefix} anchor must not also be support")
        all_ids.append(anchor)
        all_ids.extend(supports)
        if all(k in fact for k in ("fact_key", "symbol", "fact_text", "canonical_level", "canonical_tesla_link_level")):
            try:
                derived_fact_ids.append(fact_id_for(fact))
            except Exception as exc:
                errors.append(f"{prefix} fact id error: {exc}")

    for i, row in enumerate(rejects, 1):
        if not str(row.get("evidence_id") or "").strip():
            errors.append(f"reject_only[{i}].evidence_id required")
        if not str(row.get("reason_code") or "").strip():
            errors.append(f"reject_only[{i}].reason_code required")
        all_ids.append(str(row.get("evidence_id") or ""))

    nonempty_ids = [x for x in all_ids if x]
    duplicates = sorted({x for x in nonempty_ids if nonempty_ids.count(x) > 1})
    if duplicates:
        errors.append(f"evidence IDs used more than once: {duplicates}")
    if len(set(derived_fact_ids)) != len(derived_fact_ids):
        errors.append("derived fact_id collision")

    return {
        "ok": not errors,
        "errors": errors,
        "fact_count": len(facts),
        "canonical_symbol_count": len({str(f.get('symbol')) for f in facts}),
        "covered_evidence_count": len(nonempty_ids),
        "reject_only_count": len(rejects),
        "derived_fact_ids": derived_fact_ids,
    }


def import_registry(cfg: dict[str, Any]):
    path = resolve_path(cfg["registry_script"])
    spec = importlib.util.spec_from_file_location("optimus_registry_v031_for_v05", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import registry module: {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def connect_db(cfg: dict[str, Any], read_only: bool = False):
    import duckdb
    return duckdb.connect(str(resolve_path(cfg["research_db"])), read_only=read_only)


def init_tables(con: Any) -> None:
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE_FACT} (
            fact_id VARCHAR PRIMARY KEY,
            batch_id VARCHAR NOT NULL,
            symbol VARCHAR NOT NULL,
            company VARCHAR,
            fact_key VARCHAR NOT NULL,
            fact_type VARCHAR NOT NULL,
            fact_text VARCHAR NOT NULL,
            component_domain VARCHAR,
            canonical_level INTEGER NOT NULL,
            canonical_tesla_link_level INTEGER NOT NULL,
            anchor_evidence_id VARCHAR NOT NULL,
            policy_version VARCHAR NOT NULL,
            actor VARCHAR NOT NULL,
            canonicalized_at TIMESTAMPTZ NOT NULL,
            plan_sha256 VARCHAR NOT NULL
        )
    """)
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE_FACT_SOURCE} (
            fact_id VARCHAR NOT NULL,
            evidence_id VARCHAR NOT NULL,
            source_role VARCHAR NOT NULL,
            review_action VARCHAR NOT NULL,
            PRIMARY KEY (fact_id, evidence_id)
        )
    """)
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE_BATCH} (
            batch_id VARCHAR PRIMARY KEY,
            plan_id VARCHAR NOT NULL,
            plan_sha256 VARCHAR NOT NULL UNIQUE,
            actor VARCHAR NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL,
            fact_count INTEGER NOT NULL,
            canonical_symbol_count INTEGER NOT NULL,
            covered_source_count INTEGER NOT NULL,
            reject_only_count INTEGER NOT NULL,
            affected_symbols_json VARCHAR NOT NULL,
            output_dir VARCHAR,
            result_manifest_sha256 VARCHAR
        )
    """)
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE_REPAIR} (
            repair_id VARCHAR PRIMARY KEY,
            batch_id VARCHAR NOT NULL,
            fact_id VARCHAR NOT NULL,
            materialized_evidence_id VARCHAR NOT NULL,
            anchor_evidence_id VARCHAR NOT NULL,
            actor VARCHAR NOT NULL,
            repaired_at TIMESTAMPTZ NOT NULL,
            reason VARCHAR NOT NULL,
            UNIQUE(batch_id, fact_id)
        )
    """)


def _fact_materialized_evidence_id(batch_id: str, fact_id: str) -> str:
    return stable_id("CFE", {"batch_id": batch_id, "fact_id": fact_id, "policy": FACT_MATERIALIZATION_PARSER})


def _latest_review_state(con: Any, reg: Any, evidence_id: str) -> tuple[str | None, str | None]:
    row = con.execute(
        f"""SELECT COALESCE(r.decision, e.initial_review_state) AS state, r.reason
        FROM {reg.TABLE_EVIDENCE} e
        LEFT JOIN (
            SELECT evidence_id, decision, reason,
                   row_number() OVER (PARTITION BY evidence_id ORDER BY decided_at DESC, decision_id DESC) AS rn
            FROM {reg.TABLE_REVIEW}
        ) r ON r.evidence_id = e.evidence_id AND r.rn = 1
        WHERE e.evidence_id = ?""",
        [evidence_id],
    ).fetchone()
    if not row:
        return None, None
    return (str(row[0]) if row[0] is not None else None,
            str(row[1]) if row[1] is not None else None)


def _load_anchor_row(con: Any, reg: Any, evidence_id: str) -> dict[str, Any]:
    df = con.execute(f"SELECT * FROM {reg.TABLE_EVIDENCE} WHERE evidence_id = ?", [evidence_id]).fetchdf()
    if df.empty:
        raise RuntimeError(f"anchor evidence missing: {evidence_id}")
    return df.iloc[0].to_dict()


def build_materialized_fact_evidence(*, batch_id: str, fact_row: dict[str, Any], anchor_row: dict[str, Any]) -> dict[str, Any]:
    fid = str(fact_row["fact_id"])
    source_type = str(anchor_row.get("source_type") or "PRIMARY_SOURCE")
    if "PRIMARY" not in source_type.upper() and "CNINFO" not in source_type.upper():
        source_type = f"PRIMARY_DERIVED_{source_type}"
    return {
        "evidence_id": _fact_materialized_evidence_id(batch_id, fid),
        "symbol": str(fact_row["symbol"]),
        "company": str(fact_row.get("company") or anchor_row.get("company") or ""),
        "component_domain": str(fact_row.get("component_domain") or "UNKNOWN_PENDING_REVIEW"),
        "evidence_level": int(fact_row["canonical_level"]),
        "evidence_type": "CANONICAL_FACT",
        "tesla_link_level": int(fact_row["canonical_tesla_link_level"]),
        "source_type": source_type,
        "source_ref": str(anchor_row.get("source_ref") or f"CANONICAL_FACT:{fid}"),
        "source_sha256": str(anchor_row.get("source_sha256") or "").strip() or None,
        "event_time": anchor_row.get("event_time"),
        "observed_at": str(fact_row.get("canonicalized_at") or iso_now()),
        "review_state": "CANONICAL",
        "confidence": 1.0,
        "contradiction_kind": None,
        "contradiction_severity": None,
        "parser_version": FACT_MATERIALIZATION_PARSER,
        "interpretation": (
            f"Human-reviewed canonical fact materialization; fact_id={fid}; "
            f"fact_key={fact_row.get('fact_key')}; anchor={anchor_row.get('evidence_id')}; "
            f"fact_text={fact_row.get('fact_text')}"
        ),
        "canonical_fact": {
            "fact_id": fid, "batch_id": batch_id, "fact_key": fact_row.get("fact_key"),
            "fact_type": fact_row.get("fact_type"), "fact_text": fact_row.get("fact_text"),
            "anchor_evidence_id": anchor_row.get("evidence_id"),
            "policy_version": "OPTIMUS_CANONICALIZATION_V0_5_1",
        },
    }


def materialize_fact_evidence(con: Any, reg: Any, *, batch_id: str, fact_row: dict[str, Any], actor: str, append_repair_record: bool) -> dict[str, Any]:
    anchor_id = str(fact_row["anchor_evidence_id"])
    anchor = _load_anchor_row(con, reg, anchor_id)
    payload = build_materialized_fact_evidence(batch_id=batch_id, fact_row=fact_row, anchor_row=anchor)
    normalized = reg.validate_evidence_row(payload, allow_canonical_input=True)
    inserted = reg.insert_evidence(con, normalized)
    state, reason = _latest_review_state(con, reg, anchor_id)
    desired_reason = f"SOURCE_ABSORBED_BY_CANONICAL_FACT_V0_5_1:{fact_row['fact_id']}"
    review_added = False
    if state != "REJECTED" or reason != desired_reason:
        reg.add_review_decision(con, anchor_id, "REJECTED", desired_reason, actor)
        review_added = True
    repair_inserted = False
    if append_repair_record:
        repair_id = stable_id("CRP", {"batch_id": batch_id, "fact_id": fact_row["fact_id"], "materialized_evidence_id": normalized["evidence_id"], "policy": "V0_5_1"})
        exists = con.execute(f"SELECT count(*) FROM {TABLE_REPAIR} WHERE batch_id = ? AND fact_id = ?", [batch_id, fact_row["fact_id"]]).fetchone()[0]
        if not exists:
            con.execute(
                f"""INSERT INTO {TABLE_REPAIR}
                (repair_id,batch_id,fact_id,materialized_evidence_id,anchor_evidence_id,actor,repaired_at,reason)
                VALUES (?,?,?,?,?,?,?,?)""",
                [repair_id, batch_id, fact_row["fact_id"], normalized["evidence_id"], anchor_id, actor, iso_now(), "CANONICAL_FACT_MATERIALIZATION_HOTFIX_V0_5_1"],
            )
            repair_inserted = True
    return {
        "fact_id": fact_row["fact_id"], "symbol": fact_row["symbol"], "anchor_evidence_id": anchor_id,
        "materialized_evidence_id": normalized["evidence_id"], "evidence_inserted": bool(inserted),
        "anchor_review_rejected": bool(review_added), "repair_record_inserted": bool(repair_inserted),
    }


def _expected_state_from_facts(reg: Any, reg_cfg: dict[str, Any], facts_df: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if facts_df.empty:
        return out
    for sym, grp in facts_df.groupby("symbol"):
        max_level = int(grp["canonical_level"].max())
        max_tesla = int(grp["canonical_tesla_link_level"].max())
        has_component = any(str(x).strip() and str(x) != "UNKNOWN_PENDING_REVIEW" for x in grp["component_domain"].tolist())
        lifecycle, f_status = reg.derive_f_status(
            max_level=max_level, max_tesla_link=max_tesla, has_primary=True, hard_negative=False,
            has_component_relevance=has_component, rules=reg_cfg["rules"]
        )
        out[str(sym)] = {
            "expected_lifecycle_state": lifecycle, "expected_f_status": f_status,
            "max_level": max_level, "max_tesla_link": max_tesla, "has_component_relevance": has_component,
        }
    return out


def audit_fact_materialization(con: Any, reg: Any, reg_cfg: dict[str, Any], batch_id: str) -> dict[str, Any]:
    facts = con.execute(f"SELECT * FROM {TABLE_FACT} WHERE batch_id = ? ORDER BY symbol, fact_id", [batch_id]).fetchdf()
    expected = _expected_state_from_facts(reg, reg_cfg, facts)
    anomalies: list[dict[str, Any]] = []
    fact_rows: list[dict[str, Any]] = []
    for _, fr in facts.iterrows():
        f = fr.to_dict()
        anchor = _load_anchor_row(con, reg, str(f["anchor_evidence_id"]))
        state, _reason = _latest_review_state(con, reg, str(f["anchor_evidence_id"]))
        materialized_id = _fact_materialized_evidence_id(batch_id, str(f["fact_id"]))
        materialized_exists = con.execute(f"SELECT count(*) FROM {reg.TABLE_EVIDENCE} WHERE evidence_id = ?", [materialized_id]).fetchone()[0]
        fact_rows.append({
            "fact_id": str(f["fact_id"]), "symbol": str(f["symbol"]), "anchor_evidence_id": str(f["anchor_evidence_id"]),
            "anchor_effective_review_state": state, "anchor_contradiction_kind": anchor.get("contradiction_kind"),
            "anchor_contradiction_severity": anchor.get("contradiction_severity"), "anchor_component_domain": anchor.get("component_domain"),
            "fact_component_domain": f.get("component_domain"), "materialized_evidence_id": materialized_id,
            "materialized_exists": bool(materialized_exists),
        })
        if state == "CANONICAL" and str(anchor.get("contradiction_kind") or "").strip():
            anomalies.append({
                "code": "RAW_ANCHOR_CANONICAL_WITH_CONTRADICTION", "symbol": str(f["symbol"]), "fact_id": str(f["fact_id"]),
                "anchor_evidence_id": str(f["anchor_evidence_id"]), "contradiction_kind": anchor.get("contradiction_kind"),
                "contradiction_severity": anchor.get("contradiction_severity"),
            })
        if not materialized_exists:
            anomalies.append({"code": "CANONICAL_FACT_NOT_MATERIALIZED", "symbol": str(f["symbol"]), "fact_id": str(f["fact_id"]), "materialized_evidence_id": materialized_id})
    candidate_rows: list[dict[str, Any]] = []
    for sym, exp in expected.items():
        cur = reg.read_candidate(con, sym) or {}
        candidate_rows.append({"symbol": sym, "actual_lifecycle_state": cur.get("lifecycle_state"), "actual_f_status": cur.get("f_status"), **exp})
        if cur.get("lifecycle_state") != exp["expected_lifecycle_state"] or cur.get("f_status") != exp["expected_f_status"]:
            anomalies.append({"code": "CANDIDATE_STATE_MISMATCH", "symbol": sym, "actual_lifecycle_state": cur.get("lifecycle_state"), "actual_f_status": cur.get("f_status"), **exp})
    return {
        "batch_id": batch_id, "fact_count": int(len(facts)), "candidate_symbol_count": int(facts["symbol"].nunique()) if not facts.empty else 0,
        "fact_rows": fact_rows, "candidate_state_checks": candidate_rows, "anomaly_count": len(anomalies),
        "anomalies": anomalies, "repair_required": bool(anomalies),
    }


def effective_source_rows(reg: Any, con: Any, parser_version: str) -> dict[str, dict[str, Any]]:
    df = reg.latest_effective_evidence(con)
    if df.empty:
        return {}
    mask = (
        (df["effective_review_state"].astype(str) == "PROVISIONAL") &
        (df["parser_version"].astype(str) == parser_version)
    )
    out: dict[str, dict[str, Any]] = {}
    for _, r in df[mask].iterrows():
        d = {k: (None if str(v) == "nan" else v) for k, v in r.to_dict().items()}
        out[str(d["evidence_id"])] = d
    return out


def validate_plan_against_db(reg: Any, con: Any, cfg: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    schema = validate_plan_schema(plan)
    errors = list(schema["errors"])
    parser = cfg["policy"].get("expected_source_parser", EXPECTED_SOURCE_PARSER)
    current = effective_source_rows(reg, con, parser)
    covered = set(plan_covered_evidence_ids(plan))
    current_ids = set(current)

    if cfg["policy"].get("require_full_parser_scope_coverage", True):
        missing_from_plan = sorted(current_ids - covered)
        unknown_in_plan = sorted(covered - current_ids)
        if missing_from_plan:
            errors.append(f"effective V0.4.4 provisional not covered by plan: {missing_from_plan}")
        if unknown_in_plan:
            errors.append(f"plan evidence not current effective V0.4.4 provisional: {unknown_in_plan}")

    fact_checks: list[dict[str, Any]] = []
    for fact in plan.get("facts", []):
        fid = fact_id_for(fact)
        ids = [str(fact["anchor_evidence_id"])] + [str(x) for x in fact.get("support_evidence_ids", [])]
        rows = [current.get(x) for x in ids]
        local_errors: list[str] = []
        for evid, row in zip(ids, rows):
            if row is None:
                local_errors.append(f"missing/prohibited source {evid}")
                continue
            if str(row.get("symbol")) != str(fact["symbol"]):
                local_errors.append(f"{evid} symbol {row.get('symbol')} != {fact['symbol']}")
            if str(row.get("source_type")) != "CNINFO_PRIMARY_PDF":
                local_errors.append(f"{evid} source_type not CNINFO_PRIMARY_PDF")
            if not str(row.get("source_sha256") or "").strip():
                local_errors.append(f"{evid} missing source_sha256")
        anchor = current.get(str(fact["anchor_evidence_id"]))
        if anchor:
            if int(anchor.get("evidence_level") or 0) != int(fact["canonical_level"]):
                local_errors.append(
                    f"anchor level {anchor.get('evidence_level')} != canonical_level {fact['canonical_level']} (V0.5 disallows silent level override)"
                )
            if int(anchor.get("tesla_link_level") or 0) != int(fact["canonical_tesla_link_level"]):
                local_errors.append(
                    f"anchor tesla_link {anchor.get('tesla_link_level')} != canonical_tesla_link_level {fact['canonical_tesla_link_level']} (V0.5 disallows silent Tesla override)"
                )
        if local_errors:
            errors.extend([f"{fact['fact_key']}: {x}" for x in local_errors])
        fact_checks.append({
            "fact_id": fid,
            "fact_key": fact["fact_key"],
            "symbol": fact["symbol"],
            "canonical_level": int(fact["canonical_level"]),
            "canonical_tesla_link_level": int(fact["canonical_tesla_link_level"]),
            "anchor": fact["anchor_evidence_id"],
            "supports": list(fact.get("support_evidence_ids", [])),
            "ok": not local_errors,
        })

    maintenance = reg.maintenance_queue_payload(con, reg.load_config(resolve_path(cfg["registry_config"])))
    seed_before = int(maintenance["summary"]["seed_audit_debt"])
    canonical_symbols = sorted({str(f["symbol"]) for f in plan.get("facts", [])})
    current_candidates = {str(r[0]): str(r[1]) for r in con.execute(
        f"SELECT symbol,lifecycle_state FROM {reg.TABLE_CANDIDATE}"
    ).fetchall()}
    seed_to_clear = sum(1 for s in canonical_symbols if current_candidates.get(s) == "ACTIVE_SEED")
    predicted_seed_after = max(0, seed_before - seed_to_clear)

    return {
        "ok": not errors,
        "errors": errors,
        "parser_version": parser,
        "effective_parser_provisional_count": len(current_ids),
        "plan_covered_evidence_count": len(covered),
        "exact_scope_coverage": current_ids == covered,
        "fact_checks": fact_checks,
        "canonical_symbols": canonical_symbols,
        "seed_audit_debt_before": seed_before,
        "predicted_seed_audit_debt_after": predicted_seed_after,
        "current_maintenance": maintenance["summary"],
    }


def evidence_script_compatible(path: Path) -> tuple[bool, str | None]:
    if not path.exists():
        return False, None
    digest = sha256_file(path)
    return digest in SUPPORTED_EVIDENCE_SCRIPT_SHA256, digest


def check_payload(cfg: dict[str, Any]) -> dict[str, Any]:
    registry = resolve_path(cfg["registry_script"])
    evidence = resolve_path(cfg["evidence_script"])
    plan = resolve_path(cfg.get("default_plan", str(DEFAULT_PLAN)))
    evidence_ok, evidence_digest = evidence_script_compatible(evidence)
    checks = {
        "schema_version": "OPTIMUS_CANONICALIZATION_V0_5_1",
        "registry_script": str(registry),
        "registry_exists": registry.exists(),
        "registry_sha256": sha256_file(registry) if registry.exists() else None,
        "registry_expected_sha256": REGISTRY_V031_SHA256,
        "registry_compatible": registry.exists() and sha256_file(registry) == REGISTRY_V031_SHA256,
        "evidence_script": str(evidence),
        "evidence_exists": evidence.exists(),
        "evidence_sha256": evidence_digest,
        "evidence_supported_sha256": sorted(SUPPORTED_EVIDENCE_SCRIPT_SHA256),
        "evidence_compatible": evidence_ok,
        "evidence_source_parser_contract": EXPECTED_SOURCE_PARSER,
        "default_plan": str(plan),
        "default_plan_exists": plan.exists(),
        "default_plan_sha256": sha256_file(plan) if plan.exists() else None,
        "auto_apply": False,
        "apply_requires_exact_plan_sha256": True,
        "canonical_unit": "FACT_MATERIALIZED_AS_CANONICAL_EVIDENCE_WITH_PRIMARY_SOURCE_PROVENANCE",
        "raw_anchor_becomes_source_only": True,
        "fact_materialization_parser": FACT_MATERIALIZATION_PARSER,
        "source_rows_physically_deleted": False,
        "research_only": True,
        "broker": False,
        "production_db": False,
    }
    checks["ok"] = all([
        checks["registry_compatible"], checks["evidence_compatible"], checks["default_plan_exists"]
    ])
    return checks


def cmd_check(args: argparse.Namespace) -> int:
    cfg = load_json(Path(args.config))
    p = check_payload(cfg)
    status = "PASS_OPTIMUS_CANONICALIZATION_V0_5_1_CHECK" if p.pop("ok") else "FAIL_OPTIMUS_CANONICALIZATION_V0_5_1_CHECK"
    print(json.dumps({"status": status, **p}, ensure_ascii=False, indent=2))
    return 0 if status.startswith("PASS") else 2


def cmd_plan(args: argparse.Namespace) -> int:
    cfg = load_json(Path(args.config))
    checks = check_payload(cfg)
    if not checks["ok"]:
        print(json.dumps({"status": "FAIL_OPTIMUS_CANONICALIZATION_V0_5_PLAN_PRECHECK", "checks": checks}, ensure_ascii=False, indent=2))
        return 2
    plan_path = Path(args.plan)
    plan = load_json(plan_path)
    plan_sha = sha256_file(plan_path)
    reg = import_registry(cfg)
    con = connect_db(cfg, read_only=True)
    try:
        validation = validate_plan_against_db(reg, con, cfg, plan)
    finally:
        con.close()
    out = {
        "status": "PASS_OPTIMUS_CANONICALIZATION_V0_5_PLAN" if validation["ok"] else "FAIL_OPTIMUS_CANONICALIZATION_V0_5_PLAN",
        "plan_id": plan.get("plan_id"),
        "plan": str(plan_path),
        "plan_sha256": plan_sha,
        "mutation": False,
        "requires_manual_apply_approval": True,
        "apply_command_template": f"python scripts\\run_optimus_canonicalization_v05.py apply --plan {plan_path} --approve-plan-sha256 {plan_sha} --actor <YOUR_NAME>",
        "schema": validate_plan_schema(plan),
        "db_validation": validation,
        "facts": [
            {
                "fact_id": fact_id_for(f),
                "fact_key": f["fact_key"],
                "symbol": f["symbol"],
                "company": f["company"],
                "fact_type": f["fact_type"],
                "canonical_level": int(f["canonical_level"]),
                "canonical_tesla_link_level": int(f["canonical_tesla_link_level"]),
                "anchor_evidence_id": f["anchor_evidence_id"],
                "support_count": len(f.get("support_evidence_ids", [])),
                "fact_text": f["fact_text"],
            }
            for f in plan.get("facts", [])
        ],
        "reject_only": plan.get("reject_only", []),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0 if validation["ok"] else 2


def write_batch_artifacts(output_dir: Path, plan_path: Path, plan: dict[str, Any], result: dict[str, Any]) -> str:
    output_dir.mkdir(parents=True, exist_ok=False)
    shutil.copy2(plan_path, output_dir / "approved_plan.json")
    (output_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    facts_csv = output_dir / "canonical_facts.csv"
    with facts_csv.open("w", encoding="utf-8-sig", newline="") as f:
        cols = ["fact_id","symbol","company","fact_key","fact_type","canonical_level","canonical_tesla_link_level","anchor_evidence_id","fact_text"]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for x in result["canonical_facts"]:
            w.writerow({k: x.get(k) for k in cols})

    md = [
        "# Optimus Canonicalization V0.5", "",
        f"- Batch: `{result['batch_id']}`",
        f"- Plan SHA256: `{result['plan_sha256']}`",
        f"- Actor: `{result['actor']}`",
        f"- Facts: `{len(result['canonical_facts'])}`",
        f"- Canonical symbols: `{len(result['canonical_symbols'])}`",
        f"- Seed audit debt: `{result['maintenance_before']['seed_audit_debt']}` -> `{result['maintenance_after']['seed_audit_debt']}`",
        "", "## Canonical facts", "",
        "| symbol | company | level | tesla | fact | anchor |",
        "|---|---|---:|---:|---|---|",
    ]
    for x in result["canonical_facts"]:
        md.append(f"| {x['symbol']} | {x['company']} | {x['canonical_level']} | {x['canonical_tesla_link_level']} | {x['fact_text']} | {x['anchor_evidence_id']} |")
    (output_dir / "CANONICALIZATION_REPORT.md").write_text("\n".join(md), encoding="utf-8")

    files = [output_dir / "approved_plan.json", output_dir / "result.json", facts_csv, output_dir / "CANONICALIZATION_REPORT.md"]
    manifest = {
        "schema_version": "OPTIMUS_CANONICALIZATION_MANIFEST_V0_5",
        "batch_id": result["batch_id"],
        "immutable": True,
        "files": {p.name: sha256_file(p) for p in files},
    }
    mp = output_dir / "MANIFEST.json"
    mp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return sha256_file(mp)


def cmd_apply(args: argparse.Namespace) -> int:
    cfg = load_json(Path(args.config))
    if not str(args.actor or "").strip():
        print(json.dumps({"status": "FAIL_OPTIMUS_CANONICALIZATION_V0_5_APPLY", "error": "--actor required"}, ensure_ascii=False, indent=2))
        return 2
    plan_path = Path(args.plan)
    plan_sha = sha256_file(plan_path)
    if args.approve_plan_sha256 != plan_sha:
        print(json.dumps({
            "status": "FAIL_OPTIMUS_CANONICALIZATION_V0_5_APPLY",
            "error": "PLAN_SHA256_APPROVAL_MISMATCH",
            "expected": plan_sha,
            "provided": args.approve_plan_sha256,
            "mutation": False,
        }, ensure_ascii=False, indent=2))
        return 2
    checks = check_payload(cfg)
    if not checks["ok"]:
        print(json.dumps({"status": "FAIL_OPTIMUS_CANONICALIZATION_V0_5_APPLY_PRECHECK", "checks": checks, "mutation": False}, ensure_ascii=False, indent=2))
        return 2

    plan = load_json(plan_path)
    reg = import_registry(cfg)
    reg_cfg = reg.load_config(resolve_path(cfg["registry_config"]))
    con = connect_db(cfg, read_only=False)
    init_tables(con)
    validation = validate_plan_against_db(reg, con, cfg, plan)
    if not validation["ok"]:
        con.close()
        print(json.dumps({"status": "FAIL_OPTIMUS_CANONICALIZATION_V0_5_APPLY_VALIDATION", "validation": validation, "mutation": False}, ensure_ascii=False, indent=2))
        return 2

    already = con.execute(f"SELECT count(*) FROM {TABLE_BATCH} WHERE plan_sha256 = ?", [plan_sha]).fetchone()[0]
    if already:
        con.close()
        print(json.dumps({"status": "ALREADY_APPLIED_OPTIMUS_CANONICALIZATION_V0_5", "plan_sha256": plan_sha, "mutation": False}, ensure_ascii=False, indent=2))
        return 0

    batch_id = stable_id("CB", {"plan_id": plan["plan_id"], "plan_sha256": plan_sha})
    actor = str(args.actor).strip()
    maintenance_before = reg.maintenance_queue_payload(con, reg_cfg)["summary"]
    affected_symbols = sorted({f["symbol"] for f in plan["facts"]} | {
        str(x.get("symbol") or "") for x in plan.get("reject_only", []) if str(x.get("symbol") or "")
    })
    fact_rows: list[dict[str, Any]] = []
    review_decisions: list[dict[str, Any]] = []

    con.execute("BEGIN TRANSACTION")
    try:
        materialization_results = []
        for fact in plan["facts"]:
            fid = fact_id_for(fact)
            anchor = str(fact["anchor_evidence_id"])
            canonicalized_at = iso_now()
            source_rows = [(anchor, "ANCHOR", "REJECTED_AS_SOURCE_ABSORBED_BY_CANONICAL_FACT")]
            for evid in fact.get("support_evidence_ids", []):
                evid = str(evid)
                review_decisions.append(reg.add_review_decision(
                    con, evid, "REJECTED", f"DUPLICATE_CORROBORATION_SUPERSEDED_BY_CANONICAL_FACT_V0_5_1:{fid}", actor
                ))
                source_rows.append((evid, "SUPPORT", "REJECTED_AS_DUPLICATE_CORROBORATION"))

            con.execute(
                f"""INSERT INTO {TABLE_FACT}
                (fact_id,batch_id,symbol,company,fact_key,fact_type,fact_text,component_domain,canonical_level,canonical_tesla_link_level,anchor_evidence_id,policy_version,actor,canonicalized_at,plan_sha256)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [fid,batch_id,fact["symbol"],fact["company"],fact["fact_key"],fact["fact_type"],fact["fact_text"],fact["component_domain"],
                 int(fact["canonical_level"]),int(fact["canonical_tesla_link_level"]),anchor,"OPTIMUS_CANONICALIZATION_V0_5_1",actor,canonicalized_at,plan_sha]
            )
            fact_db = {
                "fact_id": fid, "batch_id": batch_id, "symbol": fact["symbol"], "company": fact["company"],
                "fact_key": fact["fact_key"], "fact_type": fact["fact_type"], "fact_text": fact["fact_text"],
                "component_domain": fact["component_domain"], "canonical_level": int(fact["canonical_level"]),
                "canonical_tesla_link_level": int(fact["canonical_tesla_link_level"]),
                "anchor_evidence_id": anchor, "canonicalized_at": canonicalized_at,
            }
            mat = materialize_fact_evidence(
                con, reg, batch_id=batch_id, fact_row=fact_db, actor=actor, append_repair_record=False
            )
            materialization_results.append(mat)
            if mat["anchor_review_rejected"]:
                review_decisions.append({"evidence_id": anchor, "decision": "REJECTED", "reason": f"SOURCE_ABSORBED_BY_CANONICAL_FACT_V0_5_1:{fid}"})
            for evid, role, action in source_rows:
                con.execute(
                    f"INSERT INTO {TABLE_FACT_SOURCE}(fact_id,evidence_id,source_role,review_action) VALUES (?,?,?,?)",
                    [fid,evid,role,action]
                )
            fact_rows.append({
                "fact_id": fid,
                "symbol": fact["symbol"],
                "company": fact["company"],
                "fact_key": fact["fact_key"],
                "fact_type": fact["fact_type"],
                "fact_text": fact["fact_text"],
                "component_domain": fact["component_domain"],
                "canonical_level": int(fact["canonical_level"]),
                "canonical_tesla_link_level": int(fact["canonical_tesla_link_level"]),
                "anchor_evidence_id": anchor,
                "support_evidence_ids": list(fact.get("support_evidence_ids", [])),
                "materialized_evidence_id": mat["materialized_evidence_id"],
            })

        for row in plan.get("reject_only", []):
            evid = str(row["evidence_id"])
            reason = f"{row['reason_code']}:OPTIMUS_CANONICALIZATION_V0_5"
            review_decisions.append(reg.add_review_decision(con, evid, "REJECTED", reason, actor))

        reconcile_results = []
        for sym in sorted({f["symbol"] for f in plan["facts"]} | {r.get("symbol", "") for r in plan.get("reject_only", []) if r.get("symbol")}):
            reconcile_results.append(reg.reconcile_symbol(con, reg_cfg, sym))

        con.execute(
            f"""INSERT INTO {TABLE_BATCH}
            (batch_id,plan_id,plan_sha256,actor,applied_at,fact_count,canonical_symbol_count,covered_source_count,reject_only_count,affected_symbols_json,output_dir,result_manifest_sha256)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            [batch_id,plan["plan_id"],plan_sha,actor,iso_now(),len(plan["facts"]),len({f['symbol'] for f in plan['facts']}),
             len(plan_covered_evidence_ids(plan)),len(plan.get("reject_only", [])),json.dumps(affected_symbols,ensure_ascii=False),None,None]
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        con.close()
        raise

    maintenance_after = reg.maintenance_queue_payload(con, reg_cfg)["summary"]
    stamp = now_cst().strftime("%Y%m%dT%H%M%S+0800")
    out_dir = resolve_path(cfg["output_root"]) / now_cst().strftime("%Y%m%d") / stamp
    result = {
        "status": "PASS_OPTIMUS_CANONICALIZATION_V0_5_1_APPLY",
        "batch_id": batch_id,
        "plan_id": plan["plan_id"],
        "plan_sha256": plan_sha,
        "actor": actor,
        "applied_at": iso_now(),
        "canonical_facts": fact_rows,
        "canonical_symbols": sorted({f["symbol"] for f in plan["facts"]}),
        "source_review_decision_count": len(review_decisions),
        "fact_materialization": materialization_results,
        "physical_delete": False,
        "maintenance_before": maintenance_before,
        "maintenance_after": maintenance_after,
        "reconcile": {
            "symbols": len(reconcile_results),
            "changed": sum(int(bool(x.get("changed"))) for x in reconcile_results),
            "results": reconcile_results,
        },
        "research_only": True,
        "broker": False,
        "production_db": False,
    }
    manifest_sha = write_batch_artifacts(out_dir, plan_path, plan, result)
    con.execute(f"UPDATE {TABLE_BATCH} SET output_dir = ?, result_manifest_sha256 = ? WHERE batch_id = ?", [str(out_dir),manifest_sha,batch_id])
    registry_snapshot = reg.snapshot(con, reg_cfg, as_of=now_cst().strftime("%Y%m%d"))
    con.close()
    result["output"] = str(out_dir)
    result["result_manifest_sha256"] = manifest_sha
    result["registry_snapshot"] = {"status": registry_snapshot.get("status"), "output": registry_snapshot.get("output"), "registry_sha256": registry_snapshot.get("registry_sha256")}
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def _latest_batch_row(con: Any, batch_id: str | None = None) -> dict[str, Any]:
    if batch_id:
        df = con.execute(f"SELECT * FROM {TABLE_BATCH} WHERE batch_id = ?", [batch_id]).fetchdf()
    else:
        df = con.execute(f"SELECT * FROM {TABLE_BATCH} ORDER BY applied_at DESC LIMIT 1").fetchdf()
    if df.empty:
        raise RuntimeError("no canonicalization batch found")
    return df.iloc[0].to_dict()


def cmd_audit_materialization(args: argparse.Namespace) -> int:
    cfg = load_json(Path(args.config))
    reg = import_registry(cfg)
    reg_cfg = reg.load_config(resolve_path(cfg["registry_config"]))
    con = connect_db(cfg, read_only=True)
    try:
        batch = _latest_batch_row(con, getattr(args, "batch_id", None))
        audit = audit_fact_materialization(con, reg, reg_cfg, str(batch["batch_id"]))
    finally:
        con.close()
    out = {
        "status": "FAIL_OPTIMUS_CANONICAL_FACT_MATERIALIZATION_AUDIT_V0_5_1" if audit["repair_required"] else "PASS_OPTIMUS_CANONICAL_FACT_MATERIALIZATION_AUDIT_V0_5_1",
        "mutation": False,
        "batch": batch,
        **audit,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 2 if audit["repair_required"] else 0


def cmd_repair_materialization(args: argparse.Namespace) -> int:
    cfg = load_json(Path(args.config))
    actor = str(args.actor or "").strip()
    if not actor:
        print(json.dumps({"status":"FAIL_OPTIMUS_CANONICAL_FACT_MATERIALIZATION_REPAIR_V0_5_1","error":"--actor required","mutation":False}, ensure_ascii=False, indent=2))
        return 2
    reg = import_registry(cfg)
    reg_cfg = reg.load_config(resolve_path(cfg["registry_config"]))
    con = connect_db(cfg, read_only=False)
    init_tables(con)
    batch = _latest_batch_row(con, getattr(args, "batch_id", None))
    expected_manifest = str(batch.get("result_manifest_sha256") or "")
    if not expected_manifest or str(args.approve_result_manifest_sha256) != expected_manifest:
        con.close()
        print(json.dumps({
            "status":"FAIL_OPTIMUS_CANONICAL_FACT_MATERIALIZATION_REPAIR_V0_5_1",
            "error":"RESULT_MANIFEST_SHA256_APPROVAL_MISMATCH",
            "expected": expected_manifest,
            "provided": str(args.approve_result_manifest_sha256),
            "mutation":False,
        }, ensure_ascii=False, indent=2))
        return 2

    audit_before = audit_fact_materialization(con, reg, reg_cfg, str(batch["batch_id"]))
    if not audit_before["repair_required"]:
        con.close()
        print(json.dumps({
            "status":"ALREADY_CLEAN_OPTIMUS_CANONICAL_FACT_MATERIALIZATION_V0_5_1",
            "batch_id":batch["batch_id"],"mutation":False,"audit":audit_before,
        }, ensure_ascii=False, indent=2, default=str))
        return 0

    facts = con.execute(f"SELECT * FROM {TABLE_FACT} WHERE batch_id = ? ORDER BY symbol, fact_id", [batch["batch_id"]]).fetchdf()
    maintenance_before = reg.maintenance_queue_payload(con, reg_cfg)["summary"]
    actions = []
    reconcile_results = []
    con.execute("BEGIN TRANSACTION")
    try:
        for _, row in facts.iterrows():
            actions.append(materialize_fact_evidence(
                con, reg, batch_id=str(batch["batch_id"]), fact_row=row.to_dict(), actor=actor, append_repair_record=True
            ))
        for sym in sorted(set(str(x) for x in facts["symbol"].tolist())):
            reconcile_results.append(reg.reconcile_symbol(con, reg_cfg, sym))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        con.close()
        raise

    audit_after = audit_fact_materialization(con, reg, reg_cfg, str(batch["batch_id"]))
    maintenance_after = reg.maintenance_queue_payload(con, reg_cfg)["summary"]
    stamp = now_cst().strftime("%Y%m%dT%H%M%S+0800")
    out_dir = resolve_path(cfg["output_root"]) / "repair_v0_5_1" / now_cst().strftime("%Y%m%d") / stamp
    out_dir.mkdir(parents=True, exist_ok=False)
    result = {
        "status": "PASS_OPTIMUS_CANONICAL_FACT_MATERIALIZATION_REPAIR_V0_5_1" if not audit_after["repair_required"] else "FAIL_OPTIMUS_CANONICAL_FACT_MATERIALIZATION_REPAIR_V0_5_1_POSTCHECK",
        "batch_id": str(batch["batch_id"]),
        "approved_result_manifest_sha256": expected_manifest,
        "actor": actor,
        "actions": actions,
        "maintenance_before": maintenance_before,
        "maintenance_after": maintenance_after,
        "reconcile": {
            "symbols": len(reconcile_results),
            "changed": sum(int(bool(x.get("changed"))) for x in reconcile_results),
            "results": reconcile_results,
        },
        "audit_before": audit_before,
        "audit_after": audit_after,
        "append_only": True,
        "physical_delete": False,
        "research_only": True,
        "broker": False,
        "production_db": False,
    }
    rp = out_dir / "result.json"
    rp.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    manifest = {
        "schema_version":"OPTIMUS_CANONICAL_FACT_MATERIALIZATION_REPAIR_MANIFEST_V0_5_1",
        "batch_id":str(batch["batch_id"]),
        "files":{"result.json":sha256_file(rp)},
        "immutable":True,
    }
    mp = out_dir / "MANIFEST.json"
    mp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    result_manifest_sha = sha256_file(mp)
    snapshot = reg.snapshot(con, reg_cfg, as_of=now_cst().strftime("%Y%m%d"))
    con.close()
    result["output"] = str(out_dir)
    result["repair_manifest_sha256"] = result_manifest_sha
    result["registry_snapshot"] = {
        "status": snapshot.get("status"), "output": snapshot.get("output"), "registry_sha256": snapshot.get("registry_sha256")
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if not audit_after["repair_required"] else 2


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_json(Path(args.config))
    reg = import_registry(cfg)
    con = connect_db(cfg, read_only=True)
    try:
        exists = con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [TABLE_BATCH]).fetchone()[0]
        if not exists:
            out = {"status":"PASS_OPTIMUS_CANONICALIZATION_V0_5_1_STATUS","batch_count":0,"fact_count":0,"latest_batch":None}
        else:
            batches = con.execute(f"SELECT * FROM {TABLE_BATCH} ORDER BY applied_at DESC").fetchdf()
            facts = con.execute(f"SELECT * FROM {TABLE_FACT} ORDER BY canonicalized_at DESC, fact_id").fetchdf()
            maintenance = reg.maintenance_queue_payload(con, reg.load_config(resolve_path(cfg["registry_config"])))
            latest = None if batches.empty else batches.iloc[0].to_dict()
            out = {
                "status":"PASS_OPTIMUS_CANONICALIZATION_V0_5_1_STATUS",
                "batch_count":int(len(batches)),
                "fact_count":int(len(facts)),
                "canonical_symbol_count":int(facts["symbol"].nunique()) if not facts.empty else 0,
                "materialized_fact_evidence_count":int(con.execute(f"SELECT count(*) FROM {reg.TABLE_EVIDENCE} WHERE parser_version = ?", [FACT_MATERIALIZATION_PARSER]).fetchone()[0]),
                "latest_batch":latest,
                "fact_level_counts":facts["canonical_level"].value_counts().sort_index().to_dict() if not facts.empty else {},
                "fact_tesla_link_counts":facts["canonical_tesla_link_level"].value_counts().sort_index().to_dict() if not facts.empty else {},
                "maintenance":maintenance["summary"],
                "research_only":True,"broker":False,"production_db":False,
            }
    finally:
        con.close()
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="PositionX Optimus Canonicalization V0.5.1")
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    sp = p.add_subparsers(dest="cmd", required=True)
    sp.add_parser("check")
    pp = sp.add_parser("plan")
    pp.add_argument("--plan", default=str(DEFAULT_PLAN))
    ap = sp.add_parser("apply")
    ap.add_argument("--plan", default=str(DEFAULT_PLAN))
    ap.add_argument("--approve-plan-sha256", required=True)
    ap.add_argument("--actor", required=True)
    am = sp.add_parser("audit-materialization")
    am.add_argument("--batch-id")
    rm = sp.add_parser("repair-materialization")
    rm.add_argument("--batch-id")
    rm.add_argument("--approve-result-manifest-sha256", required=True)
    rm.add_argument("--actor", required=True)
    sp.add_parser("status")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.cmd == "check":
        return cmd_check(args)
    if args.cmd == "plan":
        return cmd_plan(args)
    if args.cmd == "apply":
        return cmd_apply(args)
    if args.cmd == "audit-materialization":
        return cmd_audit_materialization(args)
    if args.cmd == "repair-materialization":
        return cmd_repair_materialization(args)
    if args.cmd == "status":
        return cmd_status(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
