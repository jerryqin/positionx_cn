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
CFG = PROJECT_ROOT / "config/opportunity/eva_forecast_homogeneous_reparse_acceptance_v0_1.yaml"

IDENTITY = ["announcementId", "symbol"]

REQUIRED = [
    "announcementId","symbol","secCode","secName","event_time","event_precision",
    "event_date","title","event_kind","explicit_st_at_event",
    "strategy_eligible_semantics","exclusion_reason","report_period",
    "current_profit_min_yuan","current_profit_max_yuan",
    "prior_profit_min_yuan","prior_profit_max_yuan","profit_state",
    "reported_net_profit_yoy_min_pct","reported_net_profit_yoy_max_pct",
    "profit_change_vs_prior_abs_pct","change_type","change_basis",
    "parser_confidence","pdf_path","pdf_sha256",
]

SEMANTIC = [
    "current_profit_min_yuan","current_profit_max_yuan",
    "prior_profit_min_yuan","prior_profit_max_yuan",
    "reported_net_profit_yoy_min_pct","reported_net_profit_yoy_max_pct",
    "profit_change_vs_prior_abs_pct","profit_state","change_type",
]


def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024), b""):
            h.update(chunk)
    return h.hexdigest()


def choose_reparse(cfg):
    for p in cfg["reparse_candidates"]:
        q=Path(p)
        if q.exists():
            return q
    return None


def normalize_text(s: pd.Series) -> pd.Series:
    return (
        s.astype("string")
        .fillna("")
        .str.normalize("NFKC")
        .str.replace(r"\s+", "", regex=True)
        .str.strip()
    )


def nonempty_rate(df: pd.DataFrame, col: str) -> float:
    if col not in df.columns or len(df)==0:
        return 0.0
    x=df[col].astype("string").str.strip()
    return float((x.notna() & x.ne("") & x.ne("<NA>")).mean())


def semantic_payload_rate(df: pd.DataFrame) -> float:
    mask=pd.Series(False,index=df.index)
    categorical_bad={"", "<NA>", "NA", "N/A", "NONE", "UNKNOWN", "UNPARSED", "EXCLUDED"}
    for c in SEMANTIC:
        if c not in df.columns:
            continue
        x=df[c]
        if pd.api.types.is_numeric_dtype(x):
            mask |= pd.to_numeric(x, errors="coerce").notna()
        else:
            y=x.astype("string").fillna("").str.strip().str.upper()
            mask |= ~y.isin(categorical_bad)
    return float(mask.mean()) if len(df) else 0.0


def exact_match_rate(a: pd.Series, b: pd.Series) -> float:
    aa=a.astype("string").fillna("<NA>")
    bb=b.astype("string").fillna("<NA>")
    return float((aa==bb).mean()) if len(aa) else 0.0


def datetime_match_rate(a: pd.Series, b: pd.Series) -> float:
    aa=pd.to_datetime(a, errors="coerce")
    bb=pd.to_datetime(b, errors="coerce")
    same=(aa==bb) | (aa.isna() & bb.isna())
    return float(same.mean()) if len(same) else 0.0


def numeric_agreement(a: pd.Series, b: pd.Series) -> dict:
    aa=pd.to_numeric(a, errors="coerce")
    bb=pd.to_numeric(b, errors="coerce")
    both=aa.notna() & bb.notna()
    if not both.any():
        return {"both_nonnull_rows":0,"exact_rate":None,"close_rate":None}
    exact=(aa[both]==bb[both]).mean()
    denom=(aa[both].abs()+bb[both].abs()).replace(0,1.0)
    rel=((aa[both]-bb[both]).abs()/denom)
    close=(rel <= 1e-6).mean()
    return {
        "both_nonnull_rows":int(both.sum()),
        "exact_rate":float(exact),
        "close_rate":float(close),
    }


def categorical_agreement(a: pd.Series, b: pd.Series) -> dict:
    aa=a.astype("string")
    bb=b.astype("string")
    both=aa.notna() & bb.notna()
    if not both.any():
        return {"both_nonnull_rows":0,"match_rate":None}
    return {
        "both_nonnull_rows":int(both.sum()),
        "match_rate":float((aa[both]==bb[both]).mean()),
    }


