from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CFG = PROJECT_ROOT / "config/opportunity/sector_pit_asset_audit_v0_2.yaml"

STRONG_CLASS_KEYS = (
    "industry", "industry_name", "industry_code",
    "sector", "sector_name", "sector_code",
    "classification", "classification_name", "classification_code",
    "sw_l1", "sw_l2", "sw_l3",
    "shenwan", "申万",
    "citic_l1", "citic_l2", "citic_l3", "中信",
    "csrc_industry", "证监会行业",
    "gics", "wind_industry", "中证行业",
)

EXCLUDED_CLASS_KEYS = (
    "board", "board_name", "board_code",
    "exchange", "exchange_id", "exchange_code",
    "market", "market_type",
    "instrument_type", "instrument_true_types",
    "scope_role",
)

SYMBOL_KEYS = (
    "symbol", "stock_code", "security_code", "ticker",
    "ts_code", "wind_code", "unified_code", "code",
)

DATE_KEYS = (
    "trade_date", "date", "snapshot_date", "asof_date", "as_of_date",
    "valid_from", "effective_date", "start_date", "begin_date",
    "entry_date", "announcement_date", "announce_date", "as_of",
)
END_KEYS = (
    "valid_to", "end_date", "expire_date", "exit_date",
    "classification_end_date", "effective_to",
)

FILE_NAME_KEYS = (
    "industry", "sector", "classification", "shenwan", "sw_",
    "citic", "csrc", "gics", "申万", "中信", "行业", "板块",
)

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))

def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9_\u4e00-\u9fff]+", "_", str(s).strip().lower())

def qident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'

def strong_class_col(cols):
    for c in cols:
        nc = norm(c)
        if any(k in nc for k in STRONG_CLASS_KEYS):
            if any(k in nc for k in EXCLUDED_CLASS_KEYS):
                continue
            return c
    return None

def first_matching(cols, keys):
    for c in cols:
        nc = norm(c)
        if nc in keys:
            return c
        if any(nc == k or nc.endswith("_" + k) for k in keys):
            return c
    return None

def semantic_status(cols):
    scol = first_matching(cols, SYMBOL_KEYS)
    ccol = strong_class_col(cols)
    dcol = first_matching(cols, DATE_KEYS)
    ecol = first_matching(cols, END_KEYS)

    detail = {
        "symbol_col": scol,
        "classification_col": ccol,
        "date_col": dcol,
        "end_col": ecol,
    }

    if ccol is None:
        return "NO_TRUE_SECTOR_OR_INDUSTRY_FIELD", detail
    if scol and dcol:
        return "PIT_CANDIDATE_REQUIRES_TEMPORAL_VALIDATION", detail
    if scol and not dcol:
        return "CURRENT_ONLY_PIT_UNSAFE", detail
    if ccol and dcol and not scol:
        return "INCOMPLETE_NO_SYMBOL", detail
    return "NOT_READY", detail

def table_stats(con, schema, table, detail):
    fq = f"{qident(schema)}.{qident(table)}"
    out = {}
    try:
        out["row_count"] = int(con.execute(f"select count(*) from {fq}").fetchone()[0])
    except Exception as e:
        out["row_count_error"] = f"{type(e).__name__}: {e}"
        return out

    for label, col in (
        ("symbol", detail.get("symbol_col")),
        ("classification", detail.get("classification_col")),
    ):
        if col:
            try:
                out[f"distinct_{label}s"] = int(
                    con.execute(f"select count(distinct {qident(col)}) from {fq}").fetchone()[0]
                )
            except Exception as e:
                out[f"distinct_{label}s_error"] = f"{type(e).__name__}: {e}"

    dcol = detail.get("date_col")
    if dcol:
        try:
            mn, mx = con.execute(
                f"select min({qident(dcol)}), max({qident(dcol)}) from {fq}"
            ).fetchone()
            out["date_min"] = None if mn is None else str(mn)
            out["date_max"] = None if mx is None else str(mx)
        except Exception as e:
            out["date_range_error"] = f"{type(e).__name__}: {e}"

    ccol = detail.get("classification_col")
    if ccol:
        try:
            vals = con.execute(
                f"select {qident(ccol)} as v, count(*) as n "
                f"from {fq} group by 1 order by n desc limit 20"
            ).fetchdf()
            out["top_classifications"] = [
                {"value": None if pd.isna(r.v) else str(r.v), "rows": int(r.n)}
                for r in vals.itertuples(index=False)
            ]
        except Exception as e:
            out["top_classifications_error"] = f"{type(e).__name__}: {e}"
    return out

