from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PARSER_PATH = ROOT / "scripts" / "audit_forecast_semantic_parser_v02.py"
CORPUS = Path(r"D:\positionx_data\events\forecast_semantic_corpus_v0_1\chunks")

CASES = [
    {
        "id": "1219010124", "month": "2024-01",
        "title": "2023年度业绩预告暨商誉减值风险提示的公告",
        "expected_kind": "ORIGINAL_FORECAST",
        "expected_change": "DETERIORATING",
        "expected_eligible": True,
    },
    {
        "id": "1219027994", "month": "2024-01",
        "title": "2023年度业绩预告暨商誉及资产减值风险提示的公告",
        "expected_kind": "ORIGINAL_FORECAST",
        "expected_change": "TURN_TO_LOSS",
        "expected_eligible": True,
    },
    {
        "id": "1222354431", "month": "2025-01",
        "title": "2024年年度业绩预告",
        "expected_kind": "ORIGINAL_FORECAST",
        "expected_change": "DETERIORATING",
        "expected_eligible": True,
    },
    {
        "id": "1222355597", "month": "2025-01",
        "title": "立昂微2024年年度业绩预告",
        "expected_kind": "ORIGINAL_FORECAST",
        "expected_change": "TURN_TO_LOSS",
        "expected_eligible": True,
    },
    {
        "id": "1219013043", "month": "2024-01",
        "title": "中化国际关于上海证券交易所对公司业绩预告事项监管工作函的回复公告",
        "expected_kind": "FOLLOWUP_DOCUMENT",
        "expected_eligible": False,
    },
    {
        "id": "1218830353", "month": "2024-01",
        "title": "公司控股子公司海马财务有限公司2023年度业绩预告",
        "expected_kind": "SUBSIDIARY_FORECAST",
        "expected_eligible": False,
    },
    {
        "id": "1224960347", "month": "2026-01",
        "title": "上海创兴资源开发股份有限公司2025年度业绩预告暨风险提示的公告",
        "expected_kind": "ORIGINAL_FORECAST",
        "expected_change": "LOSS_NARROWING",
        "expected_eligible": False,
        "expected_st": True,
    },
]


def load_parser():
    spec = importlib.util.spec_from_file_location("positionx_parser_v023", str(PARSER_PATH))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load parser: {PARSER_PATH}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    p = load_parser()
    pymupdf = p.select_pdf_engine()
    rows = []
    all_ok = True

    for c in CASES:
        pdf = CORPUS / c["month"] / "pdf" / f"{c['id']}.pdf"
        if not pdf.exists():
            raise RuntimeError(f"Missing committed corpus PDF: {pdf}")

        text = p.extract_pdf_text(pdf, pymupdf)
        section = p.forecast_section(text)
        kind = p.event_kind(c["title"])
        explicit_st = bool(p.ST_PAT.search(text[:800]))
        eligible = kind in {"ORIGINAL_FORECAST", "FORECAST_REVISION"} and not explicit_st
        period = p.report_period(c["title"], text)
        current = p.parse_amount_after_anchor(section)
        prior = p.parse_prior_from_prior_section(text) or p.parse_prior_from_table(section, current)
        yoy = p.parse_signed_yoy(section, current)
        change_type, change_basis = p.derive_change(section, current, prior, yoy)
        conf = p.confidence(
            eligible, period, current, change_type,
            prior=prior, signed_yoy=yoy, change_basis=change_basis,
        )

        checks = {
            "kind": kind == c["expected_kind"],
            "eligible": eligible == c["expected_eligible"],
        }
        if "expected_change" in c:
            checks["change"] = change_type == c["expected_change"]
        if "expected_st" in c:
            checks["st"] = explicit_st == c["expected_st"]

        ok = all(checks.values())
        all_ok = all_ok and ok
        rows.append({
            "announcementId": c["id"],
            "ok": ok,
            "checks": checks,
            "kind": kind,
            "explicit_st": explicit_st,
            "eligible": eligible,
            "period": period,
            "change_type": change_type,
            "change_basis": change_basis,
            "confidence": conf,
            "prior_source": prior.get("source") if prior else None,
            "current_evidence": current.get("evidence") if current else None,
            "prior_evidence": prior.get("evidence") if prior else None,
            "yoy_evidence": yoy.get("evidence") if yoy else None,
        })

    result = {
        "status": "PASS_FORECAST_SEMANTIC_PARSER_V0_2_3_REGRESSION" if all_ok
                  else "FAIL_FORECAST_SEMANTIC_PARSER_V0_2_3_REGRESSION",
        "all_ok": all_ok,
        "cases": rows,
        "no_network": True,
        "no_ocr": True,
        "no_database_mutation": True,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
