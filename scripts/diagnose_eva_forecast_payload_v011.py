from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CFG = PROJECT_ROOT / "config/opportunity/eva_forecast_payload_diagnostic_v0_1_1.yaml"

SEMANTIC_NUMERIC = [
    "current_profit_min_yuan",
    "current_profit_max_yuan",
    "prior_profit_min_yuan",
    "prior_profit_max_yuan",
    "reported_net_profit_yoy_min_pct",
    "reported_net_profit_yoy_max_pct",
    "profit_change_vs_prior_abs_pct",
]

SEMANTIC_CATEGORICAL = [
    "profit_state",
    "change_type",
    "change_basis",
]

CATEGORICAL_BAD = {
    "", "<NA>", "NA", "N/A", "NONE", "UNKNOWN",
    "UNPARSED", "EXCLUDED", "AMBIGUOUS", "NOT_APPLICABLE",
}


def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def norm_bool(s: pd.Series) -> pd.Series:
    x = s.astype("string").str.strip().str.lower()
    return x.isin(["true", "1", "yes", "y", "eligible"])


def strict_semantic_payload_mask(df: pd.DataFrame) -> pd.Series:
    mask = pd.Series(False, index=df.index)

    for c in SEMANTIC_NUMERIC:
        if c in df.columns:
            mask |= pd.to_numeric(df[c], errors="coerce").notna()

    for c in SEMANTIC_CATEGORICAL:
        if c in df.columns:
            y = df[c].astype("string").fillna("").str.strip().str.upper()
            mask |= ~y.isin(CATEGORICAL_BAD)

    return mask


def valid_rate(df: pd.DataFrame, col: str) -> float | None:
    if col not in df.columns or len(df) == 0:
        return None
    if col in SEMANTIC_NUMERIC:
        return float(pd.to_numeric(df[col], errors="coerce").notna().mean())
    y = df[col].astype("string").fillna("").str.strip().str.upper()
    return float((~y.isin(CATEGORICAL_BAD)).mean())


def counts_dict(s: pd.Series, max_items: int = 50) -> dict:
    return {
        str(k): int(v)
        for k, v in s.astype("string").value_counts(dropna=False).head(max_items).items()
    }


def profile(path: Path, label: str) -> dict:
    df = pd.read_parquet(path)
    t = pd.to_datetime(df["event_time"], errors="coerce")
    df = df.copy()
    df["_event_year"] = t.dt.year.astype("Int64")
    df["_eligible"] = (
        norm_bool(df["strategy_eligible_semantics"])
        if "strategy_eligible_semantics" in df.columns
        else False
    )
    df["_payload"] = strict_semantic_payload_mask(df)

    missing = df.loc[~df["_payload"]].copy()

    by_year = {}
    for y, g in df[df["_event_year"].notna()].groupby("_event_year"):
        yi = int(y)
        gm = g.loc[~g["_payload"]]
        ge = g.loc[g["_eligible"]]
        gem = ge.loc[~ge["_payload"]]

        by_year[str(yi)] = {
            "rows": int(len(g)),
            "strict_payload_rate": float(g["_payload"].mean()),
            "missing_payload_rows": int((~g["_payload"]).sum()),
            "strategy_eligible_rows": int(g["_eligible"].sum()),
            "strategy_eligible_strict_payload_rate": (
                float(ge["_payload"].mean()) if len(ge) else None
            ),
            "strategy_eligible_missing_payload_rows": int((~ge["_payload"]).sum()),
            "missing_event_kind_counts": (
                counts_dict(gm["event_kind"]) if "event_kind" in gm.columns else {}
            ),
            "missing_parser_confidence_counts": (
                counts_dict(gm["parser_confidence"]) if "parser_confidence" in gm.columns else {}
            ),
        }

    by_eligibility = {}
    for eligible, g in df.groupby("_eligible"):
        key = "eligible" if bool(eligible) else "ineligible"
        by_eligibility[key] = {
            "rows": int(len(g)),
            "strict_payload_rate": float(g["_payload"].mean()),
            "missing_payload_rows": int((~g["_payload"]).sum()),
            "event_kind_counts": (
                counts_dict(g["event_kind"]) if "event_kind" in g.columns else {}
            ),
            "missing_event_kind_counts": (
                counts_dict(g.loc[~g["_payload"], "event_kind"])
                if "event_kind" in g.columns else {}
            ),
        }

    field_coverage = {}
    for c in SEMANTIC_NUMERIC + SEMANTIC_CATEGORICAL:
        if c in df.columns:
            field_coverage[c] = {
                "overall": valid_rate(df, c),
                "by_year": {
                    str(int(y)): valid_rate(g, c)
                    for y, g in df[df["_event_year"].notna()].groupby("_event_year")
                },
                "eligible_only": valid_rate(df.loc[df["_eligible"]], c),
            }

    missing_cols = [
        c for c in [
            "announcementId", "symbol", "event_time", "title",
            "event_kind", "strategy_eligible_semantics",
            "exclusion_reason", "parser_confidence",
            "report_period", "event_precision",
        ]
        if c in missing.columns
    ]

    missing_sample = (
        missing[missing_cols]
        .head(100)
        .astype("string")
        .to_dict(orient="records")
    )

    return {
        "label": label,
        "path": str(path),
        "sha256": sha256_file(path),
        "rows": int(len(df)),
        "event_years": sorted(int(x) for x in df["_event_year"].dropna().unique()),
        "strict_payload_rate": float(df["_payload"].mean()),
        "missing_payload_rows": int((~df["_payload"]).sum()),
        "strategy_eligible_rows": int(df["_eligible"].sum()),
        "strategy_eligible_strict_payload_rate": (
            float(df.loc[df["_eligible"], "_payload"].mean())
            if df["_eligible"].any() else None
        ),
        "strategy_eligible_missing_payload_rows": int(
            (~df.loc[df["_eligible"], "_payload"]).sum()
        ) if df["_eligible"].any() else 0,
        "by_year": by_year,
        "by_eligibility": by_eligibility,
        "missing_event_kind_counts": (
            counts_dict(missing["event_kind"]) if "event_kind" in missing.columns else {}
        ),
        "missing_parser_confidence_counts": (
            counts_dict(missing["parser_confidence"])
            if "parser_confidence" in missing.columns else {}
        ),
        "missing_exclusion_reason_counts": (
            counts_dict(missing["exclusion_reason"])
            if "exclusion_reason" in missing.columns else {}
        ),
        "field_coverage": field_coverage,
        "missing_payload_sample": missing_sample,
    }


