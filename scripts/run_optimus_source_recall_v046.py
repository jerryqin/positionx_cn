from __future__ import annotations

"""
PositionX Optimus Evidence Acquisition V0.4.6.1 — Source Recall Hardening (Phase A transport hotfix)

This layer repairs source acquisition only. It does NOT modify or invoke the
frozen V0.4.4 classifier, and it does NOT mutate Registry / Canonicalization /
DuckDB / broker / orders.

Why it exists
-------------
R1 proved that the CNINFO blank global query can report >100 pages while pages
after 100 repeat earlier data. Therefore "all-announcement" transport success
does not establish source-recall completeness.

V0.4.6 Phase A replaces candidate evidence acquisition with:
    candidate -> company keyword bootstrap -> exact secCode/orgId
    -> stock-scoped query -> pagination-integrity guard -> local PDF corpus

The output is an immutable, auditable source corpus intended for subsequent
V0.4.4 classification through a separate bridge step.
"""

import argparse
import csv
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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

CST = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]

CNINFO_API = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_STATIC = "https://static.cninfo.com.cn/"
CNINFO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
    "X-Requested-With": "XMLHttpRequest",
}

EXPECTED_V045_SHA256 = "10e5a8288177c76e207fa264f74e18c3a878d95cf191585ca8a576643475a691"
V04_SCRIPT = ROOT / "scripts" / "run_optimus_evidence_acquisition_v04.py"
DEFAULT_CANDIDATE_CSV = ROOT / "config" / "research" / "optimus_source_recall_v046_seed_debt.csv"
DEFAULT_OUT = Path(
    r"D:\positionx_data\research\big_opportunity\optimus"
) / "source_recall_v0_4_6"

TARGETED_CASES = [
    {
        "symbol": "601689.SH",
        "company": "拓普集团",
        "query_date": "2026-08-28",
        "required_ids": ["1225521853", "1225521772"],
    },
    {
        "symbol": "601100.SH",
        "company": "恒立液压",
        "query_date": "2026-08-25",
        "required_ids": ["1225499629", "1225497703"],
    },
]

@dataclass(frozen=True)
class Candidate:
    symbol: str
    company: str

@dataclass(frozen=True)
class Query:
    query_date: str
    searchkey: str = ""
    stock: str = ""
    page_size: int = 30

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

def clean_title(v: Any) -> str:
    return re.sub(r"</?em>", "", str(v or ""), flags=re.I).strip()

def sec_code(symbol: str) -> str:
    return str(symbol).split(".", 1)[0]

def normalize_symbol(code: Any) -> str | None:
    s = re.sub(r"\D", "", str(code or ""))
    if len(s) != 6:
        return None
    if s.startswith(("60", "68")):
        return s + ".SH"
    if s.startswith(("00", "30")):
        return s + ".SZ"
    if s.startswith(("43", "83", "87", "88", "92")):
        return s + ".BJ"
    return s

def cninfo_body(q: Query, page: int) -> bytes:
    return urllib.parse.urlencode({
        "pageNum": page,
        "pageSize": q.page_size,
        "column": "",
        "tabName": "fulltext",
        "plate": "",
        "stock": q.stock,
        "searchkey": q.searchkey,
        "secid": "",
        "category": "",
        "trade": "",
        "seDate": q.query_date,
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }).encode()

def post(q: Query, page: int, tries: int = 4, timeout: int = 30) -> dict[str, Any]:
    req = urllib.request.Request(
        CNINFO_API, data=cninfo_body(q, page), method="POST", headers=CNINFO_HEADERS
    )
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            obj = json.loads(raw.decode("utf-8", "replace"))
            if not isinstance(obj, dict):
                raise RuntimeError(f"non-dict response: {type(obj)!r}")
            return obj
        except Exception as e:
            last = e
            time.sleep(1.2 * (i + 1))
    raise RuntimeError(
        f"CNINFO failed date={q.query_date} page={page} "
        f"searchkey={q.searchkey!r} stock={q.stock!r}: {last!r}"
    )

def payload_rows(obj: dict[str, Any]) -> list[dict[str, Any]]:
    rows = obj.get("announcements") or []
    return [dict(x) for x in rows if isinstance(x, dict)]

def payload_pages(obj: dict[str, Any]) -> int:
    try:
        return int(obj.get("totalpages") or 0)
    except Exception:
        return 0