def inspect_catalog(catalog: Path):
    con = duckdb.connect(str(catalog), read_only=True)
    try:
        tabs = con.execute("""
            select table_schema, table_name, table_type
            from information_schema.tables
            where table_schema not in ('information_schema','pg_catalog')
            order by table_schema, table_name
        """).fetchdf()
        results = []
        semantic_rejections = []

        for r in tabs.itertuples(index=False):
            schema, table, ttype = str(r.table_schema), str(r.table_name), str(r.table_type)
            cols_df = con.execute("""
                select column_name, data_type
                from information_schema.columns
                where table_schema=? and table_name=?
                order by ordinal_position
            """, [schema, table]).fetchdf()
            cols = [str(x) for x in cols_df["column_name"].tolist()]

            status, detail = semantic_status(cols)
            full_name = f"{schema}.{table}"
            has_name_hint = any(k in norm(full_name) for k in FILE_NAME_KEYS)

            # Explicitly record board-only false positives from V0.1.
            board_cols = [c for c in cols if "board" in norm(c)]
            if board_cols and detail["classification_col"] is None:
                semantic_rejections.append({
                    "catalog": str(catalog),
                    "schema": schema,
                    "table": table,
                    "reason": "BOARD_IS_EXCHANGE_LISTING_BOARD_NOT_INDUSTRY",
                    "board_columns": board_cols,
                })

            if detail["classification_col"] or has_name_hint:
                rec = {
                    "catalog": str(catalog),
                    "schema": schema,
                    "table": table,
                    "table_type": ttype,
                    "status": status,
                    **detail,
                    "columns": cols,
                }
                if status in (
                    "PIT_CANDIDATE_REQUIRES_TEMPORAL_VALIDATION",
                    "CURRENT_ONLY_PIT_UNSAFE",
                    "INCOMPLETE_NO_SYMBOL",
                ):
                    rec["stats"] = table_stats(con, schema, table, detail)
                results.append(rec)
        return results, semantic_rejections
    finally:
        con.close()

def inspect_parquet(path: Path):
    try:
        df = pd.read_parquet(path)
        cols = [str(c) for c in df.columns]
        status, detail = semantic_status(cols)
        rec = {
            "path": str(path),
            "suffix": ".parquet",
            "status": status,
            **detail,
            "columns": cols,
            "row_count": int(len(df)),
        }
        if detail["date_col"] and detail["date_col"] in df.columns:
            s = df[detail["date_col"]].dropna()
            if len(s):
                rec["date_min"] = str(s.min())
                rec["date_max"] = str(s.max())
        if detail["symbol_col"] and detail["symbol_col"] in df.columns:
            rec["distinct_symbols"] = int(df[detail["symbol_col"]].nunique(dropna=True))
        if detail["classification_col"] and detail["classification_col"] in df.columns:
            rec["distinct_classifications"] = int(
                df[detail["classification_col"]].nunique(dropna=True)
            )
            rec["top_classifications"] = [
                {"value": str(k), "rows": int(v)}
                for k, v in df[detail["classification_col"]].value_counts(dropna=True).head(20).items()
            ]
        return rec
    except Exception as e:
        return {
            "path": str(path),
            "suffix": ".parquet",
            "status": "INSPECTION_ERROR",
            "error": f"{type(e).__name__}: {e}",
        }

def inspect_csv(path: Path):
    try:
        df = pd.read_csv(path, nrows=50000, low_memory=False)
        cols = [str(c) for c in df.columns]
        status, detail = semantic_status(cols)
        rec = {
            "path": str(path),
            "suffix": ".csv",
            "status": status,
            **detail,
            "columns": cols,
            "sample_rows_read": int(len(df)),
            "file_size_bytes": int(path.stat().st_size),
        }
        if detail["classification_col"] and detail["classification_col"] in df.columns:
            rec["sample_distinct_classifications"] = int(
                df[detail["classification_col"]].nunique(dropna=True)
            )
            rec["sample_top_classifications"] = [
                {"value": str(k), "rows": int(v)}
                for k, v in df[detail["classification_col"]].value_counts(dropna=True).head(20).items()
            ]
        return rec
    except Exception as e:
        return {
            "path": str(path),
            "suffix": ".csv",
            "status": "INSPECTION_ERROR",
            "error": f"{type(e).__name__}: {e}",
        }

def inspect_json(path: Path):
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        rows = None
        if isinstance(obj, list) and obj and isinstance(obj[0], dict):
            rows = obj[:50000]
        elif isinstance(obj, dict):
            for v in obj.values():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    rows = v[:50000]
                    break
        if not rows:
            return {"path": str(path), "suffix": ".json", "status": "NO_TABULAR_ROWS_DETECTED"}
        df = pd.DataFrame(rows)
        cols = [str(c) for c in df.columns]
        status, detail = semantic_status(cols)
        return {
            "path": str(path), "suffix": ".json", "status": status,
            **detail, "columns": cols, "sample_rows_read": int(len(df)),
        }
    except Exception as e:
        return {
            "path": str(path), "suffix": ".json", "status": "INSPECTION_ERROR",
            "error": f"{type(e).__name__}: {e}",
        }

