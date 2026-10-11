from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CFG = PROJECT_ROOT / "config/opportunity/eva_forecast_parser_v0_2_residual_audit_v0_1.yaml"

SEM_NUM = [
    "current_profit_min_yuan",
    "current_profit_max_yuan",
    "prior_profit_min_yuan",
    "prior_profit_max_yuan",
    "reported_net_profit_yoy_min_pct",
    "reported_net_profit_yoy_max_pct",
    "profit_change_vs_prior_abs_pct",
]
SEM_CAT = ["profit_state", "change_type", "change_basis"]
BAD = {
    "", "<NA>", "NA", "N/A", "NONE", "UNKNOWN",
    "UNPARSED", "EXCLUDED", "AMBIGUOUS", "NOT_APPLICABLE",
}

TITLE_PATTERNS = {
    "RATING_OR_TRUSTEE": r"评级|受托管理|信用评估|信用评级|中证鹏元|新世纪评级|东方金诚|联合信用",
    "FOLLOWUP_OR_REPLY": r"问询函|回复|关注函|专项说明|补充公告|更正说明",
    "ATTACHMENT_OR_ENGLISH": r"附件|英文版|English",
    "STANDARD_FORECAST": r"业绩预告|业绩预亏|业绩预盈|业绩预增|业绩预减",
}

EVIDENCE_HINTS = [
    "current_profit_evidence",
    "prior_profit_evidence",
    "reported_yoy_evidence",
    "source_text",
    "raw_text",
    "semantic_text",
    "profit_evidence",
]


def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def norm_bool(s: pd.Series) -> pd.Series:
    return s.astype("string").str.strip().str.lower().isin(
        ["true", "1", "yes", "y", "eligible"]
    )


def strict_payload_mask(df: pd.DataFrame) -> pd.Series:
    m = pd.Series(False, index=df.index)
    for c in SEM_NUM:
        if c in df.columns:
            m |= pd.to_numeric(df[c], errors="coerce").notna()
    for c in SEM_CAT:
        if c in df.columns:
            y = df[c].astype("string").fillna("").str.strip().str.upper()
            m |= ~y.isin(BAD)
    return m


def nonempty_mask(s: pd.Series) -> pd.Series:
    x = s.astype("string").fillna("").str.strip()
    return x.ne("") & x.ne("<NA>")


def counts(s: pd.Series, limit: int = 100) -> dict:
    return {
        str(k): int(v)
        for k, v in s.astype("string").value_counts(dropna=False).head(limit).items()
    }


def title_pattern(title: str) -> str:
    t = str(title or "")
    hits = [name for name, pat in TITLE_PATTERNS.items() if re.search(pat, t, flags=re.I)]
    return "+".join(hits) if hits else "OTHER"


def max_missing_for_rate(n: int, target: float) -> int:
    if n <= 0:
        return 0
    return int(math.floor(n * (1.0 - target) + 1e-12))


