from __future__ import annotations

r"""
PositionX — Forecast Content Extraction Pilot V0.1

Goal
----
Before downloading/parsing thousands of PDFs, test whether CNINFO historical raw page
JSON already carries enough announcementContent to recover useful forecast fields.

Source
------
Existing immutable historical CNINFO backfill:
D:\positionx_data\events\cninfo_earnings_history_v0_1\runs\pilot_20240101_20260929

No network requests. No PDF download. No OCR. No database/catalog mutation.

Pilot sample
------------
Deterministic 180 FORECAST_GENERIC_TITLE events:
- 60 from 2024
- 60 from 2025
- 60 from 2026 (or as many as available)
Within each year, prefer coverage across Jan/Apr/Jul/Oct plus other months.

Fields attempted
----------------
- report_period
- direction: POSITIVE / NEGATIVE / MIXED / UNKNOWN
- revision_flag
- yoy_min_pct / yoy_max_pct
- net_profit_min / net_profit_max / net_profit_unit

This is a parser-readiness audit, not a Strategy backtest.
"""

import argparse
import html
import json
import math
import os
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import duckdb
import pandas as pd

DEFAULT_RUN = Path(
    r"D:\positionx_data\events\cninfo_earnings_history_v0_1"
    r"\runs\pilot_20240101_20260929"
)
OUT_ROOT = Path(r"D:\positionx_data\events\forecast_content_extraction_pilot_v0_1")


