from __future__ import annotations

import csv
import json
import re
import sqlite3
from pathlib import Path

import duckdb


ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "data"
RESEARCH_ROOT = DATA_ROOT / "research"
OUT_DIR = RESEARCH_ROOT / "edge_discovery" / "ret120_exposure_source_probe_v01"

KEYWORDS = (
    "industry", "sector", "sw_", "shenwan", "citic", "gics",
    "market_cap", "marketcap", "mkt_cap", "total_mv", "circ_mv",
    "float_mv", "free_float", "shares", "share_capital", "capitalization",
    "市值", "行业", "申万", "中信",
)

TEXT_EXTS = {".py", ".json", ".yaml", ".yml", ".toml", ".md", ".txt"}
DATA_EXTS = {".duckdb", ".db", ".sqlite", ".sqlite3", ".parquet", ".csv"}


def kw_hits(text: str) -> list[str]:
    s = text.lower()
    return sorted({k for k in KEYWORDS if k.lower() in s})


def inspect_sqlite(path: Path) -> list[dict]:
    rows = []
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except Exception as e:
        return [{"path": str(path), "kind": "sqlite", "error": repr(e)}]

    try:
        tables = [
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
        for table in tables:
            try:
                cols = con.execute(f'PRAGMA table_info("{table}")').fetchall()
            except Exception:
                continue
            col_names = [str(c[1]) for c in cols]
            hits = kw_hits(" ".join([table] + col_names))
            if hits:
                rows.append({
                    "path": str(path),
                    "kind": "sqlite",
                    "object": table,
                    "columns": col_names,
                    "keyword_hits": hits,
                })
    finally:
        con.close()
    return rows


def inspect_duckdb(path: Path) -> list[dict]:
    rows = []
    try:
        con = duckdb.connect(str(path), read_only=True)
    except Exception as e:
        return [{"path": str(path), "kind": "duckdb", "error": repr(e)}]

    try:
        tables = con.execute("""
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_schema NOT IN ('information_schema', 'pg_catalog')
            ORDER BY 1,2
        """).fetchall()

        for schema, table in tables:
            try:
                cols = con.execute("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = ? AND table_name = ?
                    ORDER BY ordinal_position
                """, [schema, table]).fetchall()
            except Exception:
                continue
            col_names = [str(c[0]) for c in cols]
            hits = kw_hits(" ".join([schema, table] + col_names))
            if hits:
                rows.append({
                    "path": str(path),
                    "kind": "duckdb",
                    "object": f"{schema}.{table}",
                    "columns": col_names,
                    "keyword_hits": hits,
                })
    finally:
        con.close()
    return rows


def inspect_parquet(path: Path) -> list[dict]:
    con = duckdb.connect()
    try:
        p = path.as_posix().replace("'", "''")
        schema = con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{p}')"
        ).fetchall()
        cols = [str(r[0]) for r in schema]
        hits = kw_hits(" ".join([path.name] + cols))
        if not hits:
            return []
        return [{
            "path": str(path),
            "kind": "parquet",
            "object": path.name,
            "columns": cols,
            "keyword_hits": hits,
        }]
    except Exception as e:
        return [{
            "path": str(path),
            "kind": "parquet",
            "error": repr(e),
        }]
    finally:
        con.close()


def inspect_csv_header(path: Path) -> list[dict]:
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
            reader = csv.reader(f)
            header = next(reader, [])
        hits = kw_hits(" ".join([path.name] + header))
        if not hits:
            return []
        return [{
            "path": str(path),
            "kind": "csv",
            "object": path.name,
            "columns": header,
            "keyword_hits": hits,
        }]
    except Exception as e:
        return [{"path": str(path), "kind": "csv", "error": repr(e)}]


def inspect_text(path: Path) -> list[dict]:
    try:
        if path.stat().st_size > 3_000_000:
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    hits = kw_hits(text)
    if not hits:
        return []

    snippets = []
    for i, line in enumerate(text.splitlines(), start=1):
        line_hits = kw_hits(line)
        if line_hits:
            snippets.append({
                "line": i,
                "text": line[:500],
                "keyword_hits": line_hits,
            })
            if len(snippets) >= 12:
                break

    return [{
        "path": str(path),
        "kind": "text",
        "keyword_hits": hits,
        "snippets": snippets,
    }]


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    results = []
    scanned = []

    if not DATA_ROOT.exists():
        raise FileNotFoundError(DATA_ROOT)

    # Data assets: schema/header only, no full-data scans.
    for path in DATA_ROOT.rglob("*"):
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext not in DATA_EXTS:
            continue

        scanned.append(str(path))
        if ext in {".db", ".sqlite", ".sqlite3"}:
            results.extend(inspect_sqlite(path))
        elif ext == ".duckdb":
            results.extend(inspect_duckdb(path))
        elif ext == ".parquet":
            # Avoid schema-probing every irrelevant parquet: filename first,
            # then inspect all edge/reference assets likely to matter.
            s = str(path).lower()
            if (
                kw_hits(path.name)
                or "reference" in s
                or "universe" in s
                or "edge_discovery" in s
            ):
                results.extend(inspect_parquet(path))
        elif ext == ".csv":
            results.extend(inspect_csv_header(path))

    # Source/config search for already-existing industry or size loaders.
    for base in (ROOT / "scripts", ROOT / "positionx", ROOT / "config"):
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_file() and path.suffix.lower() in TEXT_EXTS:
                results.extend(inspect_text(path))

    # Keep only successful keyword-bearing rows and errors that may matter.
    payload = {
        "probe_id": "RET120_EXPOSURE_SOURCE_PROBE_V01",
        "purpose": (
            "Locate existing PIT-capable industry and size/market-cap sources "
            "before writing ret120 exposure-neutralization code."
        ),
        "keywords": list(KEYWORDS),
        "results": results,
        "notes": {
            "read_only": True,
            "no_qmt_refresh": True,
            "no_source_data_modified": True,
            "no_new_dependency": True,
        },
    }

    (OUT_DIR / "probe.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    flat_rows = []
    for r in results:
        flat_rows.append({
            "path": r.get("path"),
            "kind": r.get("kind"),
            "object": r.get("object"),
            "keyword_hits": "|".join(r.get("keyword_hits", [])),
            "columns": "|".join(r.get("columns", [])),
            "error": r.get("error"),
        })

    with (OUT_DIR / "candidate_sources.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as f:
        fieldnames = [
            "path", "kind", "object", "keyword_hits", "columns", "error"
        ]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(flat_rows)

    print("=" * 100)
    print("RET120 EXPOSURE SOURCE PROBE V0.1")
    print("=" * 100)
    print("Candidates found:", len(results))
    print()

    if not results:
        print("NO CANDIDATE INDUSTRY/SIZE SOURCE FOUND")
        print("Do not build exposure neutralization yet.")
    else:
        for i, r in enumerate(results, start=1):
            print(f"[{i}] {r.get('kind')} | {r.get('path')}")
            if r.get("object"):
                print("    object:", r["object"])
            if r.get("keyword_hits"):
                print("    hits:", ", ".join(r["keyword_hits"]))
            if r.get("columns"):
                cols = r["columns"]
                print("    columns:", ", ".join(cols[:40]))
                if len(cols) > 40:
                    print(f"    ... +{len(cols)-40} more")
            if r.get("snippets"):
                for s in r["snippets"][:5]:
                    print(f"    L{s['line']}: {s['text']}")
            if r.get("error"):
                print("    error:", r["error"])
            print()

    print("Output:", OUT_DIR)
    print("Read-only probe complete.")
    print("No source data was modified.")
    print("No QMT history refresh was performed.")


if __name__ == "__main__":
    main()
