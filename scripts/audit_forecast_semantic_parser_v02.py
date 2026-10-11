from __future__ import annotations

r"""
PositionX — Forecast Semantic Parser Pilot V0.2.3

Why V0.2 exists
---------------
PDF transport/text extraction is now proven viable. However, manual review of V0.1.1
showed semantic errors that are unacceptable before scaling:
- revenue growth could be mistaken for positive net-profit direction;
- a narrative "net profit decreased by ..." amount could be mistaken for forecast profit;
- checkbox templates containing both selected and unselected options could be labeled MIXED;
- subsidiary forecasts can enter the event set;
- explicit *ST-at-event cases can enter the research sample.

This pilot reparses the already-downloaded 60 PDFs. It performs NO network requests.

Core outputs
------------
- event_kind:
    ORIGINAL_FORECAST
    FORECAST_REVISION
    SUBSIDIARY_FORECAST
    FOLLOWUP_DOCUMENT
- explicit_st_at_event
- report_period
- current net-profit range (signed, normalized to yuan)
- prior net-profit range when recoverable
- profit_state
- change_type:
    TURNAROUND
    TURN_TO_LOSS
    IMPROVING
    DETERIORATING
    LOSS_NARROWING
    LOSS_WIDENING
    FLAT
    UNKNOWN
- derived profit_change_vs_prior_abs_pct where possible
- parser_confidence

Research-only. No OCR, no network, no broker/order/DB/catalog mutation.
"""

import argparse
import json
import math
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

EVENTS = Path(
    r"D:\positionx_data\events\cninfo_earnings_history_v0_1"
    r"\runs\pilot_20240101_20260929\canonical_events_all.parquet"
)
PDF_PILOT_ROOT = Path(r"D:\positionx_data\events\forecast_pdf_text_pilot_v0_1")
OUT_ROOT = Path(r"D:\positionx_data\events\forecast_semantic_parser_pilot_v0_2")

UNITS = {"元": 1.0, "万元": 1e4, "亿元": 1e8}

FOLLOWUP_PAT = re.compile(
    r"回复|问询函|监管工作函|监管函|说明会|核查意见|关于.*业绩预告.*说明"
)
REVISION_PAT = re.compile(r"业绩预告.*(?:修正|更正|补充)|(?:修正|更正|补充).*业绩预告")
SUBSIDIARY_PAT = re.compile(r"(?:控股|全资|参股)?子公司.{0,30}业绩预告|业绩预告.{0,30}(?:控股|全资|参股)?子公司")
ST_PAT = re.compile(r"证券简称\s*[:：]\s*(?:\*ST|ST|SST|S\*ST)\s*", re.I)

PERIOD_PAT = re.compile(
    r"(20\d{2})\s*(年年度|年度|全年|年半年度|年上半年|年前三季度|年第一季度|年第三季度)"
)

ANCHOR_PAT = re.compile(
    r"归属于(?:上市公司股东|母公司所有者|母公司股东)"
    r".{0,18}?(净利润|净亏损)",
    re.S
)

SELECTED = r"(?:√|☑|■|R||þ)"
SELECTED_TURNAROUND = re.compile(SELECTED + r"\s*扭亏为盈")
SELECTED_UP = re.compile(SELECTED + r"\s*同向上升")
SELECTED_DOWN = re.compile(SELECTED + r"\s*同向下降")

NUM = r"[-+]?\d[\d,]*(?:\.\d+)?"
UNIT = r"(?:亿元|万元|元)"

RANGE_PAT = re.compile(
    rf"(?:(盈利|亏损)\s*[:：]?\s*)?"
    rf"({NUM})\s*({UNIT})?\s*"
    rf"(?:至|到|—|–|－|~|～|-)\s*"
    rf"({NUM})\s*({UNIT})?"
)
SINGLE_PAT = re.compile(
    rf"(?:(盈利|亏损)\s*[:：]?\s*)?"
    rf"({NUM})\s*({UNIT})?"
)
LABELLED_SINGLE_PAT = re.compile(
    rf"(盈利|亏损)\s*[:：]?\s*({NUM})\s*({UNIT})?"
)