def inspect_file(path: Path):
    suf = path.suffix.lower()
    if suf == ".parquet":
        return inspect_parquet(path)
    if suf == ".csv":
        return inspect_csv(path)
    if suf in (".json", ".jsonl"):
        return inspect_json(path)
    return {"path": str(path), "suffix": suf, "status": "NAME_HIT_UNINSPECTED_FORMAT"}

def scan_named_files(roots, max_results=500):
    hits = []
    for root in roots:
        if not root.exists():
            continue
        try:
            for p in root.rglob("*"):
                if len(hits) >= max_results:
                    break
                if not p.is_file():
                    continue
                if not any(k in norm(p.name) for k in FILE_NAME_KEYS):
                    continue
                hits.append(inspect_file(p))
        except Exception as e:
            hits.append({
                "path": str(root),
                "status": "FILESYSTEM_SCAN_ERROR",
                "error": f"{type(e).__name__}: {e}",
            })
    return hits

def verdict(db_candidates, file_hits):
    pit = [
        x for x in db_candidates + file_hits
        if x.get("status") == "PIT_CANDIDATE_REQUIRES_TEMPORAL_VALIDATION"
    ]
    current = [
        x for x in db_candidates + file_hits
        if x.get("status") == "CURRENT_ONLY_PIT_UNSAFE"
    ]
    if pit:
        return (
            "TRUE_SECTOR_PIT_CANDIDATES_FOUND_REQUIRES_VALIDATION",
            "Validate source lineage, temporal semantics, 2020-2026 coverage, and reclassification history."
        )
    if current:
        return (
            "ONLY_CURRENT_SECTOR_ASSETS_FOUND_PIT_UNSAFE",
            "Do not backfill current industry classifications. Acquire/build PIT membership history."
        )
    return (
        "NO_TRUE_PIT_SECTOR_ASSET_FOUND",
        "Acquire or construct a source/versioned PIT industry-membership asset before Sector Ignition."
    )

def cmd_check(cfg):
    catalogs = [Path(x) for x in cfg["catalogs"]]
    result = {
        "status": "PASS_SECTOR_PIT_ASSET_AUDIT_V0_2_CHECK"
                  if any(p.exists() for p in catalogs)
                  else "BLOCKED_SECTOR_PIT_ASSET_AUDIT_V0_2_CHECK",
        "catalogs": [{"path": str(p), "exists": p.exists()} for p in catalogs],
        "filesystem_roots": cfg["filesystem_roots"],
        "semantic_rule": (
            "board/exchange/market fields are explicitly excluded from sector/industry classification"
        ),
        "read_only": True,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if any(p.exists() for p in catalogs) else 3

def cmd_run(cfg):
    db_candidates = []
    semantic_rejections = []
    for s in cfg["catalogs"]:
        p = Path(s)
        if not p.exists():
            continue
        c, r = inspect_catalog(p)
        db_candidates.extend(c)
        semantic_rejections.extend(r)

    file_hits = scan_named_files(
        [Path(x) for x in cfg["filesystem_roots"]],
        max_results=int(cfg.get("filesystem_max_results", 500)),
    )

    v, next_action = verdict(db_candidates, file_hits)
    true_pit = [
        x for x in db_candidates + file_hits
        if x.get("status") == "PIT_CANDIDATE_REQUIRES_TEMPORAL_VALIDATION"
    ]
    current_only = [
        x for x in db_candidates + file_hits
        if x.get("status") == "CURRENT_ONLY_PIT_UNSAFE"
    ]

    out = Path(cfg["output_dir"])
    out.mkdir(parents=True, exist_ok=True)

    detail_path = out / "sector_pit_asset_semantic_candidates_v0_2.json"
    detail_path.write_text(
        json.dumps({
            "duckdb_candidates": db_candidates,
            "semantic_rejections": semantic_rejections,
            "filesystem_hits": file_hits,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    result = {
        "status": "PASS_SECTOR_PIT_ASSET_AUDIT_V0_2_RUN",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "research_id": cfg["research_id"],
        "summary": {
            "verdict": v,
            "next_action": next_action,
            "true_pit_candidate_count": len(true_pit),
            "current_only_unsafe_count": len(current_only),
            "semantic_board_rejections": len(semantic_rejections),
            "filesystem_hits_inspected": len(file_hits),
            "true_pit_candidates": true_pit[:50],
            "current_only_unsafe": current_only[:50],
            "v0_1_correction": (
                "The V0.1 six candidates were false positives because their classification field "
                "was 'board' (4 exchange/listing boards), not an industry/sector taxonomy. "
                "open_date/expire_date describe instrument lifecycle, not industry-membership validity."
            ),
        },
        "artifacts": {
            "detail_json": str(detail_path),
            "detail_json_sha256": sha256_file(detail_path),
        },
        "safety": {
            "read_only": True,
            "production_db": False,
            "broker": False,
            "qmt": False,
        },
    }

    summary_path = out / "sector_pit_asset_audit_v0_2_summary.json"
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
