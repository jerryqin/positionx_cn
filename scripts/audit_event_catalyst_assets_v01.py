from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CFG = PROJECT_ROOT / "config/opportunity/event_catalyst_asset_audit_v0_1.yaml"

# Strong PIT publication/observation timestamps. These are admissible because
# they can represent when the evidence became knowable.
PIT_DATE_KEYS = (
    "ann_date", "announcement_date", "announce_date",
    "publish_date", "published_at", "publication_date",
    "filing_date", "disclosure_date",
    "observed_at", "as_of", "asof_date", "as_of_date",
    "update_time", "updated_at", "created_at",
    "event_date", "event_time",
)

# Dates that describe an accounting / business period but are NOT by
# themselves sufficient for point-in-time research.
PERIOD_DATE_KEYS = (
    "report_date", "report_period", "period_end", "fiscal_period",
    "end_date", "trade_date",
)

SYMBOL_KEYS = (
    "symbol", "stock_code", "security_code", "ticker",
    "ts_code", "wind_code", "unified_code", "code",
)

TEXT_KEYS = (
    "title", "headline", "subject", "summary",
    "content", "text", "body", "announcement", "notice",
)

FUNDAMENTAL_KEYS = (
    "revenue", "sales", "operating_income", "net_profit", "profit",
    "eps", "roe", "gross_margin", "operating_margin",
    "yoy", "qoq", "growth", "cash_flow",
)

EXPECTATION_KEYS = (
    "estimate", "consensus", "forecast", "guidance",
    "target_price", "rating", "expectation", "surprise",
    "预期", "一致预期", "预测", "指引",
)

EVENT_NAME_KEYS = (
    "announcement", "notice", "disclosure", "filing", "news",
    "event", "contract", "order", "tender", "bid", "customer",
    "supplier", "capacity", "project", "agreement", "framework",
    "业绩", "预告", "公告", "合同", "订单", "中标", "客户", "供应商", "扩产", "项目",
)

ORDER_TEXT_TERMS = (
    "合同", "订单", "中标", "框架协议", "供应商", "客户",
    "contract", "order", "tender", "bid", "supplier", "customer",
)

EARNINGS_TEXT_TERMS = (
    "业绩预告", "业绩快报", "年报", "半年报", "季报", "盈利预测",
    "earnings", "guidance", "results", "profit forecast",
)


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9_\u4e00-\u9fff]+", "_", str(s).strip().lower())


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))


def first_matching(cols, keys):
    normed = [(c, norm(c)) for c in cols]
    for c, nc in normed:
        if nc in keys:
            return c
    for c, nc in normed:
        for k in keys:
            if nc.endswith("_" + k) or k in nc:
                return c
    return None


def any_matching(cols, keys):
    out = []
    for c in cols:
        nc = norm(c)
        if any(k in nc for k in keys):
            out.append(c)
    return out


def classify_schema(name: str, cols: list[str]) -> dict:
    symbol_col = first_matching(cols, SYMBOL_KEYS)
    pit_date_col = first_matching(cols, PIT_DATE_KEYS)
    period_date_col = first_matching(cols, PERIOD_DATE_KEYS)
    text_cols = any_matching(cols, TEXT_KEYS)
    fundamental_cols = any_matching(cols, FUNDAMENTAL_KEYS)
    expectation_cols = any_matching(cols, EXPECTATION_KEYS)

    name_l = norm(name)
    event_name_hit = any(k in name_l for k in EVENT_NAME_KEYS)

    families = []
    if text_cols or event_name_hit:
        families.append("EVENT_TEXT")
    if fundamental_cols:
        families.append("FUNDAMENTAL")
    if expectation_cols:
        families.append("EXPECTATION")

    if symbol_col and pit_date_col and families:
        status = "PIT_EVIDENCE_CANDIDATE"
    elif symbol_col and period_date_col and families and not pit_date_col:
        status = "PERIOD_ONLY_PIT_UNSAFE"
    elif symbol_col and families and not pit_date_col and not period_date_col:
        status = "CURRENT_OR_TIMELESS_UNSAFE"
    elif families:
        status = "INCOMPLETE_EVIDENCE_ASSET"
    else:
        status = "NOT_RELEVANT"

    return {
        "status": status,
        "symbol_col": symbol_col,
        "pit_date_col": pit_date_col,
        "period_date_col": period_date_col,
        "text_cols": text_cols,
        "fundamental_cols": fundamental_cols,
        "expectation_cols": expectation_cols,
        "families": families,
    }


