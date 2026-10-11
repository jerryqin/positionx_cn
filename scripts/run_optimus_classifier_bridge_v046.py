from __future__ import annotations

"""
PositionX Optimus Classifier Bridge V0.4.6

Purpose
-------
Run the exact frozen V0.4.4 Evidence Proposal Engine over the complete,
candidate-stock-scoped V0.4.6 source corpus.

This is deliberately proposal-only:
- NO CNINFO network query
- NO PDF download
- NO Registry open/write/reconcile
- NO evidence inbox write
- NO canonicalization
- NO broker / order / production DB

The bridge imports the exact frozen V0.4.5 source file by SHA256 and calls only
its local-PDF build_evidence() path.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CST = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]

FROZEN_CLASSIFIER_SCRIPT = ROOT / "scripts" / "run_optimus_evidence_acquisition_v04.py"
FROZEN_CLASSIFIER_SHA256 = "10e5a8288177c76e207fa264f74e18c3a878d95cf191585ca8a576643475a691"
FROZEN_CLASSIFIER_CONTRACT = "OPTIMUS_EVIDENCE_ACQUISITION_V0_4_4"
DEFAULT_CONFIG = ROOT / "config" / "research" / "optimus_evidence_acquisition_v0_4.json"

SOURCE_ROOT = Path(r"D:\positionx_data\research\big_opportunity\optimus\source_recall_v0_4_6\backfill")
OUTPUT_ROOT = Path(r"D:\positionx_data\research\big_opportunity\optimus\classifier_bridge_v0_4_6")


def now_cst() -> datetime:
    return datetime.now(CST)


def iso_now() -> str:
    return now_cst().isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def stable_json(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_obj(x: Any) -> str:
    return hashlib.sha256(stable_json(x).encode("utf-8")).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception as e:
                raise RuntimeError(f"invalid JSONL {path}:{line_no}: {e}") from e
            if not isinstance(obj, dict):
                raise RuntimeError(f"non-object JSONL row {path}:{line_no}")
            out.append(obj)
    return out


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) + "\n" for r in rows)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def latest_valid_source_capture(source_root: Path) -> Path:
    if not source_root.exists():
        raise RuntimeError(f"source root not found: {source_root}")
    manifests = sorted(source_root.glob("*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    reasons = []
    for mp in manifests:
        try:
            obj = load_json(mp)
            validate_source_manifest(obj, mp.parent, check_files=False)
            return mp.parent
        except Exception as e:
            reasons.append(f"{mp}: {e}")
    raise RuntimeError(
        "no valid V0.4.6 source capture found under "
        f"{source_root}; checked={len(manifests)}; reasons={reasons[:5]}"
    )


def normalized_to_cninfo(row: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct the field names expected by frozen build_evidence()."""
    return {
        "announcementId": str(row.get("announcement_id") or ""),
        "secCode": str(row.get("sec_code") or ""),
        "secName": str(row.get("sec_name") or ""),
        "orgId": str(row.get("org_id") or ""),
        "announcementTitle": str(row.get("announcement_title") or ""),
        "announcementTime": row.get("announcement_time"),
        "storageTime": row.get("storage_time"),
        "adjunctUrl": str(row.get("adjunct_url") or ""),
    }


