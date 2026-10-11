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
CFG = PROJECT_ROOT / "config/opportunity/sector_pit_candidate_validation_v0_3.yaml"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))


def parse_date_series(s: pd.Series) -> pd.Series:
    """Parse PIT date fields robustly.

    Handles YYYYMMDD, float-like YYYYMMDD.0, ISO dates, and nulls.
    """
    raw = s.astype("string").str.strip()

    def _one(v):
        if pd.isna(v):
            return pd.NaT
        sv = str(v).strip()
        if sv == "" or sv == "<NA>":
            return pd.NaT

        # Nullable numeric CSV columns can materialize as 20210730.0.
        if sv.endswith(".0"):
            head = sv[:-2]
            if head.isdigit() and len(head) == 8:
                sv = head

        if sv.isdigit() and len(sv) == 8:
            return pd.to_datetime(sv, format="%Y%m%d", errors="coerce")

        return pd.to_datetime(sv, errors="coerce")

    return raw.map(_one).astype("datetime64[ns]")


def normalize_symbol(s: pd.Series) -> pd.Series:
    return s.astype("string").str.strip().str.upper()


def lineage_completeness(df: pd.DataFrame) -> dict:
    cols = ["source_id", "source_url", "provenance_note"]
    result = {}
    for c in cols:
        if c not in df.columns:
            result[c] = {"exists": False, "nonempty_rate": 0.0}
            continue
        x = df[c].astype("string").str.strip()
        nonempty = x.notna() & x.ne("") & x.ne("<NA>")
        result[c] = {
            "exists": True,
            "nonempty_rate": float(nonempty.mean()) if len(x) else 0.0,
            "distinct_nonempty": int(x[nonempty].nunique()) if nonempty.any() else 0,
            "sample": [str(v) for v in x[nonempty].drop_duplicates().head(5).tolist()],
        }
    return result


def interval_integrity(df: pd.DataFrame, start_col: str, end_col: str) -> dict:
    x = df.copy()
    x["_start"] = parse_date_series(x[start_col])
    x["_end"] = parse_date_series(x[end_col]) if end_col in x.columns else pd.NaT
    x["_symbol"] = normalize_symbol(x["symbol"])

    invalid_start = int(x["_start"].isna().sum())
    invalid_end = int((x[end_col].notna() & x["_end"].isna()).sum()) if end_col in x.columns else 0
    nonempty_end = x["_end"].notna()
    invalid_interval = int((nonempty_end & (x["_end"] <= x["_start"])).sum())

    group_cols = ["_symbol"]
    for c in ("standard", "level"):
        if c in x.columns:
            group_cols.append(c)

    overlap_count = 0
    gap_examples = []
    overlap_examples = []
    change_symbols = 0

    for key, g in x.sort_values(group_cols + ["_start"]).groupby(group_cols, dropna=False):
        g = g.sort_values("_start")
        if len(g) > 1:
            change_symbols += 1
        prev_end = None
        prev_row = None
        for row in g.to_dict("records"):
            st = row["_start"]
            en = row["_end"]
            if prev_end is not None and pd.notna(st):
                if pd.isna(prev_end) or st < prev_end:
                    overlap_count += 1
                    if len(overlap_examples) < 10:
                        overlap_examples.append({
                            "group": str(key),
                            "prev_end": None if pd.isna(prev_end) else str(prev_end.date()),
                            "next_start": str(st.date()),
                        })
                elif st > prev_end and len(gap_examples) < 10:
                    gap_examples.append({
                        "group": str(key),
                        "prev_end": str(prev_end.date()),
                        "next_start": str(st.date()),
                        "gap_days": int((st - prev_end).days),
                    })
            prev_end = en
            prev_row = row

    return {
        "rows": int(len(x)),
        "distinct_symbols": int(x["_symbol"].nunique(dropna=True)),
        "date_start_min": str(x["_start"].min().date()) if x["_start"].notna().any() else None,
        "date_start_max": str(x["_start"].max().date()) if x["_start"].notna().any() else None,
        "date_end_min": str(x["_end"].min().date()) if x["_end"].notna().any() else None,
        "date_end_max": str(x["_end"].max().date()) if x["_end"].notna().any() else None,
        "open_ended_rows": int(x["_end"].isna().sum()),
        "invalid_start_rows": invalid_start,
        "invalid_end_rows": invalid_end,
        "invalid_interval_rows": invalid_interval,
        "overlap_count": int(overlap_count),
        "overlap_examples": overlap_examples,
        "gap_examples": gap_examples,
        "symbols_with_multiple_intervals": int(change_symbols),
        "exact_duplicate_rows": int(x.duplicated().sum()),
    }