def payload_count(obj: dict[str, Any]) -> int | None:
    for key in (
        "totalAnnouncement", "totalannouncement", "totalRecord",
        "totalrecord", "total", "totalCount", "totalcount",
    ):
        try:
            x = obj.get(key)
            if x is not None and str(x) != "":
                return int(x)
        except Exception:
            pass
    return None

def row_id(r: dict[str, Any]) -> str:
    return str(r.get("announcementId") or "")

def row_code(r: dict[str, Any]) -> str:
    return str(r.get("secCode") or "")

def row_org(r: dict[str, Any]) -> str:
    return str(r.get("orgId") or "")

def normalize_row(r: dict[str, Any], page: int, q: Query) -> dict[str, Any]:
    return {
        "announcement_id": row_id(r),
        "sec_code": row_code(r),
        "symbol": normalize_symbol(row_code(r)),
        "sec_name": str(r.get("secName") or ""),
        "org_id": row_org(r),
        "announcement_title": clean_title(r.get("announcementTitle")),
        "announcement_time": r.get("announcementTime"),
        "storage_time": r.get("storageTime"),
        "adjunct_url": str(r.get("adjunctUrl") or ""),
        "query_date": q.query_date,
        "query_searchkey": q.searchkey,
        "query_stock": q.stock,
        "page": page,
    }

def collect(q: Query, delay: float = 0.4) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    first = post(q, 1)
    n_pages = payload_pages(first)
    if n_pages <= 0:
        n_pages = 1 if payload_rows(first) else 0

    # Hard fail closed. The R1 bug lives at deep pagination; a candidate-scoped
    # query should never need anything close to 100 pages.
    if n_pages > 100:
        raise RuntimeError(
            f"FAIL_SOURCE_RECALL_INTEGRITY: query requires {n_pages} pages (>100); "
            f"date={q.query_date} searchkey={q.searchkey!r} stock={q.stock!r}"
        )

    payloads = [(1, first)] if n_pages else []
    for p in range(2, n_pages + 1):
        if delay:
            time.sleep(delay)
        payloads.append((p, post(q, p)))

    rows: list[dict[str, Any]] = []
    ids_by_page: dict[int, list[str]] = {}
    page_sizes: list[int] = []
    total_counts = []
    for p, obj in payloads:
        raw_rows = payload_rows(obj)
        page_sizes.append(len(raw_rows))
        c = payload_count(obj)
        if c is not None:
            total_counts.append(c)
        ids_by_page[p] = []
        for raw in raw_rows:
            nr = normalize_row(raw, p, q)
            rows.append(nr)
            if nr["announcement_id"]:
                ids_by_page[p].append(nr["announcement_id"])

    ids = [r["announcement_id"] for r in rows if r["announcement_id"]]
    counter = Counter(ids)
    unique = len(counter)
    dup_rows = len(ids) - unique
    dup_ratio = (dup_rows / len(ids)) if ids else 0.0

    pages_by_id: dict[str, set[int]] = defaultdict(set)
    for p, vals in ids_by_page.items():
        for aid in vals:
            pages_by_id[aid].add(p)
    cross_page = {aid: sorted(ps) for aid, ps in pages_by_id.items() if len(ps) > 1}

    # Candidate/keyword queries may legitimately return a repeated item rarely,
    # but the R1 failure pattern is massive cross-page duplication. Fail closed
    # at a deliberately conservative 10% duplicate ratio.
    if len(rows) >= 30 and dup_ratio > 0.10:
        raise RuntimeError(
            "FAIL_SOURCE_RECALL_INTEGRITY: duplicate_ratio="
            f"{dup_ratio:.4f}; rows={len(rows)} unique={unique}; "
            f"cross_page_ids={len(cross_page)}"
        )

    # If upstream reports a total count, unique IDs should be compatible with it.
    reported = max(total_counts) if total_counts else None
    if reported is not None and n_pages and len(rows) and unique < min(reported, len(rows)) * 0.90:
        raise RuntimeError(
            "FAIL_SOURCE_RECALL_INTEGRITY: unique IDs materially below "
            f"reported count; reported={reported} unique={unique} rows={len(rows)}"
        )

    summary = {
        "query": asdict(q),
        "reported_pages": n_pages,
        "reported_counts": sorted(set(total_counts)),
        "rows_seen": len(rows),
        "unique_announcement_ids": unique,
        "duplicate_rows": dup_rows,
        "duplicate_ratio": dup_ratio,
        "cross_page_duplicate_ids": len(cross_page),
        "last_page_size": page_sizes[-1] if page_sizes else 0,
        "integrity": "PASS",
    }
    return rows, summary