FLAT_PAT = re.compile(r"基本持平|大致持平")
TURN_TO_LOSS_PAT = re.compile(r"由盈转亏|将出现亏损|转为亏损")
LOSS_NARROW_PAT = re.compile(r"减亏|亏损(?:额)?(?:同比)?(?:减少|收窄)|亏损有所减少")
LOSS_WIDEN_PAT = re.compile(r"增亏|亏损(?:额)?(?:同比)?(?:增加|扩大)|亏损增加")
IMPROVE_PAT = re.compile(
    r"(?:净利润|归属于上市公司股东的净利润|归属于母公司所有者的净利润)"
    r".{0,100}?(?:同比增长|同比增加|较上年同期增长|比上年同期增长|上升)"
)
DETERIORATE_PAT = re.compile(
    r"(?:净利润|归属于上市公司股东的净利润|归属于母公司所有者的净利润)"
    r".{0,100}?(?:同比下降|同比减少|较上年同期下降|比上年同期下降|下滑)"
)

YOY_SIGNED_PAT = re.compile(
    rf"(?:比上年同期|较上年同期|同比).{{0,30}}?"
    rf"(增长|增加|上升|下降|减少|下滑)\s*[:：]?\s*"
    rf"(\d+(?:\.\d+)?)\s*%"
    rf"(?:\s*(?:至|到|—|–|－|~|～|-)\s*(\d+(?:\.\d+)?)\s*%)?"
)


def qpath(p: Path) -> str:
    return str(p).replace("\\", "/").replace("'", "''")


def safe(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, bool)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if hasattr(v, "item"):
        try:
            return safe(v.item())
        except Exception:
            pass
    return str(v)


def write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, default=safe),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def select_pdf_engine():
    try:
        import pymupdf
        return pymupdf
    except Exception as e:
        raise RuntimeError("PyMuPDF is required for this local reparse pilot") from e


def extract_pdf_text(path: Path, pymupdf) -> str:
    doc = pymupdf.open(str(path))
    try:
        text = "\n".join((doc[i].get_text("text") or "") for i in range(len(doc)))
    finally:
        doc.close()
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return text


def normalize_for_search(text: str) -> str:
    # Preserve numeric/table spacing, but repair PDF line wraps inside Chinese words.
    # Examples: "净利\n润" -> "净利润", "上市公司\n股东" -> "上市公司股东".
    t = text.replace("\u00a0", " ")
    t = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", t)
    t = re.sub(r"(?<=\d)\s+(?=年)", "", t)
    t = re.sub(r"年\s+(?=度|半年度|上半年|前三季度|第一季度|第三季度)", "年", t)
    return t


def find_latest_complete_pdf_pilot(root: Path) -> Path:
    candidates = []
    for p in root.glob("pilot_*"):
        pdf_dir = p / "pdf"
        if pdf_dir.is_dir() and len(list(pdf_dir.glob("*.pdf"))) >= 50:
            candidates.append(p)
    if not candidates:
        raise RuntimeError(f"No completed PDF pilot found under {root}")
    return sorted(candidates, key=lambda p: p.name)[-1]


def event_kind(title: str) -> str:
    t = str(title or "")
    if SUBSIDIARY_PAT.search(t):
        return "SUBSIDIARY_FORECAST"
    if REVISION_PAT.search(t):
        return "FORECAST_REVISION"
    if FOLLOWUP_PAT.search(t):
        return "FOLLOWUP_DOCUMENT"
    return "ORIGINAL_FORECAST"


def report_period(title: str, text: str) -> str | None:
    t = normalize_for_search(title + "\n" + text[:4000])
    m = PERIOD_PAT.search(t)
    if not m:
        return None
    year, label = m.group(1), m.group(2)
    mapping = {
        "年年度": "ANNUAL", "年度": "ANNUAL", "全年": "ANNUAL",
        "年半年度": "H1", "年上半年": "H1",
        "年前三季度": "Q1_Q3",
        "年第一季度": "Q1",
        "年第三季度": "Q3",
    }
    return f"{year}_{mapping[label]}"


def forecast_section(text: str) -> str:
    t = normalize_for_search(text)
    m = re.search(r"(?:一、)?\s*本期业绩(?:预告|预计)情况", t)
    start = m.start() if m else 0
    tail = t[start:start+7000]
    endm = re.search(r"\n\s*二[、.．]", tail)
    if endm:
        tail = tail[:endm.start()]
    return tail


