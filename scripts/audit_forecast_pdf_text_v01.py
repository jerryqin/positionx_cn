from __future__ import annotations

r"""
PositionX — Forecast PDF Text Extraction Pilot V0.1

Purpose
-------
The raw CNINFO page JSON pilot found announcementContent absent in 180/180 samples.
This pilot tests the next-cheapest layer: PDF text extraction WITHOUT OCR.

Default sample
--------------
60 title-generic FORECAST announcements:
- 20 from 2024
- 20 from 2025
- 20 from 2026
- SH/SZ only (BSE excluded)
- deterministic sampling with seasonal coverage

Source
------
D:\positionx_data\events\cninfo_earnings_history_v0_1\
runs\pilot_20240101_20260929\canonical_events_all.parquet

Extraction engines
------------------
Prefer PyMuPDF (`fitz`); fall back to `pypdf`.
If neither is installed, fail with a clear install hint.

No OCR. No broker/order/production SQLite/research DB/catalog mutation.
Downloaded PDFs are preserved as evidence under the pilot output directory.
"""

import argparse
import hashlib
import http.client
import html
import json
import os
import re
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

EVENTS = Path(
    r"D:\positionx_data\events\cninfo_earnings_history_v0_1"
    r"\runs\pilot_20240101_20260929\canonical_events_all.parquet"
)
OUT_ROOT = Path(r"D:\positionx_data\events\forecast_pdf_text_pilot_v0_1")

POS_TITLE_PAT = re.compile(r"预增|扭亏|预盈")
NEG_TITLE_PAT = re.compile(r"预减|预亏|首亏|续亏")
REVISION_PAT = re.compile(r"修正|更正|补充")
SPACE_PAT = re.compile(r"\s+")

# Conservative body cues, intentionally narrower than generic "增长/下降".
POS_BODY_PAT = re.compile(
    r"扭亏为盈|实现扭亏|预计盈利|预计净利润为正|比上年同期增长|同比增长|同比增加"
)
NEG_BODY_PAT = re.compile(
    r"预计亏损|由盈转亏|首亏|续亏|比上年同期下降|同比下降|同比减少"
)

PERIOD_PATTERNS = [
    re.compile(r"(20\d{2})年(?:度|全年)"),
    re.compile(r"(20\d{2})年前三季度"),
    re.compile(r"(20\d{2})年(?:半年度|上半年)"),
    re.compile(r"(20\d{2})年第一季度"),
    re.compile(r"(20\d{2})年第三季度"),
]

PCT_RANGE_PATTERNS = [
    re.compile(
        r"(?:同比|比上年同期|较上年同期|变动幅度)"
        r"[^。；;\n]{0,80}?(-?\d+(?:\.\d+)?)\s*%"
        r"\s*(?:至|到|—|-|~|～)\s*(-?\d+(?:\.\d+)?)\s*%"
    ),
    re.compile(
        r"(?:增长|增加|上升|下降|减少|下滑)"
        r"[^。；;\n]{0,50}?(-?\d+(?:\.\d+)?)\s*%"
        r"\s*(?:至|到|—|-|~|～)\s*(-?\d+(?:\.\d+)?)\s*%"
    ),
]
PCT_SINGLE_PATTERNS = [
    re.compile(
        r"(?:同比|比上年同期|较上年同期|变动幅度)"
        r"[^。；;\n]{0,80}?(-?\d+(?:\.\d+)?)\s*%"
    ),
]

PROFIT_RANGE_PATTERNS = [
    re.compile(
        r"(?:归属于上市公司股东的净利润|归母净利润|净利润)"
        r"[^。；;\n]{0,180}?(-?\d[\d,]*(?:\.\d+)?)\s*(万元|亿元|元)"
        r"\s*(?:至|到|—|-|~|～)\s*(-?\d[\d,]*(?:\.\d+)?)\s*(万元|亿元|元)"
    ),
    # Common CNINFO table phrasing: 盈利：xx万元–xx万元
    re.compile(
        r"(?:盈利|亏损)\s*[:：]\s*(-?\d[\d,]*(?:\.\d+)?)\s*(万元|亿元|元)"
        r"\s*(?:至|到|—|-|~|～)\s*(-?\d[\d,]*(?:\.\d+)?)\s*(万元|亿元|元)"
    ),
]
PROFIT_SINGLE_PATTERNS = [
    re.compile(
        r"(?:归属于上市公司股东的净利润|归母净利润|净利润)"
        r"[^。；;\n]{0,180}?(-?\d[\d,]*(?:\.\d+)?)\s*(万元|亿元|元)"
    ),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "http://www.cninfo.com.cn/",
}


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def safe(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, bool)):
        return v
    if isinstance(v, float):
        return v
    if hasattr(v, "item"):
        try:
            return safe(v.item())
        except Exception:
            pass
    return str(v)