def audit(path: Path, cfg: dict) -> dict:
    expected_sha = cfg["candidate_sha256"]
    actual_sha = sha256_file(path)
    if actual_sha != expected_sha:
        raise RuntimeError(
            f"candidate SHA mismatch expected={expected_sha} actual={actual_sha}"
        )

    df = pd.read_parquet(path)
    x = df.copy()
    x["_year"] = pd.to_datetime(x["event_time"], errors="coerce").dt.year.astype("Int64")
    x["_eligible"] = norm_bool(x["strategy_eligible_semantics"])
    x["_payload"] = strict_payload_mask(x)
    x["_title_pattern"] = x["title"].astype("string").fillna("").map(title_pattern)

    eligible = x[x["_eligible"]].copy()
    residual = eligible[~eligible["_payload"]].copy()

    target = float(cfg["target_rate"])

    by_year = {}
    for y, g in eligible[eligible["_year"].notna()].groupby("_year"):
        yi = int(y)
        miss = g[~g["_payload"]]
        n = len(g)
        m = len(miss)
        allowed = max_missing_for_rate(n, target)
        recover_needed = max(0, m - allowed)

        by_year[str(yi)] = {
            "eligible_rows": int(n),
            "missing_payload_rows": int(m),
            "strict_payload_rate": float(g["_payload"].mean()),
            "max_missing_rows_at_target": int(allowed),
            "minimum_additional_rows_needed_to_reach_target": int(recover_needed),
            "passes_target": bool(g["_payload"].mean() >= target),
            "parser_confidence_counts": (
                counts(miss["parser_confidence"])
                if "parser_confidence" in miss.columns else {}
            ),
            "unit_resolution_method_counts": (
                counts(miss["unit_resolution_method"])
                if "unit_resolution_method" in miss.columns else {}
            ),
            "event_kind_counts": (
                counts(miss["event_kind"])
                if "event_kind" in miss.columns else {}
            ),
            "report_period_counts": (
                counts(miss["report_period"], limit=30)
                if "report_period" in miss.columns else {}
            ),
            "title_pattern_counts": counts(miss["_title_pattern"]),
        }

    evidence_fields = [c for c in EVIDENCE_HINTS if c in x.columns]
    evidence_diag = {}
    for c in evidence_fields:
        mask = nonempty_mask(residual[c])
        evidence_diag[c] = {
            "nonempty_rows": int(mask.sum()),
            "nonempty_rate": float(mask.mean()) if len(mask) else None,
        }

    diagnostic_fields = [
        "announcementId", "symbol", "event_time", "event_precision",
        "title", "event_kind", "report_period",
        "parser_confidence", "unit_resolution_method",
        "current_profit_min_yuan", "current_profit_max_yuan",
        "prior_profit_min_yuan", "prior_profit_max_yuan",
        "reported_net_profit_yoy_min_pct", "reported_net_profit_yoy_max_pct",
        "profit_change_vs_prior_abs_pct", "profit_state",
        "change_type", "change_basis",
    ] + evidence_fields
    diagnostic_fields = [c for c in diagnostic_fields if c in residual.columns]

    out_dir = Path(cfg["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    residual_csv = out_dir / "eligible_missing_payload_rows_v0_1.csv"
    residual[diagnostic_fields].sort_values(
        ["event_time", "symbol"]
    ).to_csv(residual_csv, index=False, encoding="utf-8-sig")

    pattern_rows = []
    for (year, pattern), g in residual.groupby(["_year", "_title_pattern"], dropna=False):
        pattern_rows.append({
            "year": None if pd.isna(year) else int(year),
            "title_pattern": str(pattern),
            "rows": int(len(g)),
            "parser_confidence_top": (
                str(g["parser_confidence"].astype("string").value_counts().index[0])
                if "parser_confidence" in g.columns and len(g) else None
            ),
            "unit_resolution_top": (
                str(g["unit_resolution_method"].astype("string").value_counts().index[0])
                if "unit_resolution_method" in g.columns and len(g) else None
            ),
        })
    pattern_df = pd.DataFrame(pattern_rows)
    pattern_csv = out_dir / "eligible_missing_payload_patterns_v0_1.csv"
    pattern_df.to_csv(pattern_csv, index=False, encoding="utf-8-sig")

    y25 = by_year.get("2025", {})
    y26 = by_year.get("2026", {})

    return {
        "candidate_path": str(path),
        "candidate_sha256": actual_sha,
        "rows": int(len(x)),
        "eligible_rows": int(len(eligible)),
        "eligible_missing_payload_rows": int(len(residual)),
        "eligible_strict_payload_rate": float(eligible["_payload"].mean()),
        "target_rate": target,
        "by_year": by_year,
        "residual_parser_confidence_counts": (
            counts(residual["parser_confidence"])
            if "parser_confidence" in residual.columns else {}
        ),
        "residual_unit_resolution_method_counts": (
            counts(residual["unit_resolution_method"])
            if "unit_resolution_method" in residual.columns else {}
        ),
        "residual_event_kind_counts": (
            counts(residual["event_kind"])
            if "event_kind" in residual.columns else {}
        ),
        "residual_title_pattern_counts": counts(residual["_title_pattern"]),
        "evidence_field_diagnostics": evidence_diag,
        "gate_gap": {
            "2025_minimum_recover_needed": y25.get(
                "minimum_additional_rows_needed_to_reach_target"
            ),
            "2026_minimum_recover_needed": y26.get(
                "minimum_additional_rows_needed_to_reach_target"
            ),
        },
        "artifacts": {
            "residual_rows_csv": str(residual_csv),
            "residual_rows_csv_sha256": sha256_file(residual_csv),
            "pattern_csv": str(pattern_csv),
            "pattern_csv_sha256": sha256_file(pattern_csv),
        },
    }


def cmd_check(cfg):
    p = Path(cfg["candidate_path"])
    result = {
        "status": (
            "PASS_EVA_FORECAST_PARSER_V0_2_RESIDUAL_AUDIT_V0_1_CHECK"
            if p.exists() else
            "BLOCKED_EVA_FORECAST_PARSER_V0_2_RESIDUAL_AUDIT_V0_1_CHECK"
        ),
        "candidate_path": str(p),
        "exists": p.exists(),
        "sha256": sha256_file(p) if p.exists() else None,
        "expected_sha256": cfg["candidate_sha256"],
        "target_rate": cfg["target_rate"],
        "read_only": True,
    }
    if p.exists() and result["sha256"] != cfg["candidate_sha256"]:
        result["status"] = "BLOCKED_CANDIDATE_SHA_MISMATCH"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"].startswith("PASS") else 3


def cmd_run(cfg):
    p = Path(cfg["candidate_path"])
    if not p.exists():
        return cmd_check(cfg)

    try:
        a = audit(p, cfg)
        out = Path(cfg["output_dir"])
        out.mkdir(parents=True, exist_ok=True)

        result = {
            "status": "PASS_EVA_FORECAST_PARSER_V0_2_RESIDUAL_AUDIT_V0_1_RUN",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "research_id": cfg["research_id"],
            "audit": a,
            "decision_rule": (
                "Do not change the 0.99 gate. Do not patch merely to recover the minimum row count. "
                "Proceed to parser v0.2.1 only if residuals reveal a reusable, source-grounded structural "
                "pattern that can be fixed without changing identity/PIT/eligibility or using outcome data."
            ),
            "safety": {
                "read_only": True,
                "production_db": False,
                "broker": False,
                "qmt": False,
            },
        }

        sp = out / "eva_forecast_parser_v0_2_residual_audit_v0_1_summary.json"
        sp.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print(f"OUTPUT: {out}")
        return 0
    except Exception as e:
        print(json.dumps({
            "status": "FAIL_CLOSED_EVA_FORECAST_PARSER_V0_2_RESIDUAL_AUDIT_V0_1",
            "error": f"{type(e).__name__}: {e}",
        }, ensure_ascii=False, indent=2))
        return 3


def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["check", "run"])
    args = p.parse_args()
    cfg = load_cfg()
    return cmd_check(cfg) if args.command == "check" else cmd_run(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
