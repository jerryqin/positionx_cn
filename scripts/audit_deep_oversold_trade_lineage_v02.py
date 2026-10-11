from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
RESEARCH_DIR = ROOT / "data" / "research"
OUT_DIR = RESEARCH_DIR / "edge_discovery" / "deep_oversold_trade_lineage_v02"

REGIME = RESEARCH_DIR / "deep_oversold_csi500_regime_events_v1.parquet"
TARGET_COUNT = 1365
KEY = ["symbol", "signal_date", "entry_date", "exit_date"]

ALIASES = {
    "symbol": ["symbol", "stock", "stock_code", "code", "security", "ticker"],
    "signal_date": ["signal_date", "signal_day", "signal_dt", "date"],
    "entry_date": ["entry_date", "entry_day", "entry_dt", "buy_date", "open_date"],
    "exit_date": ["exit_date", "exit_day", "exit_dt", "sell_date", "close_date"],
}

KNOWN_FLAGS = [
    "complete_outcome",
    "standard_fillable",
    "can_buy_at_open",
    "is_suspended",
    "new_listing_special",
    "st_known",
    "status_found",
]


def relation(path: Path) -> str:
    q = path.as_posix().replace("'", "''")
    if path.suffix.lower() == ".parquet":
        return f"read_parquet('{q}')"
    if path.suffix.lower() == ".csv":
        return f"read_csv_auto('{q}', header=true, sample_size=-1)"
    raise RuntimeError(path)


def columns_for(con: duckdb.DuckDBPyConnection, path: Path) -> list[str]:
    return [
        str(r[0])
        for r in con.execute(
            f"DESCRIBE SELECT * FROM {relation(path)}"
        ).fetchall()
    ]


def resolve_alias(columns: list[str], logical: str) -> str | None:
    cmap = {c.lower(): c for c in columns}
    for c in ALIASES[logical]:
        if c.lower() in cmap:
            return cmap[c.lower()]
    return None


def core_mapping(columns: list[str]) -> dict[str, str | None]:
    return {
        logical: resolve_alias(columns, logical)
        for logical in KEY
    }


def distinct_key_count(
    con: duckdb.DuckDBPyConnection,
    path: Path,
    mapping: dict[str, str | None],
) -> int | None:
    if any(mapping[k] is None for k in KEY):
        return None

    rel = relation(path)
    select = ", ".join(
        f'"{mapping[k]}" AS {k}' for k in KEY
    )
    return int(
        con.execute(
            f"""
            SELECT COUNT(*)
            FROM (
                SELECT DISTINCT {", ".join(KEY)}
                FROM (SELECT {select} FROM {rel})
            )
            """
        ).fetchone()[0]
    )


def key_set(
    con: duckdb.DuckDBPyConnection,
    path: Path,
    mapping: dict[str, str | None],
    where_sql: str | None = None,
) -> set[tuple]:
    if any(mapping[k] is None for k in KEY):
        return set()

    rel = relation(path)
    select = ", ".join(
        f'CAST("{mapping[k]}" AS VARCHAR) AS {k}' for k in KEY
    )
    where = f"WHERE {where_sql}" if where_sql else ""
    df = con.execute(
        f"""
        SELECT DISTINCT {select}
        FROM {rel}
        {where}
        """
    ).fetchdf()
    return set(map(tuple, df[KEY].to_numpy()))


def scan_all_csi500_trade_sources(
    con: duckdb.DuckDBPyConnection,
) -> list[dict[str, Any]]:
    rows = []

    for path in RESEARCH_DIR.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".csv", ".parquet"}:
            continue

        lname = path.as_posix().lower()
        if "deep" not in lname or "oversold" not in lname:
            continue

        try:
            cols = columns_for(con, path)
        except Exception:
            continue

        mapping = core_mapping(cols)
        if any(mapping[k] is None for k in KEY):
            continue

        rel = relation(path)
        row_count = int(
            con.execute(f"SELECT COUNT(*) FROM {rel}").fetchone()[0]
        )
        dk = distinct_key_count(con, path, mapping)

        symbol_col = mapping["symbol"]
        sym_count = int(
            con.execute(
                f'SELECT COUNT(DISTINCT "{symbol_col}") FROM {rel}'
            ).fetchone()[0]
        )

        rows.append({
            "path": str(path),
            "filename": path.name,
            "row_count": row_count,
            "distinct_trade_keys": dk,
            "symbol_count": sym_count,
            "exact_target_count": dk == TARGET_COUNT,
            "columns": cols,
            "mapping": mapping,
        })

    rows.sort(
        key=lambda x: (
            not x["exact_target_count"],
            abs((x["distinct_trade_keys"] or 10**9) - TARGET_COUNT),
            x["filename"],
        )
    )
    return rows


