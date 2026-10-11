from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CFG = PROJECT_ROOT / "config/opportunity/eva_forecast_parser_v0_2_acceptance.yaml"

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
BAD = {"", "<NA>", "NA", "N/A", "NONE", "UNKNOWN", "UNPARSED", "EXCLUDED", "AMBIGUOUS", "NOT_APPLICABLE"}

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
]


def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))


def sha256_file(p: Path) -> str:
    h=hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024), b""):
            h.update(chunk)
    return h.hexdigest()


def choose_candidate(cfg):
    for x in cfg["candidate_paths"]:
        p=Path(x)
        if p.exists():
            return p
    return None


def norm_text(s: pd.Series) -> pd.Series:
    return (
        s.astype("string").fillna("")
        .str.normalize("NFKC")
        .str.replace(r"\s+","",regex=True)
        .str.strip()
    )


def norm_bool(s: pd.Series) -> pd.Series:
    return s.astype("string").str.strip().str.lower().isin(
        ["true","1","yes","y","eligible"]
    )


def norm_nullable(s: pd.Series) -> pd.Series:
    return s.astype("string").fillna("<NA>").str.strip()


def strict_payload_mask(df: pd.DataFrame) -> pd.Series:
    m=pd.Series(False,index=df.index)
    for c in SEM_NUM:
        if c in df.columns:
            m |= pd.to_numeric(df[c],errors="coerce").notna()
    for c in SEM_CAT:
        if c in df.columns:
            y=df[c].astype("string").fillna("").str.strip().str.upper()
            m |= ~y.isin(BAD)
    return m


def exact_rate(a: pd.Series,b: pd.Series) -> float:
    aa=norm_nullable(a)
    bb=norm_nullable(b)
    return float((aa==bb).mean()) if len(aa) else 0.0


def datetime_rate(a: pd.Series,b: pd.Series) -> float:
    # Parse per value so mixed date-only and second-precision strings are
    # compared on their actual timestamps instead of being coerced by one
    # inferred vector format.
    aa=a.map(lambda v: pd.to_datetime(v, errors="coerce"))
    bb=b.map(lambda v: pd.to_datetime(v, errors="coerce"))
    return float(((aa==bb)|(aa.isna()&bb.isna())).mean()) if len(aa) else 0.0


def make_baseline(cfg):
    a=Path(cfg["baseline_2020_2023"]["path"])
    b=Path(cfg["baseline_2024_2026"]["path"])
    if sha256_file(a)!=cfg["baseline_2020_2023"]["sha256"]:
        raise RuntimeError("baseline_2020_2023 SHA mismatch")
    if sha256_file(b)!=cfg["baseline_2024_2026"]["sha256"]:
        raise RuntimeError("baseline_2024_2026 SHA mismatch")
    da=pd.read_parquet(a)
    db=pd.read_parquet(b)
    x=pd.concat([da,db],ignore_index=True,sort=False)
    if x[IDENTITY].astype("string").duplicated().any():
        raise RuntimeError("baseline union contains duplicate identities")
    return x


def yearly_eligible_stats(df: pd.DataFrame) -> dict:
    x=df.copy()
    x["_year"]=pd.to_datetime(x["event_time"],errors="coerce").dt.year.astype("Int64")
    x["_eligible"]=norm_bool(x["strategy_eligible_semantics"])
    x["_payload"]=strict_payload_mask(x)

    out={}
    for y,g in x[x["_year"].notna()].groupby("_year"):
        ge=g[g["_eligible"]]
        cpmin=pd.to_numeric(ge["current_profit_min_yuan"],errors="coerce")
        cpmax=pd.to_numeric(ge["current_profit_max_yuan"],errors="coerce")
        out[str(int(y))]={
            "rows":int(len(g)),
            "eligible_rows":int(len(ge)),
            "eligible_strict_payload_rate":float(ge["_payload"].mean()) if len(ge) else None,
            "eligible_missing_payload_rows":int((~ge["_payload"]).sum()) if len(ge) else 0,
            "eligible_current_profit_min_coverage":float(cpmin.notna().mean()) if len(ge) else None,
            "eligible_current_profit_max_coverage":float(cpmax.notna().mean()) if len(ge) else None,
        }
    return out