def unit_near(section: str, anchor_start: int) -> str | None:
    before = section[max(0, anchor_start-700):anchor_start]
    ms = list(re.finditer(r"单位\s*[:：]\s*(亿元|万元|元)", before))
    if ms:
        return ms[-1].group(1)
    # Common Shenzhen-table header: 本报告期（万元） / 上年同期（万元）
    ms = list(re.finditer(r"(?:本报告期|上年同期)\s*[（(]\s*(亿元|万元|元)\s*[）)]", before))
    return ms[-1].group(1) if ms else None


def num(v: str) -> float:
    return float(v.replace(",", ""))


def apply_sign(a: float, b: float, label: str | None, metric_word: str) -> tuple[float,float]:
    if metric_word == "净亏损" or label == "亏损":
        a, b = -abs(a), -abs(b)
    elif label == "盈利":
        a, b = abs(a), abs(b)
    return min(a,b), max(a,b)


def parse_amount_after_anchor(section: str):
    """
    Parse the current-period primary net-profit range immediately after the first
    issuer-level net-profit anchor in the forecast section.
    """
    anchors = list(ANCHOR_PAT.finditer(section))
    if not anchors:
        return None

    a = anchors[0]
    metric_word = a.group(1)
    region = section[a.end():a.end()+700]
    # Stop before the next metric so we do not accidentally parse 扣非净利润.
    cut = re.search(r"扣除非经常性|扣非|基本每股收益", region)
    if cut:
        region = region[:cut.start()]

    global_unit = unit_near(section, a.start())

    m = RANGE_PAT.search(region)
    if m:
        label = m.group(1)
        x1, u1, x2, u2 = num(m.group(2)), m.group(3), num(m.group(4)), m.group(5)
        unit = u1 or u2 or global_unit
        lo, hi = apply_sign(x1, x2, label, metric_word)
        return {
            "lo": lo, "hi": hi, "unit": unit,
            "anchor_start": a.start(),
            "anchor_end": a.end(),
            "match_start_abs": a.end()+m.start(),
            "match_end_abs": a.end()+m.end(),
            "metric_word": metric_word,
            "label": label,
            "evidence": (a.group(0) + region[:m.end()])[:500],
        }

    m = SINGLE_PAT.search(region)
    if m:
        label = m.group(1)
        x, unit = num(m.group(2)), (m.group(3) or global_unit)
        lo, hi = apply_sign(x, x, label, metric_word)
        return {
            "lo": lo, "hi": hi, "unit": unit,
            "anchor_start": a.start(),
            "anchor_end": a.end(),
            "match_start_abs": a.end()+m.start(),
            "match_end_abs": a.end()+m.end(),
            "metric_word": metric_word,
            "label": label,
            "evidence": (a.group(0) + region[:m.end()])[:500],
        }
    return None


def to_yuan(v: float | None, unit: str | None) -> float | None:
    if v is None or unit not in UNITS:
        return None
    return v * UNITS[unit]


def parse_prior_from_table(section: str, current: dict | None):
    if not current:
        return None

    # PDF extraction order varies by exchange/template. The prior-year single
    # value may appear either before or after the current forecast range.
    left = section[current.get("anchor_end", current["anchor_start"]):current["match_start_abs"]]
    right = section[current["match_end_abs"]:current["match_end_abs"]+320]

    candidates = []
    for side_name, region in (("BEFORE_CURRENT", left), ("AFTER_CURRENT", right)):
        for m in LABELLED_SINGLE_PAT.finditer(region):
            label, x = m.group(1), num(m.group(2))
            unit = m.group(3) or current.get("unit")
            lo, hi = apply_sign(x, x, label, "净利润")
            candidates.append((side_name, m.start(), {
                "lo": lo, "hi": hi, "unit": unit,
                "evidence": m.group(0)[:220],
                "source": f"SAME_ROW_{side_name}",
            }))

    if not candidates:
        return None

    # Prefer the closest labelled single to the current range.
    before = [x for x in candidates if x[0] == "BEFORE_CURRENT"]
    after = [x for x in candidates if x[0] == "AFTER_CURRENT"]
    if before:
        return sorted(before, key=lambda x: x[1])[-1][2]
    return sorted(after, key=lambda x: x[1])[0][2]