def write_json_atomic(path: Path, obj: Any) -> None:
    """Atomically write UTF-8 JSON so a failed write never leaves a partial summary."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    os.replace(tmp, path)


POS_PAT = re.compile(r"预增|扭亏|预盈|增长|增加|上升")
NEG_PAT = re.compile(r"预减|预亏|首亏|续亏|下降|减少|下滑")
REVISION_PAT = re.compile(r"修正|更正|补充")
TAG_PAT = re.compile(r"<[^>]+>")
SPACE_PAT = re.compile(r"\s+")

PERCENT_RANGE_PATTERNS = [
    re.compile(r"(?:同比|比上年同期|较上年同期|变动幅度)[^。；;\n]{0,60}?(-?\d+(?:\.\d+)?)\s*%\s*(?:至|到|—|-|~|～)\s*(-?\d+(?:\.\d+)?)\s*%"),
    re.compile(r"(?:增长|增加|上升|下降|减少|下滑)[^。；;\n]{0,40}?(-?\d+(?:\.\d+)?)\s*%\s*(?:至|到|—|-|~|～)\s*(-?\d+(?:\.\d+)?)\s*%"),
]
PERCENT_SINGLE_PATTERNS = [
    re.compile(r"(?:同比|比上年同期|较上年同期|变动幅度)[^。；;\n]{0,60}?(-?\d+(?:\.\d+)?)\s*%"),
]

PROFIT_RANGE_PATTERNS = [
    re.compile(
        r"(?:归属于上市公司股东的净利润|归母净利润|净利润)"
        r"[^。；;\n]{0,120}?(-?\d[\d,]*(?:\.\d+)?)\s*(万元|亿元|元)"
        r"\s*(?:至|到|—|-|~|～)\s*(-?\d[\d,]*(?:\.\d+)?)\s*(万元|亿元|元)"
    ),
]
PROFIT_SINGLE_PATTERNS = [
    re.compile(
        r"(?:归属于上市公司股东的净利润|归母净利润|净利润)"
        r"[^。；;\n]{0,120}?(-?\d[\d,]*(?:\.\d+)?)\s*(万元|亿元|元)"
    ),
]

PERIOD_PATTERNS = [
    re.compile(r"(20\d{2})年(?:度|全年)"),
    re.compile(r"(20\d{2})年前三季度"),
    re.compile(r"(20\d{2})年(?:半年度|上半年)"),
    re.compile(r"(20\d{2})年第一季度"),
    re.compile(r"(20\d{2})年第三季度"),
]


def clean_text(v: Any) -> str:
    s = html.unescape(str(v or ""))
    s = TAG_PAT.sub(" ", s)
    s = SPACE_PAT.sub(" ", s)
    return s.strip()


def parse_num(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(str(s).replace(",", ""))
    except Exception:
        return None


def classify_direction(text: str) -> str:
    pos = bool(POS_PAT.search(text))
    neg = bool(NEG_PAT.search(text))
    if pos and neg:
        return "MIXED"
    if pos:
        return "POSITIVE"
    if neg:
        return "NEGATIVE"
    return "UNKNOWN"


def extract_report_period(text: str) -> str | None:
    for pat in PERIOD_PATTERNS:
        m = pat.search(text)
        if m:
            full = m.group(0)
            return full
    return None


def extract_pct(text: str):
    for pat in PERCENT_RANGE_PATTERNS:
        m = pat.search(text)
        if m:
            a, b = parse_num(m.group(1)), parse_num(m.group(2))
            if a is not None and b is not None:
                return min(a,b), max(a,b), m.group(0)[:200]
    for pat in PERCENT_SINGLE_PATTERNS:
        m = pat.search(text)
        if m:
            a = parse_num(m.group(1))
            if a is not None:
                return a, a, m.group(0)[:200]
    return None, None, None


def extract_profit(text: str):
    for pat in PROFIT_RANGE_PATTERNS:
        m = pat.search(text)
        if m:
            a, ua, b, ub = parse_num(m.group(1)), m.group(2), parse_num(m.group(3)), m.group(4)
            if a is not None and b is not None and ua == ub:
                return min(a,b), max(a,b), ua, m.group(0)[:220]
    for pat in PROFIT_SINGLE_PATTERNS:
        m = pat.search(text)
        if m:
            a, ua = parse_num(m.group(1)), m.group(2)
            if a is not None:
                return a, a, ua, m.group(0)[:220]
    return None, None, None, None


def generic_forecast_title(title: str) -> bool:
    # Same logic as previous title-only audit: generic means no explicit directional cue in title.
    t = str(title or "")
    directional = re.search(r"预增|扭亏|预盈|预减|预亏|首亏|续亏", t)
    return directional is None


def load_raw_page_index(raw_pages_root: Path) -> Dict[str, dict]:
    """
    Build first-seen announcementId -> raw announcement record from immutable page JSON.
    The same announcement may appear in more than one keyword/page; one record is enough
    because announcementContent is the same payload field for our readiness audit.
    """
    idx: Dict[str, dict] = {}
    for p in sorted(raw_pages_root.rglob("page_*.json")):
        obj = json.loads(p.read_text(encoding="utf-8"))
        for row in obj.get("announcements") or []:
            ann_id = str(row.get("announcementId") or "")
            if ann_id and ann_id not in idx:
                z = dict(row)
                z["_raw_page_path"] = str(p)
                idx[ann_id] = z
    return idx


def deterministic_sample(df: pd.DataFrame, per_year: int = 60) -> pd.DataFrame:
    picks = []
    peak_months = [1,4,7,10]
    for year in sorted(df["year"].unique()):
        g = df[df["year"] == year].copy()
        if g.empty:
            continue
        quota = min(per_year, len(g))

        selected = []
        # First allocate roughly half the sample across seasonal peak months.
        peak_quota_total = min(quota // 2, len(g[g["month"].isin(peak_months)]))
        each = max(1, peak_quota_total // len(peak_months)) if peak_quota_total else 0
        for m in peak_months:
            gm = g[g["month"] == m].sort_values("announcementId")
            if each and len(gm):
                selected.extend(gm.head(each).index.tolist())

        # Fill remaining deterministically across the whole year.
        remaining = quota - len(set(selected))
        if remaining > 0:
            rest = g.drop(index=list(set(selected)), errors="ignore").sort_values(
                ["month","announcementId"]
            )
            if len(rest) <= remaining:
                selected.extend(rest.index.tolist())
            else:
                # Evenly spaced deterministic positions.
                step = len(rest) / remaining
                positions = [min(int(i * step), len(rest)-1) for i in range(remaining)]
                selected.extend(rest.iloc[positions].index.tolist())

        picks.extend(list(dict.fromkeys(selected))[:quota])

    return df.loc[picks].copy().reset_index(drop=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", default=str(DEFAULT_RUN))
    ap.add_argument("--out-root", default=str(OUT_ROOT))
    ap.add_argument("--per-year", type=int, default=60)
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    canonical_path = run_dir / "canonical_events_all.parquet"
    chunks_root = run_dir / "chunks"
    if not canonical_path.exists():
        raise RuntimeError(f"canonical events missing: {canonical_path}")
    if not chunks_root.exists():
        raise RuntimeError(f"chunks root missing: {chunks_root}")

    out = Path(args.out_root) / f"pilot_{datetime.now():%Y%m%d_%H%M%S}"
    out.mkdir(parents=True, exist_ok=False)

    con = duckdb.connect(":memory:")
    try:
        ev = con.execute(
            f"SELECT * FROM read_parquet('{str(canonical_path).replace(chr(92),'/').replace(chr(39), chr(39)*2)}')"
        ).df()
    finally:
        con.close()

    forecast = ev[ev["event_class"] == "FORECAST"].copy()
    forecast["year"] = pd.to_datetime(forecast["event_time"], errors="coerce").dt.year
    forecast["month"] = pd.to_datetime(forecast["event_time"], errors="coerce").dt.month
    forecast = forecast[
        forecast["announcementTitle"].fillna("").map(generic_forecast_title)
    ].copy()
    forecast = forecast.dropna(subset=["year","month"]).copy()
    forecast["year"] = forecast["year"].astype(int)
    forecast["month"] = forecast["month"].astype(int)

    sample = deterministic_sample(forecast, per_year=args.per_year)
    if sample.empty:
        raise RuntimeError("No generic forecast events available for sampling")

    raw_index = load_raw_page_index(chunks_root)

    results = []
    for _, row in sample.iterrows():
        ann_id = str(row["announcementId"])
        raw = raw_index.get(ann_id)
        content = clean_text(raw.get("announcementContent") if raw else None)
        title = clean_text(row.get("announcementTitle"))
        merged_text = (title + " " + content).strip()

        yoy_min, yoy_max, yoy_evidence = extract_pct(content)
        pmin, pmax, punit, profit_evidence = extract_profit(content)
        period = extract_report_period(merged_text)
        direction = classify_direction(content)
        revision = bool(REVISION_PAT.search(merged_text))

        fields = {
            "report_period": period is not None,
            "direction": direction != "UNKNOWN",
            "yoy_range": yoy_min is not None,
            "profit_range": pmin is not None,
        }
        extracted_count = sum(fields.values())

        if not content:
            status = "NO_CONTENT"
        elif extracted_count >= 3:
            status = "STRUCTURED_3PLUS"
        elif extracted_count >= 1:
            status = "PARTIAL"
        else:
            status = "CONTENT_PRESENT_UNPARSED"

        results.append({
            "announcementId": ann_id,
            "secCode": row.get("secCode"),
            "secName": row.get("secName"),
            "event_time": row.get("event_time"),
            "year": int(row["year"]),
            "month": int(row["month"]),
            "title": title,
            "raw_page_found": raw is not None,
            "raw_page_path": raw.get("_raw_page_path") if raw else None,
            "content_present": bool(content),
            "content_length": len(content),
            "report_period": period,
            "direction": direction,
            "revision_flag": revision,
            "yoy_min_pct": yoy_min,
            "yoy_max_pct": yoy_max,
            "net_profit_min": pmin,
            "net_profit_max": pmax,
            "net_profit_unit": punit,
            "yoy_evidence": yoy_evidence,
            "profit_evidence": profit_evidence,
            "extraction_status": status,
            "content_preview": content[:500],
        })

    rdf = pd.DataFrame(results)
    rdf.to_csv(out/"extraction_results.csv", index=False, encoding="utf-8-sig")

    # Deterministic manual-review set: 10 per year when available.
    review = (
        rdf.sort_values(["year","extraction_status","announcementId"])
        .groupby("year", group_keys=False)
        .head(10)
    )
    review.to_csv(out/"manual_review_30.csv", index=False, encoding="utf-8-sig")

    counts = rdf["extraction_status"].value_counts(dropna=False).to_dict()
    summary = {
        "status": "PASS_FORECAST_CONTENT_EXTRACTION_PILOT_V0_1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "run_dir": str(run_dir),
        "sample_rows": int(len(rdf)),
        "sample_by_year": {str(k): int(v) for k,v in rdf.groupby("year").size().to_dict().items()},
        "raw_page_found_rate": float(rdf["raw_page_found"].mean()),
        "content_present_rate": float(rdf["content_present"].mean()),
        "report_period_extract_rate": float(rdf["report_period"].notna().mean()),
        "direction_extract_rate": float((rdf["direction"] != "UNKNOWN").mean()),
        "yoy_range_extract_rate": float(rdf["yoy_min_pct"].notna().mean()),
        "profit_range_extract_rate": float(rdf["net_profit_min"].notna().mean()),
        "structured_3plus_rate": float((rdf["extraction_status"] == "STRUCTURED_3PLUS").mean()),
        "status_counts": {str(k): int(v) for k,v in counts.items()},
        "decision_rule": {
            "raw_json_content_route_promising": (
                "content_present_rate >= 0.80 AND structured_3plus_rate >= 0.60"
            ),
            "if_not": "Escalate only unresolved cases to PDF-text extraction pilot; do not OCR by default.",
        },
        "notes": [
            "Regex extraction is intentionally conservative and must be manually audited.",
            "This pilot does not create Strategy outcomes.",
            "No network/PDF/OCR/database mutation occurs.",
        ],
        "output_dir": str(out),
        "outputs": [
            "extraction_results.csv",
            "manual_review_30.csv",
            "extraction_summary.json",
        ],
    }
    write_json_atomic(out/"extraction_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
