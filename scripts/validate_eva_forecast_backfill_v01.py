from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CFG = PROJECT_ROOT / "config/opportunity/eva_forecast_backfill_acceptance_v0_1.yaml"

REQUIRED_COLUMNS = [
    "announcementId","symbol","secCode","secName","event_time","event_precision",
    "event_date","title","event_kind","explicit_st_at_event",
    "strategy_eligible_semantics","exclusion_reason","report_period",
    "current_profit_min_yuan","current_profit_max_yuan",
    "prior_profit_min_yuan","prior_profit_max_yuan","profit_state",
    "reported_net_profit_yoy_min_pct","reported_net_profit_yoy_max_pct",
    "profit_change_vs_prior_abs_pct","change_type","change_basis",
    "parser_confidence","pdf_path","pdf_sha256",
]

SEMANTIC_COLS = [
    "current_profit_min_yuan","current_profit_max_yuan",
    "prior_profit_min_yuan","prior_profit_max_yuan",
    "profit_state","reported_net_profit_yoy_min_pct",
    "reported_net_profit_yoy_max_pct","profit_change_vs_prior_abs_pct",
    "change_type",
]


def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024), b""):
            h.update(chunk)
    return h.hexdigest()


def nonempty_rate(df: pd.DataFrame, col: str) -> float:
    if col not in df.columns or len(df)==0:
        return 0.0
    x=df[col].astype("string").str.strip()
    return float((x.notna() & x.ne("") & x.ne("<NA>")).mean())


def semantic_payload_rate(df: pd.DataFrame) -> float:
    mask=pd.Series(False,index=df.index)
    for c in SEMANTIC_COLS:
        if c not in df.columns:
            continue
        x=df[c]
        if pd.api.types.is_numeric_dtype(x):
            mask |= pd.to_numeric(x, errors="coerce").notna()
        else:
            y=x.astype("string").str.strip()
            mask |= y.notna() & y.ne("") & y.ne("<NA>")
    return float(mask.mean()) if len(df) else 0.0


def event_years(df: pd.DataFrame):
    t=pd.to_datetime(df["event_time"], errors="coerce")
    years=t.dt.year
    return t, years


def suspicious_report_period_leakage(df: pd.DataFrame, years: pd.Series) -> dict:
    """Heuristic only: flag suspicious cases where event year appears extracted from report_period.

    Legitimate forecasts can refer to the same fiscal year, so this is diagnostic, not a hard gate.
    """
    if "report_period" not in df.columns:
        return {"rows": 0, "rate": 0.0, "examples": []}
    rp=df["report_period"].astype("string")
    rp_year=pd.to_numeric(rp.str.extract(r"((?:19|20)\d{2})", expand=False), errors="coerce")
    same=(rp_year.notna() & years.notna() & (rp_year == years))
    examples=df.loc[same, ["announcementId","symbol","event_time","report_period"]].head(20)
    return {
        "rows": int(same.sum()),
        "rate": float(same.mean()) if len(df) else 0.0,
        "note": "Same report-period year and event year is normal for many forecasts; inspect only for mass synthetic timing patterns.",
        "examples": examples.astype("string").to_dict(orient="records"),
    }


def choose_input(cfg):
    for p in cfg["incoming_candidates"]:
        q=Path(p)
        if q.exists():
            return q
    return None


