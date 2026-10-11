from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

IDENTITY = ["announcementId", "symbol"]

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

REQUIRED = [
    "announcementId","symbol","secCode","secName","event_time","event_precision",
    "event_date","title","event_kind","explicit_st_at_event",
    "strategy_eligible_semantics","exclusion_reason","report_period",
    "current_profit_min_yuan","current_profit_max_yuan",
    "prior_profit_min_yuan","prior_profit_max_yuan",
    "profit_state","reported_net_profit_yoy_min_pct",
    "reported_net_profit_yoy_max_pct",
    "profit_change_vs_prior_abs_pct","change_type","change_basis",
    "parser_confidence","pdf_path","pdf_sha256",
    "parser_version","parser_rule_id",
]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def norm_bool(s: pd.Series) -> pd.Series:
    return s.astype("string").str.strip().str.lower().isin(
        ["true", "1", "yes", "y", "eligible"]
    )


def strict_payload_mask(df: pd.DataFrame) -> pd.Series:
    mask = pd.Series(False, index=df.index)
    for c in SEM_NUM:
        if c in df.columns:
            mask |= pd.to_numeric(df[c], errors="coerce").notna()
    for c in SEM_CAT:
        if c in df.columns:
            y = df[c].astype("string").fillna("").str.strip().str.upper()
            mask |= ~y.isin(BAD)
    return mask


def counts(s: pd.Series, limit: int = 100) -> dict:
    return {
        str(k): int(v)
        for k, v in s.astype("string").value_counts(dropna=False).head(limit).items()
    }


def profile(df: pd.DataFrame) -> dict:
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        return {"schema_ok": False, "required_missing": missing}

    x = df.copy()
    x["_event_time"] = pd.to_datetime(x["event_time"], errors="coerce")
    x["_year"] = x["_event_time"].dt.year.astype("Int64")
    x["_eligible"] = norm_bool(x["strategy_eligible_semantics"])
    x["_payload"] = strict_payload_mask(x)

    identity = x[IDENTITY].astype("string")
    eligible = x[x["_eligible"]]

    cpmin = pd.to_numeric(eligible["current_profit_min_yuan"], errors="coerce")
    cpmax = pd.to_numeric(eligible["current_profit_max_yuan"], errors="coerce")

    yearly = {}
    for y, g in x[x["_year"].notna()].groupby("_year"):
        ge = g[g["_eligible"]]
        ymin = pd.to_numeric(ge["current_profit_min_yuan"], errors="coerce")
        ymax = pd.to_numeric(ge["current_profit_max_yuan"], errors="coerce")
        yearly[str(int(y))] = {
            "rows": int(len(g)),
            "eligible_rows": int(len(ge)),
            "eligible_strict_payload_rate": float(ge["_payload"].mean()) if len(ge) else None,
            "eligible_missing_payload_rows": int((~ge["_payload"]).sum()) if len(ge) else 0,
            "eligible_current_profit_min_coverage": float(ymin.notna().mean()) if len(ge) else None,
            "eligible_current_profit_max_coverage": float(ymax.notna().mean()) if len(ge) else None,
        }

    return {
        "schema_ok": True,
        "required_missing": [],
        "rows": int(len(x)),
        "distinct_identity": int(identity.drop_duplicates().shape[0]),
        "duplicate_identity_rows": int(identity.duplicated().sum()),
        "distinct_symbols": int(x["symbol"].astype("string").nunique(dropna=True)),
        "event_time_nonnull_rate": float(x["_event_time"].notna().mean()),
        "event_time_min": str(x["_event_time"].min()) if x["_event_time"].notna().any() else None,
        "event_time_max": str(x["_event_time"].max()) if x["_event_time"].notna().any() else None,
        "event_years": sorted(int(v) for v in x["_year"].dropna().unique()),
        "eligible_rows": int(x["_eligible"].sum()),
        "eligible_strict_payload_rate": float(eligible["_payload"].mean()) if len(eligible) else None,
        "eligible_current_profit_min_coverage": float(cpmin.notna().mean()) if len(eligible) else None,
        "eligible_current_profit_max_coverage": float(cpmax.notna().mean()) if len(eligible) else None,
        "yearly": yearly,
        "parser_version_counts": counts(x["parser_version"]),
        "parser_rule_id_counts": counts(x["parser_rule_id"]),
        "event_kind_counts": counts(x["event_kind"]),
        "parser_confidence_counts": counts(x["parser_confidence"]),
        "profit_state_counts": counts(x["profit_state"]),
        "change_type_counts": counts(x["change_type"]),
        "report_period_counts": counts(x["report_period"], limit=50),
    }


def evaluate_research_gate(p: dict, gate: dict, parser_version: str) -> dict:
    yearly = p.get("yearly", {})
    event_years = p.get("event_years", [])

    checks = {
        "schema_ok": bool(p.get("schema_ok")),
        "identity_rows_exact": p.get("rows") == int(gate["identity_rows_exact"]),
        "distinct_identity_exact": p.get("distinct_identity") == int(gate["identity_rows_exact"]),
        "duplicate_identity_zero": p.get("duplicate_identity_rows") == 0,
        "event_time_complete": p.get("event_time_nonnull_rate") == 1.0,
        "event_years_exact": event_years == [int(x) for x in gate["event_years_exact"]],
        "formal_history_years_min": len(event_years) >= int(gate["formal_history_years_min"]),
        "parser_version_frozen": p.get("parser_version_counts") == {
            parser_version: int(gate["identity_rows_exact"])
        },
        "eligible_strict_payload_min": (
            p.get("eligible_strict_payload_rate") is not None
            and p["eligible_strict_payload_rate"] >= float(gate["eligible_strict_payload_min"])
        ),
        "eligible_current_profit_min_coverage_min": (
            p.get("eligible_current_profit_min_coverage") is not None
            and p["eligible_current_profit_min_coverage"] >= float(gate["eligible_current_profit_min_coverage_min"])
        ),
        "eligible_current_profit_max_coverage_min": (
            p.get("eligible_current_profit_max_coverage") is not None
            and p["eligible_current_profit_max_coverage"] >= float(gate["eligible_current_profit_max_coverage_min"])
        ),
        "eligible_yearly_strict_payload_min": all(
            v["eligible_strict_payload_rate"] is not None
            and v["eligible_strict_payload_rate"] >= float(gate["eligible_yearly_strict_payload_min"])
            for v in yearly.values()
        ) and len(yearly) == len(gate["event_years_exact"]),
        "eligible_yearly_current_profit_min_coverage_min": all(
            v["eligible_current_profit_min_coverage"] is not None
            and v["eligible_current_profit_min_coverage"] >= float(gate["eligible_yearly_current_profit_min_coverage_min"])
            for v in yearly.values()
        ) and len(yearly) == len(gate["event_years_exact"]),
        "eligible_yearly_current_profit_max_coverage_min": all(
            v["eligible_current_profit_max_coverage"] is not None
            and v["eligible_current_profit_max_coverage"] >= float(gate["eligible_yearly_current_profit_max_coverage_min"])
            for v in yearly.values()
        ) and len(yearly) == len(gate["event_years_exact"]),
    }
    return {
        "checks": checks,
        "accepted": all(checks.values()),
    }