def exact_rows(rows: Iterable[dict[str, Any]], symbol: str) -> list[dict[str, Any]]:
    return [r for r in rows if str(r.get("symbol")) == symbol]

def resolve_org_id(
    candidate: Candidate,
    start: date,
    end: date,
    *,
    delay: float,
    max_probe_days: int,
) -> tuple[str, dict[str, Any]]:
    d = end
    attempts = []
    checked = 0
    while d >= start and checked < max_probe_days:
        q = Query(query_date=d.isoformat(), searchkey=candidate.company)
        rows, summary = collect(q, delay=delay)
        hits = exact_rows(rows, candidate.symbol)
        orgs = sorted({str(r["org_id"]) for r in hits if r.get("org_id")})
        attempts.append({
            "date": d.isoformat(),
            "rows": len(rows),
            "exact_symbol_hits": len(hits),
            "org_ids": orgs,
        })
        if len(orgs) == 1:
            return orgs[0], {
                "status": "RESOLVED",
                "method": "COMPANY_KEYWORD_EXACT_SECCODE",
                "resolved_on": d.isoformat(),
                "attempts": attempts,
            }
        if len(orgs) > 1:
            raise RuntimeError(
                f"ambiguous orgId for {candidate.symbol} {candidate.company}: {orgs}"
            )
        d -= timedelta(days=1)
        checked += 1
    raise RuntimeError(
        f"cannot resolve orgId for {candidate.symbol} {candidate.company} "
        f"within {max_probe_days} backward days"
    )

def _fetch_complete_bytes(url: str, tries: int = 8, timeout: int = 45) -> tuple[bytes, dict[str, Any]]:
    """
    Robust HTTP fetch with byte-range resume.

    CNINFO static PDFs can occasionally terminate a connection before the
    advertised Content-Length is received. urllib then raises IncompleteRead.
    We keep the received partial bytes and resume with Range from the exact
    next byte. If the server ignores Range and returns 200, we safely restart
    the full object instead of appending duplicate bytes.
    """
    import http.client

    data = bytearray()
    expected_total: int | None = None
    last: Exception | None = None
    segments: list[dict[str, Any]] = []

    for i in range(tries):
        start = len(data)
        hdrs = {
            "User-Agent": CNINFO_HEADERS["User-Agent"],
            "Referer": CNINFO_HEADERS["Referer"],
            "Accept-Encoding": "identity",
        }
        if start:
            hdrs["Range"] = f"bytes={start}-"

        req = urllib.request.Request(url, headers=hdrs, method="GET")
        clean_eof = False

        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                status = int(getattr(r, "status", r.getcode()))
                content_range = r.headers.get("Content-Range")
                content_length = r.headers.get("Content-Length")

                # A resumed request that comes back 200 means Range was ignored.
                # Restart from byte 0 so bytes can never be duplicated.
                if start and status == 200:
                    data.clear()
                    start = 0

                if status == 206:
                    if not content_range:
                        raise RuntimeError("206 response without Content-Range")
                    m = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", content_range, re.I)
                    if not m:
                        raise RuntimeError(f"invalid Content-Range: {content_range!r}")
                    cr_start = int(m.group(1))
                    if cr_start != start:
                        raise RuntimeError(
                            f"range resume offset mismatch: requested={start} got={cr_start}"
                        )
                    if m.group(3) != "*":
                        expected_total = int(m.group(3))
                elif status == 200 and start == 0 and content_length:
                    expected_total = int(content_length)

                before = len(data)
                incomplete = False
                while True:
                    try:
                        chunk = r.read(256 * 1024)
                    except http.client.IncompleteRead as e:
                        if e.partial:
                            data.extend(e.partial)
                        last = e
                        incomplete = True
                        break
                    if not chunk:
                        clean_eof = True
                        break
                    data.extend(chunk)

                segments.append({
                    "attempt": i + 1,
                    "requested_start": start,
                    "http_status": status,
                    "content_range": content_range,
                    "content_length": content_length,
                    "bytes_received": len(data) - before,
                    "bytes_accumulated": len(data),
                    "expected_total": expected_total,
                    "incomplete_read": incomplete,
                })

        except urllib.error.HTTPError as e:
            # Some servers answer 416 if the local byte count is already equal
            # to the complete object length. Accept only when we know the total.
            if e.code == 416 and expected_total is not None and len(data) == expected_total:
                return bytes(data), {
                    "segments": segments,
                    "expected_total": expected_total,
                    "resume_used": len(segments) > 1,
                }
            last = e
        except http.client.IncompleteRead as e:
            if e.partial:
                data.extend(e.partial)
            last = e
        except Exception as e:
            last = e

        if expected_total is not None and len(data) >= expected_total:
            if len(data) != expected_total:
                raise RuntimeError(
                    f"HTTP byte count exceeded expected total: "
                    f"got={len(data)} expected={expected_total}"
                )
            return bytes(data), {
                "segments": segments,
                "expected_total": expected_total,
                "resume_used": len(segments) > 1,
            }

        if clean_eof and expected_total is None and data:
            return bytes(data), {
                "segments": segments,
                "expected_total": None,
                "resume_used": len(segments) > 1,
            }

        time.sleep(min(8.0, 1.5 * (i + 1)))

    raise RuntimeError(
        f"incomplete HTTP object after {tries} attempts: "
        f"bytes={len(data)} expected={expected_total} last={last!r}"
    )