def validate(baseline: pd.DataFrame, candidate: pd.DataFrame, cfg: dict, candidate_path: Path) -> dict:
    missing=[c for c in REQUIRED if c not in candidate.columns]
    if missing:
        return {
            "accepted":False,
            "verdict":"REJECT_PARSER_V0_2_SCHEMA",
            "required_missing":missing,
        }

    bkeys=set(map(tuple,baseline[IDENTITY].astype("string").itertuples(index=False,name=None)))
    ckeys=set(map(tuple,candidate[IDENTITY].astype("string").itertuples(index=False,name=None)))
    missing_keys=bkeys-ckeys
    extra_keys=ckeys-bkeys
    coverage=(len(bkeys)-len(missing_keys))/len(bkeys) if bkeys else 0.0

    merged=baseline.merge(
        candidate,
        on=IDENTITY,
        how="inner",
        suffixes=("_base","_v02"),
        validate="one_to_one",
    )

    event_years=sorted(
        int(x) for x in pd.to_datetime(candidate["event_time"],errors="coerce").dt.year.dropna().unique()
    )

    eligible=norm_bool(candidate["strategy_eligible_semantics"])
    epayload=strict_payload_mask(candidate.loc[eligible])
    cpmin=pd.to_numeric(candidate.loc[eligible,"current_profit_min_yuan"],errors="coerce")
    cpmax=pd.to_numeric(candidate.loc[eligible,"current_profit_max_yuan"],errors="coerce")

    yearly=yearly_eligible_stats(candidate)
    g=cfg["gate"]
    yearly_payload_pass=all(
        v["eligible_strict_payload_rate"] is not None
        and v["eligible_strict_payload_rate"] >= float(g["eligible_yearly_strict_payload_min"])
        for v in yearly.values()
    )
    yearly_cpmin_pass=all(
        v["eligible_current_profit_min_coverage"] is not None
        and v["eligible_current_profit_min_coverage"] >= float(g["eligible_yearly_current_profit_min_coverage_min"])
        for v in yearly.values()
    )
    yearly_cpmax_pass=all(
        v["eligible_current_profit_max_coverage"] is not None
        and v["eligible_current_profit_max_coverage"] >= float(g["eligible_yearly_current_profit_max_coverage_min"])
        for v in yearly.values()
    )

    hard_metrics={
        "rows":int(len(candidate)),
        "identity_coverage":coverage,
        "missing_identity_count":len(missing_keys),
        "extra_identity_count":len(extra_keys),
        "duplicate_identity_rows":int(candidate[IDENTITY].astype("string").duplicated().sum()),
        "event_time_exact_match_rate":datetime_rate(merged["event_time_base"],merged["event_time_v02"]),
        "event_precision_exact_match_rate":exact_rate(merged["event_precision_base"],merged["event_precision_v02"]),
        "pdf_sha256_exact_match_rate":exact_rate(merged["pdf_sha256_base"],merged["pdf_sha256_v02"]),
        "title_normalized_match_rate":float(
            (norm_text(merged["title_base"])==norm_text(merged["title_v02"])).mean()
        ),
        "event_kind_exact_match_rate":exact_rate(merged["event_kind_base"],merged["event_kind_v02"]),
        "explicit_st_exact_match_rate":exact_rate(
            merged["explicit_st_at_event_base"],merged["explicit_st_at_event_v02"]
        ),
        "strategy_eligible_exact_match_rate":exact_rate(
            merged["strategy_eligible_semantics_base"],merged["strategy_eligible_semantics_v02"]
        ),
        "exclusion_reason_exact_match_rate":exact_rate(
            merged["exclusion_reason_base"],merged["exclusion_reason_v02"]
        ),
        "eligible_rows":int(eligible.sum()),
        "eligible_strict_payload_rate":float(epayload.mean()) if len(epayload) else None,
        "eligible_current_profit_min_coverage":float(cpmin.notna().mean()) if len(cpmin) else None,
        "eligible_current_profit_max_coverage":float(cpmax.notna().mean()) if len(cpmax) else None,
        "event_years":event_years,
    }

    checks={
        "row_count_exact":hard_metrics["rows"]==int(g["row_count_exact"]),
        "identity_coverage_min":coverage>=float(g["identity_coverage_min"]),
        "identity_extra_max":len(extra_keys)<=int(g["identity_extra_max"]),
        "duplicate_identity_max":hard_metrics["duplicate_identity_rows"]<=int(g["duplicate_identity_max"]),
        "event_time_exact_match_min":hard_metrics["event_time_exact_match_rate"]>=float(g["event_time_exact_match_min"]),
        "event_precision_exact_match_min":hard_metrics["event_precision_exact_match_rate"]>=float(g["event_precision_exact_match_min"]),
        "pdf_sha256_exact_match_min":hard_metrics["pdf_sha256_exact_match_rate"]>=float(g["pdf_sha256_exact_match_min"]),
        "title_normalized_match_min":hard_metrics["title_normalized_match_rate"]>=float(g["title_normalized_match_min"]),
        "event_kind_exact_match_min":hard_metrics["event_kind_exact_match_rate"]>=float(g["event_kind_exact_match_min"]),
        "explicit_st_exact_match_min":hard_metrics["explicit_st_exact_match_rate"]>=float(g["explicit_st_exact_match_min"]),
        "strategy_eligible_exact_match_min":hard_metrics["strategy_eligible_exact_match_rate"]>=float(g["strategy_eligible_exact_match_min"]),
        "exclusion_reason_exact_match_min":hard_metrics["exclusion_reason_exact_match_rate"]>=float(g["exclusion_reason_exact_match_min"]),
        "eligible_strict_payload_min":hard_metrics["eligible_strict_payload_rate"]>=float(g["eligible_strict_payload_min"]),
        "eligible_yearly_strict_payload_min":yearly_payload_pass,
        "eligible_current_profit_min_coverage_min":hard_metrics["eligible_current_profit_min_coverage"]>=float(g["eligible_current_profit_min_coverage_min"]),
        "eligible_current_profit_max_coverage_min":hard_metrics["eligible_current_profit_max_coverage"]>=float(g["eligible_current_profit_max_coverage_min"]),
        "eligible_yearly_current_profit_min_coverage_min":yearly_cpmin_pass,
        "eligible_yearly_current_profit_max_coverage_min":yearly_cpmax_pass,
        "event_years_exact":event_years==[int(x) for x in g["event_years_exact"]],
    }

    accepted=all(checks.values())

    diagnostics={
        "parser_confidence_counts":{
            str(k):int(v)
            for k,v in candidate["parser_confidence"].astype("string").value_counts(dropna=False).items()
        },
        "unit_resolution_method_counts":(
            {
                str(k):int(v)
                for k,v in candidate["unit_resolution_method"].astype("string").value_counts(dropna=False).items()
            }
            if "unit_resolution_method" in candidate.columns else None
        ),
        "yearly_eligible":yearly,
    }

    return {
        "candidate_path":str(candidate_path),
        "candidate_sha256":sha256_file(candidate_path),
        "hard_metrics":hard_metrics,
        "checks":checks,
        "diagnostics":diagnostics,
        "accepted":accepted,
        "verdict":(
            "ACCEPT_FORECAST_SEMANTIC_PARSER_V0_2_FULL_2020_2026"
            if accepted else
            "REJECT_OR_HOLD_FORECAST_SEMANTIC_PARSER_V0_2_FULL_2020_2026"
        ),
    }