def validate_source_manifest(
    manifest: dict[str, Any],
    capture: Path,
    *,
    check_files: bool,
) -> dict[str, Any]:
    errors: list[str] = []

    if manifest.get("status") != "PASS_OPTIMUS_SOURCE_RECALL_V0_4_6_BACKFILL":
        errors.append(f"status={manifest.get('status')!r}")
    if manifest.get("source_recall_quality") != "CANDIDATE_STOCK_SCOPED_COMPLETE":
        errors.append(f"source_recall_quality={manifest.get('source_recall_quality')!r}")
    if manifest.get("transport_quality") != "COMPLETE":
        errors.append(f"transport_quality={manifest.get('transport_quality')!r}")
    if manifest.get("classifier_quality") != "FROZEN_NOT_INVOKED":
        errors.append(f"classifier_quality={manifest.get('classifier_quality')!r}")

    safety = manifest.get("safety") or {}
    for key in (
        "classifier_invoked", "db_opened", "registry_mutation",
        "canonicalization_mutation", "broker", "orders",
    ):
        if safety.get(key) not in (False, None):
            errors.append(f"safety.{key}={safety.get(key)!r}")

    candidate_results = list(manifest.get("candidate_results") or [])
    if int(manifest.get("candidate_count") or 0) != len(candidate_results):
        errors.append("candidate_count mismatch")
    for c in candidate_results:
        if c.get("all_daily_integrity_pass") is not True:
            errors.append(f"daily integrity failed for {c.get('symbol')}")

    ann_count = int(manifest.get("announcement_count") or 0)
    downloads = list(manifest.get("pdf_downloads") or [])
    pdf_count = int(manifest.get("pdf_download_count") or 0)
    if pdf_count != ann_count or len(downloads) != ann_count:
        errors.append(
            f"pdf/announcement mismatch announcements={ann_count} "
            f"pdf_download_count={pdf_count} pdf_records={len(downloads)}"
        )

    if errors:
        raise RuntimeError("INVALID_SOURCE_MANIFEST: " + "; ".join(errors))

    result = {
        "candidate_count": len(candidate_results),
        "announcement_count": ann_count,
        "pdf_download_count": pdf_count,
    }

    if check_files:
        rows_path = capture / "announcements_all.jsonl"
        rows = read_jsonl(rows_path)
        ids = [str(r.get("announcement_id") or "") for r in rows]
        if any(not x for x in ids):
            raise RuntimeError("blank announcement_id in announcements_all.jsonl")
        if len(ids) != len(set(ids)):
            raise RuntimeError("duplicate announcement_id in announcements_all.jsonl")
        if len(rows) != ann_count:
            raise RuntimeError(
                f"announcement row count mismatch manifest={ann_count} file={len(rows)}"
            )

        dl_by_id = {str(x.get("announcement_id") or ""): x for x in downloads}
        if len(dl_by_id) != len(downloads):
            raise RuntimeError("duplicate/blank announcement_id in pdf_downloads")
        if set(ids) != set(dl_by_id):
            missing_dl = sorted(set(ids) - set(dl_by_id))
            missing_row = sorted(set(dl_by_id) - set(ids))
            raise RuntimeError(
                f"announcement/PDF ID set mismatch missing_download={missing_dl[:10]} "
                f"missing_row={missing_row[:10]}"
            )

        missing_pdfs = []
        hash_mismatch = []
        bad_status = []
        bad_prefix = []
        for r in rows:
            aid = str(r["announcement_id"])
            symbol = str(r.get("symbol") or "")
            rec = dl_by_id[aid]
            if rec.get("status") not in {"DOWNLOADED", "EXISTING"}:
                bad_status.append((aid, rec.get("status")))
            pdf = capture / symbol.replace(".", "_") / "pdfs" / f"{aid}.pdf"
            if not pdf.exists():
                missing_pdfs.append(str(pdf))
                continue
            if sha256_file(pdf) != str(rec.get("sha256") or ""):
                hash_mismatch.append(aid)
            with pdf.open("rb") as f:
                if f.read(4) != b"%PDF":
                    bad_prefix.append(aid)

        if missing_pdfs or hash_mismatch or bad_status or bad_prefix:
            raise RuntimeError(
                "SOURCE_CORPUS_FILE_VALIDATION_FAILED: "
                f"missing_pdfs={len(missing_pdfs)} "
                f"hash_mismatch={hash_mismatch[:10]} "
                f"bad_status={bad_status[:10]} "
                f"bad_prefix={bad_prefix[:10]}"
            )

        result["rows"] = rows
        result["download_by_id"] = dl_by_id
        result["rows_path"] = rows_path

    return result