def value_counts(
    con: duckdb.DuckDBPyConnection,
    rel: str,
    col: str,
) -> list[dict[str, Any]]:
    qcol = '"' + col.replace('"', '""') + '"'
    rows = con.execute(
        f"""
        SELECT
            CAST({qcol} AS VARCHAR) AS value,
            COUNT(*) AS n
        FROM {rel}
        GROUP BY 1
        ORDER BY n DESC, value
        """
    ).fetchall()
    return [
        {"value": v, "n": int(n)}
        for v, n in rows
    ]


def bool_like_values(
    con: duckdb.DuckDBPyConnection,
    rel: str,
    col: str,
) -> set[str]:
    vals = {
        str(x["value"]).strip().lower()
        for x in value_counts(con, rel, col)
        if x["value"] is not None
    }
    allowed = {
        "true", "false", "1", "0",
        "yes", "no", "y", "n",
        "t", "f",
    }
    return vals if vals and vals <= allowed else set()


def true_sql(col: str) -> str:
    qcol = '"' + col.replace('"', '""') + '"'
    return (
        f"LOWER(TRIM(CAST({qcol} AS VARCHAR))) "
        f"IN ('true','1','yes','y','t')"
    )


def false_sql(col: str) -> str:
    qcol = '"' + col.replace('"', '""') + '"'
    return (
        f"LOWER(TRIM(CAST({qcol} AS VARCHAR))) "
        f"IN ('false','0','no','n','f')"
    )


def count_where(
    con: duckdb.DuckDBPyConnection,
    rel: str,
    where_sql: str,
) -> int:
    return int(
        con.execute(
            f"SELECT COUNT(*) FROM {rel} WHERE {where_sql}"
        ).fetchone()[0]
    )


def regime_flag_audit(
    con: duckdb.DuckDBPyConnection,
) -> dict[str, Any]:
    if not REGIME.exists():
        raise FileNotFoundError(REGIME)

    rel = relation(REGIME)
    cols = columns_for(con, REGIME)

    present_known = [c for c in KNOWN_FLAGS if c in cols]

    flag_counts = []
    bool_cols = []

    for col in present_known:
        counts = value_counts(con, rel, col)
        vals = bool_like_values(con, rel, col)
        flag_counts.append({
            "column": col,
            "counts": counts,
            "boolean_like": bool(vals),
        })
        if vals:
            bool_cols.append(col)

    # Also include any other true boolean-typed / boolean-like low-card columns.
    for col in cols:
        if col in bool_cols or col in KEY:
            continue
        try:
            vals = bool_like_values(con, rel, col)
        except Exception:
            continue
        if vals:
            bool_cols.append(col)
            if col not in [x["column"] for x in flag_counts]:
                flag_counts.append({
                    "column": col,
                    "counts": value_counts(con, rel, col),
                    "boolean_like": True,
                })

    single_predicates = []
    for col in bool_cols:
        for label, sql in [
            (f"{col}=TRUE", true_sql(col)),
            (f"{col}=FALSE", false_sql(col)),
        ]:
            n = count_where(con, rel, sql)
            single_predicates.append({
                "predicate": label,
                "where_sql": sql,
                "n": n,
                "exact_target": n == TARGET_COUNT,
                "distance": abs(n - TARGET_COUNT),
            })

    # Conjunctions of semantically plausible "good trade" values.
    preferred = []
    for col in bool_cols:
        if col in {"is_suspended", "new_listing_special"}:
            preferred.append((f"{col}=FALSE", false_sql(col)))
        else:
            preferred.append((f"{col}=TRUE", true_sql(col)))

    combo_rows = []
    # Search small conjunctions only: lineage reconstruction, not arbitrary fitting.
    max_size = min(4, len(preferred))
    for r in range(2, max_size + 1):
        for combo in itertools.combinations(preferred, r):
            label = " AND ".join(x[0] for x in combo)
            where_sql = " AND ".join(x[1] for x in combo)
            n = count_where(con, rel, where_sql)
            combo_rows.append({
                "predicate": label,
                "where_sql": where_sql,
                "n": n,
                "exact_target": n == TARGET_COUNT,
                "distance": abs(n - TARGET_COUNT),
            })

    single_predicates.sort(
        key=lambda x: (not x["exact_target"], x["distance"], x["predicate"])
    )
    combo_rows.sort(
        key=lambda x: (
            not x["exact_target"],
            x["distance"],
            x["predicate"],
        )
    )

    return {
        "columns": cols,
        "flag_counts": flag_counts,
        "single_predicates": single_predicates,
        "combo_predicates": combo_rows,
    }