def _first_amount_match(region: str):
    """
    Return the earliest amount/range in a prior-period region.

    V0.2.2 always preferred a range if one existed anywhere in the region.
    That could skip an earlier true prior-year single amount and accidentally
    capture a repeated current-period forecast range later in the narrative.
    If a range and single start at the same character, the range wins.
    """
    rm = RANGE_PAT.search(region)
    sm = SINGLE_PAT.search(region)
    if rm is None and sm is None:
        return None, None
    if rm is not None and (sm is None or rm.start() <= sm.start()):
        return "RANGE", rm
    return "SINGLE", sm


def parse_prior_from_prior_section(text: str):
    t = normalize_for_search(text)
    msec = re.search(r"二[、.．]\s*上年同期", t)
    if not msec:
        return None
    sec = t[msec.start():msec.start()+3500]
    a = ANCHOR_PAT.search(sec)
    if not a:
        return None
    region = sec[a.end():a.end()+300]
    global_unit = unit_near(sec, a.start())
    kind, m = _first_amount_match(region)
    if m is None:
        return None
    if kind == "RANGE":
        label = m.group(1)
        x1,u1,x2,u2 = num(m.group(2)),m.group(3),num(m.group(4)),m.group(5)
        unit = u1 or u2 or global_unit
        lo,hi = apply_sign(x1,x2,label,a.group(1))
    else:
        label = m.group(1)
        x,unit = num(m.group(2)),m.group(3) or global_unit
        lo,hi = apply_sign(x,x,label,a.group(1))
    return {
        "lo":lo, "hi":hi, "unit":unit,
        "evidence":(a.group(0)+region[:m.end()])[:300],
        "source":"PRIOR_SECTION",
    }


def parse_signed_yoy(section: str, current: dict | None):
    if current:
        region = section[current["anchor_start"]:current["anchor_start"]+1000]
    else:
        region = section[:2000]
    m = YOY_SIGNED_PAT.search(region)
    if not m:
        return None
    word = m.group(1)
    a = float(m.group(2))
    b = float(m.group(3)) if m.group(3) else a
    sign = -1.0 if word in {"下降","减少","下滑"} else 1.0
    vals = sorted([sign*a, sign*b])
    return {"lo":vals[0], "hi":vals[1], "evidence":m.group(0)[:260]}


def profit_state(cur_yuan_lo, cur_yuan_hi):
    if cur_yuan_lo is None or cur_yuan_hi is None:
        return "UNKNOWN"
    if cur_yuan_hi < 0:
        return "LOSS"
    if cur_yuan_lo > 0:
        return "PROFIT"
    return "CROSS_ZERO"


def _numeric_change_type(current, prior):
    if not current or not prior:
        return None
    c_lo = to_yuan(current["lo"], current["unit"])
    c_hi = to_yuan(current["hi"], current["unit"])
    p_lo = to_yuan(prior["lo"], prior["unit"])
    p_hi = to_yuan(prior["hi"], prior["unit"])
    if None in (c_lo, c_hi, p_lo, p_hi):
        return None

    cmid = (c_lo + c_hi) / 2
    pmid = (p_lo + p_hi) / 2
    if pmid < 0 < cmid:
        return "TURNAROUND"
    if pmid > 0 > cmid:
        return "TURN_TO_LOSS"
    eps = max(abs(pmid) * 0.02, 1.0)
    if abs(cmid - pmid) <= eps:
        return "FLAT"
    if cmid > pmid:
        if cmid < 0 and pmid < 0:
            return "LOSS_NARROWING"
        return "IMPROVING"
    if cmid < pmid:
        if cmid < 0 and pmid < 0:
            return "LOSS_WIDENING"
        return "DETERIORATING"
    return None


def _same_numeric_range(current, prior, rel_tol: float = 1e-9) -> bool:
    """
    Detect the reviewed failure mode where the prior parser re-read the
    current-period amount/range.
    """
    if not current or not prior:
        return False
    for a, b in (
        (to_yuan(current["lo"], current["unit"]), to_yuan(prior["lo"], prior["unit"])),
        (to_yuan(current["hi"], current["unit"]), to_yuan(prior["hi"], prior["unit"])),
    ):
        if a is None or b is None:
            return False
        scale = max(abs(a), abs(b), 1.0)
        if abs(a - b) > scale * rel_tol:
            return False
    return True