def classify(result: dict, cfg: dict) -> dict:
    backfill = result["backfill_2020_2023"]
    reparse = result["reparse_2024_2026"]

    threshold = float(cfg["diagnostic_thresholds"]["strict_payload_target"])

    all_reparse_pass = reparse["strict_payload_rate"] >= threshold
    eligible_reparse_pass = (
        reparse["strategy_eligible_strict_payload_rate"] is not None
        and reparse["strategy_eligible_strict_payload_rate"] >= threshold
    )

    y2026 = reparse["by_year"].get("2026", {})
    y2026_eligible_pass = (
        y2026.get("strategy_eligible_strict_payload_rate") is not None
        and y2026["strategy_eligible_strict_payload_rate"] >= threshold
    )

    if all_reparse_pass:
        interpretation = "ALL_ROW_PAYLOAD_GATE_WOULD_PASS"
        next_action = (
            "The earlier failure was not reproducible under this strict diagnostic; inspect validator version mismatch."
        )
    elif eligible_reparse_pass and y2026_eligible_pass:
        interpretation = "FAILURE_CONCENTRATED_OUTSIDE_STRATEGY_ELIGIBLE_EVENTS"
        next_action = (
            "Do not lower the gate yet. Review whether semantic-payload completeness should be defined on the "
            "strategy-eligible research universe rather than excluded/follow-up rows; if so, amend the acceptance "
            "spec prospectively and rerun both 2020-2023 and 2024-2026 under the amended definition."
        )
    else:
        interpretation = "MATERIAL_PAYLOAD_GAP_INSIDE_STRATEGY_ELIGIBLE_EVENTS"
        next_action = (
            "Do not relax the 0.99 gate. Design parser v0.2 for the 2026 unit-header format, freeze it, and rerun "
            "both 2020-2023 and 2024-2026 so the entire 2020-2026 corpus remains homogeneous."
        )

    return {
        "strict_payload_target": threshold,
        "backfill_overall_pass": backfill["strict_payload_rate"] >= threshold,
        "backfill_eligible_pass": (
            backfill["strategy_eligible_strict_payload_rate"] is not None
            and backfill["strategy_eligible_strict_payload_rate"] >= threshold
        ),
        "reparse_overall_pass": all_reparse_pass,
        "reparse_eligible_pass": eligible_reparse_pass,
        "reparse_2026_eligible_pass": y2026_eligible_pass,
        "interpretation": interpretation,
        "next_action": next_action,
    }


def cmd_check(cfg):
    paths = {
        "backfill": Path(cfg["backfill_path"]),
        "reparse": Path(cfg["reparse_path"]),
    }
    result = {
        "status": (
            "PASS_EVA_FORECAST_PAYLOAD_DIAGNOSTIC_V0_1_1_CHECK"
            if all(p.exists() for p in paths.values())
            else "BLOCKED_EVA_FORECAST_PAYLOAD_DIAGNOSTIC_V0_1_1_CHECK"
        ),
        "paths": {
            k: {"path": str(v), "exists": v.exists()}
            for k, v in paths.items()
        },
        "purpose": (
            "Diagnose the existing 0.99 semantic-payload failure without changing any research gate."
        ),
        "read_only": True,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"].startswith("PASS") else 3


def cmd_run(cfg):
    b = Path(cfg["backfill_path"])
    r = Path(cfg["reparse_path"])
    if not b.exists() or not r.exists():
        return cmd_check(cfg)

    result = {
        "status": "PASS_EVA_FORECAST_PAYLOAD_DIAGNOSTIC_V0_1_1_RUN",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "research_id": cfg["research_id"],
        "metric_definition": {
            "numeric": "non-null numeric value",
            "categorical": (
                "non-empty and not one of UNKNOWN/UNPARSED/EXCLUDED/AMBIGUOUS/NOT_APPLICABLE"
            ),
            "row_payload": "at least one valid semantic numeric or categorical field",
            "note": "Diagnostic only; this package does not alter acceptance gates or source files.",
        },
        "backfill_2020_2023": profile(b, "backfill_2020_2023"),
        "reparse_2024_2026": profile(r, "reparse_2024_2026"),
    }
    result["decision"] = classify(result, cfg)

    out = Path(cfg["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    summary = out / "eva_forecast_payload_diagnostic_v0_1_1_summary.json"
    summary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"OUTPUT: {out}")
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["check", "run"])
    args = p.parse_args()
    cfg = load_cfg()
    return cmd_check(cfg) if args.command == "check" else cmd_run(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