def validate(canonical_path: Path, reparse_path: Path, cfg: dict) -> dict:
    c=pd.read_parquet(canonical_path)
    r=pd.read_parquet(reparse_path)

    missing=[x for x in REQUIRED if x not in r.columns]
    if missing:
        return {
            "accepted":False,
            "verdict":"REJECT_REPARSE_SCHEMA",
            "required_missing":missing,
            "reparse_sha256":sha256_file(reparse_path),
            "reparse_rows":int(len(r)),
        }

    c_id=c[IDENTITY].astype("string")
    r_id=r[IDENTITY].astype("string")

    c_keys=set(map(tuple,c_id.itertuples(index=False,name=None)))
    r_keys=set(map(tuple,r_id.itertuples(index=False,name=None)))

    missing_keys=c_keys-r_keys
    extra_keys=r_keys-c_keys
    identity_coverage=(len(c_keys)-len(missing_keys))/len(c_keys) if c_keys else 0.0

    merged=c.merge(
        r,
        on=IDENTITY,
        how="inner",
        suffixes=("_canonical","_reparse"),
        validate="one_to_one",
    )

    event_years=sorted(
        int(x) for x in pd.to_datetime(r["event_time"],errors="coerce").dt.year.dropna().unique()
    )

    hard={
        "row_count_exact":int(len(r))==int(cfg["gate"]["row_count_exact"]),
        "identity_coverage_min":identity_coverage >= float(cfg["gate"]["identity_coverage_min"]),
        "identity_extra_max":len(extra_keys) <= int(cfg["gate"]["identity_extra_max"]),
        "duplicate_identity_max":int(r_id.duplicated().sum()) <= int(cfg["gate"]["duplicate_identity_max"]),
        "event_time_exact_match_min":datetime_match_rate(
            merged["event_time_canonical"],merged["event_time_reparse"]
        ) >= float(cfg["gate"]["event_time_exact_match_min"]),
        "event_precision_exact_match_min":exact_match_rate(
            merged["event_precision_canonical"],merged["event_precision_reparse"]
        ) >= float(cfg["gate"]["event_precision_exact_match_min"]),
        "pdf_sha256_exact_match_min":exact_match_rate(
            merged["pdf_sha256_canonical"],merged["pdf_sha256_reparse"]
        ) >= float(cfg["gate"]["pdf_sha256_exact_match_min"]),
        "title_normalized_match_min":float((
            normalize_text(merged["title_canonical"])==normalize_text(merged["title_reparse"])
        ).mean()) >= float(cfg["gate"]["title_normalized_match_min"]),
        "event_time_nonnull_min":float(pd.to_datetime(r["event_time"],errors="coerce").notna().mean())
            >= float(cfg["gate"]["event_time_nonnull_min"]),
        "symbol_nonempty_min":nonempty_rate(r,"symbol") >= float(cfg["gate"]["symbol_nonempty_min"]),
        "semantic_payload_min":semantic_payload_rate(r) >= float(cfg["gate"]["semantic_payload_min"]),
        "event_years_exact":event_years == [int(x) for x in cfg["gate"]["event_years_exact"]],
    }

    diagnostics={
        "event_kind":categorical_agreement(
            merged["event_kind_canonical"],merged["event_kind_reparse"]
        ),
        "report_period":categorical_agreement(
            merged["report_period_canonical"],merged["report_period_reparse"]
        ),
        "strategy_eligible_semantics":categorical_agreement(
            merged["strategy_eligible_semantics_canonical"],
            merged["strategy_eligible_semantics_reparse"],
        ),
        "profit_state":categorical_agreement(
            merged["profit_state_canonical"],merged["profit_state_reparse"]
        ),
        "change_type":categorical_agreement(
            merged["change_type_canonical"],merged["change_type_reparse"]
        ),
    }

    for c0 in [
        "current_profit_min_yuan","current_profit_max_yuan",
        "prior_profit_min_yuan","prior_profit_max_yuan",
        "reported_net_profit_yoy_min_pct","reported_net_profit_yoy_max_pct",
        "profit_change_vs_prior_abs_pct",
    ]:
        diagnostics[c0]=numeric_agreement(
            merged[f"{c0}_canonical"],merged[f"{c0}_reparse"]
        )

    accepted=all(hard.values())
    return {
        "canonical_path":str(canonical_path),
        "canonical_sha256":sha256_file(canonical_path),
        "canonical_rows":int(len(c)),
        "reparse_path":str(reparse_path),
        "reparse_sha256":sha256_file(reparse_path),
        "reparse_rows":int(len(r)),
        "identity":{
            "canonical_distinct":len(c_keys),
            "reparse_distinct":len(r_keys),
            "missing_identity_count":len(missing_keys),
            "extra_identity_count":len(extra_keys),
            "identity_coverage":identity_coverage,
            "duplicate_reparse_identity_rows":int(r_id.duplicated().sum()),
            "missing_identity_examples":[list(x) for x in list(sorted(missing_keys))[:20]],
            "extra_identity_examples":[list(x) for x in list(sorted(extra_keys))[:20]],
        },
        "hard_metrics":{
            "event_time_exact_match_rate":datetime_match_rate(
                merged["event_time_canonical"],merged["event_time_reparse"]
            ),
            "event_precision_exact_match_rate":exact_match_rate(
                merged["event_precision_canonical"],merged["event_precision_reparse"]
            ),
            "pdf_sha256_exact_match_rate":exact_match_rate(
                merged["pdf_sha256_canonical"],merged["pdf_sha256_reparse"]
            ),
            "title_normalized_match_rate":float((
                normalize_text(merged["title_canonical"])==normalize_text(merged["title_reparse"])
            ).mean()),
            "event_time_nonnull_rate":float(pd.to_datetime(r["event_time"],errors="coerce").notna().mean()),
            "symbol_nonempty_rate":nonempty_rate(r,"symbol"),
            "semantic_payload_rate":semantic_payload_rate(r),
            "event_years":event_years,
        },
        "semantic_diagnostics_old_vs_new_parser":diagnostics,
        "checks":hard,
        "accepted":accepted,
        "verdict":(
            "ACCEPT_HOMOGENEOUS_REPARSE_2024_2026_V0_1"
            if accepted else
            "REJECT_OR_HOLD_HOMOGENEOUS_REPARSE_2024_2026_V0_1"
        ),
        "note":(
            "Semantic agreement with the old parser is diagnostic only. "
            "Identity, PIT time, PDF identity, and new-parser payload are the hard gates."
        ),
    }