def _yoy_override_type(current, signed_yoy):
    if not signed_yoy:
        return None
    mid = (signed_yoy["lo"] + signed_yoy["hi"]) / 2
    c_lo = to_yuan(current["lo"], current["unit"]) if current else None
    c_hi = to_yuan(current["hi"], current["unit"]) if current else None

    # Current loss plus <= -100% reported net-profit YoY strongly indicates
    # prior-year profit -> current-year loss.
    if None not in (c_lo, c_hi) and c_hi < 0 and mid <= -100:
        return "TURN_TO_LOSS"
    if mid > 0:
        return "IMPROVING"
    if mid < 0:
        return "DETERIORATING"
    return "FLAT"


def semantic_conflict(current, prior, signed_yoy, change_type) -> bool:
    """
    Conservative confidence guard.

    Do not broadly override loss-widening/narrowing based on the sign of a
    percentage phrase, because some announcements describe growth of the loss
    amount. We only flag the reviewed high-risk pattern: numeric FLAT while a
    material (>5%) reported net-profit YoY move is present.
    """
    if not signed_yoy or change_type != "FLAT":
        return False
    mid = (signed_yoy["lo"] + signed_yoy["hi"]) / 2
    return abs(mid) > 5.0


def derive_change(section: str, current, prior, signed_yoy):
    # Selected checkbox templates have highest priority. Choose the EARLIEST
    # selected option, because some announcements contain a second sub-period
    # table (e.g. Q3) with a different selected category.
    selected = []
    for pat, label in (
        (SELECTED_TURNAROUND, "TURNAROUND"),
        (SELECTED_UP, "IMPROVING"),
        (SELECTED_DOWN, "DETERIORATING"),
    ):
        m = pat.search(section[:2200])
        if m:
            selected.append((m.start(), label))
    if selected:
        return sorted(selected, key=lambda x: x[0])[0][1], "SELECTED_CHECKBOX"

    # Numeric current-vs-prior comparison is the preferred semantic basis.
    numeric_type = _numeric_change_type(current, prior)
    if numeric_type:
        # Reviewed failure mode: the "prior" amount is an exact duplicate of
        # the current forecast while the announcement reports a material YoY
        # move. In that narrow case, the prior amount is treated as suspect.
        if (
            numeric_type == "FLAT"
            and _same_numeric_range(current, prior)
            and signed_yoy
            and abs((signed_yoy["lo"] + signed_yoy["hi"]) / 2) > 5.0
        ):
            override = _yoy_override_type(current, signed_yoy)
            if override:
                return override, "YOY_OVERRIDE_DUPLICATE_PRIOR"
        return numeric_type, "NUMERIC_CURRENT_VS_PRIOR"

    # Signed YoY anchored to the primary net-profit region.
    if signed_yoy:
        mid = (signed_yoy["lo"] + signed_yoy["hi"]) / 2
        if mid > 0:
            return "IMPROVING", "SIGNED_NET_PROFIT_YOY"
        if mid < 0:
            return "DETERIORATING", "SIGNED_NET_PROFIT_YOY"

    # Conservative prose fallbacks.
    if TURN_TO_LOSS_PAT.search(section):
        return "TURN_TO_LOSS", "PROSE"
    if LOSS_NARROW_PAT.search(section):
        return "LOSS_NARROWING", "PROSE"
    if LOSS_WIDEN_PAT.search(section):
        return "LOSS_WIDENING", "PROSE"
    if FLAT_PAT.search(section):
        return "FLAT", "PROSE"
    if IMPROVE_PAT.search(section):
        return "IMPROVING", "PROSE"
    if DETERIORATE_PAT.search(section):
        return "DETERIORATING", "PROSE"
    return "UNKNOWN", None