def load_frozen_classifier(script: Path):
    if not script.exists():
        raise RuntimeError(f"frozen classifier script not found: {script}")
    actual = sha256_file(script)
    if actual != FROZEN_CLASSIFIER_SHA256:
        raise RuntimeError(
            "FROZEN_CLASSIFIER_SHA_MISMATCH: "
            f"actual={actual} expected={FROZEN_CLASSIFIER_SHA256} path={script}"
        )

    spec = importlib.util.spec_from_file_location("positionx_optimus_v044_frozen_bridge", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import frozen classifier: {script}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    if getattr(mod, "V044_PARSER", None) != FROZEN_CLASSIFIER_CONTRACT:
        raise RuntimeError(
            "FROZEN_CLASSIFIER_CONTRACT_MISMATCH: "
            f"{getattr(mod, 'V044_PARSER', None)!r}"
        )
    if not hasattr(mod, "build_evidence"):
        raise RuntimeError("frozen classifier missing build_evidence")

    # Defense in depth: the bridge must never use these mutation/network paths.
    def forbidden(*args, **kwargs):
        raise RuntimeError("FORBIDDEN_PATH_IN_CLASSIFIER_BRIDGE")

    mod.cninfo_post = forbidden
    mod.fetch_url_bytes = forbidden
    mod.ingest_with_registry = forbidden
    mod.load_registry_module = forbidden
    return mod


def proposal_review_row(ev: dict[str, Any]) -> dict[str, Any]:
    acq = ev.get("acquisition") or {}
    return {
        "evidence_id": ev.get("evidence_id"),
        "symbol": ev.get("symbol"),
        "company": ev.get("company"),
        "component_domain": ev.get("component_domain"),
        "evidence_level": ev.get("evidence_level"),
        "tesla_link_level": ev.get("tesla_link_level"),
        "contradiction_kind": ev.get("contradiction_kind"),
        "contradiction_severity": ev.get("contradiction_severity"),
        "event_time": ev.get("event_time"),
        "observed_at": ev.get("observed_at"),
        "source_ref": ev.get("source_ref"),
        "source_sha256": ev.get("source_sha256"),
        "announcement_id": acq.get("announcement_id"),
        "announcement_title": acq.get("announcement_title"),
        "level_rule": acq.get("level_rule"),
        "level_basis": acq.get("level_basis"),
        "tesla_rule": acq.get("tesla_rule"),
        "tesla_basis": acq.get("tesla_basis"),
        "snippets": acq.get("snippets"),
        "review_state": ev.get("review_state"),
        "parser_version": ev.get("parser_version"),
    }


def compact_summary(proposals: list[dict[str, Any]], audits: list[dict[str, Any]]) -> dict[str, Any]:
    by_company: dict[str, dict[str, Any]] = {}
    symbols = sorted({str(a.get("symbol") or "") for a in audits if a.get("symbol")})
    for symbol in symbols:
        aa = [a for a in audits if str(a.get("symbol") or "") == symbol]
        ee = [e for e in proposals if str(e.get("symbol") or "") == symbol]
        company = next((str(a.get("company") or "") for a in aa if a.get("company")), "")
        levels = Counter(int(e.get("evidence_level") or 0) for e in ee)
        tlinks = Counter(int(e.get("tesla_link_level") or 0) for e in ee)
        by_company[symbol] = {
            "company": company,
            "scanned": len(aa),
            "proposals": len(ee),
            "max_evidence_level": max([int(e.get("evidence_level") or 0) for e in ee], default=0),
            "max_tesla_link_level": max([int(e.get("tesla_link_level") or 0) for e in ee], default=0),
            "level_counts": {str(k): v for k, v in sorted(levels.items())},
            "tesla_link_counts": {str(k): v for k, v in sorted(tlinks.items())},
            "hard_contradictions": sum(
                1 for e in ee if str(e.get("contradiction_severity") or "") == "HARD"
            ),
        }
    dispositions = Counter(str(a.get("disposition") or "") for a in audits)
    return {
        "by_company": by_company,
        "disposition_counts": dict(sorted(dispositions.items())),
        "level_counts": {
            str(k): v for k, v in sorted(
                Counter(int(e.get("evidence_level") or 0) for e in proposals).items()
            )
        },
        "tesla_link_counts": {
            str(k): v for k, v in sorted(
                Counter(int(e.get("tesla_link_level") or 0) for e in proposals).items()
            )
        },
    }


def resolve_capture(arg: str | None, source_root: Path) -> Path:
    if arg:
        p = Path(arg)
        if p.name.lower() == "manifest.json":
            p = p.parent
        return p
    return latest_valid_source_capture(source_root)


def run_check(args: argparse.Namespace) -> int:
    classifier = Path(args.classifier_script)
    source_root = Path(args.source_root)
    out = {
        "status": "PASS_OPTIMUS_CLASSIFIER_BRIDGE_V0_4_6_CHECK",
        "classifier_script": str(classifier),
        "classifier_script_exists": classifier.exists(),
        "classifier_script_sha256": sha256_file(classifier) if classifier.exists() else None,
        "expected_classifier_sha256": FROZEN_CLASSIFIER_SHA256,
        "classifier_exact_match": classifier.exists() and sha256_file(classifier) == FROZEN_CLASSIFIER_SHA256,
        "classifier_contract": FROZEN_CLASSIFIER_CONTRACT,
        "source_root": str(source_root),
        "source_root_exists": source_root.exists(),
        "network_queries": 0,
        "proposal_only": True,
        "registry_opened": False,
        "registry_mutation": False,
        "canonicalization_mutation": False,
        "evidence_inbox_write": False,
        "broker": False,
        "orders": False,
    }
    if not out["classifier_exact_match"]:
        out["status"] = "FAIL_OPTIMUS_CLASSIFIER_BRIDGE_V0_4_6_CHECK"
    if source_root.exists():
        try:
            capture = resolve_capture(args.source_capture, source_root)
            manifest = load_json(capture / "manifest.json")
            val = validate_source_manifest(manifest, capture, check_files=False)
            out["source_capture"] = str(capture)
            out["source_manifest_quality"] = "PASS"
            out["candidate_count"] = val["candidate_count"]
            out["announcement_count"] = val["announcement_count"]
        except Exception as e:
            out["status"] = "FAIL_OPTIMUS_CLASSIFIER_BRIDGE_V0_4_6_CHECK"
            out["source_manifest_quality"] = "FAIL"
            out["source_manifest_error"] = repr(e)
    else:
        out["status"] = "FAIL_OPTIMUS_CLASSIFIER_BRIDGE_V0_4_6_CHECK"

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["status"].startswith("PASS_") else 2


def run_bridge(args: argparse.Namespace) -> int:
    classifier_path = Path(args.classifier_script)
    source_root = Path(args.source_root)
    capture = resolve_capture(args.source_capture, source_root)
    source_manifest_path = capture / "manifest.json"
    source_manifest = load_json(source_manifest_path)
    val = validate_source_manifest(source_manifest, capture, check_files=True)
    rows: list[dict[str, Any]] = val["rows"]
    dl_by_id: dict[str, dict[str, Any]] = val["download_by_id"]

    v04 = load_frozen_classifier(classifier_path)
    cfg = v04.load_json(Path(args.config))
    if getattr(v04, "V044_PARSER", None) != FROZEN_CLASSIFIER_CONTRACT:
        raise RuntimeError("classifier contract changed after import")

    observed_at = iso_now()
    capture_id = now_cst().strftime("%Y%m%dT%H%M%S%z")
    out = Path(args.output_root) / capture_id
    out.mkdir(parents=True, exist_ok=False)

    proposals: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    parse_errors: list[dict[str, Any]] = []
    local_read_errors: list[dict[str, Any]] = []

    for i, nr in enumerate(rows, 1):
        aid = str(nr.get("announcement_id") or "")
        symbol = str(nr.get("symbol") or "")
        company = str(nr.get("sec_name") or "")
        pdf = capture / symbol.replace(".", "_") / "pdfs" / f"{aid}.pdf"

        print(
            f"[V0.4.6-BRIDGE] {i}/{len(rows)} {symbol} {company} announcement={aid}",
            file=sys.stderr,
            flush=True,
        )

        raw_row = normalized_to_cninfo(nr)
        item = {"row": raw_row, "symbol": symbol, "matched_keywords": set()}
        ev, au = v04.build_evidence(
            cfg, item, out, observed_at, local_pdf_path=pdf
        )

        au["bridge_source_capture"] = str(capture)
        au["bridge_source_manifest_sha256"] = sha256_file(source_manifest_path)
        au["bridge_source_pdf_sha256"] = dl_by_id[aid]["sha256"]

        if au.get("parse_error"):
            parse_errors.append({
                "announcement_id": aid,
                "symbol": symbol,
                "parse_error": au.get("parse_error"),
            })
        if au.get("download_error"):
            local_read_errors.append({
                "announcement_id": aid,
                "symbol": symbol,
                "download_error": au.get("download_error"),
            })

        if ev is not None:
            acq = ev.setdefault("acquisition", {})
            acq["bridge_source_capture"] = str(capture)
            acq["bridge_source_manifest_sha256"] = sha256_file(source_manifest_path)
            acq["bridge_source_pdf_sha256"] = dl_by_id[aid]["sha256"]
            acq["bridge_network_requery"] = False
            acq["bridge_registry_ingest"] = False
            proposals.append(ev)
        audits.append(au)

    # Fail closed on any local PDF parse/read failure. Still write diagnostic output.
    proposals_file = out / "evidence_proposals_v0_4_4.jsonl"
    audits_file = out / "audit_all_206.jsonl"
    review_file = out / "review_queue.json"
    write_jsonl(proposals_file, proposals)
    write_jsonl(audits_file, audits)
    review_queue = [proposal_review_row(e) for e in proposals]
    write_json(review_file, review_queue)

    summary = compact_summary(proposals, audits)
    status = (
        "PASS_OPTIMUS_CLASSIFIER_BRIDGE_V0_4_6"
        if not parse_errors and not local_read_errors
        else "FAIL_OPTIMUS_CLASSIFIER_BRIDGE_V0_4_6_PARSE_OR_READ_ERROR"
    )

    manifest = {
        "status": status,
        "bridge_version": "OPTIMUS_CLASSIFIER_BRIDGE_V0_4_6",
        "observed_at": observed_at,
        "output": str(out),
        "source_capture": str(capture),
        "source_manifest": str(source_manifest_path),
        "source_manifest_sha256": sha256_file(source_manifest_path),
        "source_status": source_manifest.get("status"),
        "source_recall_quality": source_manifest.get("source_recall_quality"),
        "source_transport_quality": source_manifest.get("transport_quality"),
        "source_candidate_count": val["candidate_count"],
        "source_announcement_count": val["announcement_count"],
        "source_pdf_count": val["pdf_download_count"],
        "classifier_script": str(classifier_path),
        "classifier_script_sha256": sha256_file(classifier_path),
        "classifier_expected_sha256": FROZEN_CLASSIFIER_SHA256,
        "classifier_exact_match": True,
        "classifier_contract": FROZEN_CLASSIFIER_CONTRACT,
        "classifier_method": "CONTEXT_SCOPED_MILESTONE_STRICT_V0_4_4",
        "network_queries": 0,
        "reused_local_pdf_count": len(rows),
        "proposal_count": len(proposals),
        "parse_error_count": len(parse_errors),
        "local_pdf_read_error_count": len(local_read_errors),
        "parse_errors": parse_errors,
        "local_pdf_read_errors": local_read_errors,
        "summary": summary,
        "files": {
            "evidence_proposals": str(proposals_file),
            "evidence_proposals_sha256": sha256_file(proposals_file),
            "audit_all": str(audits_file),
            "audit_all_sha256": sha256_file(audits_file),
            "review_queue": str(review_file),
            "review_queue_sha256": sha256_file(review_file),
        },
        "policy": {
            "proposal_only": True,
            "review_state": "PROVISIONAL",
            "human_review_required": True,
            "auto_canonical_upgrade": False,
            "evidence_inbox_write": False,
            "registry_ingest": False,
            "registry_reconcile": False,
            "canonicalization": False,
        },
        "safety": {
            "research_only": True,
            "network_query": False,
            "db_opened": False,
            "registry_mutation": False,
            "canonicalization_mutation": False,
            "broker": False,
            "orders": False,
            "production_db": False,
        },
    }
    manifest["manifest_payload_sha256"] = sha256_obj(manifest)
    write_json(out / "manifest.json", manifest)

    print(json.dumps({
        "status": status,
        "output": str(out),
        "source_announcement_count": len(rows),
        "reused_local_pdf_count": len(rows),
        "proposal_count": len(proposals),
        "parse_error_count": len(parse_errors),
        "local_pdf_read_error_count": len(local_read_errors),
        "summary": summary,
        "review_queue": str(review_file),
        "manifest": str(out / "manifest.json"),
        "manifest_payload_sha256": manifest["manifest_payload_sha256"],
        "registry_mutation": False,
        "canonicalization_mutation": False,
    }, ensure_ascii=False, indent=2))

    return 0 if status.startswith("PASS_") else 3


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="PositionX Optimus V0.4.6 frozen-classifier bridge"
    )
    ap.add_argument("--classifier-script", default=str(FROZEN_CLASSIFIER_SCRIPT))
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--source-root", default=str(SOURCE_ROOT))
    ap.add_argument("--source-capture")
    ap.add_argument("--output-root", default=str(OUTPUT_ROOT))
    sp = ap.add_subparsers(dest="command", required=True)
    sp.add_parser("check")
    sp.add_parser("run")
    return ap


def main() -> int:
    args = parser().parse_args()
    if args.command == "check":
        return run_check(args)
    if args.command == "run":
        return run_bridge(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