def compare_exact_target_files_to_regime(
    con: duckdb.DuckDBPyConnection,
    scanned: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    regime_cols = columns_for(con, REGIME)
    regime_map = core_mapping(regime_cols)
    regime_keys = key_set(con, REGIME, regime_map)

    out = []
    for item in scanned:
        if not item["exact_target_count"]:
            continue

        p = Path(item["path"])
        ks = key_set(con, p, item["mapping"])

        out.append({
            "path": item["path"],
            "target_keys": len(ks),
            "intersection_with_regime": len(ks & regime_keys),
            "target_only": len(ks - regime_keys),
            "regime_only": len(regime_keys - ks),
            "is_subset_of_regime": ks <= regime_keys,
        })
    return out


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    try:
        scanned = scan_all_csi500_trade_sources(con)
        flags = regime_flag_audit(con)
        exact_files = compare_exact_target_files_to_regime(
            con,
            scanned,
        )
    finally:
        con.close()

    report = {
        "audit_id": "DEEP_OVERSOLD_TRADE_LINEAGE_V02",
        "target_frozen_trade_count_reference": TARGET_COUNT,
        "all_deep_oversold_trade_sources": scanned,
        "regime_flag_audit": flags,
        "exact_1365_files_vs_regime": exact_files,
        "automatic_canonical_selection": "NONE",
        "notes": [
            "1365 is used only as a frozen lineage reference count.",
            "Exact-count predicates are diagnostics, not strategy optimization.",
            "A predicate should only be accepted if it matches the documented frozen execution semantics.",
        ],
    }

    (OUT_DIR / "audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    pd.DataFrame([
        {
            k: v
            for k, v in item.items()
            if k not in {"columns", "mapping"}
        }
        for item in scanned
    ]).to_csv(
        OUT_DIR / "all_trade_sources.csv",
        index=False,
        encoding="utf-8-sig",
    )

    pd.DataFrame(
        flags["single_predicates"]
    ).to_csv(
        OUT_DIR / "regime_single_predicates.csv",
        index=False,
        encoding="utf-8-sig",
    )

    pd.DataFrame(
        flags["combo_predicates"]
    ).to_csv(
        OUT_DIR / "regime_combo_predicates.csv",
        index=False,
        encoding="utf-8-sig",
    )

    print("=" * 110)
    print("DEEP OVERSOLD CSI500 TRADE LINEAGE AUDIT V0.2")
    print("=" * 110)
    print("Frozen trade-count reference:", TARGET_COUNT)
    print()

    print("ALL DEEP-OVERSOLD TRADE-LIKE FILES")
    print("-" * 110)
    for item in scanned:
        marker = "  <== EXACT 1365" if item["exact_target_count"] else ""
        print(
            f"{item['filename']}: rows={item['row_count']} "
            f"keys={item['distinct_trade_keys']} "
            f"symbols={item['symbol_count']}{marker}"
        )
    print()

    print("CSI500 REGIME EXECUTION / VALIDITY FLAGS")
    print("-" * 110)
    for item in flags["flag_counts"]:
        vals = ", ".join(
            f"{v['value']}={v['n']}"
            for v in item["counts"]
        )
        print(f"{item['column']}: {vals}")
    print()

    print("SINGLE PREDICATES CLOSEST TO 1365")
    print("-" * 110)
    for item in flags["single_predicates"][:20]:
        marker = "  <== EXACT" if item["exact_target"] else ""
        print(
            f"{item['predicate']}: n={item['n']} "
            f"diff={item['n'] - TARGET_COUNT:+d}{marker}"
        )
    print()

    print("COMBINATION PREDICATES CLOSEST TO 1365")
    print("-" * 110)
    for item in flags["combo_predicates"][:30]:
        marker = "  <== EXACT" if item["exact_target"] else ""
        print(
            f"{item['predicate']}: n={item['n']} "
            f"diff={item['n'] - TARGET_COUNT:+d}{marker}"
        )
    print()

    if exact_files:
        print("FILES WITH EXACTLY 1365 DISTINCT TRADE KEYS")
        print("-" * 110)
        for item in exact_files:
            print(
                f"{item['path']} | "
                f"intersection={item['intersection_with_regime']} "
                f"target-only={item['target_only']} "
                f"regime-only={item['regime_only']} "
                f"subsetOfRegime={item['is_subset_of_regime']}"
            )
        print()

    print("Output:", OUT_DIR)
    print("No canonical subset was selected automatically.")
    print("No source data or production database was modified.")


if __name__ == "__main__":
    main()