def confidence(
    eligible: bool,
    period,
    current,
    change_type,
    prior=None,
    signed_yoy=None,
    change_basis=None,
):
    if not eligible:
        return "EXCLUDED"
    score = int(period is not None) + int(current is not None) + int(change_type != "UNKNOWN")
    level = "HIGH" if score == 3 else ("MEDIUM" if score == 2 else "LOW")

    # A reviewed consistency conflict is not allowed to remain HIGH.
    if level == "HIGH" and (
        semantic_conflict(current, prior, signed_yoy, change_type)
        or change_basis == "YOY_OVERRIDE_DUPLICATE_PRIOR"
    ):
        return "MEDIUM"
    return level


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default=str(EVENTS))
    ap.add_argument("--pdf-pilot-root", default=str(PDF_PILOT_ROOT))
    ap.add_argument("--pilot-dir", default=None)
    ap.add_argument("--out-root", default=str(OUT_ROOT))
    args = ap.parse_args()

    events_path = Path(args.events)
    pilot_dir = Path(args.pilot_dir) if args.pilot_dir else find_latest_complete_pdf_pilot(Path(args.pdf_pilot_root))
    pdf_dir = pilot_dir / "pdf"
    if not events_path.exists():
        raise RuntimeError(f"events missing: {events_path}")
    if not pdf_dir.is_dir():
        raise RuntimeError(f"pdf dir missing: {pdf_dir}")

    out = Path(args.out_root) / f"pilot_{datetime.now():%Y%m%d_%H%M%S}"
    out.mkdir(parents=True, exist_ok=False)

    con = duckdb.connect(":memory:")
    try:
        ev = con.execute(f"SELECT * FROM read_parquet('{qpath(events_path)}')").df()
    finally:
        con.close()

    ev["announcementId"] = ev["announcementId"].astype(str)
    meta = ev.set_index("announcementId", drop=False)
    pymupdf = select_pdf_engine()

    rows = []
    pdfs = sorted(pdf_dir.glob("*.pdf"))
    if len(pdfs) < 50:
        raise RuntimeError(f"expected completed pilot PDFs, found only {len(pdfs)}")

    for i,pdf in enumerate(pdfs, start=1):
        ann_id = pdf.stem
        if ann_id not in meta.index:
            raise RuntimeError(f"announcementId not found in canonical metadata: {ann_id}")
        r = meta.loc[ann_id]
        title = str(r["announcementTitle"])
        text = extract_pdf_text(pdf, pymupdf)
        section = forecast_section(text)

        kind = event_kind(title)
        explicit_st = bool(ST_PAT.search(text[:800]))
        eligible = kind in {"ORIGINAL_FORECAST","FORECAST_REVISION"} and not explicit_st

        period = report_period(title, text)
        current = parse_amount_after_anchor(section)
        # Prefer an explicit "上年同期" section over looser same-row/table fallback.
        prior = parse_prior_from_prior_section(text) or parse_prior_from_table(section, current)
        yoy = parse_signed_yoy(section, current)
        change_type, change_basis = derive_change(section, current, prior, yoy)

        c_lo_y = to_yuan(current["lo"], current["unit"]) if current else None
        c_hi_y = to_yuan(current["hi"], current["unit"]) if current else None
        p_lo_y = to_yuan(prior["lo"], prior["unit"]) if prior else None
        p_hi_y = to_yuan(prior["hi"], prior["unit"]) if prior else None

        derived_change_pct = None
        if None not in (c_lo_y,c_hi_y,p_lo_y,p_hi_y):
            cmid = (c_lo_y+c_hi_y)/2
            pmid = (p_lo_y+p_hi_y)/2
            if abs(pmid) > 1:
                derived_change_pct = (cmid-pmid)/abs(pmid)*100

        conf = confidence(
            eligible, period, current, change_type,
            prior=prior, signed_yoy=yoy, change_basis=change_basis,
        )

        rows.append({
            "announcementId": ann_id,
            "secCode": r.get("secCode"),
            "secName": r.get("secName"),
            "event_time": r.get("event_time"),
            "title": title,
            "event_kind": kind,
            "explicit_st_at_event": explicit_st,
            "strategy_eligible_semantics": eligible,
            "exclusion_reason": (
                kind if kind not in {"ORIGINAL_FORECAST","FORECAST_REVISION"}
                else ("EXPLICIT_ST_AT_EVENT" if explicit_st else None)
            ),
            "report_period": period,
            "current_profit_min": current["lo"] if current else None,
            "current_profit_max": current["hi"] if current else None,
            "current_profit_unit": current["unit"] if current else None,
            "current_profit_min_yuan": c_lo_y,
            "current_profit_max_yuan": c_hi_y,
            "current_profit_evidence": current["evidence"] if current else None,
            "prior_profit_min": prior["lo"] if prior else None,
            "prior_profit_max": prior["hi"] if prior else None,
            "prior_profit_unit": prior["unit"] if prior else None,
            "prior_profit_min_yuan": p_lo_y,
            "prior_profit_max_yuan": p_hi_y,
            "prior_profit_evidence": prior["evidence"] if prior else None,
            "prior_profit_source": prior["source"] if prior else None,
            "profit_state": profit_state(c_lo_y,c_hi_y),
            "reported_net_profit_yoy_min_pct": yoy["lo"] if yoy else None,
            "reported_net_profit_yoy_max_pct": yoy["hi"] if yoy else None,
            "reported_yoy_evidence": yoy["evidence"] if yoy else None,
            "profit_change_vs_prior_abs_pct": derived_change_pct,
            "change_type": change_type,
            "change_basis": change_basis,
            "parser_confidence": conf,
            "pdf_path": str(pdf),
            "text_preview": normalize_for_search(text)[:1200],
        })
        print(
            f"[{i:02d}/{len(pdfs):02d}] {ann_id} kind={kind} st={explicit_st} "
            f"period={period} profit={'Y' if current else 'N'} "
            f"change={change_type} confidence={conf}"
        )

    rdf = pd.DataFrame(rows)
    rdf.to_csv(out/"semantic_parser_results.csv", index=False, encoding="utf-8-sig")

    # Manual review: all exclusions first, then LOW/MEDIUM, then deterministic HIGH examples.
    review = pd.concat([
        rdf[~rdf["strategy_eligible_semantics"]],
        rdf[rdf["strategy_eligible_semantics"] & rdf["parser_confidence"].eq("LOW")],
        rdf[rdf["strategy_eligible_semantics"] & rdf["parser_confidence"].eq("MEDIUM")],
        rdf[rdf["strategy_eligible_semantics"] & rdf["parser_confidence"].eq("HIGH")],
    ], ignore_index=True).drop_duplicates("announcementId").head(30)
    review.to_csv(out/"manual_review_30.csv", index=False, encoding="utf-8-sig")

    eligible_df = rdf[rdf["strategy_eligible_semantics"]].copy()
    summary = {
        "status": "PASS_FORECAST_SEMANTIC_PARSER_PILOT_V0_2",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_pdf_pilot_dir": str(pilot_dir),
        "sample_pdfs": int(len(rdf)),
        "event_kind_counts": {str(k):int(v) for k,v in rdf["event_kind"].value_counts().to_dict().items()},
        "explicit_st_at_event_count": int(rdf["explicit_st_at_event"].sum()),
        "strategy_eligible_rows": int(len(eligible_df)),
        "eligible_coverage": {
            "report_period_rate": float(eligible_df["report_period"].notna().mean()) if len(eligible_df) else None,
            "current_profit_rate": float(eligible_df["current_profit_min_yuan"].notna().mean()) if len(eligible_df) else None,
            "current_profit_raw_rate": float(eligible_df["current_profit_min"].notna().mean()) if len(eligible_df) else None,
            "current_profit_unit_rate_when_amount_found": (
                float(eligible_df.loc[eligible_df["current_profit_min"].notna(), "current_profit_unit"].notna().mean())
                if eligible_df["current_profit_min"].notna().any() else None
            ),
            "prior_profit_rate": float(eligible_df["prior_profit_min_yuan"].notna().mean()) if len(eligible_df) else None,
            "change_type_rate": float((eligible_df["change_type"]!="UNKNOWN").mean()) if len(eligible_df) else None,
            "high_confidence_rate": float((eligible_df["parser_confidence"]=="HIGH").mean()) if len(eligible_df) else None,
        },
        "profit_state_counts": {str(k):int(v) for k,v in eligible_df["profit_state"].value_counts().to_dict().items()},
        "change_type_counts": {str(k):int(v) for k,v in eligible_df["change_type"].value_counts().to_dict().items()},
        "confidence_counts": {str(k):int(v) for k,v in rdf["parser_confidence"].value_counts().to_dict().items()},
        "scale_gate": {
            "automatic_coverage_target": (
                "eligible current_profit >= 0.90 AND change_type >= 0.80 "
                "AND high_confidence >= 0.75"
            ),
            "manual_correctness_gate": (
                "Manual review must show signed profit/event-subject/direction semantics "
                "are acceptably correct before any full-corpus extraction."
            ),
        },
        "no_network": True,
        "no_ocr": True,
        "no_database_mutation": True,
        "output_dir": str(out),
        "outputs": [
            "semantic_parser_results.csv",
            "manual_review_30.csv",
            "semantic_parser_summary.json",
        ],
    }
    write_json_atomic(out/"semantic_parser_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