def qident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def table_stats(con, schema: str, table: str, detail: dict) -> dict:
    fq = f"{qident(schema)}.{qident(table)}"
    out = {}
    try:
        out["row_count"] = int(con.execute(f"select count(*) from {fq}").fetchone()[0])
    except Exception as e:
        out["row_count_error"] = f"{type(e).__name__}: {e}"
        return out

    for label, col in (
        ("pit_date", detail.get("pit_date_col")),
        ("period_date", detail.get("period_date_col")),
    ):
        if col:
            try:
                mn, mx = con.execute(
                    f"select min({qident(col)}), max({qident(col)}) from {fq}"
                ).fetchone()
                out[f"{label}_min"] = None if mn is None else str(mn)
                out[f"{label}_max"] = None if mx is None else str(mx)
            except Exception as e:
                out[f"{label}_range_error"] = f"{type(e).__name__}: {e}"

    scol = detail.get("symbol_col")
    if scol:
        try:
            out["distinct_symbols"] = int(
                con.execute(f"select count(distinct {qident(scol)}) from {fq}").fetchone()[0]
            )
        except Exception as e:
            out["distinct_symbols_error"] = f"{type(e).__name__}: {e}"

    return out


def inspect_catalog(catalog: Path) -> list[dict]:
    con = duckdb.connect(str(catalog), read_only=True)
    try:
        tabs = con.execute("""
            select table_schema, table_name, table_type
            from information_schema.tables
            where table_schema not in ('information_schema','pg_catalog')
            order by table_schema, table_name
        """).fetchdf()

        out = []
        for r in tabs.itertuples(index=False):
            schema, table, ttype = str(r.table_schema), str(r.table_name), str(r.table_type)
            cols_df = con.execute("""
                select column_name, data_type
                from information_schema.columns
                where table_schema=? and table_name=?
                order by ordinal_position
            """, [schema, table]).fetchdf()
            cols = [str(x) for x in cols_df["column_name"].tolist()]
            detail = classify_schema(f"{schema}.{table}", cols)
            if detail["status"] == "NOT_RELEVANT":
                continue
            rec = {
                "source_type": "DUCKDB",
                "catalog": str(catalog),
                "schema": schema,
                "table": table,
                "table_type": ttype,
                "columns": cols,
                **detail,
            }
            if detail["status"] in (
                "PIT_EVIDENCE_CANDIDATE",
                "PERIOD_ONLY_PIT_UNSAFE",
                "CURRENT_OR_TIMELESS_UNSAFE",
            ):
                rec["stats"] = table_stats(con, schema, table, detail)
            out.append(rec)
        return out
    finally:
        con.close()


def inspect_tabular_file(path: Path) -> dict:
    suf = path.suffix.lower()
    try:
        if suf == ".parquet":
            df = pd.read_parquet(path)
        elif suf == ".csv":
            df = pd.read_csv(path, nrows=50000, low_memory=False)
        elif suf in (".json", ".jsonl"):
            try:
                df = pd.read_json(path, lines=(suf == ".jsonl"))
                if len(df) > 50000:
                    df = df.head(50000)
            except Exception:
                return {
                    "source_type": "FILE",
                    "path": str(path),
                    "status": "INSPECTION_ERROR",
                    "error": "json could not be parsed as a flat table",
                }
        else:
            return {
                "source_type": "FILE",
                "path": str(path),
                "status": "NAME_HIT_UNINSPECTED_FORMAT",
            }

        cols = [str(c) for c in df.columns]
        detail = classify_schema(path.name, cols)
        rec = {
            "source_type": "FILE",
            "path": str(path),
            "suffix": suf,
            "sample_rows": int(len(df)),
            "file_size_bytes": int(path.stat().st_size),
            "columns": cols,
            **detail,
        }

        for label, col in (
            ("pit_date", detail.get("pit_date_col")),
            ("period_date", detail.get("period_date_col")),
        ):
            if col and col in df.columns:
                s = df[col].dropna()
                if len(s):
                    rec[f"sample_{label}_min"] = str(s.min())
                    rec[f"sample_{label}_max"] = str(s.max())

        if detail.get("symbol_col") and detail["symbol_col"] in df.columns:
            rec["sample_distinct_symbols"] = int(df[detail["symbol_col"]].nunique(dropna=True))

        # Lightweight text-term diagnostics only; no outcome conditioning.
        texts = []
        for c in detail.get("text_cols", []):
            if c in df.columns:
                texts.append(df[c].astype("string").fillna(""))
        if texts:
            combined = texts[0]
            for s in texts[1:]:
                combined = combined + " " + s
            lower = combined.str.lower()
            rec["sample_order_contract_hits"] = int(
                lower.apply(lambda x: any(term.lower() in x for term in ORDER_TEXT_TERMS)).sum()
            )
            rec["sample_earnings_hits"] = int(
                lower.apply(lambda x: any(term.lower() in x for term in EARNINGS_TEXT_TERMS)).sum()
            )

        return rec
    except Exception as e:
        return {
            "source_type": "FILE",
            "path": str(path),
            "status": "INSPECTION_ERROR",
            "error": f"{type(e).__name__}: {e}",
        }