def qpath(p: Path) -> str:
    return str(p).replace("\\", "/").replace("'", "''")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, default=safe),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def normalize_symbol(code: Any) -> str | None:
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(digits) != 6:
        return None
    if digits.startswith(("5", "6", "9")):
        return digits + ".SH"
    if digits.startswith(("0", "1", "2", "3")):
        return digits + ".SZ"
    return None


def generic_forecast_title(title: Any) -> bool:
    t = str(title or "")
    return not (POS_TITLE_PAT.search(t) or NEG_TITLE_PAT.search(t))


def deterministic_sample(df: pd.DataFrame, per_year: int) -> pd.DataFrame:
    peak_months = [1, 4, 7, 10]
    picks = []
    for year in sorted(df["year"].dropna().astype(int).unique()):
        g = df[df["year"] == year].copy()
        quota = min(per_year, len(g))
        if quota <= 0:
            continue

        chosen = []
        # Reserve roughly half for 1/4/7/10 when possible.
        each = max(1, (quota // 2) // 4)
        for month in peak_months:
            gm = g[g["month"] == month].sort_values("announcementId")
            chosen.extend(gm.head(each).index.tolist())

        chosen = list(dict.fromkeys(chosen))
        remaining = quota - len(chosen)
        if remaining > 0:
            rest = g.drop(index=chosen, errors="ignore").sort_values(
                ["month", "announcementId"]
            )
            if len(rest) <= remaining:
                chosen.extend(rest.index.tolist())
            else:
                step = len(rest) / remaining
                positions = [min(int(i * step), len(rest)-1) for i in range(remaining)]
                chosen.extend(rest.iloc[positions].index.tolist())

        picks.extend(list(dict.fromkeys(chosen))[:quota])
    return df.loc[picks].copy().reset_index(drop=True)


def select_pdf_engine():
    try:
        import pymupdf
        return "pymupdf", pymupdf
    except Exception:
        pass
    try:
        import pypdf
        return "pypdf", pypdf
    except Exception:
        raise RuntimeError(
            "No PDF text extractor available. Install one inside the active venv:\n"
            "  pip install PyMuPDF\n"
            "or\n"
            "  pip install pypdf\n"
            "Then rerun this script."
        )


def _pdf_candidate_urls(url: str) -> list[str]:
    out = []
    if url.startswith("http://"):
        out.append("https://" + url[len("http://"):])
    out.append(url)
    # preserve order and uniqueness
    return list(dict.fromkeys(out))


def _valid_pdf_file(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 1024:
        return False
    with path.open("rb") as f:
        head = f.read(8)
        try:
            f.seek(max(0, path.stat().st_size - 4096))
        except Exception:
            return False
        tail = f.read()
    return head.startswith(b"%PDF") and b"%%EOF" in tail


def download_pdf(url: str, dest: Path, tries: int = 4) -> None:
    """
    Robust CNINFO PDF download:
    - HTTPS first, HTTP fallback
    - preserves partial bytes
    - resumes with Range when the server supports it
    - verifies PDF header + EOF marker before commit
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".pdf.part")
    last = None

    if _valid_pdf_file(dest):
        return

    for candidate in _pdf_candidate_urls(url):
        for i in range(tries):
            start = tmp.stat().st_size if tmp.exists() else 0
            headers = dict(HEADERS)
            headers["Accept-Encoding"] = "identity"
            headers["Connection"] = "close"
            if start > 0:
                headers["Range"] = f"bytes={start}-"

            req = urllib.request.Request(candidate, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    status = getattr(r, "status", None) or r.getcode()
                    if start > 0 and status != 206:
                        # Server ignored Range. Start clean rather than append duplicate bytes.
                        tmp.unlink(missing_ok=True)
                        start = 0

                    mode = "ab" if start > 0 and status == 206 else "wb"
                    with tmp.open(mode) as f:
                        while True:
                            try:
                                chunk = r.read(65536)
                            except http.client.IncompleteRead as e:
                                if e.partial:
                                    f.write(e.partial)
                                raise
                            if not chunk:
                                break
                            f.write(chunk)

                if _valid_pdf_file(tmp):
                    os.replace(tmp, dest)
                    return

                last = RuntimeError(
                    f"incomplete/non-PDF file size={tmp.stat().st_size if tmp.exists() else 0}"
                )

            except Exception as e:
                last = e

            time.sleep(1.5 * (i + 1))

        # Candidate transport failed; keep partial file only if Range may help the
        # next candidate. HTTPS/HTTP may serve identical bytes.
        time.sleep(0.5)

    raise RuntimeError(f"PDF download failed: {url}: {last!r}")


def extract_pdf_text(path: Path, engine_name: str, engine_module):
    pages = []
    if engine_name == "pymupdf":
        doc = engine_module.open(str(path))
        try:
            for i in range(len(doc)):
                pages.append(doc[i].get_text("text") or "")
        finally:
            doc.close()
    else:
        reader = engine_module.PdfReader(str(path))
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                pages.append("")
    text = "\n".join(pages)
    text = html.unescape(text)
    text = SPACE_PAT.sub(" ", text).strip()
    return text, len(pages)


def parse_num(v: Any) -> float | None:
    try:
        return float(str(v).replace(",", ""))
    except Exception:
        return None


def extract_period(text: str) -> str | None:
    for pat in PERIOD_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(0)
    return None


def extract_direction(text: str) -> tuple[str, str | None]:
    # Use a bounded evidence region around the net-profit section when possible.
    anchor = text.find("归属于上市公司股东的净利润")
    window = text[max(0, anchor-200):anchor+1200] if anchor >= 0 else text[:2500]

    pos = POS_BODY_PAT.search(window)
    neg = NEG_BODY_PAT.search(window)
    if pos and neg:
        return "MIXED", window[:1200]
    if pos:
        return "POSITIVE", window[:1200]
    if neg:
        return "NEGATIVE", window[:1200]
    return "UNKNOWN", window[:1200] if window else None


def extract_pct(text: str):
    for pat in PCT_RANGE_PATTERNS:
        m = pat.search(text)
        if m:
            a, b = parse_num(m.group(1)), parse_num(m.group(2))
            if a is not None and b is not None:
                return min(a,b), max(a,b), m.group(0)[:240]
    for pat in PCT_SINGLE_PATTERNS:
        m = pat.search(text)
        if m:
            a = parse_num(m.group(1))
            if a is not None:
                return a, a, m.group(0)[:240]
    return None, None, None


def extract_profit(text: str):
    for pat in PROFIT_RANGE_PATTERNS:
        m = pat.search(text)
        if m:
            a, ua, b, ub = (
                parse_num(m.group(1)), m.group(2),
                parse_num(m.group(3)), m.group(4),
            )
            if a is not None and b is not None and ua == ub:
                return min(a,b), max(a,b), ua, m.group(0)[:260]
    for pat in PROFIT_SINGLE_PATTERNS:
        m = pat.search(text)
        if m:
            a, ua = parse_num(m.group(1)), m.group(2)
            if a is not None:
                return a, a, ua, m.group(0)[:260]
    return None, None, None, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default=str(EVENTS))
    ap.add_argument("--out-root", default=str(OUT_ROOT))
    ap.add_argument("--per-year", type=int, default=20)
    ap.add_argument("--tries", type=int, default=4)
    ap.add_argument("--delay", type=float, default=0.4)
    args = ap.parse_args()

    events_path = Path(args.events)
    if not events_path.exists():
        raise RuntimeError(f"Events parquet missing: {events_path}")

    out = Path(args.out_root) / f"pilot_{datetime.now():%Y%m%d_%H%M%S}"
    pdf_dir = out / "pdf"
    out.mkdir(parents=True, exist_ok=False)

    con = duckdb.connect(":memory:")
    try:
        ev = con.execute(
            f"SELECT * FROM read_parquet('{qpath(events_path)}')"
        ).df()
    finally:
        con.close()

    required = {
        "announcementId", "secCode", "secName", "announcementTitle",
        "event_time", "event_class", "pdf_url"
    }
    missing = sorted(required - set(ev.columns))
    if missing:
        raise RuntimeError(f"Canonical events missing columns: {missing}")

    f = ev[ev["event_class"].eq("FORECAST")].copy()
    f["symbol"] = f["secCode"].map(normalize_symbol)
    f = f[f["symbol"].notna()].copy()  # SH/SZ only
    f = f[f["announcementTitle"].map(generic_forecast_title)].copy()
    t = pd.to_datetime(f["event_time"], errors="coerce")
    f["year"] = t.dt.year
    f["month"] = t.dt.month
    f = f.dropna(subset=["year","month"]).copy()
    f["year"] = f["year"].astype(int)
    f["month"] = f["month"].astype(int)

    sample = deterministic_sample(f, per_year=args.per_year)
    if sample.empty:
        raise RuntimeError("No eligible generic forecast rows")

    engine_name, engine = select_pdf_engine()
    print("PDF_TEXT_ENGINE:", engine_name)
    print("SAMPLE_ROWS:", len(sample))

    rows = []
    for i, (_, r) in enumerate(sample.iterrows(), start=1):
        ann_id = str(r["announcementId"])
        pdf_url = str(r.get("pdf_url") or "").strip()
        pdf_path = pdf_dir / f"{ann_id}.pdf"

        status = "OK"
        error = None
        text = ""
        pages = 0
        sha = None

        try:
            if not pdf_url:
                raise RuntimeError("missing pdf_url")
            if not pdf_path.exists():
                download_pdf(pdf_url, pdf_path, tries=args.tries)
                time.sleep(args.delay)
            sha = sha256_file(pdf_path)
            text, pages = extract_pdf_text(pdf_path, engine_name, engine)
        except Exception as e:
            status = "ERROR"
            error = f"{type(e).__name__}: {e}"

        semantic_text = (str(r["announcementTitle"]) + " " + text).strip()
        period = extract_period(semantic_text) if semantic_text else None
        direction, direction_evidence = extract_direction(text) if text else ("UNKNOWN", None)
        yoy_min, yoy_max, yoy_evidence = extract_pct(text) if text else (None, None, None)
        pmin, pmax, punit, profit_evidence = extract_profit(text) if text else (None, None, None, None)
        revision = bool(REVISION_PAT.search((str(r["announcementTitle"]) + " " + text))) if text else bool(
            REVISION_PAT.search(str(r["announcementTitle"]))
        )

        structured = sum([
            period is not None,
            direction != "UNKNOWN",
            yoy_min is not None,
            pmin is not None,
        ])

        if status == "ERROR":
            extraction_status = "ERROR"
        elif not text:
            extraction_status = "NO_TEXT_LAYER"
        elif structured >= 3:
            extraction_status = "STRUCTURED_3PLUS"
        elif structured >= 1:
            extraction_status = "PARTIAL"
        else:
            extraction_status = "TEXT_PRESENT_UNPARSED"

        rows.append({
            "announcementId": ann_id,
            "symbol": r["symbol"],
            "secName": r["secName"],
            "event_time": r["event_time"],
            "year": int(r["year"]),
            "month": int(r["month"]),
            "title": r["announcementTitle"],
            "pdf_url": pdf_url,
            "pdf_path": str(pdf_path),
            "pdf_sha256": sha,
            "pdf_pages": pages,
            "pdf_text_engine": engine_name,
            "pdf_text_chars": len(text),
            "pdf_text_present": bool(text),
            "report_period": period,
            "direction": direction,
            "revision_flag": revision,
            "yoy_min_pct": yoy_min,
            "yoy_max_pct": yoy_max,
            "net_profit_min": pmin,
            "net_profit_max": pmax,
            "net_profit_unit": punit,
            "direction_evidence": direction_evidence[:800] if direction_evidence else None,
            "yoy_evidence": yoy_evidence,
            "profit_evidence": profit_evidence,
            "extraction_status": extraction_status,
            "error": error,
            "text_preview": text[:800],
        })
        print(
            f"[{i:02d}/{len(sample):02d}] {ann_id} {r['symbol']} "
            f"pages={pages} chars={len(text)} status={extraction_status}"
        )

    rdf = pd.DataFrame(rows)
    rdf.to_csv(out/"extraction_results.csv", index=False, encoding="utf-8-sig")

    review = (
        rdf.sort_values(["year","extraction_status","announcementId"])
        .groupby("year", group_keys=False)
        .head(10)
    )
    review.to_csv(out/"manual_review_30.csv", index=False, encoding="utf-8-sig")

    status_counts = rdf["extraction_status"].value_counts(dropna=False).to_dict()
    text_rate = float(rdf["pdf_text_present"].mean())
    structured_rate = float((rdf["extraction_status"]=="STRUCTURED_3PLUS").mean())
    downloaded = rdf[rdf["extraction_status"]!="ERROR"].copy()
    error_types = (
        rdf.loc[rdf["extraction_status"]=="ERROR", "error"]
        .fillna("UNKNOWN")
        .map(lambda x: str(x).split(":", 1)[0])
        .value_counts()
        .to_dict()
    )

    summary = {
        "status": "PASS_FORECAST_PDF_TEXT_PILOT_V0_1",
        "generated_at": iso_now(),
        "events": str(events_path),
        "sample_rows": int(len(rdf)),
        "sample_by_year": {str(k): int(v) for k,v in rdf.groupby("year").size().to_dict().items()},
        "scope": "SH/SZ generic FORECAST titles only; BSE excluded",
        "pdf_text_engine": engine_name,
        "download_success_rate": float((rdf["extraction_status"]!="ERROR").mean()),
        "pdf_text_present_rate": text_rate,
        "report_period_extract_rate": float(rdf["report_period"].notna().mean()),
        "direction_extract_rate": float((rdf["direction"]!="UNKNOWN").mean()),
        "yoy_range_extract_rate": float(rdf["yoy_min_pct"].notna().mean()),
        "profit_range_extract_rate": float(rdf["net_profit_min"].notna().mean()),
        "structured_3plus_rate": structured_rate,
        "no_text_layer_rate": float((rdf["extraction_status"]=="NO_TEXT_LAYER").mean()),
        "status_counts": {str(k): int(v) for k,v in status_counts.items()},
        "error_type_counts": {str(k): int(v) for k,v in error_types.items()},
        "conditional_on_downloaded": {
            "rows": int(len(downloaded)),
            "pdf_text_present_rate": (
                float(downloaded["pdf_text_present"].mean()) if len(downloaded) else None
            ),
            "report_period_extract_rate": (
                float(downloaded["report_period"].notna().mean()) if len(downloaded) else None
            ),
            "direction_extract_rate": (
                float((downloaded["direction"]!="UNKNOWN").mean()) if len(downloaded) else None
            ),
            "yoy_range_extract_rate": (
                float(downloaded["yoy_min_pct"].notna().mean()) if len(downloaded) else None
            ),
            "profit_range_extract_rate": (
                float(downloaded["net_profit_min"].notna().mean()) if len(downloaded) else None
            ),
            "structured_3plus_rate": (
                float((downloaded["extraction_status"]=="STRUCTURED_3PLUS").mean())
                if len(downloaded) else None
            ),
        },
        "decision_rule": {
            "pdf_text_route_promising": (
                "download_success_rate >= 0.90 AND conditional_on_downloaded.structured_3plus_rate >= 0.60"
            ),
            "if_text_high_but_structured_low": (
                "Refine parser from manual-review evidence before scaling."
            ),
            "if_no_text_layer_high": (
                "Only then consider a small OCR pilot; do not OCR the full corpus."
            ),
        },
        "no_ocr": True,
        "no_database_mutation": True,
        "output_dir": str(out),
        "outputs": [
            "extraction_results.csv",
            "manual_review_30.csv",
            "extraction_summary.json",
            "pdf/*.pdf",
        ],
    }
    write_json_atomic(out/"extraction_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