def cmd_check(cfg):
    c=Path(cfg["canonical"]["path"])
    b=Path(cfg["backfill"]["path"])
    r=choose_reparse(cfg)
    result={
        "status":"PASS_EVA_FORECAST_HOMOGENEOUS_REPARSE_ACCEPTANCE_V0_1_CHECK",
        "research_id":cfg["research_id"],
        "canonical":{
            "path":str(c),"exists":c.exists(),
            "sha256":sha256_file(c) if c.exists() else None,
            "expected_sha256":cfg["canonical"]["sha256"],
        },
        "backfill":{
            "path":str(b),"exists":b.exists(),
            "sha256":sha256_file(b) if b.exists() else None,
            "expected_sha256":cfg["backfill"]["sha256"],
        },
        "reparse_candidates":[{"path":x,"exists":Path(x).exists()} for x in cfg["reparse_candidates"]],
        "selected_reparse":str(r) if r else None,
        "gate":cfg["gate"],
        "read_only":True,
    }
    if not c.exists() or sha256_file(c)!=cfg["canonical"]["sha256"]:
        result["status"]="BLOCKED_CANONICAL_SHA_MISMATCH_OR_MISSING"
    elif not b.exists() or sha256_file(b)!=cfg["backfill"]["sha256"]:
        result["status"]="BLOCKED_BACKFILL_SHA_MISMATCH_OR_MISSING"
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result["status"].startswith("PASS") else 3


def cmd_run(cfg):
    c=Path(cfg["canonical"]["path"])
    b=Path(cfg["backfill"]["path"])
    r=choose_reparse(cfg)

    if not c.exists() or sha256_file(c)!=cfg["canonical"]["sha256"]:
        print(json.dumps({"status":"BLOCKED_CANONICAL_SHA_MISMATCH_OR_MISSING"},indent=2))
        return 3
    if not b.exists() or sha256_file(b)!=cfg["backfill"]["sha256"]:
        print(json.dumps({"status":"BLOCKED_BACKFILL_SHA_MISMATCH_OR_MISSING"},indent=2))
        return 3
    if r is None:
        print(json.dumps({
            "status":"BLOCKED_HOMOGENEOUS_REPARSE_INPUT_NOT_FOUND",
            "reparse_candidates":cfg["reparse_candidates"],
        },ensure_ascii=False,indent=2))
        return 3

    v=validate(c,r,cfg)
    out=Path(cfg["output_dir"]); out.mkdir(parents=True,exist_ok=True)

    result={
        "status":"PASS_EVA_FORECAST_HOMOGENEOUS_REPARSE_ACCEPTANCE_V0_1_RUN",
        "generated_at":datetime.now().isoformat(timespec="seconds"),
        "research_id":cfg["research_id"],
        "validation":v,
        "next_action":(
            "If accepted: build a versioned homogeneous 2020-2026 merged candidate from accepted 2020-2023 backfill + accepted 2024-2026 reparse; then run merged-corpus validation before promotion."
            if v.get("accepted") else
            "Repair identity/PIT/PDF/payload failures; do not merge."
        ),
        "safety":{"read_only":True,"production_db":False,"broker":False,"qmt":False},
    }
    p=out/"eva_forecast_homogeneous_reparse_acceptance_v0_1_summary.json"
    p.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False,indent=2))
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