def candidate_profile(path: Path, start_col: str, end_col: str) -> dict:
    df = pd.read_csv(path, low_memory=False)
    required = ["symbol", "industry_code", "industry_name", start_col]
    missing = [c for c in required if c not in df.columns]
    result = {
        "path": str(path),
        "exists": True,
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
    result["standards"] = (
        [str(v) for v in df["standard"].dropna().astype(str).unique().tolist()]
        if "standard" in df.columns else []
    )
    result["levels"] = (
        [str(v) for v in df["level"].dropna().astype(str).unique().tolist()]
        if "level" in df.columns else []
    )
    result["distinct_industry_codes"] = int(df["industry_code"].nunique(dropna=True))
    result["distinct_industry_names"] = int(df["industry_name"].nunique(dropna=True))
    result["top_industries"] = [
        {"industry_code": str(k), "rows": int(v)}
        for k, v in df["industry_code"].value_counts(dropna=True).head(20).items()
    ]
    result["interval_integrity"] = interval_integrity(df, start_col, end_col)
    result["lineage"] = lineage_completeness(df)
    return result


def coverage_against_pit(
    candidate_path: Path,
    pit_path: Path,
    start_col: str,
    end_col: str,
) -> dict:
    ind = pd.read_csv(candidate_path, low_memory=False)
    pit = pd.read_parquet(pit_path, columns=["trade_date", "symbol"]).drop_duplicates()

    ind["_symbol"] = normalize_symbol(ind["symbol"])
    ind["_start"] = parse_date_series(ind[start_col])
    if end_col in ind.columns:
        ind["_end"] = parse_date_series(ind[end_col])
    else:
        ind["_end"] = pd.NaT

    pit["_symbol"] = normalize_symbol(pit["symbol"])
    pit["_date"] = parse_date_series(pit["trade_date"])

    # Keep the classification identity needed for ambiguity checks.
    keep = ["_symbol", "_start", "_end", "industry_code", "industry_name"]
    for c in ("standard", "level"):
        if c in ind.columns:
            keep.append(c)
    ind = ind[keep].dropna(subset=["_symbol", "_start"]).copy()

    merged = pit.reset_index(names="_pit_row").merge(ind, on="_symbol", how="left")
    active = (
        merged["_start"].notna()
        & (merged["_date"] >= merged["_start"])
        & (merged["_end"].isna() | (merged["_date"] < merged["_end"]))
    )
    active_rows = merged.loc[active].copy()

    match_counts = active_rows.groupby("_pit_row").size()
    matched_idx = match_counts.index
    covered = int(len(matched_idx))
    ambiguous = int((match_counts > 1).sum())

    total = int(len(pit))
    coverage_rate = covered / total if total else 0.0
    ambiguity_rate = ambiguous / total if total else 0.0

    # Year coverage.
    pit_year = pit[["_date"]].copy()
    pit_year["year"] = pit_year["_date"].dt.year.astype("Int64")
    pit_year["matched"] = False
    pit_year.loc[matched_idx, "matched"] = True

    by_year = {}
    for y, g in pit_year.groupby("year", dropna=True):
        by_year[str(int(y))] = {
            "rows": int(len(g)),
            "coverage_rate": float(g["matched"].mean()),
        }

    unmatched_sample = (
        pit.loc[~pit.index.isin(matched_idx), ["trade_date", "symbol"]]
        .head(20)
        .astype(str)
        .to_dict("records")
    )

    ambiguous_sample = []
    if ambiguous:
        amb_ids = match_counts[match_counts > 1].head(10).index.tolist()
        for pid in amb_ids:
            rows = active_rows[active_rows["_pit_row"] == pid]
            ambiguous_sample.append({
                "trade_date": str(pit.loc[pid, "trade_date"]),
                "symbol": str(pit.loc[pid, "symbol"]),
                "matches": rows[
                    ["industry_code", "industry_name", "_start", "_end"]
                ].astype(str).to_dict("records"),
            })

    return {
        "pit_panel": str(pit_path),
        "pit_panel_sha256": sha256_file(pit_path),
        "pit_rows": total,
        "matched_pit_rows": covered,
        "coverage_rate": coverage_rate,
        "ambiguous_pit_rows": ambiguous,
        "ambiguity_rate": ambiguity_rate,
        "by_year": by_year,
        "unmatched_sample": unmatched_sample,
        "ambiguous_sample": ambiguous_sample,
    }


def acceptance(candidate: dict, coverage: dict, cfg: dict) -> dict:
    integ = candidate.get("interval_integrity") or {}
    lineage = candidate.get("lineage") or {}
    years = coverage.get("by_year") or {}

    source_ok = all(
        lineage.get(c, {}).get("exists") and lineage.get(c, {}).get("nonempty_rate", 0) >= cfg["gate"]["lineage_nonempty_min"]
        for c in ("source_id", "source_url", "provenance_note")
    )

    yearly_ok = all(
        years.get(str(y), {}).get("coverage_rate", 0.0) >= cfg["gate"]["yearly_coverage_min"]
        for y in range(2020, 2027)
        if str(y) in years
    )

    checks = {
        "schema_ok": candidate.get("status") == "SCHEMA_OK",
        "intervals_valid": (
            integ.get("invalid_start_rows", 1) == 0
            and integ.get("invalid_end_rows", 1) == 0
            and integ.get("invalid_interval_rows", 1) == 0
        ),
        "no_overlaps": integ.get("overlap_count", 1) == 0,
        "coverage_min": coverage.get("coverage_rate", 0.0) >= cfg["gate"]["overall_coverage_min"],
        "yearly_coverage_min": yearly_ok,
        "ambiguity_max": coverage.get("ambiguity_rate", 1.0) <= cfg["gate"]["ambiguity_rate_max"],
        "lineage_complete": source_ok,
        "historical_start_ok": (
            integ.get("date_start_min") is not None
            and integ["date_start_min"] <= cfg["gate"]["required_start_date"]
        ),
    }
    passed = all(checks.values())
    return {
        "checks": checks,
        "accepted": bool(passed),
        "verdict": (
            "ACCEPT_PIT_SECTOR_ASSET_FOR_SECTOR_RESEARCH"
            if passed
            else "REJECT_OR_HOLD_PIT_SECTOR_ASSET_PENDING_REPAIR"
        ),
    }


def cmd_check(cfg):
    candidates = [Path(x) for x in cfg["candidate_paths"]]
    pit = Path(cfg["pit_panel"])
    result = {
        "status": "PASS_SECTOR_PIT_CANDIDATE_VALIDATION_V0_3_CHECK"
                  if pit.exists() and any(p.exists() for p in candidates)
                  else "BLOCKED_SECTOR_PIT_CANDIDATE_VALIDATION_V0_3_CHECK",
        "candidate_paths": [{"path": str(p), "exists": p.exists()} for p in candidates],
        "pit_panel": str(pit),
        "pit_panel_exists": pit.exists(),
        "gate": cfg["gate"],
        "read_only": True,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if pit.exists() and any(p.exists() for p in candidates) else 3


def cmd_run(cfg):
    candidates = [Path(x) for x in cfg["candidate_paths"] if Path(x).exists()]
    if not candidates:
        return cmd_check(cfg)

    profiles = [
        candidate_profile(p, cfg["start_col"], cfg["end_col"])
        for p in candidates
    ]

    hash_groups = {}
    for p in profiles:
        hash_groups.setdefault(p.get("sha256"), []).append(p.get("path"))

    # Validate the first unique valid candidate against the PIT panel.
    canonical = next(
        (Path(p["path"]) for p in profiles if p.get("status") == "SCHEMA_OK"),
        None,
    )
    pit_path = Path(cfg["pit_panel"])
    coverage = (
        coverage_against_pit(
            canonical, pit_path, cfg["start_col"], cfg["end_col"]
        )
        if canonical is not None else {}
    )
    accept = (
        acceptance(
            next(p for p in profiles if p.get("path") == str(canonical)),
            coverage,
            cfg,
        )
        if canonical is not None else {
            "accepted": False,
            "verdict": "NO_VALID_SCHEMA_CANDIDATE",
            "checks": {},
        }
    )

    out = Path(cfg["output_dir"])
    out.mkdir(parents=True, exist_ok=True)

    result = {
        "status": "PASS_SECTOR_PIT_CANDIDATE_VALIDATION_V0_3_RUN",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "research_id": cfg["research_id"],
        "semantic_corrections": {
            "effective_from": "membership-valid-from timestamp",
            "effective_to_exclusive": "membership-valid-to exclusive timestamp",
            "list_date": "security listing date; NOT industry membership effective date",
            "classification_identity": "industry_code + industry_name; level is taxonomy depth, not classification itself",
        },
        "candidate_profiles": profiles,
        "duplicate_hash_groups": [
            {"sha256": h, "paths": paths, "count": len(paths)}
            for h, paths in hash_groups.items()
        ],
        "canonical_candidate_evaluated": str(canonical) if canonical else None,
        "pit_coverage": coverage,
        "acceptance": accept,
        "next_action": (
            "Promote the accepted canonical asset into the durable research catalog with source/version/hash metadata, then build Sector Ignition."
            if accept.get("accepted")
            else "Do not use for Sector Ignition until the failed acceptance checks are repaired or a better PIT sector asset is acquired."
        ),
        "safety": {
            "read_only": True,
            "production_db": False,
            "broker": False,
            "qmt": False,
        },
    }

    summary_path = out / "sector_pit_candidate_validation_v0_3_summary.json"
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