def scan_files(roots: list[Path], max_results: int) -> list[dict]:
    allowed = {".parquet", ".csv", ".json", ".jsonl", ".duckdb", ".db", ".xlsx", ".xls"}
    out = []
    for root in roots:
        if not root.exists():
            continue
        try:
            for p in root.rglob("*"):
                if len(out) >= max_results:
                    break
                if not p.is_file() or p.suffix.lower() not in allowed:
                    continue

                name_l = norm(p.name)
                if not any(k in name_l for k in EVENT_NAME_KEYS + EXPECTATION_KEYS + FUNDAMENTAL_KEYS):
                    continue

                if p.suffix.lower() in {".parquet", ".csv", ".json", ".jsonl"}:
                    out.append(inspect_tabular_file(p))
                else:
                    out.append({
                        "source_type": "FILE",
                        "path": str(p),
                        "suffix": p.suffix.lower(),
                        "status": "NAME_HIT_UNINSPECTED_FORMAT",
                    })
        except Exception as e:
            out.append({
                "source_type": "SCAN_ERROR",
                "path": str(root),
                "status": "FILESYSTEM_SCAN_ERROR",
                "error": f"{type(e).__name__}: {e}",
            })
    return out


def summarize(db_assets: list[dict], file_assets: list[dict]) -> dict:
    all_assets = db_assets + file_assets
    pit = [x for x in all_assets if x.get("status") == "PIT_EVIDENCE_CANDIDATE"]
    period_only = [x for x in all_assets if x.get("status") == "PERIOD_ONLY_PIT_UNSAFE"]
    timeless = [x for x in all_assets if x.get("status") == "CURRENT_OR_TIMELESS_UNSAFE"]

    family_counts = {}
    for x in pit:
        for fam in x.get("families", []):
            family_counts[fam] = family_counts.get(fam, 0) + 1

    if pit:
        verdict = "PIT_EVENT_CATALYST_CANDIDATES_FOUND_REQUIRES_VALIDATION"
        next_action = (
            "Validate source lineage, publication-time semantics, historical coverage, "
            "and survivorship before choosing the first non-price Opportunity mechanism."
        )
    elif period_only or timeless:
        verdict = "ONLY_PIT_UNSAFE_OR_PERIOD_ONLY_EVIDENCE_FOUND"
        next_action = (
            "Do not use report-period or current-snapshot data as historical evidence. "
            "Acquire publication/observation timestamps or a proper PIT feed."
        )
    else:
        verdict = "NO_LOCAL_EVENT_CATALYST_EVIDENCE_FOUND"
        next_action = (
            "Acquire a versioned PIT event/evidence asset before non-price Opportunity research."
        )

    return {
        "verdict": verdict,
        "next_action": next_action,
        "pit_candidate_count": len(pit),
        "period_only_unsafe_count": len(period_only),
        "timeless_unsafe_count": len(timeless),
        "pit_family_counts": family_counts,
        "pit_candidates": pit[:100],
        "period_only_unsafe": period_only[:50],
        "timeless_unsafe": timeless[:50],
    }


def cmd_check(cfg):
    catalogs = [Path(x) for x in cfg["catalogs"]]
    result = {
        "status": (
            "PASS_EVENT_CATALYST_ASSET_AUDIT_V0_1_CHECK"
            if any(p.exists() for p in catalogs)
            else "BLOCKED_EVENT_CATALYST_ASSET_AUDIT_V0_1_CHECK"
        ),
        "research_id": cfg["research_id"],
        "catalogs": [{"path": str(p), "exists": p.exists()} for p in catalogs],
        "filesystem_roots": cfg["filesystem_roots"],
        "pit_rule": (
            "symbol + knowable publication/observation timestamp + evidence payload; "
            "report period alone is PIT-unsafe"
        ),
        "read_only": True,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if any(p.exists() for p in catalogs) else 3


def cmd_run(cfg):
    db_assets = []
    for s in cfg["catalogs"]:
        p = Path(s)
        if p.exists():
            db_assets.extend(inspect_catalog(p))

    file_assets = scan_files(
        [Path(x) for x in cfg["filesystem_roots"]],
        int(cfg.get("filesystem_max_results", 500)),
    )

    summary = summarize(db_assets, file_assets)

    out = Path(cfg["output_dir"])
    out.mkdir(parents=True, exist_ok=True)

    detail_path = out / "event_catalyst_asset_candidates_v0_1.json"
    detail_path.write_text(
        json.dumps(
            {"duckdb_assets": db_assets, "filesystem_assets": file_assets},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    result = {
        "status": "PASS_EVENT_CATALYST_ASSET_AUDIT_V0_1_RUN",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "research_id": cfg["research_id"],
        "summary": summary,
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

    summary_path = out / "event_catalyst_asset_audit_v0_1_summary.json"
    summary_path.write_text(
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
