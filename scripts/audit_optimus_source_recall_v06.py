from __future__ import annotations

"""
PositionX — Optimus Seed Debt Resolution V0.6
R1 Targeted Source Recall Probe

Purpose
-------
Localize known source-recall misses BEFORE artifact download / PDF parsing /
V0.4.4 classification.

Safety contract
---------------
- READ-ONLY with respect to PositionX project state and DuckDB.
- NO broker / order / production DB.
- NO evidence ingest.
- NO registry reconcile.
- NO canonicalization.
- NO classifier invocation or modification.
- Network reads only from CNINFO query/static endpoints.
- Writes only diagnostic artifacts under an explicit output directory.

This probe intentionally mirrors the existing blank CNINFO fulltext-query shape
(pageSize=30, blank column/plate/stock/searchkey/secid/category/trade) so the
result is directly comparable with V0.4.5 backfill behavior.
"""

import argparse
import csv
import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable

CNINFO_API = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_STATIC = "https://static.cninfo.com.cn/"
CNINFO_HDRS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
    "X-Requested-With": "XMLHttpRequest",
}
CST = timezone(timedelta(hours=8))

DEFAULT_ANCHORS = [
    {
        "query_date": "2026-08-25",
        "announcement_id": "1225499629",
        "sec_code": "601100",
        "company": "恒立液压",
        "expected_prior_state": "KNOWN_PRESENT_IN_V045_ARTIFACT",
        "note": "2026H1 earnings briefing notice; positive control from local V0.4.5 log",
    },
    {
        "query_date": "2026-08-25",
        "announcement_id": "1225497703",
        "sec_code": "601100",
        "company": "恒立液压",
        "expected_prior_state": "KNOWN_MISSED_IN_V045_ARTIFACT",
        "note": "2026H1 report summary; authoritative CNINFO static PDF known to exist",
    },
    {
        "query_date": "2026-08-28",
        "announcement_id": "1225521853",
        "sec_code": "601689",
        "company": "拓普集团",
        "expected_prior_state": "KNOWN_MISSED_IN_V045_ARTIFACT",
        "note": "2026H1 report; contains robot actuator small-batch delivery evidence",
    },
    {
        "query_date": "2026-08-28",
        "announcement_id": "1225521772",
        "sec_code": "601689",
        "company": "拓普集团",
        "expected_prior_state": "KNOWN_MISSED_IN_V045_ARTIFACT",
        "note": "2026H1 report summary; same-day corroborating CNINFO document",
    },
]

@dataclass(frozen=True)
class QuerySpec:
    name: str
    query_date: str
    searchkey: str = ""
    stock: str = ""
    secid: str = ""
    sort_name: str = ""
    sort_type: str = ""

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

def clean_title(value: Any) -> str:
    return re.sub(r"</?em>", "", str(value or ""), flags=re.I).strip()

def normalize_symbol(sec_code: Any) -> str | None:
    s = re.sub(r"\D", "", str(sec_code or ""))
    if len(s) != 6:
        return None
    if s.startswith(("60", "68")):
        return s + ".SH"
    if s.startswith(("00", "30")):
        return s + ".SZ"
    if s.startswith(("43", "83", "87", "88", "92")):
        return s + ".BJ"
    return s

def is_a_share_code(sec_code: Any) -> bool:
    s = re.sub(r"\D", "", str(sec_code or ""))
    if len(s) != 6:
        return False
    return s.startswith(("60", "68", "00", "30", "43", "83", "87", "88", "92"))

def cninfo_body(spec: QuerySpec, page_num: int, page_size: int = 30) -> bytes:
    return urllib.parse.urlencode({
        "pageNum": page_num,
        "pageSize": page_size,
        "column": "",
        "tabName": "fulltext",
        "plate": "",
        "stock": spec.stock,
        "searchkey": spec.searchkey,
        "secid": spec.secid,
        "category": "",
        "trade": "",
        "seDate": spec.query_date,
        "sortName": spec.sort_name,
        "sortType": spec.sort_type,
        "isHLtitle": "true",
    }).encode()

