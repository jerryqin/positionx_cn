from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

CST = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config/research/optimus_evidence_acquisition_v0_4.json"
SYMBOL_RE = re.compile(r"^\d{6}\.(?:SH|SZ)$")

REGISTRY_V031_SHA256 = "3955c6deb314bee4bf6821ae9cc0d2f09c606d0fbd695f34877afa3d4b782884"
SEED_V02_SHA256 = "4d14a523026408a9b8be13197ab4023ac0e31231499d2a0c9791def0d0e74b15"

ROBOT_TERMS = [
    "人形机器人", "具身智能", "Optimus", "特斯拉机器人", "机器人执行器",
    "行星滚柱丝杠", "滚柱丝杠", "谐波减速器", "无框力矩电机", "空心杯电机",
    "六维力传感器", "力矩传感器", "灵巧手", "关节模组", "机器人关节", "人形力控",
]
# Seed backfill must not rely on announcement-title/theme-keyword hits.  We fetch all
# announcements on the known source dates for the seed symbols, then inspect the
# primary PDF text locally.  These broader terms are used only as a relevance gate.
SEED_RELEVANCE_TERMS = list(dict.fromkeys(ROBOT_TERMS + [
    "机器人", "人形", "具身", "执行器", "丝杠", "减速器", "无框", "空心杯",
    "力传感", "力矩", "灵巧手", "关节", "运动控制", "编码器", "3D视觉",
    "深度相机", "轴承", "钕铁硼", "永磁",
]))
TESLA_TERMS = ["Tesla", "TESLA", "特斯拉", "Optimus", "擎天柱"]
UNNAMED_HEAD_TERMS = ["海外头部客户", "头部机器人客户", "头部整机厂", "世界知名科技公司", "海外头部机器人客户", "海外头部机器人"]

# V0.4.4: classification is context-scoped and milestone-strict. Whole-document keyword co-occurrence
# is not sufficient to infer commercialization or a Tesla/Optimus link.
ROBOT_CONTEXT_ANCHORS = [
    "人形机器人", "具身机器人", "具身智能", "仿生机器人", "机器人执行器",
    "机器人减速器", "机器人核心部件", "机器人零部件", "机器人业务",
    "机器人产品", "机器人客户", "机器人相关", "机器人视觉", "机器人关节",
    "机器人电机", "灵巧手", "Optimus", "特斯拉机器人", "擎天柱",
]
STRONG_COMPONENT_ANCHORS = [
    "行星滚柱丝杠", "滚柱丝杠", "谐波减速器", "无框力矩电机", "空心杯电机",
    "六维力传感器", "力矩传感器", "关节模组", "电机转子",
]
NON_COMMERCIAL_DOC_TITLE_PATTERNS = [
    r"公司章程", r"法律意见书", r"募集资金", r"验资报告", r"股东大会",
    r"董事会决议", r"监事会决议", r"保荐.*意见", r"专项核查意见",
]

COMPONENT_MAP = [
    (("滚柱丝杠", "丝杠"), "planetary roller screws / linear drive"),
    (("谐波减速器", "减速器", "精密齿轮"), "reducers / precision transmission"),
    (("无框力矩电机", "空心杯电机", "伺服电机", "电机转子"), "motors / motor components"),
    (("六维力传感器", "力矩传感器", "力传感器", "触觉"), "force / torque / tactile sensing"),
    (("灵巧手",), "dexterous hand"),
    (("执行器", "关节模组", "机器人关节", "直线关节", "旋转关节"), "actuators / joint modules"),
    (("3D视觉", "3D 视觉", "深度相机", "机器人视觉"), "3D vision / sensing"),
    (("轴承",), "bearings"),
    (("磁材", "钕铁硼", "永磁"), "magnetic materials"),
    (("控制器", "编码器", "运动控制"), "control / encoders"),
]

LEVEL_PATTERNS = [
    # L9: realized robot-specific commercialization. "交付能力"/industry forecasts
    # are deliberately excluded; L9 requires actual batch/recurring delivery or
    # robot-specific realized revenue/shipments.
    (9, [
        r"(?:人形机器人|具身机器人|仿生机器人|机器人(?:核心部件|相关零部件|零部件|业务|产品|执行器|电机|关节)|Optimus|擎天柱).{0,70}(?:批量交付|小批量(?:产品)?(?:交付|供货|出货)|持续(?:交付|供货|出货)|交付放量(?:期)?|批量(?:供货|出货)|规模化(?:交付|供货|出货)(?!能力))",
        r"(?:批量交付|小批量(?:产品)?(?:交付|供货|出货)|持续(?:交付|供货|出货)|交付放量(?:期)?|批量(?:供货|出货)|规模化(?:交付|供货|出货)(?!能力)).{0,70}(?:人形机器人|具身机器人|仿生机器人|机器人(?:核心部件|相关零部件|零部件|业务|产品|执行器|电机|关节)|Optimus|擎天柱)",
        r"机器人(?:相关)?(?:核心)?(?:零部件|业务|产品|部件).{0,32}(?:实现|形成|贡献|取得|录得)?(?:销售)?(?:收入|营收)",
        r"(?:销售)?(?:收入|营收).{0,32}机器人(?:相关)?(?:核心)?(?:零部件|业务|产品|部件)",
        r"(?:公司|本公司).{0,24}(?:具身智能|人形机器人|仿生机器人)(?:相关)?业务.{0,40}(?:累计)?(?:出货量|交付量|收入|营收)",
    ]),
    # L8: company-specific explicit mass production, not an industry forecast.
    (8, [
        r"(?:公司|本公司|报告期内公司).{0,60}(?:人形机器人|具身机器人|仿生机器人|机器人(?:业务|产品|客户|零部件|部件|执行器|关节)|Optimus|擎天柱).{0,60}(?:量产订单|进入量产|实现量产|开始量产|规模化量产)",
        r"(?:公司|本公司|报告期内公司).{0,100}(?:量产订单|进入量产|实现量产|开始量产|规模化量产).{0,60}(?:人形机器人|具身机器人|仿生机器人|机器人(?:业务|产品|客户|零部件|部件|执行器|关节)|Optimus|擎天柱)",
    ]),
    # L7: robot-specific order / contract. Order growth is L7, not L9.
    (7, [
        r"(?:人形机器人|具身机器人|仿生机器人|机器人(?:业务|产品|客户|零部件|部件|执行器|关节)|Optimus|擎天柱).{0,70}(?:采购订单|在手订单|订单规模增长|获得.{0,12}订单|签订.{0,16}(?:合同|协议))",
        r"(?:采购订单|在手订单|订单规模增长|获得.{0,12}订单|签订.{0,16}(?:合同|协议)).{0,70}(?:人形机器人|具身机器人|仿生机器人|机器人(?:业务|产品|客户|零部件|部件|执行器|关节)|Optimus|擎天柱)",
    ]),
    # L6: robot-specific dedicated capacity / line build-out.
    (6, [
        r"(?:公司|本公司|报告期内公司).{0,80}(?:人形机器人|具身机器人|仿生机器人|机器人(?:业务|产品|客户|零部件|部件|执行器|关节)|Optimus|擎天柱).{0,70}(?:专用产线|产线.{0,16}(?:爬坡|建设|投产)|扩产|产能.{0,16}(?:建设|提升|扩张))",
        r"(?:公司|本公司|报告期内公司).{0,100}(?:专用产线|产线.{0,16}(?:爬坡|建设|投产)|扩产|产能.{0,16}(?:建设|提升|扩张)).{0,70}(?:人形机器人|具身机器人|仿生机器人|机器人(?:业务|产品|客户|零部件|部件|执行器|关节)|Optimus|擎天柱)",
    ]),
    # L5: company-specific sample / customer-validation milestone. Generic industry
    # phrases such as “from functional validation to mass production” are NOT L5.
    (5, [
        r"(?:公司|本公司|报告期内公司).{0,80}(?:人形机器人|具身机器人|仿生机器人|机器人(?:客户项目|产品|零部件|部件|执行器|关节)|Optimus|擎天柱).{0,60}(?:已?送样|样品(?:验证|测试)|客户(?:验证|认证|测试)|进入客户验证|通过客户认证)",
        r"(?:公司|本公司|报告期内公司).{0,110}(?:已?送样|样品(?:验证|测试)|客户(?:验证|认证|测试)|进入客户验证|通过客户认证).{0,70}(?:人形机器人|具身机器人|仿生机器人|机器人(?:客户项目|产品|零部件|部件|执行器|关节)|Optimus|擎天柱)",
        r"(?:人形机器人|具身机器人|仿生机器人|机器人(?:客户项目|产品|零部件|部件|执行器|关节)|Optimus|擎天柱).{0,60}(?:已向.{0,12}客户送样|客户验证阶段|客户测试阶段|样品验证阶段)",
    ]),
    # L4: robot/Tesla-specific designation or supplier nomination.
    (4, [
        r"(?:公司|本公司|报告期内公司).{0,80}(?:人形机器人|具身机器人|仿生机器人|机器人(?:业务|产品|客户|零部件|部件|执行器|关节)|Tesla|TESLA|特斯拉|Optimus|擎天柱|世界知名科技公司).{0,70}(?:定点|供应商资格|供应商.{0,10}(?:提名|选定|指定))",
        r"(?:公司|本公司|报告期内公司).{0,100}(?:定点|供应商资格|供应商.{0,10}(?:提名|选定|指定)).{0,70}(?:人形机器人|具身机器人|仿生机器人|机器人(?:业务|产品|客户|零部件|部件|执行器|关节)|Tesla|TESLA|特斯拉|Optimus|擎天柱|世界知名科技公司)",
        r"(?:人形机器人|具身机器人|仿生机器人|机器人(?:客户项目|项目|产品|零部件|部件|执行器|关节)|Optimus|擎天柱).{0,50}(?:定点|供应商资格|供应商.{0,10}(?:提名|选定|指定))",
        r"(?:定点|供应商资格|供应商.{0,10}(?:提名|选定|指定)).{0,50}(?:人形机器人|具身机器人|仿生机器人|机器人(?:客户项目|项目|产品|零部件|部件|执行器|关节)|Optimus|擎天柱)",
    ]),
    # L3: explicit positive named Tesla/Optimus relationship, not an investor question.
    (3, [
        r"(?:公司|本公司).{0,36}(?:与|向|为).{0,16}(?:Tesla|TESLA|特斯拉|Optimus|擎天柱).{0,40}(?:合作|客户|供应|供货|项目|送样|验证)",
        r"(?:Tesla|TESLA|特斯拉|Optimus|擎天柱).{0,36}(?:为公司客户|与公司合作|向公司采购|公司供应|公司供货|公司项目)",
    ]),
    # L2: company-specific robot product/business/customer statement.
    (2, [
        r"机器人(?:客户|核心部件|相关零部件|零部件|产品|业务|部件|模组|执行器|关节|视觉|电机)",
        r"(?:人形机器人|具身机器人|仿生机器人).{0,40}(?:产品|业务|零部件|部件|模组|执行器|关节|视觉|电机|客户)",
    ]),
]

