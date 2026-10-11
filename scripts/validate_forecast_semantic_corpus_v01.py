from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CFG = PROJECT_ROOT / "config/opportunity/forecast_semantic_corpus_validation_v0_1.yaml"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))


def norm_str(s: pd.Series) -> pd.Series:
    return s.astype("string").str.strip()


def nonempty_rate(df: pd.DataFrame, col: str) -> float:
    if col not in df.columns or len(df) == 0:
        return 0.0
    x = norm_str(df[col])
    ok = x.notna() & x.ne("") & x.ne("<NA>")
    return float(ok.mean())


def parse_time(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce")


def semantic_payload_mask(df: pd.DataFrame) -> pd.Series:
    candidates = [
        "current_profit_min_yuan", "current_profit_max_yuan",
        "prior_profit_min_yuan", "prior_profit_max_yuan",
        "reported_net_profit_yoy_min_pct", "reported_net_profit_yoy_max_pct",
        "profit_change_vs_prior_abs_pct", "profit_state", "change_type",
    ]
    mask = pd.Series(False, index=df.index)
    for c in candidates:
        if c not in df.columns:
            continue
        x = df[c]
        if pd.api.types.is_numeric_dtype(x):
            mask |= pd.to_numeric(x, errors="coerce").notna()
        else:
            y = norm_str(x)
            mask |= y.notna() & y.ne("") & y.ne("<NA>")
    return mask


def canonical_profile(path: Path) -> dict:
    df = pd.read_parquet(path)

    required = [
        "announcementId", "symbol", "event_time", "event_precision",
        "event_kind", "strategy_eligible_semantics", "report_period",
        "parser_confidence", "pdf_path", "pdf_sha256",
    ]
    missing = [c for c in required if c not in df.columns]

    result = {
        "path": str(path),
        "sha256": sha256_file(path),
        "size_bytes": int(path.stat().st_size),
        "rows": int(len(df)),
        "columns": [str(c) for c in df.columns],
        "required_missing": missing,
    }
    if missing:
        result["status"] = "INVALID_SCHEMA"
        return result

    result["status"] = "SCHEMA_OK"

    df["_event_time"] = parse_time(df["event_time"])
    df["_year"] = df["_event_time"].dt.year.astype("Int64")

    result["event_time_nonnull_rate"] = float(df["_event_time"].notna().mean())
    result["event_time_min"] = (
        str(df["_event_time"].min()) if df["_event_time"].notna().any() else None
    )
    result["event_time_max"] = (
        str(df["_event_time"].max()) if df["_event_time"].notna().any() else None
    )
    result["distinct_event_years"] = sorted(
        int(y) for y in df["_year"].dropna().unique().tolist()
    )
    result["event_year_count"] = len(result["distinct_event_years"])

    result["symbol_nonempty_rate"] = nonempty_rate(df, "symbol")
    result["distinct_symbols"] = int(norm_str(df["symbol"]).nunique(dropna=True))
    result["announcement_id_nonempty_rate"] = nonempty_rate(df, "announcementId")
    result["distinct_announcement_ids"] = int(
        norm_str(df["announcementId"]).nunique(dropna=True)
    )

    # Duplicate identity diagnostics.
    identity = df[["announcementId", "symbol"]].astype("string")
    result["duplicate_announcement_symbol_rows"] = int(identity.duplicated().sum())
    result["duplicate_exact_rows"] = int(df.astype("string").duplicated().sum())

    result["event_precision_counts"] = {
        str(k): int(v)
        for k, v in df["event_precision"].astype("string").value_counts(dropna=False).items()
    }
    result["event_kind_counts"] = {
        str(k): int(v)
        for k, v in df["event_kind"].astype("string").value_counts(dropna=False).items()
    }
    result["strategy_eligible_counts"] = {
        str(k): int(v)
        for k, v in df["strategy_eligible_semantics"].astype("string").value_counts(dropna=False).items()
    }
    result["exclusion_reason_counts"] = (
        {
            str(k): int(v)
            for k, v in df["exclusion_reason"].astype("string").value_counts(dropna=False).head(30).items()
        }
        if "exclusion_reason" in df.columns else {}
    )
    result["parser_confidence_counts"] = {
        str(k): int(v)
        for k, v in df["parser_confidence"].astype("string").value_counts(dropna=False).items()
    }

    result["pdf_path_nonempty_rate"] = nonempty_rate(df, "pdf_path")
    result["pdf_sha256_nonempty_rate"] = nonempty_rate(df, "pdf_sha256")
    result["report_period_nonempty_rate"] = nonempty_rate(df, "report_period")
    result["semantic_payload_rate"] = float(semantic_payload_mask(df).mean())

    # Conservative strategy-eligible mask: accepts common boolean encodings.
    elig = norm_str(df["strategy_eligible_semantics"]).str.lower()
    eligible_mask = elig.isin(["true", "1", "yes", "y", "eligible"])
    result["strategy_eligible_rate"] = float(eligible_mask.mean())
    result["strategy_eligible_rows"] = int(eligible_mask.sum())
    result["eligible_semantic_payload_rate"] = (
        float(semantic_payload_mask(df.loc[eligible_mask]).mean())
        if eligible_mask.any() else None
    )

    # Event-year counts, not report-period years.
    by_year = {}
    for y, g in df[df["_year"].notna()].groupby("_year"):
        yi = int(y)
        e = norm_str(g["strategy_eligible_semantics"]).str.lower().isin(
            ["true", "1", "yes", "y", "eligible"]
        )
        by_year[str(yi)] = {
            "rows": int(len(g)),
            "distinct_symbols": int(norm_str(g["symbol"]).nunique(dropna=True)),
            "strategy_eligible_rows": int(e.sum()),
            "semantic_payload_rate": float(semantic_payload_mask(g).mean()),
        }
    result["by_event_year"] = by_year

    return result


def compare_profiles(profiles: list[dict]) -> dict:
    valid = [p for p in profiles if p.get("status") == "SCHEMA_OK"]
    if len(valid) < 2:
        return {"comparable": False}

    a, b = valid[0], valid[1]
    return {
        "comparable": True,
        "same_sha256": a["sha256"] == b["sha256"],
        "same_row_count": a["rows"] == b["rows"],
        "same_event_time_range": (
            a["event_time_min"] == b["event_time_min"]
            and a["event_time_max"] == b["event_time_max"]
        ),
        "same_distinct_symbols": a["distinct_symbols"] == b["distinct_symbols"],
        "note": (
            "Different SHA with identical row count can still represent parser/serialization revisions. "
            "Do not treat versions as interchangeable without row-level comparison."
        ),
    }


def readiness(profile: dict, cfg: dict) -> dict:
    g = cfg["gate"]
    years = profile.get("distinct_event_years", [])

    checks = {
        "schema_ok": profile.get("status") == "SCHEMA_OK",
        "rows_min": profile.get("rows", 0) >= int(g["rows_min"]),
        "symbols_min": profile.get("distinct_symbols", 0) >= int(g["symbols_min"]),
        "event_time_nonnull_min": profile.get("event_time_nonnull_rate", 0.0) >= float(g["event_time_nonnull_min"]),
        "symbol_nonempty_min": profile.get("symbol_nonempty_rate", 0.0) >= float(g["symbol_nonempty_min"]),
        "announcement_id_nonempty_min": profile.get("announcement_id_nonempty_rate", 0.0) >= float(g["announcement_id_nonempty_min"]),
        "duplicate_identity_max": profile.get("duplicate_announcement_symbol_rows", 10**9) <= int(g["duplicate_identity_max"]),
        "semantic_payload_min": profile.get("semantic_payload_rate", 0.0) >= float(g["semantic_payload_min"]),
        "pdf_sha256_min": profile.get("pdf_sha256_nonempty_rate", 0.0) >= float(g["pdf_sha256_min"]),
        "formal_history_years_min": len(years) >= int(g["formal_history_years_min"]),
    }

    data_quality_pass = all(
        checks[k] for k in checks if k != "formal_history_years_min"
    )
    formal_ready = all(checks.values())

    if formal_ready:
        verdict = "ACCEPT_FORECAST_SEMANTIC_CORPUS_FOR_FORMAL_FAST_REJECT"
    elif data_quality_pass:
        verdict = "ACCEPT_CORPUS_QUALITY_BUT_BLOCK_FORMAL_RESEARCH_HISTORY_TOO_SHORT"
    else:
        verdict = "REJECT_OR_HOLD_FORECAST_SEMANTIC_CORPUS_PENDING_DATA_REPAIR"

    return {
        "checks": checks,
        "data_quality_pass": bool(data_quality_pass),
        "formal_research_ready": bool(formal_ready),
        "verdict": verdict,
        "observed_event_years": years,
        "required_event_years_min": int(g["formal_history_years_min"]),
        "missing_years_for_2020_2026_frame": [
            y for y in range(2020, 2027) if y not in years
        ],
    }


def cmd_check(cfg):
    paths = [Path(x) for x in cfg["candidate_paths"]]
    result = {
        "status": (
            "PASS_FORECAST_SEMANTIC_CORPUS_VALIDATION_V0_1_CHECK"
            if any(p.exists() for p in paths)
            else "BLOCKED_FORECAST_SEMANTIC_CORPUS_VALIDATION_V0_1_CHECK"
        ),
        "research_id": cfg["research_id"],
        "candidate_paths": [{"path": str(p), "exists": p.exists()} for p in paths],
        "gate": cfg["gate"],
        "research_rule": (
            "event_time defines PIT history; report_period never backfills knowability."
        ),
        "read_only": True,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if any(p.exists() for p in paths) else 3


def cmd_run(cfg):
    paths = [Path(x) for x in cfg["candidate_paths"] if Path(x).exists()]
    if not paths:
        return cmd_check(cfg)

    profiles = [canonical_profile(p) for p in paths]
    primary = profiles[0]
    ready = readiness(primary, cfg)
    comparison = compare_profiles(profiles)

    out = Path(cfg["output_dir"])
    out.mkdir(parents=True, exist_ok=True)

    backfill_spec = {
        "asset_family": "CNINFO_EARNINGS_FORECAST_SEMANTIC_EVENTS",
        "purpose": (
            "Extend true publication-time event history so formal Opportunity Fast Reject "
            "can preserve the frozen >=5 positive-year requirement."
        ),
        "required_missing_event_years": ready["missing_years_for_2020_2026_frame"],
        "minimum_target_history": "at least 5 distinct event years; prefer 2020-2026 complete",
        "required_fields": [
            "announcementId", "symbol", "secCode", "secName",
            "event_time", "event_precision", "event_date", "title", "event_kind",
            "strategy_eligible_semantics", "exclusion_reason", "report_period",
            "current_profit_min_yuan", "current_profit_max_yuan",
            "prior_profit_min_yuan", "prior_profit_max_yuan",
            "profit_state",
            "reported_net_profit_yoy_min_pct", "reported_net_profit_yoy_max_pct",
            "profit_change_vs_prior_abs_pct", "change_type", "change_basis",
            "parser_confidence", "pdf_path", "pdf_sha256",
        ],
        "pit_requirement": (
            "event_time must be the actual announcement/publication knowable time or a "
            "conservative day-precision timestamp. Never infer event_time from report_period."
        ),
        "dedup_key": ["announcementId", "symbol"],
        "source_preference": "CNINFO original announcement + archived PDF",
        "acceptance": {
            "event_time_nonnull_min": cfg["gate"]["event_time_nonnull_min"],
            "symbol_nonempty_min": cfg["gate"]["symbol_nonempty_min"],
            "pdf_sha256_nonempty_min": cfg["gate"]["pdf_sha256_min"],
            "formal_history_years_min": cfg["gate"]["formal_history_years_min"],
        },
    }

    spec_path = out / "forecast_semantic_backfill_spec_v0_1.json"
    spec_path.write_text(json.dumps(backfill_spec, ensure_ascii=False, indent=2), encoding="utf-8")

    result = {
        "status": "PASS_FORECAST_SEMANTIC_CORPUS_VALIDATION_V0_1_RUN",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "research_id": cfg["research_id"],
        "candidate_profiles": profiles,
        "version_comparison": comparison,
        "primary_candidate": str(paths[0]),
        "readiness": ready,
        "next_action": (
            "Proceed to formal earnings-event Opportunity mechanism."
            if ready["formal_research_ready"]
            else (
                "Backfill missing true event years, then rerun this validation. "
                "Do not relax the >=5 positive-year research gate."
                if ready["data_quality_pass"]
                else "Repair corpus quality failures before research."
            )
        ),
        "artifacts": {
            "backfill_spec": str(spec_path),
            "backfill_spec_sha256": sha256_file(spec_path),
        },
        "safety": {
            "read_only": True,
            "production_db": False,
            "broker": False,
            "qmt": False,
        },
    }

    summary_path = out / "forecast_semantic_corpus_validation_v0_1_summary.json"
    summary_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

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