def cmd_check(cfg):
    a=Path(cfg["baseline_2020_2023"]["path"])
    b=Path(cfg["baseline_2024_2026"]["path"])
    c=choose_candidate(cfg)
    result={
        "status":"PASS_EVA_FORECAST_PARSER_V0_2_ACCEPTANCE_CHECK",
        "baseline_2020_2023":{
            "path":str(a),"exists":a.exists(),
            "sha256":sha256_file(a) if a.exists() else None,
            "expected_sha256":cfg["baseline_2020_2023"]["sha256"],
        },
        "baseline_2024_2026":{
            "path":str(b),"exists":b.exists(),
            "sha256":sha256_file(b) if b.exists() else None,
            "expected_sha256":cfg["baseline_2024_2026"]["sha256"],
        },
        "candidate_paths":[{"path":x,"exists":Path(x).exists()} for x in cfg["candidate_paths"]],
        "selected_candidate":str(c) if c else None,
        "gate":cfg["gate"],
        "read_only":True,
    }
    if not a.exists() or result["baseline_2020_2023"]["sha256"]!=cfg["baseline_2020_2023"]["sha256"]:
        result["status"]="BLOCKED_BASELINE_2020_2023_SHA"
    elif not b.exists() or result["baseline_2024_2026"]["sha256"]!=cfg["baseline_2024_2026"]["sha256"]:
        result["status"]="BLOCKED_BASELINE_2024_2026_SHA"
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result["status"].startswith("PASS") else 3


def cmd_run(cfg):
    try:
        baseline=make_baseline(cfg)
        p=choose_candidate(cfg)
        if p is None:
            print(json.dumps({
                "status":"BLOCKED_PARSER_V0_2_CANDIDATE_NOT_FOUND",
                "candidate_paths":cfg["candidate_paths"],
            },ensure_ascii=False,indent=2))
            return 3

        candidate=pd.read_parquet(p)
        v=validate(baseline,candidate,cfg,p)

        out=Path(cfg["output_dir"])
        out.mkdir(parents=True,exist_ok=True)
        payload={
            "status":"PASS_EVA_FORECAST_PARSER_V0_2_ACCEPTANCE_RUN",
            "generated_at":datetime.now().isoformat(timespec="seconds"),
            "research_id":cfg["research_id"],
            "baseline_union":{
                "rows":int(len(baseline)),
                "years":sorted(
                    int(x) for x in pd.to_datetime(baseline["event_time"],errors="coerce").dt.year.dropna().unique()
                ),
            },
            "validation":v,
            "next_action":(
                "If accepted: create immutable/versioned homogeneous 2020-2026 research asset, register source/parser hashes, then rerun formal corpus validation before starting Opportunity Fast Reject."
                if v.get("accepted")
                else "Do not merge or promote. Repair parser v0.2 or rerun full corpus until all frozen gates pass."
            ),
            "safety":{"read_only":True,"production_db":False,"broker":False,"qmt":False},
        }
        sp=out/"eva_forecast_parser_v0_2_acceptance_summary.json"
        sp.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(payload,ensure_ascii=False,indent=2))
        print(f"OUTPUT: {out}")
        return 0
    except Exception as e:
        print(json.dumps({
            "status":"FAIL_CLOSED_EVA_FORECAST_PARSER_V0_2_ACCEPTANCE",
            "error":f"{type(e).__name__}: {e}",
        },ensure_ascii=False,indent=2))
        return 3


def main():
    p=argparse.ArgumentParser()
    p.add_argument("command",choices=["check","run"])
    args=p.parse_args()
    cfg=load_cfg()
    return cmd_check(cfg) if args.command=="check" else cmd_run(cfg)


if __name__=="__main__":
    raise SystemExit(main())
