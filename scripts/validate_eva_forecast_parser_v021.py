from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CFG = PROJECT_ROOT / "config/opportunity/eva_forecast_parser_v0_2_1_acceptance.yaml"

IDENTITY = ["announcementId", "symbol"]
SEM_NUM = [
    "current_profit_min_yuan","current_profit_max_yuan",
    "prior_profit_min_yuan","prior_profit_max_yuan",
    "reported_net_profit_yoy_min_pct","reported_net_profit_yoy_max_pct",
    "profit_change_vs_prior_abs_pct",
]
SEM_CAT = ["profit_state","change_type","change_basis"]
BAD = {
    "", "<NA>", "NA", "N/A", "NONE", "UNKNOWN", "UNPARSED",
    "EXCLUDED", "AMBIGUOUS", "NOT_APPLICABLE",
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


def norm_bool(s: pd.Series) -> pd.Series:
    return s.astype("string").str.strip().str.lower().isin(
        ["true","1","yes","y","eligible"]
    )


def norm_nullable(s: pd.Series) -> pd.Series:
    return s.astype("string").fillna("<NA>").str.strip()


def norm_text(s: pd.Series) -> pd.Series:
    return (
        s.astype("string").fillna("")
        .str.normalize("NFKC")
        .str.replace(r"\s+","",regex=True)
        .str.strip()
    )


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
    aa=norm_nullable(a); bb=norm_nullable(b)
    return float((aa==bb).mean()) if len(aa) else 0.0


def datetime_rate(a: pd.Series,b: pd.Series) -> float:
    aa=a.map(lambda v: pd.to_datetime(v,errors="coerce"))
    bb=b.map(lambda v: pd.to_datetime(v,errors="coerce"))
    return float(((aa==bb)|(aa.isna()&bb.isna())).mean()) if len(aa) else 0.0


def yearly_stats(df: pd.DataFrame) -> dict:
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


def validate(base: pd.DataFrame, cand: pd.DataFrame, cfg: dict, p: Path) -> dict:
    missing=[c for c in REQUIRED if c not in cand.columns]
    if missing:
        return {"accepted":False,"verdict":"REJECT_V021_SCHEMA","required_missing":missing}

    bkeys=set(map(tuple,base[IDENTITY].astype("string").itertuples(index=False,name=None)))
    ckeys=set(map(tuple,cand[IDENTITY].astype("string").itertuples(index=False,name=None)))
    missing_keys=bkeys-ckeys
    extra_keys=ckeys-bkeys
    coverage=(len(bkeys)-len(missing_keys))/len(bkeys) if bkeys else 0.0

    m=base.merge(cand,on=IDENTITY,how="inner",suffixes=("_v02","_v021"),validate="one_to_one")

    years=sorted(int(x) for x in pd.to_datetime(cand["event_time"],errors="coerce").dt.year.dropna().unique())

    eligible=norm_bool(cand["strategy_eligible_semantics"])
    c_payload=strict_payload_mask(cand)
    b_payload=strict_payload_mask(base)

    # Align baseline masks to candidate by identity.
    bmask_df=base[IDENTITY].copy()
    bmask_df["_eligible_v02"]=norm_bool(base["strategy_eligible_semantics"]).to_numpy()
    bmask_df["_payload_v02"]=b_payload.to_numpy()
    bmask_df["_cpmin_v02"]=pd.to_numeric(base["current_profit_min_yuan"],errors="coerce").notna().to_numpy()
    bmask_df["_cpmax_v02"]=pd.to_numeric(base["current_profit_max_yuan"],errors="coerce").notna().to_numpy()

    cmask_df=cand[IDENTITY].copy()
    cmask_df["_eligible_v021"]=eligible.to_numpy()
    cmask_df["_payload_v021"]=c_payload.to_numpy()
    cmask_df["_cpmin_v021"]=pd.to_numeric(cand["current_profit_min_yuan"],errors="coerce").notna().to_numpy()
    cmask_df["_cpmax_v021"]=pd.to_numeric(cand["current_profit_max_yuan"],errors="coerce").notna().to_numpy()

    rr=bmask_df.merge(cmask_df,on=IDENTITY,validate="one_to_one")
    research=rr["_eligible_v02"] & rr["_eligible_v021"]

    payload_reg=int((research & rr["_payload_v02"] & ~rr["_payload_v021"]).sum())
    cpmin_reg=int((research & rr["_cpmin_v02"] & ~rr["_cpmin_v021"]).sum())
    cpmax_reg=int((research & rr["_cpmax_v02"] & ~rr["_cpmax_v021"]).sum())

    recovered=research & ~rr["_payload_v02"] & rr["_payload_v021"]
    recovered_ids=rr.loc[recovered,IDENTITY].copy()
    cand_year=cand[IDENTITY+["event_time"]].copy()
    recovered_ids=recovered_ids.merge(cand_year,on=IDENTITY,how="left",validate="one_to_one")
    recovered_ids["_year"]=pd.to_datetime(recovered_ids["event_time"],errors="coerce").dt.year

    recovered_by_year={
        str(int(y)):int(v)
        for y,v in recovered_ids["_year"].value_counts().sort_index().items()
        if pd.notna(y)
    }

    ge=cand.loc[eligible]
    ep=strict_payload_mask(ge)
    cpmin=pd.to_numeric(ge["current_profit_min_yuan"],errors="coerce")
    cpmax=pd.to_numeric(ge["current_profit_max_yuan"],errors="coerce")

    yearly=yearly_stats(cand)
    g=cfg["gate"]

    yearly_payload_pass=all(
        v["eligible_strict_payload_rate"] is not None
        and v["eligible_strict_payload_rate"]>=float(g["eligible_yearly_strict_payload_min"])
        for v in yearly.values()
    )
    yearly_min_pass=all(
        v["eligible_current_profit_min_coverage"] is not None
        and v["eligible_current_profit_min_coverage"]>=float(g["eligible_yearly_current_profit_min_coverage_min"])
        for v in yearly.values()
    )
    yearly_max_pass=all(
        v["eligible_current_profit_max_coverage"] is not None
        and v["eligible_current_profit_max_coverage"]>=float(g["eligible_yearly_current_profit_max_coverage_min"])
        for v in yearly.values()
    )

    hard={
        "rows":int(len(cand)),
        "identity_coverage":coverage,
        "missing_identity_count":len(missing_keys),
        "extra_identity_count":len(extra_keys),
        "duplicate_identity_rows":int(cand[IDENTITY].astype("string").duplicated().sum()),
        "event_time_exact_match_rate":datetime_rate(m["event_time_v02"],m["event_time_v021"]),
        "event_precision_exact_match_rate":exact_rate(m["event_precision_v02"],m["event_precision_v021"]),
        "pdf_sha256_exact_match_rate":exact_rate(m["pdf_sha256_v02"],m["pdf_sha256_v021"]),
        "title_normalized_match_rate":float((norm_text(m["title_v02"])==norm_text(m["title_v021"])).mean()),
        "event_kind_exact_match_rate":exact_rate(m["event_kind_v02"],m["event_kind_v021"]),
        "explicit_st_exact_match_rate":exact_rate(m["explicit_st_at_event_v02"],m["explicit_st_at_event_v021"]),
        "strategy_eligible_exact_match_rate":exact_rate(m["strategy_eligible_semantics_v02"],m["strategy_eligible_semantics_v021"]),
        "exclusion_reason_exact_match_rate":exact_rate(m["exclusion_reason_v02"],m["exclusion_reason_v021"]),
        "eligible_rows":int(eligible.sum()),
        "eligible_strict_payload_rate":float(ep.mean()) if len(ep) else None,
        "eligible_current_profit_min_coverage":float(cpmin.notna().mean()) if len(cpmin) else None,
        "eligible_current_profit_max_coverage":float(cpmax.notna().mean()) if len(cpmax) else None,
        "payload_regression_rows":payload_reg,
        "current_profit_min_regression_rows":cpmin_reg,
        "current_profit_max_regression_rows":cpmax_reg,
        "recovered_payload_rows":int(recovered.sum()),
        "recovered_payload_rows_by_year":recovered_by_year,
        "event_years":years,
    }

    checks={
        "row_count_exact":hard["rows"]==int(g["row_count_exact"]),
        "identity_coverage_min":coverage>=float(g["identity_coverage_min"]),
        "identity_extra_max":len(extra_keys)<=int(g["identity_extra_max"]),
        "duplicate_identity_max":hard["duplicate_identity_rows"]<=int(g["duplicate_identity_max"]),
        "event_time_exact_match_min":hard["event_time_exact_match_rate"]>=float(g["event_time_exact_match_min"]),
        "event_precision_exact_match_min":hard["event_precision_exact_match_rate"]>=float(g["event_precision_exact_match_min"]),
        "pdf_sha256_exact_match_min":hard["pdf_sha256_exact_match_rate"]>=float(g["pdf_sha256_exact_match_min"]),
        "title_normalized_match_min":hard["title_normalized_match_rate"]>=float(g["title_normalized_match_min"]),
        "event_kind_exact_match_min":hard["event_kind_exact_match_rate"]>=float(g["event_kind_exact_match_min"]),
        "explicit_st_exact_match_min":hard["explicit_st_exact_match_rate"]>=float(g["explicit_st_exact_match_min"]),
        "strategy_eligible_exact_match_min":hard["strategy_eligible_exact_match_rate"]>=float(g["strategy_eligible_exact_match_min"]),
        "exclusion_reason_exact_match_min":hard["exclusion_reason_exact_match_rate"]>=float(g["exclusion_reason_exact_match_min"]),
        "eligible_strict_payload_min":hard["eligible_strict_payload_rate"]>=float(g["eligible_strict_payload_min"]),
        "eligible_yearly_strict_payload_min":yearly_payload_pass,
        "eligible_current_profit_min_coverage_min":hard["eligible_current_profit_min_coverage"]>=float(g["eligible_current_profit_min_coverage_min"]),
        "eligible_current_profit_max_coverage_min":hard["eligible_current_profit_max_coverage"]>=float(g["eligible_current_profit_max_coverage_min"]),
        "eligible_yearly_current_profit_min_coverage_min":yearly_min_pass,
        "eligible_yearly_current_profit_max_coverage_min":yearly_max_pass,
        "payload_regression_rows_max":payload_reg<=int(g["payload_regression_rows_max"]),
        "current_profit_min_regression_rows_max":cpmin_reg<=int(g["current_profit_min_regression_rows_max"]),
        "current_profit_max_regression_rows_max":cpmax_reg<=int(g["current_profit_max_regression_rows_max"]),
        "event_years_exact":years==[int(x) for x in g["event_years_exact"]],
    }

    accepted=all(checks.values())

    diagnostics={
        "yearly_eligible":yearly,
        "parser_confidence_counts":{
            str(k):int(v) for k,v in cand["parser_confidence"].astype("string").value_counts(dropna=False).items()
        },
        "unit_resolution_method_counts":(
            {str(k):int(v) for k,v in cand["unit_resolution_method"].astype("string").value_counts(dropna=False).items()}
            if "unit_resolution_method" in cand.columns else None
        ),
        "parser_rule_id_counts":(
            {str(k):int(v) for k,v in cand["parser_rule_id"].astype("string").value_counts(dropna=False).items()}
            if "parser_rule_id" in cand.columns else None
        ),
        "parser_version_counts":(
            {str(k):int(v) for k,v in cand["parser_version"].astype("string").value_counts(dropna=False).items()}
            if "parser_version" in cand.columns else None
        ),
    }

    return {
        "candidate_path":str(p),
        "candidate_sha256":sha256_file(p),
        "hard_metrics":hard,
        "checks":checks,
        "diagnostics":diagnostics,
        "accepted":accepted,
        "verdict":(
            "ACCEPT_FORECAST_SEMANTIC_PARSER_V0_2_1_FULL_2020_2026"
            if accepted else
            "REJECT_OR_HOLD_FORECAST_SEMANTIC_PARSER_V0_2_1_FULL_2020_2026"
        ),
    }


def cmd_check(cfg):
    b=Path(cfg["baseline_v02"]["path"])
    c=choose_candidate(cfg)
    result={
        "status":"PASS_EVA_FORECAST_PARSER_V0_2_1_ACCEPTANCE_CHECK",
        "baseline_v02":{
            "path":str(b),"exists":b.exists(),
            "sha256":sha256_file(b) if b.exists() else None,
            "expected_sha256":cfg["baseline_v02"]["sha256"],
        },
        "candidate_paths":[{"path":x,"exists":Path(x).exists()} for x in cfg["candidate_paths"]],
        "selected_candidate":str(c) if c else None,
        "gate":cfg["gate"],
        "read_only":True,
    }
    if not b.exists() or result["baseline_v02"]["sha256"]!=cfg["baseline_v02"]["sha256"]:
        result["status"]="BLOCKED_BASELINE_V02_SHA"
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result["status"].startswith("PASS") else 3


def cmd_run(cfg):
    try:
        b=Path(cfg["baseline_v02"]["path"])
        if not b.exists() or sha256_file(b)!=cfg["baseline_v02"]["sha256"]:
            raise RuntimeError("baseline V0.2 missing or SHA mismatch")

        p=choose_candidate(cfg)
        if p is None:
            print(json.dumps({
                "status":"BLOCKED_PARSER_V0_2_1_CANDIDATE_NOT_FOUND",
                "candidate_paths":cfg["candidate_paths"],
            },ensure_ascii=False,indent=2))
            return 3

        base=pd.read_parquet(b)
        cand=pd.read_parquet(p)
        v=validate(base,cand,cfg,p)

        out=Path(cfg["output_dir"]); out.mkdir(parents=True,exist_ok=True)
        result={
            "status":"PASS_EVA_FORECAST_PARSER_V0_2_1_ACCEPTANCE_RUN",
            "generated_at":datetime.now().isoformat(timespec="seconds"),
            "research_id":cfg["research_id"],
            "validation":v,
            "next_action":(
                "If accepted: freeze and promote the 2020-2026 homogeneous semantic corpus as a versioned research asset with parser/source hashes, rerun formal corpus validation, then start the first non-price Opportunity Fast Reject."
                if v.get("accepted")
                else "Do not promote. Inspect failed gates; no gate relaxation and no identity/PIT/eligibility changes."
            ),
            "safety":{"read_only":True,"production_db":False,"broker":False,"qmt":False},
        }
        sp=out/"eva_forecast_parser_v0_2_1_acceptance_summary.json"
        sp.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(result,ensure_ascii=False,indent=2))
        print(f"OUTPUT: {out}")
        return 0
    except Exception as e:
        print(json.dumps({
            "status":"FAIL_CLOSED_EVA_FORECAST_PARSER_V0_2_1_ACCEPTANCE",
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