def cninfo_post(
    spec: QuerySpec,
    page_num: int,
    *,
    page_size: int = 30,
    tries: int = 4,
    timeout: int = 30,
) -> dict[str, Any]:
    body = cninfo_body(spec, page_num, page_size=page_size)
    req = urllib.request.Request(
        CNINFO_API, data=body, method="POST", headers=CNINFO_HDRS
    )
    last: Exception | None = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = r.read()
            obj = json.loads(payload.decode("utf-8", "replace"))
            if not isinstance(obj, dict):
                raise RuntimeError(f"CNINFO response is not dict: {type(obj)!r}")
            return obj
        except Exception as exc:
            last = exc
            time.sleep(1.2 * (i + 1))
    raise RuntimeError(
        f"cninfo failed query={spec.name} date={spec.query_date} "
        f"page={page_num}: {last!r}"
    )

def save_gzip_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(obj, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    with gzip.open(path, "wb") as f:
        f.write(raw)

def extract_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("announcements") or []
    return [dict(x) for x in rows if isinstance(x, dict)]

def payload_total_pages(payload: dict[str, Any]) -> int:
    for key in ("totalpages", "totalPages", "pages"):
        try:
            v = int(payload.get(key) or 0)
            if v >= 0:
                return v
        except Exception:
            pass
    return 0

def payload_total_count(payload: dict[str, Any]) -> int | None:
    for key in (
        "totalAnnouncement", "totalannouncement", "totalRecord",
        "totalrecord", "total", "totalCount", "totalcount"
    ):
        try:
            raw = payload.get(key)
            if raw is not None and str(raw) != "":
                return int(raw)
        except Exception:
            pass
    return None

def row_id(row: dict[str, Any]) -> str:
    return str(row.get("announcementId") or row.get("announcementid") or "")

def row_code(row: dict[str, Any]) -> str:
    return str(row.get("secCode") or row.get("seccode") or "")

def row_name(row: dict[str, Any]) -> str:
    return str(row.get("secName") or row.get("secname") or "")

def row_org_id(row: dict[str, Any]) -> str:
    return str(row.get("orgId") or row.get("orgid") or "")

def row_view(row: dict[str, Any], page_num: int, query_name: str) -> dict[str, Any]:
    code = row_code(row)
    return {
        "query_name": query_name,
        "page_num": page_num,
        "announcement_id": row_id(row),
        "sec_code": code,
        "normalized_symbol": normalize_symbol(code),
        "sec_name": row_name(row),
        "org_id": row_org_id(row),
        "announcement_title": clean_title(row.get("announcementTitle")),
        "announcement_time": row.get("announcementTime"),
        "storage_time": row.get("storageTime"),
        "adjunct_url": row.get("adjunctUrl"),
        "valid_a_share": is_a_share_code(code),
    }

def collect_query(
    spec: QuerySpec,
    *,
    out_dir: Path,
    delay: float,
    page_size: int,
    save_raw_pages: bool,
    max_pages: int | None = None,
) -> dict[str, Any]:
    started_at = iso_now()
    first = cninfo_post(spec, 1, page_size=page_size)
    first_total_pages = payload_total_pages(first)
    if first_total_pages <= 0:
        first_total_pages = 1 if extract_rows(first) else 0
    request_pages = first_total_pages
    if max_pages is not None:
        request_pages = min(request_pages, max_pages)

    payloads: list[tuple[int, dict[str, Any]]] = []
    if request_pages > 0:
        payloads.append((1, first))
        if save_raw_pages:
            save_gzip_json(
                out_dir / "raw_pages" / spec.name / "page_0001.json.gz", first
            )
    for p in range(2, request_pages + 1):
        if delay > 0:
            time.sleep(delay)
        payload = cninfo_post(spec, p, page_size=page_size)
        payloads.append((p, payload))
        if save_raw_pages:
            save_gzip_json(
                out_dir / "raw_pages" / spec.name / f"page_{p:04d}.json.gz",
                payload,
            )

    flat: list[dict[str, Any]] = []
    page_sizes: list[int] = []
    total_pages_seen: list[int] = []
    total_counts_seen: list[int] = []
    ids_by_page: dict[int, list[str]] = {}

    for p, payload in payloads:
        rows = extract_rows(payload)
        page_sizes.append(len(rows))
        tp = payload_total_pages(payload)
        if tp:
            total_pages_seen.append(tp)
        tc = payload_total_count(payload)
        if tc is not None:
            total_counts_seen.append(tc)
        ids = []
        for row in rows:
            flat.append(row_view(row, p, spec.name))
            ids.append(row_id(row))
        ids_by_page[p] = ids

    ids = [r["announcement_id"] for r in flat if r["announcement_id"]]
    counts = Counter(ids)
    duplicate_ids = sorted(k for k, v in counts.items() if v > 1)
    unique_ids = set(ids)
    same_id_multiple_pages = []
    pages_by_id: dict[str, set[int]] = defaultdict(set)
    for p, pids in ids_by_page.items():
        for aid in pids:
            if aid:
                pages_by_id[aid].add(p)
    for aid, pages in pages_by_id.items():
        if len(pages) > 1:
            same_id_multiple_pages.append({"announcement_id": aid, "pages": sorted(pages)})

    rows_path = out_dir / "rows" / f"{spec.name}.jsonl"
    rows_path.parent.mkdir(parents=True, exist_ok=True)
    with rows_path.open("w", encoding="utf-8") as f:
        for row in flat:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    summary = {
        "query": asdict(spec),
        "started_at": started_at,
        "finished_at": iso_now(),
        "reported_total_pages_first": first_total_pages,
        "reported_total_pages_values": sorted(set(total_pages_seen)),
        "reported_total_count_values": sorted(set(total_counts_seen)),
        "requested_pages": request_pages,
        "page_size_param": page_size,
        "page_sizes": page_sizes,
        "rows_seen": len(flat),
        "unique_announcement_ids": len(unique_ids),
        "duplicate_row_count": max(0, len(ids) - len(unique_ids)),
        "duplicate_id_count": len(duplicate_ids),
        "duplicate_ratio": (
            (len(ids) - len(unique_ids)) / len(ids) if ids else 0.0
        ),
        "same_id_seen_on_multiple_pages_count": len(same_id_multiple_pages),
        "same_id_seen_on_multiple_pages_sample": same_id_multiple_pages[:100],
        "last_page_size": page_sizes[-1] if page_sizes else 0,
        "last_page_full": bool(page_sizes and page_sizes[-1] == page_size),
        "valid_a_share_rows": sum(1 for r in flat if r["valid_a_share"]),
        "rows_file": str(rows_path),
        "rows_file_sha256": sha256_file(rows_path),
    }
    return {"summary": summary, "rows": flat}

def static_pdf_probe(query_date: str, announcement_id: str, timeout: int = 20) -> dict[str, Any]:
    url = f"{CNINFO_STATIC}finalpage/{query_date}/{announcement_id}.PDF"
    headers = {
        "User-Agent": CNINFO_HDRS["User-Agent"],
        "Referer": CNINFO_HDRS["Referer"],
        "Range": "bytes=0-1023",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            sample = r.read(1024)
            status = getattr(r, "status", None) or r.getcode()
            content_type = r.headers.get("Content-Type")
            content_length = r.headers.get("Content-Length")
        return {
            "url": url,
            "reachable": True,
            "http_status": int(status),
            "content_type": content_type,
            "content_length_header": content_length,
            "sample_is_pdf": sample.startswith(b"%PDF"),
            "sample_sha256": hashlib.sha256(sample).hexdigest(),
        }
    except urllib.error.HTTPError as exc:
        return {
            "url": url,
            "reachable": False,
            "http_status": exc.code,
            "error": repr(exc),
        }
    except Exception as exc:
        return {
            "url": url,
            "reachable": False,
            "http_status": None,
            "error": repr(exc),
        }

def find_anchor(rows: Iterable[dict[str, Any]], announcement_id: str) -> dict[str, Any] | None:
    target = str(announcement_id)
    for row in rows:
        if str(row.get("announcement_id")) == target:
            return row
    return None

def first_org_id_for_company(rows: Iterable[dict[str, Any]], sec_code: str) -> str | None:
    for row in rows:
        if str(row.get("sec_code")) == str(sec_code) and row.get("org_id"):
            return str(row["org_id"])
    return None

def read_seed_v02_symbols(project_root: Path) -> set[str]:
    path = project_root / "config" / "research" / "optimus_candidate_seed_v0_2.csv"
    if not path.exists():
        return set()
    symbols: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw = (
                row.get("symbol") or row.get("ticker") or row.get("code")
                or row.get("sec_code") or row.get("stock_code") or ""
            )
            raw = str(raw).strip()
            if not raw:
                continue
            if "." in raw:
                symbols.add(raw.upper())
            else:
                n = normalize_symbol(raw)
                if n:
                    symbols.add(n.upper())
    return symbols

def diagnose_anchor(
    anchor: dict[str, Any],
    *,
    blank_rows: list[dict[str, Any]],
    keyword_rows: list[dict[str, Any]],
    stock_rows: list[dict[str, Any]] | None,
    static_probe: dict[str, Any],
    seed_v02: set[str],
) -> dict[str, Any]:
    aid = str(anchor["announcement_id"])
    blank = find_anchor(blank_rows, aid)
    keyword = find_anchor(keyword_rows, aid)
    stock = find_anchor(stock_rows or [], aid)
    target_symbol = normalize_symbol(anchor["sec_code"])
    result = dict(anchor)
    result.update({
        "target_symbol": target_symbol,
        "in_seed_v02": bool(target_symbol and target_symbol.upper() in seed_v02),
        "raw_global_present": blank is not None,
        "raw_keyword_present": keyword is not None,
        "raw_stock_scoped_present": stock is not None if stock_rows is not None else None,
        "raw_global_page": blank.get("page_num") if blank else None,
        "raw_global_sec_code": blank.get("sec_code") if blank else None,
        "raw_global_normalized_symbol": blank.get("normalized_symbol") if blank else None,
        "raw_global_valid_a_share": blank.get("valid_a_share") if blank else None,
        "raw_keyword_page": keyword.get("page_num") if keyword else None,
        "raw_stock_page": stock.get("page_num") if stock else None,
        "static_pdf": static_probe,
        "classifier_called": False,
        "download_attempted_by_probe": False,
        "db_opened": False,
        "state_mutation": False,
    })

    if blank is not None:
        if blank.get("normalized_symbol") != target_symbol:
            diagnosis = "Q1_NORMALIZATION_MISMATCH"
        elif not blank.get("valid_a_share"):
            diagnosis = "Q1_A_SHARE_VALIDITY_FILTER_RISK"
        else:
            diagnosis = "RAW_GLOBAL_PRESENT_DOWNSTREAM_REVIEW_REQUIRED"
    elif keyword is not None:
        diagnosis = "Q0_GLOBAL_BLANK_QUERY_RECALL_GAP"
    elif stock is not None:
        diagnosis = "Q0_GLOBAL_BLANK_QUERY_RECALL_GAP_STOCK_SCOPE_RECOVERS"
    elif static_probe.get("reachable"):
        diagnosis = "Q0_CNINFO_QUERY_RECALL_OR_INDEXING_GAP"
    else:
        diagnosis = "SOURCE_EXISTENCE_NOT_CONFIRMED_BY_PROBE"

    result["diagnosis"] = diagnosis
    return result

def build_query_specs(
    dates: list[str],
    anchors: list[dict[str, Any]],
    *,
    include_sorted_variant: bool,
) -> list[QuerySpec]:
    specs: list[QuerySpec] = []
    for date in dates:
        specs.append(QuerySpec(name=f"{date}_blank", query_date=date))
        if include_sorted_variant:
            specs.append(QuerySpec(
                name=f"{date}_blank_time_desc",
                query_date=date,
                sort_name="time",
                sort_type="desc",
            ))
        companies = sorted({
            a["company"] for a in anchors if a["query_date"] == date
        })
        for company in companies:
            safe = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", company)
            specs.append(QuerySpec(
                name=f"{date}_kw_{safe}",
                query_date=date,
                searchkey=company,
            ))
    return specs

def run(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    anchors = list(DEFAULT_ANCHORS)
    if args.anchor_file:
        anchors = json.loads(Path(args.anchor_file).read_text(encoding="utf-8"))
        if not isinstance(anchors, list):
            raise SystemExit("--anchor-file must contain a JSON array")

    if args.date:
        date_set = set(args.date)
        anchors = [a for a in anchors if a["query_date"] in date_set]
    dates = sorted({a["query_date"] for a in anchors})
    if not dates:
        raise SystemExit("no anchors selected")

    capture_id = now_cst().strftime("%Y%m%dT%H%M%S%z")
    out_root = Path(args.out_root)
    out_dir = out_root / capture_id
    out_dir.mkdir(parents=True, exist_ok=False)

    seed_v02 = read_seed_v02_symbols(project_root)

    manifest = {
        "probe": "OPTIMUS_R1_SOURCE_RECALL_PROBE_V0_6",
        "capture_id": capture_id,
        "observed_at": iso_now(),
        "project_root": str(project_root),
        "dates": dates,
        "anchors": anchors,
        "safety": {
            "read_only": True,
            "db_opened": False,
            "broker": False,
            "orders": False,
            "production_db": False,
            "evidence_ingest": False,
            "registry_reconcile": False,
            "canonicalization": False,
            "classifier_invoked": False,
        },
        "query_contract": {
            "page_size": args.page_size,
            "blank_query_matches_v045_shape": True,
            "classifier_contract_unchanged": "OPTIMUS_EVIDENCE_ACQUISITION_V0_4_4",
        },
    }

    query_results: dict[str, dict[str, Any]] = {}
    specs = build_query_specs(
        dates, anchors, include_sorted_variant=args.sorted_variant
    )
    for spec in specs:
        print(f"[R1] query {spec.name}")
        try:
            qr = collect_query(
                spec,
                out_dir=out_dir,
                delay=args.delay,
                page_size=args.page_size,
                save_raw_pages=not args.no_raw_pages,
                max_pages=args.max_pages,
            )
            query_results[spec.name] = qr
            print(json.dumps(qr["summary"], ensure_ascii=False, indent=2))
        except Exception as exc:
            query_results[spec.name] = {
                "summary": {
                    "query": asdict(spec),
                    "error": repr(exc),
                    "finished_at": iso_now(),
                },
                "rows": [],
            }
            print(f"[R1] ERROR {spec.name}: {exc!r}", file=sys.stderr)

    # Derive stock-scoped probes from company-keyword rows using actual orgId.
    stock_specs: list[QuerySpec] = []
    for anchor in anchors:
        date = anchor["query_date"]
        company = anchor["company"]
        safe = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", company)
        kw_name = f"{date}_kw_{safe}"
        kw_rows = query_results.get(kw_name, {}).get("rows", [])
        org_id = first_org_id_for_company(kw_rows, anchor["sec_code"])
        if org_id:
            stock_token = f"{anchor['sec_code']},{org_id}"
            stock_name = f"{date}_stock_{anchor['sec_code']}"
            if all(s.name != stock_name for s in stock_specs):
                stock_specs.append(QuerySpec(
                    name=stock_name,
                    query_date=date,
                    stock=stock_token,
                ))

    for spec in stock_specs:
        print(f"[R1] query {spec.name}")
        try:
            qr = collect_query(
                spec,
                out_dir=out_dir,
                delay=args.delay,
                page_size=args.page_size,
                save_raw_pages=not args.no_raw_pages,
                max_pages=args.max_pages,
            )
            query_results[spec.name] = qr
            print(json.dumps(qr["summary"], ensure_ascii=False, indent=2))
        except Exception as exc:
            query_results[spec.name] = {
                "summary": {
                    "query": asdict(spec),
                    "error": repr(exc),
                    "finished_at": iso_now(),
                },
                "rows": [],
            }
            print(f"[R1] ERROR {spec.name}: {exc!r}", file=sys.stderr)

    anchor_results = []
    for anchor in anchors:
        date = anchor["query_date"]
        company = anchor["company"]
        safe = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", company)
        blank_rows = query_results.get(f"{date}_blank", {}).get("rows", [])
        keyword_rows = query_results.get(f"{date}_kw_{safe}", {}).get("rows", [])
        stock_name = f"{date}_stock_{anchor['sec_code']}"
        stock_rows = (
            query_results.get(stock_name, {}).get("rows")
            if stock_name in query_results else None
        )
        sp = static_pdf_probe(date, str(anchor["announcement_id"]))
        ar = diagnose_anchor(
            anchor,
            blank_rows=blank_rows,
            keyword_rows=keyword_rows,
            stock_rows=stock_rows,
            static_probe=sp,
            seed_v02=seed_v02,
        )

        # Optional sorted blank result is diagnostic only; it never overwrites the
        # exact V0.4.5 blank-query result.
        sorted_rows = query_results.get(
            f"{date}_blank_time_desc", {}
        ).get("rows", [])
        sorted_hit = find_anchor(sorted_rows, str(anchor["announcement_id"]))
        ar["raw_sorted_global_present"] = sorted_hit is not None
        ar["raw_sorted_global_page"] = (
            sorted_hit.get("page_num") if sorted_hit else None
        )
        if (
            ar["diagnosis"].startswith("Q0_")
            and not ar["raw_global_present"]
            and ar["raw_sorted_global_present"]
        ):
            ar["diagnosis"] = "Q0_UNSORTED_GLOBAL_QUERY_RECALL_OR_PAGINATION_GAP"
        anchor_results.append(ar)

    summaries = {
        name: data["summary"] for name, data in query_results.items()
    }
    diag_counts = Counter(x["diagnosis"] for x in anchor_results)

    final = {
        **manifest,
        "finished_at": iso_now(),
        "seed_v02_symbol_count": len(seed_v02),
        "query_summaries": summaries,
        "anchor_results": anchor_results,
        "diagnosis_counts": dict(diag_counts),
        "interpretation_contract": {
            "query_success_does_not_prove_source_recall_completeness": True,
            "raw_global_absent_but_keyword_or_stock_present":
                "Q0 global blank-query recall gap",
            "raw_global_present_but_symbol_invalid":
                "Q1 normalization/validity-filter investigation",
            "static_pdf_reachable_but_query_absent":
                "Q0 query/indexing gap; classifier is not implicated",
            "classifier_defect_can_be_inferred_here": False,
        },
    }

    result_path = out_dir / "result.json"
    result_path.write_text(
        json.dumps(final, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    sha = sha256_file(result_path)
    (out_dir / "RESULT_SHA256.txt").write_text(
        f"{sha}  result.json\n", encoding="utf-8"
    )

    print("=" * 100)
    print("OPTIMUS R1 SOURCE RECALL PROBE V0.6")
    print("output:", out_dir)
    print("result_sha256:", sha)
    print("diagnosis_counts:", json.dumps(dict(diag_counts), ensure_ascii=False))
    print("state_mutation: false")
    print("db_opened: false")
    print("classifier_invoked: false")
    print("=" * 100)
    return 0

def make_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Read-only Optimus V0.6 R1 source-recall diagnostic probe."
    )
    ap.add_argument(
        "--project-root",
        default=r"D:\positionx_cn",
        help="PositionX project root; used only to read seed V0.2 CSV.",
    )
    ap.add_argument(
        "--out-root",
        default=r"D:\positionx_data\research\big_opportunity\optimus\source_recall_probe_v0_6",
    )
    ap.add_argument(
        "--date",
        action="append",
        help="Limit to one or more anchor dates, e.g. --date 2026-08-28",
    )
    ap.add_argument(
        "--anchor-file",
        help="Optional JSON array replacing built-in anchors.",
    )
    ap.add_argument("--page-size", type=int, default=30)
    ap.add_argument("--delay", type=float, default=0.6)
    ap.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Diagnostic dev cap only. Omit for a valid completeness probe.",
    )
    ap.add_argument(
        "--sorted-variant",
        action="store_true",
        help="Also query blank fulltext with sortName=time/sortType=desc.",
    )
    ap.add_argument(
        "--no-raw-pages",
        action="store_true",
        help="Do not persist gzipped raw page payloads. Not recommended for audit.",
    )
    return ap

def main() -> int:
    return run(make_parser().parse_args())

if __name__ == "__main__":
    raise SystemExit(main())