def validate(path: Path, cfg: dict) -> dict:
    df=pd.read_parquet(path)
    missing=[c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        return {
            "status":"REJECT_SCHEMA",
            "path":str(path),
            "sha256":sha256_file(path),
            "rows":int(len(df)),
            "required_missing":missing,
        }

    t, years=event_years(df)
    requested=set(int(x) for x in cfg["requested_event_years"])
    observed=sorted(int(x) for x in years.dropna().unique())
    missing_years=sorted(requested - set(observed))
    out_mask=years.notna() & ~years.isin(requested)

    identity=df[["announcementId","symbol"]].astype("string")
    precision=df["event_precision"].astype("string").str.upper()
    bad_precision=~precision.isin(["DAY","SECOND"])

    stats={
        "path":str(path),
        "sha256":sha256_file(path),
        "rows":int(len(df)),
        "required_missing":[],
        "event_time_nonnull_rate":float(t.notna().mean()),
        "event_time_min":str(t.min()) if t.notna().any() else None,
        "event_time_max":str(t.max()) if t.notna().any() else None,
        "observed_event_years":observed,
        "requested_event_years":sorted(requested),
        "missing_requested_years":missing_years,
        "out_of_requested_year_rows":int(out_mask.sum()),
        "out_of_requested_year_rate":float(out_mask.mean()) if len(df) else 0.0,
        "symbol_nonempty_rate":nonempty_rate(df,"symbol"),
        "announcement_id_nonempty_rate":nonempty_rate(df,"announcementId"),
        "distinct_symbols":int(df["symbol"].astype("string").nunique(dropna=True)),
        "distinct_announcement_ids":int(df["announcementId"].astype("string").nunique(dropna=True)),
        "duplicate_announcement_symbol_rows":int(identity.duplicated().sum()),
        "semantic_payload_rate":semantic_payload_rate(df),
        "pdf_sha256_nonempty_rate":nonempty_rate(df,"pdf_sha256"),
        "pdf_path_nonempty_rate":nonempty_rate(df,"pdf_path"),
        "event_precision_counts":{
            str(k):int(v) for k,v in precision.value_counts(dropna=False).items()
        },
        "invalid_event_precision_rows":int(bad_precision.sum()),
        "event_kind_counts":{
            str(k):int(v) for k,v in df["event_kind"].astype("string").value_counts(dropna=False).items()
        },
        "parser_confidence_counts":{
            str(k):int(v) for k,v in df["parser_confidence"].astype("string").value_counts(dropna=False).items()
        },
        "strategy_eligible_counts":{
            str(k):int(v) for k,v in df["strategy_eligible_semantics"].astype("string").value_counts(dropna=False).items()
        },
        "by_event_year":{},
        "report_period_same_year_diagnostic":suspicious_report_period_leakage(df, years),
    }

    for y,g in df[years.notna()].groupby(years[years.notna()].astype(int)):
        stats["by_event_year"][str(int(y))]={
            "rows":int(len(g)),
            "distinct_symbols":int(g["symbol"].astype("string").nunique(dropna=True)),
            "semantic_payload_rate":semantic_payload_rate(g),
        }

    gate=cfg["gate"]
    checks={
        "event_time_nonnull_min":stats["event_time_nonnull_rate"] >= float(gate["event_time_nonnull_min"]),
        "symbol_nonempty_min":stats["symbol_nonempty_rate"] >= float(gate["symbol_nonempty_min"]),
        "announcement_id_nonempty_min":stats["announcement_id_nonempty_rate"] >= float(gate["announcement_id_nonempty_min"]),
        "duplicate_identity_max":stats["duplicate_announcement_symbol_rows"] <= int(gate["duplicate_identity_max"]),
        "semantic_payload_min":stats["semantic_payload_rate"] >= float(gate["semantic_payload_min"]),
        "pdf_sha256_nonempty_min":stats["pdf_sha256_nonempty_rate"] >= float(gate["pdf_sha256_nonempty_min"]),
        "requested_year_coverage_required":len(missing_years)==0,
        "out_of_requested_year_rate_max":stats["out_of_requested_year_rate"] <= float(gate["out_of_requested_year_rate_max"]),
        "event_precision_valid":stats["invalid_event_precision_rows"]==0,
    }
    stats["checks"]=checks
    stats["accepted"]=all(checks.values())
    stats["verdict"]="ACCEPT_EVA_FORECAST_BACKFILL_V0_1" if stats["accepted"] else "REJECT_OR_HOLD_EVA_FORECAST_BACKFILL_V0_1"
    return stats


def cmd_check(cfg):
    p=choose_input(cfg)
    ref=Path(cfg["reference_canonical"]["path"])
    result={
        "status":"PASS_EVA_FORECAST_BACKFILL_ACCEPTANCE_V0_1_CHECK",
        "research_id":cfg["research_id"],
        "incoming_candidates":[{"path":x,"exists":Path(x).exists()} for x in cfg["incoming_candidates"]],
        "selected_input":str(p) if p else None,
        "reference_canonical":{
            "path":str(ref),
            "exists":ref.exists(),
            "sha256":sha256_file(ref) if ref.exists() else None,
            "expected_sha256":cfg["reference_canonical"]["sha256"],
        },
        "requested_event_years":cfg["requested_event_years"],
        "gate":cfg["gate"],
        "read_only":True,
    }
    if ref.exists() and result["reference_canonical"]["sha256"] != cfg["reference_canonical"]["sha256"]:
        result["status"]="BLOCKED_REFERENCE_CANONICAL_SHA_MISMATCH"
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result["status"].startswith("PASS") else 3


def cmd_run(cfg):
    p=choose_input(cfg)
    if p is None:
        print(json.dumps({
            "status":"BLOCKED_EVA_FORECAST_BACKFILL_INPUT_NOT_FOUND",
            "incoming_candidates":cfg["incoming_candidates"],
        },ensure_ascii=False,indent=2))
        return 3

    ref=Path(cfg["reference_canonical"]["path"])
    if not ref.exists() or sha256_file(ref) != cfg["reference_canonical"]["sha256"]:
        print(json.dumps({
            "status":"BLOCKED_REFERENCE_CANONICAL_SHA_MISMATCH_OR_MISSING",
            "reference":str(ref),
        },ensure_ascii=False,indent=2))
        return 3

    result=validate(p,cfg)
    out=Path(cfg["output_dir"]); out.mkdir(parents=True,exist_ok=True)

    payload={
        "status":"PASS_EVA_FORECAST_BACKFILL_ACCEPTANCE_V0_1_RUN",
        "generated_at":datetime.now().isoformat(timespec="seconds"),
        "research_id":cfg["research_id"],
        "reference_canonical":{
            "path":str(ref),
            "sha256":sha256_file(ref),
        },
        "backfill_validation":result,
        "next_action":(
            "If accepted: perform row-level overlap check against canonical 2024-2026, then create a versioned merged research asset. Do not promote directly from incoming."
            if result.get("accepted")
            else "Repair/recollect failed fields or missing years; do not merge into canonical research assets."
        ),
        "safety":{"read_only":True,"production_db":False,"broker":False,"qmt":False},
    }

    summary=out/"eva_forecast_backfill_acceptance_v0_1_summary.json"
    summary.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(payload,ensure_ascii=False,indent=2))
    print(f"OUTPUT: {out}")
    return 0


def main():
    p=argparse.ArgumentParser()
    p.add_argument("command",choices=["check","run"])
    args=p.parse_args()
    cfg=load_cfg()
    return cmd_check(cfg) if args.command=="check" else cmd_run(cfg)


if __name__=="__main__":
    raise SystemExit(main())