HARD_NEGATIVE_PATTERNS = [
    r"(?:未|没有|不存在).{0,12}(?:向|与).{0,8}(?:Tesla|TESLA|特斯拉|Optimus|擎天柱).{0,20}(?:供货|合作|订单|定点)",
    r"(?:不是|并非).{0,10}(?:Tesla|TESLA|特斯拉|Optimus|擎天柱).{0,10}(?:供应商|合作方)",
    r"(?:未收到|未获得|不存在).{0,12}(?:Tesla|TESLA|特斯拉|Optimus|擎天柱).{0,12}订单",
]
SOFT_NEGATIVE_PATTERNS = [r"尚未.{0,12}量产", r"尚未.{0,12}形成收入", r"处于.{0,8}(?:样品|送样|验证)阶段"]


def now_cst() -> datetime:
    return datetime.now(CST)


def iso_now() -> str:
    return now_cst().isoformat()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def stable_json(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def stable_id(prefix: str, x: Any, n: int = 24) -> str:
    return f"{prefix}_{sha256_bytes(stable_json(x).encode('utf-8'))[:n]}"


def load_json(p: Path) -> dict[str, Any]:
    return json.loads(p.read_text(encoding="utf-8"))


def resolve_path(v: str) -> Path:
    p = Path(v)
    return p if p.is_absolute() else ROOT / p


def symbol_from_cninfo(code: str) -> str | None:
    c = str(code or "").strip()
    if not re.fullmatch(r"\d{6}", c):
        return None
    if c.startswith(("0", "2", "3")):
        return c + ".SZ"
    if c.startswith(("6", "9")):
        return c + ".SH"
    return None


def clean_html(s: str) -> str:
    return re.sub(r"</?em>", "", str(s or ""))


def cninfo_post(cfg: dict[str, Any], se_date: str, page_num: int, searchkey: str = "") -> dict[str, Any]:
    ccfg = cfg["cninfo"]
    body = urllib.parse.urlencode({
        "pageNum": page_num,
        "pageSize": int(ccfg.get("page_size", 30)),
        "column": "", "tabName": "fulltext", "plate": "", "stock": "",
        "searchkey": searchkey, "secid": "", "category": "", "trade": "",
        "seDate": se_date, "sortName": "", "sortType": "", "isHLtitle": "true",
    }).encode()
    req = urllib.request.Request(
        ccfg["api"], data=body, method="POST",
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    last = None
    for i in range(int(ccfg.get("tries", 4))):
        try:
            with urllib.request.urlopen(req, timeout=int(ccfg.get("timeout_seconds", 30))) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as exc:
            last = exc
            time.sleep(1.2 * (i + 1))
    raise RuntimeError(f"cninfo failed date={se_date} page={page_num} keyword={searchkey}: {last!r}")


def fetch_url_bytes(url: str, timeout: int, tries: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as exc:
            last = exc
            time.sleep(1.0 * (i + 1))
    raise RuntimeError(f"download failed url={url}: {last!r}")


def pdf_parser_available() -> bool:
    try:
        import pypdf  # noqa: F401
        return True
    except Exception:
        return False


def extract_pdf_text(pdf_bytes: bytes, max_pages: int) -> tuple[str, int, str | None]:
    try:
        from pypdf import PdfReader
    except Exception as exc:
        return "", 0, f"PYPDF_UNAVAILABLE:{exc!r}"
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
        chunks: list[str] = []
        n = min(len(reader.pages), max_pages)
        for i in range(n):
            try:
                t = reader.pages[i].extract_text() or ""
            except Exception:
                t = ""
            if t:
                chunks.append(t)
        return "\n".join(chunks), n, None
    except Exception as exc:
        return "", 0, f"PDF_PARSE_ERROR:{exc!r}"


V045_PARSER = "OPTIMUS_EVIDENCE_ACQUISITION_V0_4_5"
V045_AMBIGUOUS_DOMAIN = "AMBIGUOUS_MULTI_DOMAIN_PENDING_REVIEW"
V045_UNKNOWN_DOMAIN = "UNKNOWN_PENDING_REVIEW"
V045_MIN_WINDOW_MARGIN = 2
V045_MIN_WINDOW_RATIO = 1.5


def component_domain_v044(text: str) -> str:
    """Frozen V0.4.4 first-match behavior retained only for compatibility diagnostics."""
    low = str(text or "").lower()
    for terms, domain in COMPONENT_MAP:
        if any(str(t).lower() in low for t in terms):
            return domain
    return V045_UNKNOWN_DOMAIN


def _component_domain_scores_v045(windows: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for idx, (terms, domain) in enumerate(COMPONENT_MAP):
        window_hits = 0
        occurrence_count = 0
        term_counts: dict[str, int] = {}
        for w in windows:
            low = str(w or "").lower()
            hit = False
            for t in terms:
                c = low.count(str(t).lower())
                if c:
                    hit = True
                    occurrence_count += c
                    term_counts[str(t)] = term_counts.get(str(t), 0) + c
            if hit:
                window_hits += 1
        if window_hits or occurrence_count:
            rows.append({
                "map_index": idx,
                "domain": domain,
                "window_hit_count": window_hits,
                "occurrence_count": occurrence_count,
                "term_counts": term_counts,
            })
    rows.sort(key=lambda x: (-int(x["window_hit_count"]),
                             -int(x["occurrence_count"]),
                             int(x["map_index"])))
    return rows


def component_domain_decision_v045(text: str) -> tuple[str, dict[str, Any]]:
    """Future-only domain classifier.

    V0.4.5 keeps the frozen V0.4.4 first-match result only when it is also the
    unique context-window leader. A different domain may replace it only when
    window dominance is material (margin >=2 and ratio >=1.5). Other multi-domain
    evidence is sent to human review instead of being guessed.
    """
    evidence_text = str(text or "")
    windows = context_windows(evidence_text)
    if not windows:
        windows = [evidence_text]
    scores = _component_domain_scores_v045(windows)
    legacy = component_domain_v044("\n".join(windows) or evidence_text)

    if not scores:
        domain = V045_UNKNOWN_DOMAIN
        decision = "NO_HITS"
        top = second = None
        margin = None
        ratio = None
    elif len(scores) == 1:
        domain = scores[0]["domain"]
        decision = "SINGLE_DOMAIN"
        top, second = scores[0], None
        margin = None
        ratio = None
    else:
        top, second = scores[0], scores[1]
        top_unique = int(top["window_hit_count"]) > int(second["window_hit_count"])
        margin = int(top["window_hit_count"]) - int(second["window_hit_count"])
        ratio = (int(top["window_hit_count"]) / int(second["window_hit_count"])
                 if int(second["window_hit_count"]) > 0 else 999.0)
        if top_unique and top["domain"] == legacy:
            domain = legacy
            decision = "LEGACY_DOMAIN_IS_UNIQUE_TOP"
        elif (top_unique and margin >= V045_MIN_WINDOW_MARGIN
              and ratio >= V045_MIN_WINDOW_RATIO):
            domain = top["domain"]
            decision = "NEW_UNIQUE_DOMINANT_DOMAIN"
        else:
            domain = V045_AMBIGUOUS_DOMAIN
            decision = "AMBIGUOUS_MULTI_DOMAIN_PENDING_REVIEW"

    diag = {
        "policy_id": "COMPONENT_DOMAIN_CONTEXT_WINDOW_DOMINANCE_V0_4_5",
        "legacy_first_match_domain": legacy,
        "candidate_domain": domain,
        "decision": decision,
        "context_window_count": len(windows),
        "scores": scores,
        "top": top,
        "second": second,
        "margin": margin,
        "ratio": ratio,
        "human_review_required": domain == V045_AMBIGUOUS_DOMAIN,
    }
    return domain, diag


def component_domain(text: str) -> str:
    """Current production component-domain API (V0.4.5 future-only)."""
    return component_domain_decision_v045(text)[0]


def _flat_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _anchor_terms() -> list[str]:
    return list(dict.fromkeys(ROBOT_CONTEXT_ANCHORS + STRONG_COMPONENT_ANCHORS))


def context_windows(text: str, radius: int = 260, max_windows: int = 48) -> list[str]:
    """Return local windows around robot/strong-component anchors.

    V0.4.1 classified the entire PDF as one bag of words. That allowed unrelated
    revenue/order/capacity language elsewhere in a 200-page report to inflate a
    robot evidence level. V0.4.4 classifies only within local anchor windows and applies milestone-specific rules.
    """
    flat = _flat_text(text)
    low = flat.lower()
    spans: list[tuple[int, int]] = []
    for term in _anchor_terms():
        tl = term.lower()
        pos = 0
        while True:
            i = low.find(tl, pos)
            if i < 0:
                break
            spans.append((max(0, i - radius), min(len(flat), i + len(term) + radius)))
            pos = i + max(1, len(term))
            if len(spans) >= max_windows * 4:
                break
    if not spans:
        return []
    spans.sort()
    merged: list[tuple[int, int]] = []
    for a, b in spans:
        if not merged or a > merged[-1][1] + 40:
            merged.append((a, b))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
    return [flat[a:b] for a, b in merged[:max_windows]]


def document_level_cap(title: str) -> int:
    t = str(title or "")
    if any(re.search(p, t, flags=re.I) for p in NON_COMMERCIAL_DOC_TITLE_PATTERNS):
        return 2
    return 9


def _operational_robot_window(w: str) -> bool:
    """Reject governance/name-list-only robot mentions from becoming L1 evidence."""
    if re.search(r"(?:受同一最终控制方控制|关联方|关联交易|股东|董事|监事|章程|企业名称|公司名称)", w, flags=re.I):
        if not re.search(r"(?:产品|业务|客户|研发|合作|收入|营收|订单|产能|量产|交付|供货|销售|应用|市场|项目|需求|核心部件|解决方案|定点|送样|验证)", w, flags=re.I):
            return False
    return bool(re.search(r"(?:产品|业务|客户|研发|合作|收入|营收|订单|产能|量产|交付|供货|销售|应用|市场|项目|需求|核心部件|解决方案|定点|送样|验证|机器人减速器|机器人视觉|灵巧手|关节模组)", w, flags=re.I))


def classify_evidence_level(text: str, title: str = "") -> tuple[int, str, str]:
    """Return proposed level, matched rule, and a compact basis excerpt."""
    windows = context_windows(text)
    if not windows:
        return 0, "NO_ROBOT_OR_STRONG_COMPONENT_CONTEXT", ""
    cap = document_level_cap(title)
    for level, pats in LEVEL_PATTERNS:
        for w in windows:
            for pat in pats:
                m = re.search(pat, w, flags=re.I | re.S)
                if m:
                    actual = min(level, cap)
                    basis = w[max(0, m.start()-140): min(len(w), m.end()+180)]
                    rule = f"L{level}:{pat}"
                    if actual != level:
                        rule += f":CAPPED_TO_L{actual}_BY_DOCUMENT_ROLE"
                    return actual, rule, basis
    # Strong robot/component context without a stronger commercial milestone.
    # Governance/name-list-only mentions are not economic evidence.
    operational = [w for w in windows if _operational_robot_window(w)]
    if not operational:
        return 0, "NO_OPERATIONAL_ROBOT_EVIDENCE_CONTEXT", windows[0][:520]
    return 1, "L1:OPERATIONAL_ROBOT_OR_STRONG_COMPONENT_CONTEXT", operational[0][:520]


def max_evidence_level(text: str, title: str = "") -> int:
    return classify_evidence_level(text, title)[0]


def tesla_link_assessment(text: str, evidence_level: int) -> tuple[int, str, str]:
    """Strict Tesla/Optimus link assessment.

    A named Tesla mention in an investor question, rumor, industry comparison, or
    denial is not a supplier/customer relationship. T3/T4 requires an affirmative
    company-specific relation. T2 is reserved for an unnamed head-client relation
    that is both robot-specific and commercial.
    """
    flat = _flat_text(text)

    named_patterns = [
        r"(?:公司|本公司).{0,40}(?:与|向|为).{0,18}(Tesla|TESLA|特斯拉|Optimus|擎天柱).{0,50}(?:合作|客户|供应|供货|项目|送样|验证|定点|订单|量产|交付)",
        r"(Tesla|TESLA|特斯拉|Optimus|擎天柱).{0,40}(?:为公司客户|与公司合作|向公司采购|公司供应|公司供货|公司项目|公司已获.{0,12}定点|公司已获.{0,12}订单)",
    ]
    for pat in named_patterns:
        m = re.search(pat, flat, flags=re.I | re.S)
        if m:
            basis = flat[max(0, m.start()-120): min(len(flat), m.end()+180)]
            # Investor questions, rumors, hypothetical wording and explicit denials are mentions, not links.
            if re.search(r"(?:传闻|是否|有无|请问|Q\d|不属实|未筹划|未商谈|未接触|若|如果)", basis, flags=re.I):
                continue
            commercial = bool(re.search(r"(?:定点|订单|量产|交付|供货|供应商|送样|验证|认证|测试)", m.group(0), flags=re.I))
            lvl = 4 if evidence_level >= 4 and commercial else 3
            return lvl, f"NAMED_AFFIRMATIVE:{m.group(1)}", basis

    for term in UNNAMED_HEAD_TERMS:
        pos = flat.find(term)
        while pos >= 0:
            seg = flat[max(0, pos-220): min(len(flat), pos+len(term)+300)]
            robot_specific = any(a in seg for a in ROBOT_CONTEXT_ANCHORS + STRONG_COMPONENT_ANCHORS)
            # T2 is only an EXISTING affirmative unnamed-head relationship clue.
            # Conditional / hypothetical / question / denial language is not a link.
            conditional = bool(re.search(r"(?:未来若|若|如果|有无|是否|请问|传闻|不属实|未筹划|未商谈|未接触|尚未|计划|有望)", seg, flags=re.I))
            affirmative = bool(re.search(r"(?:已获得|已获|已成为|已经成为|与.{0,20}合作|正在.{0,12}(?:供货|交付)|已.{0,12}(?:供货|交付|送样)|项目定点|小批量产品交付|批量交付)", seg, flags=re.I))
            commercial = bool(re.search(r"(?:定点|订单|量产|交付|供货|合作|送样|客户验证)", seg, flags=re.I))
            if robot_specific and commercial and affirmative and not conditional:
                return 2, f"UNNAMED_HEAD_AFFIRMATIVE_EXISTING:{term}", seg[:620]
            pos = flat.find(term, pos + len(term))

    return 0, "NO_AFFIRMATIVE_TESLA_LINK", ""

def tesla_link_level(text: str, evidence_level: int) -> int:
    return tesla_link_assessment(text, evidence_level)[0]


def contradiction(text: str) -> tuple[str | None, str | None]:
    if re.search(r"(?:Tesla|TESLA|特斯拉).{0,80}(?:传闻不属实|不属实|未筹划|未商谈|未接触)", text, flags=re.I | re.S):
        return "NAMED_TESLA_RUMOR_DENIAL", "HARD"
    if any(re.search(p, text, flags=re.I | re.S) for p in HARD_NEGATIVE_PATTERNS):
        return "SUPPLIER_OR_ORDER_DENIAL", "HARD"
    if any(re.search(p, text, flags=re.I | re.S) for p in SOFT_NEGATIVE_PATTERNS):
        return "COMMERCIALIZATION_NOT_YET_CONFIRMED", "SOFT"
    return None, None


def snippets_for(text: str, terms: list[str], radius: int, max_snippets: int) -> list[str]:
    if not text:
        return []
    flat = re.sub(r"\s+", " ", text)
    spans: list[tuple[int, int]] = []
    low = flat.lower()
    for term in terms:
        pos = 0
        tl = term.lower()
        while True:
            i = low.find(tl, pos)
            if i < 0:
                break
            spans.append((max(0, i - radius), min(len(flat), i + len(term) + radius)))
            pos = i + max(1, len(term))
            if len(spans) >= max_snippets * 3:
                break
    spans.sort()
    out: list[str] = []
    for a, b in spans:
        s = flat[a:b].strip()
        if s and all(s not in x for x in out):
            out.append(s)
        if len(out) >= max_snippets:
            break
    return out


def seed_text_is_relevant(text: str) -> bool:
    # Broad words such as "轴承" or "永磁" alone are not sufficient. Require a
    # robot anchor or a strong component anchor that is specific enough to merit review.
    return bool(context_windows(text))


def event_time_from_row(row: dict[str, Any]) -> str | None:
    x = pd.to_numeric(row.get("announcementTime"), errors="coerce")
    if pd.isna(x):
        return None
    ts = pd.to_datetime(x, unit="ms", utc=True, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.tz_convert(CST).isoformat()


def load_registry_module(cfg: dict[str, Any]):
    p = resolve_path(cfg["registry_script"])
    spec = importlib.util.spec_from_file_location("positionx_optimus_registry_v03_runtime", p)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import registry module: {p}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def query_window(cfg: dict[str, Any], dates: list[date], scope_symbols: set[str] | None, raw_root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    by_ann: dict[str, dict[str, Any]] = {}
    stats = {"queries": 0, "rows_seen": 0, "valid_a_share_rows": 0, "out_of_scope_rows": 0, "errors": []}
    kws = list(cfg["cninfo"]["keywords"])
    for di, d in enumerate(dates, 1):
        ds = d.strftime("%Y-%m-%d")
        for ki, kw in enumerate(kws, 1):
            print(f"[CNINFO] date {di}/{len(dates)} {ds} keyword {ki}/{len(kws)} {kw}", file=sys.stderr, flush=True)
            stats["queries"] += 1
            try:
                first = cninfo_post(cfg, ds, 1, kw)
                pages = [first]
                n = int(first.get("totalpages") or 0)
                for pageno in range(2, n + 1):
                    time.sleep(float(cfg["cninfo"].get("delay_seconds", 0.45)))
                    pages.append(cninfo_post(cfg, ds, pageno, kw))
                fn = f"{d.strftime('%Y%m%d')}_{hashlib.sha1(kw.encode('utf-8')).hexdigest()[:10]}.json"
                (raw_root / fn).write_text(json.dumps(pages, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
                for payload in pages:
                    for row0 in payload.get("announcements") or []:
                        stats["rows_seen"] += 1
                        row = dict(row0)
                        symbol = symbol_from_cninfo(row.get("secCode"))
                        if not symbol:
                            stats["out_of_scope_rows"] += 1
                            continue
                        stats["valid_a_share_rows"] += 1
                        if scope_symbols is not None and symbol not in scope_symbols:
                            continue
                        ann_id = str(row.get("announcementId") or "") or stable_id("ANN", row)
                        item = by_ann.setdefault(ann_id, {"row": row, "symbol": symbol, "matched_keywords": set()})
                        item["matched_keywords"].add(kw)
            except Exception as exc:
                stats["errors"].append(f"{ds} {kw}: {exc!r}")
            time.sleep(float(cfg["cninfo"].get("delay_seconds", 0.45)))
    return by_ann, stats


def query_seed_all_announcements(
    cfg: dict[str, Any],
    dates: list[date],
    scope_symbols: set[str],
    raw_root: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Fetch every CNINFO announcement on the seed source dates, then filter locally.

    This deliberately avoids thematic search keys.  Periodic reports and IR records often
    have generic titles, so a keyword search can miss the very documents that contain the
    relevant robot/Optimus evidence in the PDF body.
    """
    by_ann: dict[str, dict[str, Any]] = {}
    stats = {
        "queries": 0, "pages": 0, "rows_seen": 0, "valid_a_share_rows": 0,
        "seed_scope_rows": 0, "out_of_scope_rows": 0, "errors": [],
    }
    for di, d in enumerate(dates, 1):
        ds = d.strftime("%Y-%m-%d")
        print(f"[CNINFO-SEED-ALL] date {di}/{len(dates)} {ds}", file=sys.stderr, flush=True)
        try:
            stats["queries"] += 1
            first = cninfo_post(cfg, ds, 1, "")
            pages = [first]
            n = int(first.get("totalpages") or 0)
            for pageno in range(2, n + 1):
                time.sleep(float(cfg["cninfo"].get("delay_seconds", 0.45)))
                pages.append(cninfo_post(cfg, ds, pageno, ""))
            stats["pages"] += len(pages)
            fn = f"{d.strftime('%Y%m%d')}_ALL_ANNOUNCEMENTS.json"
            (raw_root / fn).write_text(json.dumps(pages, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            for payload in pages:
                for row0 in payload.get("announcements") or []:
                    stats["rows_seen"] += 1
                    row = dict(row0)
                    symbol = symbol_from_cninfo(row.get("secCode"))
                    if not symbol:
                        stats["out_of_scope_rows"] += 1
                        continue
                    stats["valid_a_share_rows"] += 1
                    if symbol not in scope_symbols:
                        continue
                    stats["seed_scope_rows"] += 1
                    ann_id = str(row.get("announcementId") or "") or stable_id("ANN", row)
                    by_ann.setdefault(ann_id, {"row": row, "symbol": symbol, "matched_keywords": set()})
        except Exception as exc:
            stats["errors"].append(f"{ds} ALL: {exc!r}")
        time.sleep(float(cfg["cninfo"].get("delay_seconds", 0.45)))
    return by_ann, stats


def build_evidence(cfg: dict[str, Any], item: dict[str, Any], capture_root: Path, observed_at: str, local_pdf_path: Path | None = None) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    row = item["row"]
    symbol = item["symbol"]
    kws = sorted(item["matched_keywords"])
    ann_id = str(row.get("announcementId") or "")
    title = clean_html(row.get("announcementTitle"))
    company = clean_html(row.get("secName"))
    adjunct = str(row.get("adjunctUrl") or "")
    pdf_url = cfg["cninfo"].get("static_root", "") + adjunct if adjunct else ""
    metadata_payload = {"announcement": row, "matched_keywords": kws}
    metadata_bytes = stable_json(metadata_payload).encode("utf-8")
    metadata_sha = sha256_bytes(metadata_bytes)
    pdf_sha = None
    pdf_path = None
    text = ""
    pages = 0
    parse_error = None
    download_error = None
    if local_pdf_path is not None:
        try:
            b = local_pdf_path.read_bytes()
            pdf_sha = sha256_bytes(b)
            pdf_path = local_pdf_path
            text, pages, parse_error = extract_pdf_text(b, int(cfg["pdf"].get("max_pages", 260)))
        except Exception as exc:
            download_error = f"LOCAL_PDF_READ_ERROR:{exc!r}"
    elif cfg.get("pdf", {}).get("download", True) and pdf_url:
        try:
            b = fetch_url_bytes(pdf_url, int(cfg["pdf"].get("timeout_seconds", 45)), int(cfg["pdf"].get("tries", 3)))
            pdf_sha = sha256_bytes(b)
            pdf_dir = capture_root / "pdf"
            pdf_dir.mkdir(parents=True, exist_ok=True)
            pdf_path = pdf_dir / f"{ann_id or metadata_sha[:16]}.pdf"
            if pdf_path.exists() and sha256_file(pdf_path) != pdf_sha:
                raise RuntimeError(f"immutable pdf path hash conflict: {pdf_path}")
            if not pdf_path.exists():
                pdf_path.write_bytes(b)
            text, pages, parse_error = extract_pdf_text(b, int(cfg["pdf"].get("max_pages", 260)))
        except Exception as exc:
            download_error = repr(exc)
    evidence_text = "\n".join([title, " ".join(kws), text])
    relevant = seed_text_is_relevant(evidence_text)
    if not relevant:
        audit = {
            "evidence_id": None, "symbol": symbol, "company": company,
            "announcement_id": ann_id, "title": title, "matched_keywords": kws,
            "level": None, "tesla_link": None, "component_domain": None,
            "contradiction_kind": None, "source_type": "CNINFO_PRIMARY_PDF" if pdf_sha else "CNINFO_FULLTEXT_METADATA",
            "pdf_sha256": pdf_sha, "pdf_pages_parsed": pages,
            "parse_error": parse_error, "download_error": download_error,
            "disposition": "SCANNED_NOT_RELEVANT",
        }
        return None, audit
    level, level_rule, level_basis = classify_evidence_level(evidence_text, title)
    if level <= 0:
        zero_domain, zero_domain_diag = component_domain_decision_v045(evidence_text)
        audit = {
            "evidence_id": None, "symbol": symbol, "company": company,
            "announcement_id": ann_id, "title": title, "matched_keywords": kws,
            "level": 0, "tesla_link": 0, "component_domain": zero_domain,
            "component_domain_decision": zero_domain_diag,
            "contradiction_kind": None, "source_type": "CNINFO_PRIMARY_PDF" if pdf_sha else "CNINFO_FULLTEXT_METADATA",
            "pdf_sha256": pdf_sha, "pdf_pages_parsed": pages,
            "parse_error": parse_error, "download_error": download_error,
            "disposition": "SCANNED_NOT_OPERATIONAL_EVIDENCE",
            "level_rule": level_rule, "level_basis": level_basis[:700],
        }
        return None, audit
    tlink, tlink_rule, tlink_basis = tesla_link_assessment(evidence_text, level)
    ckind, csev = contradiction(evidence_text)
    domain, domain_diag = component_domain_decision_v045(evidence_text)
    terms = list(dict.fromkeys(kws + TESLA_TERMS + ["定点", "送样", "验证", "订单", "量产", "批量", "交付", "供货", "收入", "产线", "客户"]))
    snippets = snippets_for(text, terms, int(cfg["pdf"].get("snippet_radius", 260)), int(cfg["pdf"].get("max_snippets", 12)))
    source_type = "CNINFO_PRIMARY_PDF" if pdf_sha else "CNINFO_FULLTEXT_METADATA"
    source_sha = pdf_sha or metadata_sha
    event_time = event_time_from_row(row)
    confidence = 0.82 if pdf_sha and text else (0.68 if pdf_sha else 0.55)
    evidence_id = stable_id("EACQ", {
        "source": "CNINFO", "announcement_id": ann_id, "symbol": symbol,
        "source_sha256": source_sha, "level": level, "tesla": tlink,
        "contradiction_kind": ckind, "domain": domain,
        "parser_version": V045_PARSER,
    })
    interpretation = (
        f"V0.4.5 milestone-strict official-source acquisition; keywords={kws}; "
        f"component_domain_decision={domain_diag.get('decision')}; "
        f"legacy_component_domain={domain_diag.get('legacy_first_match_domain')}; "
        f"proposed_level=L{level}; level_rule={level_rule}; tesla_link={tlink}; "
        f"tesla_rule={tlink_rule}; title={title}; level_basis={level_basis[:700]!r}; "
        f"tesla_basis={tlink_basis[:500]!r}; snippets={snippets[:3]}"
    )
    evidence = {
        "evidence_id": evidence_id,
        "symbol": symbol,
        "company": company,
        "component_domain": domain,
        "evidence_level": level,
        "evidence_type": "OFFICIAL_DISCLOSURE_FULLTEXT_HIT",
        "tesla_link_level": tlink,
        "source_type": source_type,
        "source_ref": pdf_url or f"CNINFO:{ann_id}",
        "source_sha256": source_sha,
        "event_time": event_time,
        "observed_at": observed_at,
        "review_state": "PROVISIONAL",
        "confidence": confidence,
        "contradiction_kind": ckind,
        "contradiction_severity": csev,
        "parser_version": V045_PARSER,
        "interpretation": interpretation,
        "acquisition": {
            "announcement_id": ann_id,
            "announcement_title": title,
            "matched_keywords": kws,
            "metadata_sha256": metadata_sha,
            "pdf_sha256": pdf_sha,
            "pdf_path": str(pdf_path) if pdf_path else None,
            "pdf_pages_parsed": pages,
            "pdf_parse_error": parse_error,
            "pdf_download_error": download_error,
            "snippets": snippets,
            "level_rule": level_rule,
            "level_basis": level_basis,
            "tesla_rule": tlink_rule,
            "tesla_basis": tlink_basis,
            "component_domain_policy": "COMPONENT_DOMAIN_CONTEXT_WINDOW_DOMINANCE_V0_4_5",
            "component_domain_decision": domain_diag.get("decision"),
            "component_domain_legacy_first_match": domain_diag.get("legacy_first_match_domain"),
            "component_domain_scores": domain_diag.get("scores"),
            "component_domain_review_required": bool(domain_diag.get("human_review_required")),
            "document_level_cap": document_level_cap(title),
            "auto_canonical": False,
        },
    }
    audit = {
        "evidence_id": evidence_id, "symbol": symbol, "company": company,
        "announcement_id": ann_id, "title": title, "matched_keywords": kws,
        "level": level, "tesla_link": tlink, "component_domain": domain,
        "component_domain_decision": domain_diag,
        "level_rule": level_rule, "tesla_rule": tlink_rule,
        "contradiction_kind": ckind, "source_type": source_type,
        "pdf_sha256": pdf_sha, "pdf_pages_parsed": pages,
        "parse_error": parse_error, "download_error": download_error,
        "disposition": "PROVISIONAL_EVIDENCE",
    }
    return evidence, audit


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) for r in rows)
    if content:
        content += "\n"
    if path.exists() and path.read_text(encoding="utf-8") != content:
        raise RuntimeError(f"immutable evidence file conflict: {path}")
    path.write_text(content, encoding="utf-8")


def ingest_with_registry(cfg: dict[str, Any], evidence_file: Path) -> dict[str, Any]:
    if evidence_file.stat().st_size == 0:
        return {"status": "NO_EVIDENCE_TO_INGEST", "rows_seen": 0, "rows_inserted": 0, "reconcile": {"symbols": 0, "changed": 0}}
    reg = load_registry_module(cfg)
    rcfg = reg.load_config(resolve_path(cfg["registry_config"]))
    con = reg.open_db(rcfg, read_only=False)
    try:
        reg.init_tables(con)
        ing = reg.ingest_jsonl(con, rcfg, evidence_file, allow_canonical_input=False)
        rec = reg.reconcile_all(con, rcfg)
        maint = reg.maintenance_queue_payload(con, rcfg)
        return {"status": "INGESTED", "ingest": ing, "reconcile": {"symbols": rec["symbols"], "changed": rec["changed"]}, "maintenance": maint["summary"]}
    finally:
        con.close()


def date_range_inclusive(start_s: str, end_s: str) -> list[date]:
    start = pd.Timestamp(start_s).date()
    end = pd.Timestamp(end_s).date()
    if end < start:
        raise ValueError(f"end before start: {start_s} > {end_s}")
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def current_seed_debt_symbols(cfg: dict[str, Any]) -> set[str]:
    reg = load_registry_module(cfg)
    rcfg = reg.load_config(resolve_path(cfg["registry_config"]))
    con = reg.open_db(rcfg, read_only=True)
    try:
        payload = reg.maintenance_queue_payload(con, rcfg)
        return {str(x["symbol"]).upper() for x in payload.get("seed_audit_debt", [])}
    finally:
        con.close()


def _clean_identity_value(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if not s or s.lower() in {"nan", "none", "<na>"}:
        return ""
    return s


def evidence_source_identity(row: dict[str, Any]) -> str:
    symbol = _clean_identity_value(row.get("symbol")).upper()
    source_sha = _clean_identity_value(row.get("source_sha256"))
    source_ref = _clean_identity_value(row.get("source_ref"))
    if source_sha:
        return f"{symbol}|sha256:{source_sha}"
    if source_ref:
        return f"{symbol}|ref:{source_ref}"
    return ""


def registry_existing_source_identities(cfg: dict[str, Any]) -> set[str]:
    """Cross-parser dedupe gate: preserve historical V0.4.4 rows as historical."""
    reg = load_registry_module(cfg)
    rcfg = reg.load_config(resolve_path(cfg["registry_config"]))
    con = reg.open_db(rcfg, read_only=True)
    try:
        df = reg.latest_effective_evidence(con)
        if df.empty:
            return set()
        out: set[str] = set()
        for _, r in df.iterrows():
            sid = evidence_source_identity(r.to_dict())
            if sid:
                out.add(sid)
        return out
    finally:
        con.close()


def capture(cfg: dict[str, Any], dates: list[date], scope_symbols: set[str] | None, mode: str) -> dict[str, Any]:
    observed = iso_now()
    end = max(dates)
    capture_id = now_cst().strftime("%Y%m%dT%H%M%S+0800")
    root = Path(cfg["output_root"]) / mode / end.strftime("%Y%m%d") / capture_id
    root.mkdir(parents=True, exist_ok=False)
    raw_root = root / "cninfo_raw"
    raw_root.mkdir(parents=True, exist_ok=False)
    if mode in {"seed_backfill", "seed_debt_backfill"}:
        if scope_symbols is None:
            raise RuntimeError(f"{mode} requires a symbol scope")
        by_ann, qstats = query_seed_all_announcements(cfg, dates, scope_symbols, raw_root)
    else:
        by_ann, qstats = query_window(cfg, dates, scope_symbols, raw_root)
    evidence: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    existing_source_identities = registry_existing_source_identities(cfg)
    skipped_existing_source_across_parser = 0
    for i, ann_id in enumerate(sorted(by_ann), 1):
        item = by_ann[ann_id]
        print(f"[ARTIFACT] {i}/{len(by_ann)} {item['symbol']} announcement={ann_id}", file=sys.stderr, flush=True)
        ev, au = build_evidence(cfg, item, root, observed)
        if ev is not None:
            sid = evidence_source_identity(ev)
            if sid and sid in existing_source_identities:
                skipped_existing_source_across_parser += 1
                au = dict(au)
                au["disposition"] = "SKIP_EXISTING_SOURCE_ACROSS_PARSER_VERSIONS"
                au["source_identity"] = sid
            else:
                evidence.append(ev)
        audits.append(au)
    evidence_file = root / "evidence_provisional.jsonl"
    write_jsonl(evidence_file, evidence)
    inbox = Path(cfg["evidence_inbox"]) / f"optimus_v045_{mode}_{capture_id}.jsonl"
    write_jsonl(inbox, evidence)
    ingest = ingest_with_registry(cfg, inbox)
    manifest = {
        "schema_version": V045_PARSER,
        "acquisition_id": cfg["acquisition_id"],
        "mode": mode,
        "capture_id": capture_id,
        "observed_at": observed,
        "date_min": min(dates).isoformat(),
        "date_max": max(dates).isoformat(),
        "scope": "ALL_SH_SZ_A_SHARE" if scope_symbols is None else "REGISTRY_SEED_SCOPE",
        "scope_symbol_count": None if scope_symbols is None else len(scope_symbols),
        "query_stats": qstats,
        "announcement_count": len(by_ann),
        "evidence_count": len(evidence),
        "skipped_existing_source_across_parser": skipped_existing_source_across_parser,
        "scanned_not_relevant": sum(1 for a in audits if a.get("disposition") == "SCANNED_NOT_RELEVANT"),
        "source_type_counts": pd.Series([a["source_type"] for a in audits]).value_counts().to_dict() if audits else {},
        "proposed_level_counts": pd.Series([a["level"] for a in audits]).value_counts().sort_index().to_dict() if audits else {},
        "pdf_downloaded": sum(1 for a in audits if a["pdf_sha256"]),
        "pdf_parse_errors": sum(1 for a in audits if a["parse_error"]),
        "download_errors": sum(1 for a in audits if a["download_error"]),
        "evidence_file": str(evidence_file),
        "evidence_file_sha256": sha256_file(evidence_file),
        "inbox_file": str(inbox),
        "inbox_file_sha256": sha256_file(inbox),
        "ingest": ingest,
        "audits": audits,
        "policy": {"review_state": "PROVISIONAL", "auto_canonical_upgrade": False},
        "safety": {"research_only": True, "broker": False, "orders": False, "production_db": False},
    }
    mbytes = stable_json(manifest).encode("utf-8")
    manifest["manifest_payload_sha256"] = sha256_bytes(mbytes)
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return {"status": "SUCCESS" if not qstats["errors"] else "PARTIAL", "output": str(root), **{k: manifest[k] for k in ("mode", "date_min", "date_max", "scope_symbol_count", "query_stats", "announcement_count", "evidence_count", "skipped_existing_source_across_parser", "scanned_not_relevant", "source_type_counts", "proposed_level_counts", "pdf_downloaded", "pdf_parse_errors", "download_errors", "ingest", "manifest_payload_sha256")}}


def run_check(cfg: dict[str, Any]) -> int:
    reg = resolve_path(cfg["registry_script"])
    seed = resolve_path(cfg["seed_universe"])
    checks = {
        "registry_script": str(reg),
        "registry_script_exists": reg.exists(),
        "registry_script_sha256": sha256_file(reg) if reg.exists() else None,
        "registry_v0_3_1_expected_sha256": REGISTRY_V031_SHA256,
        "registry_compatible": bool(reg.exists() and sha256_file(reg) == REGISTRY_V031_SHA256),
        "seed_universe": str(seed),
        "seed_exists": seed.exists(),
        "seed_sha256": sha256_file(seed) if seed.exists() else None,
        "seed_v0_2_expected_sha256": SEED_V02_SHA256,
        "seed_compatible": bool(seed.exists() and sha256_file(seed) == SEED_V02_SHA256),
        "pdf_text_parser_available": pdf_parser_available(),
        "initial_review_state": cfg["policy"]["initial_review_state"],
        "auto_canonical_upgrade": cfg["policy"]["auto_canonical_upgrade"],
        "keyword_count": len(cfg["cninfo"]["keywords"]),
        "seed_backfill_method": "ALL_ANNOUNCEMENTS_ON_SOURCE_DATES_THEN_LOCAL_PDF_RELEVANCE_GATE",
        "classifier_method": "CONTEXT_SCOPED_MILESTONE_STRICT_V0_4_5_COMPONENT_DOMAIN_DOMINANCE",
        "whole_document_commercial_cooccurrence_allowed": False,
        "legacy_v041_quarantine_supported": True,
        "legacy_v042_quarantine_supported": True,
        "legacy_v043_quarantine_supported": True,
        "local_reclassification_supported": True,
        "seed_debt_range_backfill_supported": True,
        "seed_debt_range_backfill_method": "ALL_ANNOUNCEMENTS_PER_DATE_THEN_FILTER_CURRENT_SEED_AUDIT_DEBT",
        "research_only": True,
        "broker": False,
        "production_db": False,
    }
    ok = checks["registry_compatible"] and checks["seed_compatible"]
    print(json.dumps({"status": "PASS_OPTIMUS_EVIDENCE_ACQUISITION_V0_4_5_CHECK" if ok else "FAIL_OPTIMUS_EVIDENCE_ACQUISITION_V0_4_5_CHECK", **checks}, ensure_ascii=False, indent=2))
    return 0 if ok else 2


def latest_manifest(cfg: dict[str, Any]) -> dict[str, Any]:
    root = Path(cfg["output_root"])
    files = sorted(root.rglob("manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True) if root.exists() else []
    if not files:
        return {"status": "NO_ACQUISITION_RUNS"}
    obj = json.loads(files[0].read_text(encoding="utf-8"))
    qstats = obj.get("query_stats") or {}
    quality = "COMPLETE" if not qstats.get("errors") else "PARTIAL_QUERY_ERRORS"
    latest = {k: obj.get(k) for k in ("mode", "capture_id", "observed_at", "date_min", "date_max", "scope", "scope_symbol_count", "announcement_count", "evidence_count", "scanned_not_relevant", "source_type_counts", "proposed_level_counts", "pdf_downloaded", "pdf_parse_errors", "download_errors", "ingest", "manifest_payload_sha256")}
    latest["query_stats"] = qstats
    latest["query_quality"] = quality
    return {"status": "PASS_OPTIMUS_EVIDENCE_ACQUISITION_V0_4_5_STATUS" if quality == "COMPLETE" else "PARTIAL_OPTIMUS_EVIDENCE_ACQUISITION_V0_4_5_STATUS", "latest_manifest": str(files[0]), "latest": latest}


LEGACY_V041_PARSER = "OPTIMUS_EVIDENCE_ACQUISITION_V0_4_1"
V042_PARSER = "OPTIMUS_EVIDENCE_ACQUISITION_V0_4_2"
V043_PARSER = "OPTIMUS_EVIDENCE_ACQUISITION_V0_4_3"
V044_PARSER = "OPTIMUS_EVIDENCE_ACQUISITION_V0_4_4"


def legacy_v041_provisional_payload(cfg: dict[str, Any], read_only: bool = True) -> tuple[Any, Any, pd.DataFrame]:
    reg = load_registry_module(cfg)
    rcfg = reg.load_config(resolve_path(cfg["registry_config"]))
    con = reg.open_db(rcfg, read_only=read_only)
    reg.init_tables(con) if not read_only else None
    ev = reg.latest_effective_evidence(con)
    if ev.empty:
        q = ev
    else:
        q = ev[
            (ev["parser_version"].astype(str) == LEGACY_V041_PARSER) &
            (ev["effective_review_state"].astype(str) == "PROVISIONAL")
        ].copy()
    return reg, con, q


def audit_v041(cfg: dict[str, Any]) -> dict[str, Any]:
    reg, con, q = legacy_v041_provisional_payload(cfg, read_only=True)
    try:
        by_company = q["company"].fillna("").astype(str).value_counts().to_dict() if not q.empty else {}
        by_level = pd.to_numeric(q["evidence_level"], errors="coerce").fillna(0).astype(int).value_counts().sort_index().to_dict() if not q.empty else {}
        by_tesla = pd.to_numeric(q["tesla_link_level"], errors="coerce").fillna(0).astype(int).value_counts().sort_index().to_dict() if not q.empty else {}
        return {
            "status": "PASS_OPTIMUS_V041_PROVISIONAL_AUDIT_V0_4_2",
            "parser_version": LEGACY_V041_PARSER,
            "eligible_provisional_count": int(len(q)),
            "by_company": by_company,
            "by_level": {str(k): int(v) for k, v in by_level.items()},
            "by_tesla_link": {str(k): int(v) for k, v in by_tesla.items()},
            "mutation": False,
        }
    finally:
        con.close()


def quarantine_v041(cfg: dict[str, Any]) -> dict[str, Any]:
    reg, con, q = legacy_v041_provisional_payload(cfg, read_only=False)
    try:
        if q.empty:
            return {
                "status": "PASS_OPTIMUS_V041_QUARANTINE_V0_4_2",
                "eligible": 0, "rejected": 0, "reconcile": {"symbols": 0, "changed": 0},
                "reason": "NO_EFFECTIVE_V041_PROVISIONAL_EVIDENCE",
            }
        reason = "SUPERSEDED_BY_CONTEXT_SCOPED_CLASSIFIER_V0_4_2"
        actor = "SYSTEM_MIGRATION_V0_4_2"
        con.execute("BEGIN TRANSACTION")
        rejected = 0
        try:
            for eid in q["evidence_id"].astype(str).tolist():
                reg.add_review_decision(con, eid, "REJECTED", reason, actor)
                rejected += 1
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        rec = reg.reconcile_all(con, reg.load_config(resolve_path(cfg["registry_config"])))
        return {
            "status": "PASS_OPTIMUS_V041_QUARANTINE_V0_4_2",
            "eligible": int(len(q)),
            "rejected": int(rejected),
            "append_only_review_decisions": True,
            "physical_delete": False,
            "reconcile": {"symbols": rec["symbols"], "changed": rec["changed"]},
            "reason": reason,
        }
    finally:
        con.close()



def parser_provisional_payload(cfg: dict[str, Any], parser_version: str, read_only: bool = True) -> tuple[Any, Any, pd.DataFrame]:
    reg = load_registry_module(cfg)
    rcfg = reg.load_config(resolve_path(cfg["registry_config"]))
    con = reg.open_db(rcfg, read_only=read_only)
    reg.init_tables(con) if not read_only else None
    ev = reg.latest_effective_evidence(con)
    if ev.empty:
        q = ev
    else:
        q = ev[
            (ev["parser_version"].astype(str) == parser_version) &
            (ev["effective_review_state"].astype(str) == "PROVISIONAL")
        ].copy()
    return reg, con, q


def audit_v042(cfg: dict[str, Any]) -> dict[str, Any]:
    reg, con, q = parser_provisional_payload(cfg, V042_PARSER, read_only=True)
    try:
        by_company = q["company"].fillna("").astype(str).value_counts().to_dict() if not q.empty else {}
        by_level = pd.to_numeric(q["evidence_level"], errors="coerce").fillna(0).astype(int).value_counts().sort_index().to_dict() if not q.empty else {}
        by_tesla = pd.to_numeric(q["tesla_link_level"], errors="coerce").fillna(0).astype(int).value_counts().sort_index().to_dict() if not q.empty else {}
        return {
            "status": "PASS_OPTIMUS_V042_PROVISIONAL_AUDIT_V0_4_4",
            "parser_version": V042_PARSER,
            "eligible_provisional_count": int(len(q)),
            "by_company": by_company,
            "by_level": {str(k): int(v) for k, v in by_level.items()},
            "by_tesla_link": {str(k): int(v) for k, v in by_tesla.items()},
            "mutation": False,
        }
    finally:
        con.close()


def quarantine_v042(cfg: dict[str, Any]) -> dict[str, Any]:
    reg, con, q = parser_provisional_payload(cfg, V042_PARSER, read_only=False)
    try:
        if q.empty:
            return {
                "status": "PASS_OPTIMUS_V042_QUARANTINE_V0_4_4",
                "eligible": 0, "rejected": 0, "reconcile": {"symbols": 0, "changed": 0},
                "reason": "NO_EFFECTIVE_V042_PROVISIONAL_EVIDENCE",
            }
        reason = "SUPERSEDED_BY_MILESTONE_STRICT_CLASSIFIER_V0_4_4"
        actor = "SYSTEM_MIGRATION_V0_4_4"
        con.execute("BEGIN TRANSACTION")
        rejected = 0
        try:
            for eid in q["evidence_id"].astype(str).tolist():
                reg.add_review_decision(con, eid, "REJECTED", reason, actor)
                rejected += 1
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        rec = reg.reconcile_all(con, reg.load_config(resolve_path(cfg["registry_config"])))
        return {
            "status": "PASS_OPTIMUS_V042_QUARANTINE_V0_4_4",
            "eligible": int(len(q)),
            "rejected": int(rejected),
            "append_only_review_decisions": True,
            "physical_delete": False,
            "reconcile": {"symbols": rec["symbols"], "changed": rec["changed"]},
            "reason": reason,
        }
    finally:
        con.close()


def _latest_v041_seed_manifest(cfg: dict[str, Any]) -> Path:
    root = Path(cfg["output_root"]) / "seed_backfill"
    files = sorted(root.rglob("manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True) if root.exists() else []
    for p in files:
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if obj.get("schema_version") == LEGACY_V041_PARSER and obj.get("mode") == "seed_backfill":
            return p
    raise RuntimeError("no V0.4.1 seed_backfill manifest found for local reclassification")


def _row_map_from_v041_capture(capture_root: Path, wanted_ids: set[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    raw = capture_root / "cninfo_raw"
    for p in sorted(raw.glob("*_ALL_ANNOUNCEMENTS.json")):
        pages = json.loads(p.read_text(encoding="utf-8"))
        for payload in pages:
            for row0 in payload.get("announcements") or []:
                ann_id = str(row0.get("announcementId") or "")
                if ann_id in wanted_ids and ann_id not in out:
                    out[ann_id] = dict(row0)
        if len(out) == len(wanted_ids):
            break
    return out


def audit_v043(cfg: dict[str, Any]) -> dict[str, Any]:
    reg, con, q = parser_provisional_payload(cfg, V043_PARSER, read_only=True)
    try:
        by_company = q["company"].fillna("").astype(str).value_counts().to_dict() if not q.empty else {}
        by_level = pd.to_numeric(q["evidence_level"], errors="coerce").fillna(0).astype(int).value_counts().sort_index().to_dict() if not q.empty else {}
        by_tesla = pd.to_numeric(q["tesla_link_level"], errors="coerce").fillna(0).astype(int).value_counts().sort_index().to_dict() if not q.empty else {}
        return {
            "status": "PASS_OPTIMUS_V043_PROVISIONAL_AUDIT_V0_4_4",
            "parser_version": V043_PARSER,
            "eligible_provisional_count": int(len(q)),
            "by_company": by_company,
            "by_level": {str(k): int(v) for k, v in by_level.items()},
            "by_tesla_link": {str(k): int(v) for k, v in by_tesla.items()},
            "mutation": False,
        }
    finally:
        con.close()


def quarantine_v043(cfg: dict[str, Any]) -> dict[str, Any]:
    reg, con, q = parser_provisional_payload(cfg, V043_PARSER, read_only=False)
    try:
        if q.empty:
            return {
                "status": "PASS_OPTIMUS_V043_QUARANTINE_V0_4_4",
                "eligible": 0, "rejected": 0, "reconcile": {"symbols": 0, "changed": 0},
                "reason": "NO_EFFECTIVE_V043_PROVISIONAL_EVIDENCE",
            }
        reason = "SUPERSEDED_BY_CONTEXT_QUALITY_CLASSIFIER_V0_4_4"
        actor = "SYSTEM_MIGRATION_V0_4_4"
        con.execute("BEGIN TRANSACTION")
        rejected = 0
        try:
            for eid in q["evidence_id"].astype(str).tolist():
                reg.add_review_decision(con, eid, "REJECTED", reason, actor)
                rejected += 1
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        rec = reg.reconcile_all(con, reg.load_config(resolve_path(cfg["registry_config"])))
        return {
            "status": "PASS_OPTIMUS_V043_QUARANTINE_V0_4_4",
            "eligible": int(len(q)),
            "rejected": int(rejected),
            "append_only_review_decisions": True,
            "physical_delete": False,
            "reconcile": {"symbols": rec["symbols"], "changed": rec["changed"]},
            "reason": reason,
        }
    finally:
        con.close()


def reclassify_latest_v041(cfg: dict[str, Any]) -> dict[str, Any]:
    src_manifest = _latest_v041_seed_manifest(cfg)
    src_obj = json.loads(src_manifest.read_text(encoding="utf-8"))
    src_root = src_manifest.parent
    audits0 = list(src_obj.get("audits") or [])
    wanted_ids = {str(a.get("announcement_id") or "") for a in audits0 if str(a.get("announcement_id") or "")}
    row_map = _row_map_from_v041_capture(src_root, wanted_ids)

    observed = iso_now()
    capture_id = now_cst().strftime("%Y%m%dT%H%M%S+0800")
    end = str(src_obj.get("date_max") or now_cst().date().isoformat()).replace("-", "")
    root = Path(cfg["output_root"]) / "reclassify_v0_4_4" / end / capture_id
    root.mkdir(parents=True, exist_ok=False)

    evidence: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    missing_rows = 0
    missing_pdfs = 0
    reused_pdfs = 0

    for i, a0 in enumerate(audits0, 1):
        ann_id = str(a0.get("announcement_id") or "")
        symbol = str(a0.get("symbol") or "")
        row = row_map.get(ann_id)
        if row is None:
            missing_rows += 1
            audits.append({
                "announcement_id": ann_id, "symbol": symbol,
                "disposition": "MISSING_SOURCE_ROW_IN_V041_CAPTURE",
            })
            continue
        pdf_path = src_root / "pdf" / f"{ann_id}.pdf"
        if not pdf_path.exists():
            missing_pdfs += 1
            audits.append({
                "announcement_id": ann_id, "symbol": symbol,
                "disposition": "MISSING_LOCAL_PDF_IN_V041_CAPTURE",
            })
            continue
        reused_pdfs += 1
        item = {"row": row, "symbol": symbol, "matched_keywords": set()}
        print(f"[RECLASSIFY-V044] {i}/{len(audits0)} {symbol} announcement={ann_id}", file=sys.stderr, flush=True)
        ev, au = build_evidence(cfg, item, root, observed, local_pdf_path=pdf_path)
        if ev is not None:
            ev.setdefault("acquisition", {})["reclassified_from_manifest"] = str(src_manifest)
            ev["acquisition"]["reclassified_from_manifest_sha256"] = sha256_file(src_manifest)
            evidence.append(ev)
        au["reclassified_from_v041"] = True
        audits.append(au)

    evidence_file = root / "evidence_provisional_v0_4_4.jsonl"
    write_jsonl(evidence_file, evidence)
    inbox = Path(cfg["evidence_inbox"]) / f"optimus_v043_reclassify_{capture_id}.jsonl"
    write_jsonl(inbox, evidence)
    ingest = ingest_with_registry(cfg, inbox)

    manifest = {
        "schema_version": V044_PARSER,
        "acquisition_id": cfg["acquisition_id"],
        "mode": "reclassify_v0_4_4",
        "capture_id": capture_id,
        "observed_at": observed,
        "source_manifest": str(src_manifest),
        "source_manifest_sha256": sha256_file(src_manifest),
        "date_min": src_obj.get("date_min"),
        "date_max": src_obj.get("date_max"),
        "network_queries": 0,
        "source_announcement_count": int(len(audits0)),
        "reused_pdf_count": int(reused_pdfs),
        "missing_source_rows": int(missing_rows),
        "missing_local_pdfs": int(missing_pdfs),
        "evidence_count": int(len(evidence)),
        "scanned_not_relevant": sum(1 for a in audits if a.get("disposition") == "SCANNED_NOT_RELEVANT"),
        "proposed_level_counts": pd.Series([a.get("level") for a in audits if a.get("level") is not None]).value_counts().sort_index().to_dict() if audits else {},
        "tesla_link_counts": pd.Series([a.get("tesla_link") for a in audits if a.get("tesla_link") is not None]).value_counts().sort_index().to_dict() if audits else {},
        "evidence_file": str(evidence_file),
        "evidence_file_sha256": sha256_file(evidence_file),
        "inbox_file": str(inbox),
        "inbox_file_sha256": sha256_file(inbox),
        "ingest": ingest,
        "audits": audits,
        "policy": {
            "review_state": "PROVISIONAL",
            "auto_canonical_upgrade": False,
            "classifier": "CONTEXT_SCOPED_MILESTONE_STRICT_V0_4_5_COMPONENT_DOMAIN_DOMINANCE",
            "network_requery": False,
        },
        "safety": {"research_only": True, "broker": False, "orders": False, "production_db": False},
    }
    manifest["manifest_payload_sha256"] = sha256_bytes(stable_json(manifest).encode("utf-8"))
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return {
        "status": "PASS_OPTIMUS_EVIDENCE_RECLASSIFY_V0_4_4",
        "output": str(root),
        "source_manifest": str(src_manifest),
        "network_queries": 0,
        "reused_pdf_count": reused_pdfs,
        "missing_source_rows": missing_rows,
        "missing_local_pdfs": missing_pdfs,
        "evidence_count": len(evidence),
        "scanned_not_relevant": manifest["scanned_not_relevant"],
        "proposed_level_counts": manifest["proposed_level_counts"],
        "tesla_link_counts": manifest["tesla_link_counts"],
        "ingest": ingest,
        "manifest_payload_sha256": manifest["manifest_payload_sha256"],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    sp = ap.add_subparsers(dest="command", required=True)
    sp.add_parser("check")
    s = sp.add_parser("backfill-seed"); s.add_argument("--slop-days", type=int)
    s = sp.add_parser("daily"); s.add_argument("--days", type=int, default=1)
    s = sp.add_parser("backfill-seed-debt-range")
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    sp.add_parser("audit-v041")
    sp.add_parser("quarantine-v041")
    sp.add_parser("audit-v042")
    sp.add_parser("quarantine-v042")
    sp.add_parser("audit-v043")
    sp.add_parser("quarantine-v043")
    sp.add_parser("reclassify-latest")
    sp.add_parser("status")
    args = ap.parse_args()
    cfg = load_json(Path(args.config))
    if args.command == "check":
        return run_check(cfg)
    if args.command == "status":
        print(json.dumps(latest_manifest(cfg), ensure_ascii=False, indent=2, default=str)); return 0
    if args.command == "audit-v041":
        print(json.dumps(audit_v041(cfg), ensure_ascii=False, indent=2, default=str)); return 0
    if args.command == "quarantine-v041":
        print(json.dumps(quarantine_v041(cfg), ensure_ascii=False, indent=2, default=str)); return 0
    if args.command == "audit-v042":
        print(json.dumps(audit_v042(cfg), ensure_ascii=False, indent=2, default=str)); return 0
    if args.command == "quarantine-v042":
        print(json.dumps(quarantine_v042(cfg), ensure_ascii=False, indent=2, default=str)); return 0
    if args.command == "audit-v043":
        print(json.dumps(audit_v043(cfg), ensure_ascii=False, indent=2, default=str)); return 0
    if args.command == "quarantine-v043":
        print(json.dumps(quarantine_v043(cfg), ensure_ascii=False, indent=2, default=str)); return 0
    if args.command == "reclassify-latest":
        raise RuntimeError("V0_4_5_FUTURE_ONLY: historical reclassification is frozen; use preserved V0.4.4 artifacts for audit only")
    seed = pd.read_csv(resolve_path(cfg["seed_universe"]))
    if args.command == "backfill-seed":
        raise RuntimeError("V0_4_5_FUTURE_ONLY: historical seed backfill is disabled")
        slop = int(cfg.get("backfill", {}).get("source_date_slop_days", 1) if args.slop_days is None else args.slop_days)
        ds: set[date] = set()
        for x in seed["source_date"].dropna().astype(str):
            d0 = pd.Timestamp(x).date()
            for k in range(-slop, slop + 1):
                ds.add(d0 + timedelta(days=k))
        scope = set(seed["symbol"].astype(str).str.upper())
        out = capture(cfg, sorted(ds), scope, "seed_backfill")
    elif args.command == "daily":
        end = now_cst().date()
        start = end - timedelta(days=max(0, args.days - 1))
        dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        out = capture(cfg, dates, None, "daily")
    elif args.command == "backfill-seed-debt-range":
        raise RuntimeError("V0_4_5_FUTURE_ONLY: historical seed-debt backfill is disabled")
        dates = date_range_inclusive(args.start, args.end)
        scope = current_seed_debt_symbols(cfg)
        if not scope:
            raise RuntimeError("no current seed_audit_debt symbols; refusing empty-scope backfill")
        out = capture(cfg, dates, scope, "seed_debt_backfill")
    else:
        raise RuntimeError("unknown command")
    print(json.dumps({"status": "PASS_OPTIMUS_EVIDENCE_ACQUISITION_V0_4_5" if out["status"] == "SUCCESS" else "PARTIAL_OPTIMUS_EVIDENCE_ACQUISITION_V0_4_5", **out}, ensure_ascii=False, indent=2, default=str))
    return 0 if out["status"] == "SUCCESS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