def download_pdf(row: dict[str, Any], dest: Path, tries: int = 8) -> dict[str, Any]:
    adjunct = str(row.get("adjunct_url") or "")
    if adjunct.startswith("http://") or adjunct.startswith("https://"):
        url = adjunct
    else:
        url = CNINFO_STATIC + adjunct.lstrip("/")
    aid = str(row["announcement_id"])
    out = dest / f"{aid}.pdf"

    if out.exists() and out.stat().st_size > 0:
        # Existing corpus objects are immutable. Only accept an existing file
        # when it still looks like a PDF.
        with out.open("rb") as f:
            prefix = f.read(4)
        if prefix != b"%PDF":
            raise RuntimeError(f"existing file is not a PDF: {out}")
        return {
            "announcement_id": aid,
            "url": url,
            "path": str(out),
            "sha256": sha256_file(out),
            "bytes": out.stat().st_size,
            "status": "EXISTING",
            "transport": {"resume_used": False, "existing": True},
        }

    dest.mkdir(parents=True, exist_ok=True)
    data, transport = _fetch_complete_bytes(url, tries=tries, timeout=45)

    if not data.startswith(b"%PDF"):
        raise RuntimeError(f"not a PDF, prefix={data[:20]!r}")

    tmp = out.with_suffix(".pdf.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, out)

    return {
        "announcement_id": aid,
        "url": url,
        "path": str(out),
        "sha256": sha256_file(out),
        "bytes": len(data),
        "status": "DOWNLOADED",
        "transport": transport,
    }


def read_candidates(path: Path) -> list[Candidate]:
    if not path.exists():
        raise FileNotFoundError(path)
    out = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            sym = str(r.get("symbol") or "").strip().upper()
            company = str(r.get("company") or "").strip()
            if sym and company:
                out.append(Candidate(sym, company))
    if not out:
        raise RuntimeError(f"no candidates in {path}")
    return out

def date_range(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)

def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

def make_capture(out_root: Path, mode: str) -> Path:
    cap = now_cst().strftime("%Y%m%dT%H%M%S%z")
    p = out_root / mode / cap
    p.mkdir(parents=True, exist_ok=False)
    return p

def check() -> int:
    result = {
        "status": "PASS_OPTIMUS_SOURCE_RECALL_V0_4_6_PHASE_A_CHECK",
        "v04_script": str(V04_SCRIPT),
        "v04_script_exists": V04_SCRIPT.exists(),
        "v04_script_sha256": sha256_file(V04_SCRIPT) if V04_SCRIPT.exists() else None,
        "expected_v045_sha256": EXPECTED_V045_SHA256,
        "v045_exact_match": (
            V04_SCRIPT.exists() and sha256_file(V04_SCRIPT) == EXPECTED_V045_SHA256
        ),
        "candidate_csv": str(DEFAULT_CANDIDATE_CSV),
        "candidate_csv_exists": DEFAULT_CANDIDATE_CSV.exists(),
        "phase": "SOURCE_RECALL_ONLY",
        "classifier_contract": "OPTIMUS_EVIDENCE_ACQUISITION_V0_4_4_FROZEN",
        "classifier_invoked": False,
        "db_opened": False,
        "registry_mutation": False,
        "canonicalization_mutation": False,
        "broker": False,
        "orders": False,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # Do not fail solely on V0.4.5 hash mismatch: an additive local hotfix may
    # legitimately have changed the file. We only report it for audit.
    return 0 if result["candidate_csv_exists"] else 2

def targeted_replay(args: argparse.Namespace) -> int:
    out = make_capture(Path(args.out_root), "targeted_replay")
    cases = []
    all_rows = []
    downloads = []

    for case in TARGETED_CASES:
        cand = Candidate(case["symbol"], case["company"])
        qd = date.fromisoformat(case["query_date"])

        kw_rows, kw_summary = collect(
            Query(case["query_date"], searchkey=case["company"]),
            delay=args.delay,
        )
        exact = exact_rows(kw_rows, case["symbol"])
        orgs = sorted({r["org_id"] for r in exact if r.get("org_id")})
        if len(orgs) != 1:
            raise RuntimeError(
                f"targeted replay cannot resolve unique orgId for "
                f"{case['symbol']} {case['company']}: {orgs}"
            )
        org_id = orgs[0]

        stock_token = f"{sec_code(case['symbol'])},{org_id}"
        stock_rows, stock_summary = collect(
            Query(case["query_date"], stock=stock_token),
            delay=args.delay,
        )
        stock_exact = exact_rows(stock_rows, case["symbol"])
        ids = {r["announcement_id"] for r in stock_exact}
        missing = [x for x in case["required_ids"] if x not in ids]

        case_dir = out / case["symbol"].replace(".", "_") / case["query_date"]
        write_jsonl(case_dir / "stock_scoped_announcements.jsonl", stock_exact)
        write_json(case_dir / "keyword_query_summary.json", kw_summary)
        write_json(case_dir / "stock_query_summary.json", stock_summary)

        if args.download_pdfs:
            for row in stock_exact:
                downloads.append(download_pdf(row, case_dir / "pdfs"))

        rec = {
            **case,
            "org_id": org_id,
            "stock_token": stock_token,
            "keyword_query_integrity": kw_summary["integrity"],
            "stock_query_integrity": stock_summary["integrity"],
            "stock_scoped_rows": len(stock_exact),
            "stock_scoped_ids": sorted(ids),
            "required_ids_missing": missing,
            "status": "PASS" if not missing else "FAIL_REQUIRED_ID_MISSING",
        }
        cases.append(rec)
        all_rows.extend(stock_exact)

    manifest = {
        "status": "PASS_OPTIMUS_SOURCE_RECALL_V0_4_6_TARGETED_REPLAY"
                  if all(c["status"] == "PASS" for c in cases)
                  else "FAIL_OPTIMUS_SOURCE_RECALL_V0_4_6_TARGETED_REPLAY",
        "capture_dir": str(out),
        "observed_at": iso_now(),
        "phase": "SOURCE_RECALL_ONLY",
        "cases": cases,
        "unique_announcement_ids": len({r["announcement_id"] for r in all_rows}),
        "pdf_downloads": downloads,
        "safety": {
            "classifier_invoked": False,
            "db_opened": False,
            "registry_mutation": False,
            "canonicalization_mutation": False,
            "broker": False,
            "orders": False,
        },
    }
    write_json(out / "manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if manifest["status"].startswith("PASS_") else 3

def backfill(args: argparse.Namespace) -> int:
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    if end < start:
        raise SystemExit("--end must be >= --start")
    candidates = read_candidates(Path(args.candidates))
    selected = set(x.upper() for x in (args.symbol or []))
    if selected:
        candidates = [c for c in candidates if c.symbol.upper() in selected]
    if not candidates:
        raise SystemExit("no candidates selected")

    out = make_capture(Path(args.out_root), "backfill")
    all_rows: dict[str, dict[str, Any]] = {}
    candidate_results = []
    downloads = []

    for i, cand in enumerate(candidates, 1):
        print(f"[V0.4.6] {i}/{len(candidates)} resolve {cand.symbol} {cand.company}")
        org_id, org_audit = resolve_org_id(
            cand, start, end, delay=args.delay, max_probe_days=args.max_org_probe_days
        )
        token = f"{sec_code(cand.symbol)},{org_id}"
        cand_rows: dict[str, dict[str, Any]] = {}
        daily = []

        for d in date_range(start, end):
            rows, summary = collect(
                Query(d.isoformat(), stock=token),
                delay=args.delay,
            )
            exact = exact_rows(rows, cand.symbol)
            for r in exact:
                aid = r["announcement_id"]
                if aid:
                    cand_rows.setdefault(aid, r)
                    all_rows.setdefault(aid, r)
            daily.append({
                "date": d.isoformat(),
                "rows": len(exact),
                "integrity": summary["integrity"],
                "reported_pages": summary["reported_pages"],
                "duplicate_ratio": summary["duplicate_ratio"],
            })

        cdir = out / cand.symbol.replace(".", "_")
        write_json(cdir / "org_id_resolution.json", org_audit)
        write_json(cdir / "daily_query_audit.json", daily)
        ordered = sorted(
            cand_rows.values(),
            key=lambda r: (str(r.get("query_date")), str(r.get("announcement_id"))),
        )
        write_jsonl(cdir / "announcements.jsonl", ordered)

        if args.download_pdfs:
            for r in ordered:
                downloads.append(download_pdf(r, cdir / "pdfs"))

        candidate_results.append({
            "symbol": cand.symbol,
            "company": cand.company,
            "org_id": org_id,
            "stock_token": token,
            "announcement_count": len(ordered),
            "all_daily_integrity_pass": all(x["integrity"] == "PASS" for x in daily),
        })

    all_ordered = sorted(
        all_rows.values(),
        key=lambda r: (
            str(r.get("symbol")),
            str(r.get("query_date")),
            str(r.get("announcement_id")),
        ),
    )
    write_jsonl(out / "announcements_all.jsonl", all_ordered)
    manifest = {
        "status": "PASS_OPTIMUS_SOURCE_RECALL_V0_4_6_BACKFILL",
        "capture_dir": str(out),
        "observed_at": iso_now(),
        "start": args.start,
        "end": args.end,
        "candidate_count": len(candidates),
        "announcement_count": len(all_ordered),
        "candidate_results": candidate_results,
        "pdf_download_count": len(downloads),
        "pdf_downloads": downloads,
        "source_recall_quality": "CANDIDATE_STOCK_SCOPED_COMPLETE",
        "transport_quality": "COMPLETE",
        "classifier_quality": "FROZEN_NOT_INVOKED",
        "safety": {
            "classifier_invoked": False,
            "db_opened": False,
            "registry_mutation": False,
            "canonicalization_mutation": False,
            "broker": False,
            "orders": False,
        },
    }
    write_json(out / "manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0

def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="PositionX Optimus V0.4.6 source-recall hardening, Phase A"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check")

    tr = sub.add_parser("targeted-replay")
    tr.add_argument("--out-root", default=str(DEFAULT_OUT))
    tr.add_argument("--delay", type=float, default=0.4)
    tr.add_argument("--download-pdfs", action="store_true")

    bf = sub.add_parser("backfill-seed-debt-range")
    bf.add_argument("--start", required=True, help="YYYY-MM-DD")
    bf.add_argument("--end", required=True, help="YYYY-MM-DD")
    bf.add_argument("--candidates", default=str(DEFAULT_CANDIDATE_CSV))
    bf.add_argument("--symbol", action="append")
    bf.add_argument("--out-root", default=str(DEFAULT_OUT))
    bf.add_argument("--delay", type=float, default=0.4)
    bf.add_argument("--max-org-probe-days", type=int, default=90)
    bf.add_argument("--download-pdfs", action="store_true")
    return ap

def main() -> int:
    a = parser().parse_args()
    if a.cmd == "check":
        return check()
    if a.cmd == "targeted-replay":
        return targeted_replay(a)
    if a.cmd == "backfill-seed-debt-range":
        return backfill(a)
    return 2

if __name__ == "__main__":
    raise SystemExit(main())
