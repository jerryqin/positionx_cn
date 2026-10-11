from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
RESEARCH_DIR = ROOT / "data" / "research"
OUT_DIR = RESEARCH_DIR / "edge_discovery" / "deep_oversold_trade_lineage_v01"

CANDIDATES = {
    "generic_replication": RESEARCH_DIR / "deep_oversold_replication_events_latest.csv",
    "csi500_regime": RESEARCH_DIR / "deep_oversold_csi500_regime_events_v1.parquet",
    "csi500_capacity": RESEARCH_DIR / "deep_oversold_csi500_capacity_events_v1.parquet",
}

KEY = ["symbol", "signal_date", "entry_date", "exit_date"]

ALIASES = {
    "symbol": ["symbol", "stock", "stock_code", "code", "security", "ticker"],
    "signal_date": ["signal_date", "signal_day", "signal_dt", "date"],
    "entry_date": ["entry_date", "entry_day", "entry_dt", "buy_date", "open_date"],
    "exit_date": ["exit_date", "exit_day", "exit_dt", "sell_date", "close_date"],
}


def relation(path: Path) -> str:
    q = path.as_posix().replace("'", "''")
    if path.suffix.lower() == ".parquet":
        return f"read_parquet('{q}')"
    if path.suffix.lower() == ".csv":
        return f"read_csv_auto('{q}', header=true, sample_size=-1)"
    raise RuntimeError(path)


def resolve_alias(columns: list[str], logical: str) -> str | None:
    cmap = {c.lower(): c for c in columns}
    for c in ALIASES[logical]:
        if c.lower() in cmap:
            return cmap[c.lower()]
    return None


def low_cardinality_profile(
    con: duckdb.DuckDBPyConnection,
    rel: str,
    columns: list[str],
) -> list[dict[str, Any]]:
    out = []
    ignore = set(KEY)

    for col in columns:
        if col.lower() in ignore:
            continue

        qcol = '"' + col.replace('"', '""') + '"'

        try:
            distinct = con.execute(
                f"SELECT COUNT(DISTINCT {qcol}) FROM {rel}"
            ).fetchone()[0]
        except Exception:
            continue

        if distinct is None or distinct > 25:
            continue

        try:
            vals = con.execute(
                f"""
                SELECT CAST({qcol} AS VARCHAR) AS value, COUNT(*) AS n
                FROM {rel}
                GROUP BY 1
                ORDER BY n DESC, value
                LIMIT 30
                """
            ).fetchall()
        except Exception:
            continue

        out.append({
            "column": col,
            "distinct_count": int(distinct),
            "values": [
                {"value": v, "n": int(n)}
                for v, n in vals
            ],
        })

    return out


def profile_source(
    con: duckdb.DuckDBPyConnection,
    name: str,
    path: Path,
) -> dict[str, Any]:
    if not path.exists():
        return {
            "name": name,
            "path": str(path),
            "exists": False,
        }

    rel = relation(path)
    desc = con.execute(f"DESCRIBE SELECT * FROM {rel}").fetchall()
    columns = [str(r[0]) for r in desc]

    mapping = {
        logical: resolve_alias(columns, logical)
        for logical in KEY
    }

    missing = [k for k in KEY if mapping[k] is None]
    if missing:
        return {
            "name": name,
            "path": str(path),
            "exists": True,
            "columns": columns,
            "missing_key_fields": missing,
        }

    sel = ", ".join(
        f'"{mapping[k]}" AS {k}' for k in KEY
    )
    base = f"(SELECT {sel} FROM {rel})"

    row_count = int(
        con.execute(f"SELECT COUNT(*) FROM {rel}").fetchone()[0]
    )
    distinct_keys = int(
        con.execute(
            f"""
            SELECT COUNT(*)
            FROM (
                SELECT DISTINCT {", ".join(KEY)}
                FROM {base}
            )
            """
        ).fetchone()[0]
    )
    duplicate_rows = row_count - distinct_keys

    symbol_count = int(
        con.execute(
            f"SELECT COUNT(DISTINCT symbol) FROM {base}"
        ).fetchone()[0]
    )

    date_min, date_max = con.execute(
        f"""
        SELECT
            MIN(CAST(signal_date AS VARCHAR)),
            MAX(CAST(signal_date AS VARCHAR))
        FROM {base}
        """
    ).fetchone()

    dup_groups = con.execute(
        f"""
        SELECT
            symbol, signal_date, entry_date, exit_date,
            COUNT(*) AS n
        FROM {base}
        GROUP BY 1,2,3,4
        HAVING COUNT(*) > 1
        ORDER BY n DESC, symbol, signal_date
        LIMIT 30
        """
    ).fetchdf()

    low_card = low_cardinality_profile(
        con,
        rel,
        columns,
    )

    return {
        "name": name,
        "path": str(path),
        "exists": True,
        "row_count": row_count,
        "distinct_trade_keys": distinct_keys,
        "duplicate_rows_by_trade_key": duplicate_rows,
        "symbol_count": symbol_count,
        "signal_date_min": str(date_min),
        "signal_date_max": str(date_max),
        "columns": columns,
        "mapping": mapping,
        "duplicate_key_sample": dup_groups.to_dict(orient="records"),
        "low_cardinality_columns": low_card,
    }


def load_key_set(
    con: duckdb.DuckDBPyConnection,
    path: Path,
) -> set[tuple]:
    rel = relation(path)
    desc = con.execute(f"DESCRIBE SELECT * FROM {rel}").fetchall()
    columns = [str(r[0]) for r in desc]
    mapping = {
        logical: resolve_alias(columns, logical)
        for logical in KEY
    }
    if any(mapping[k] is None for k in KEY):
        raise RuntimeError(f"Cannot map core keys for {path}")

    sel = ", ".join(
        f'CAST("{mapping[k]}" AS VARCHAR) AS {k}' for k in KEY
    )
    df = con.execute(
        f"SELECT DISTINCT {sel} FROM {rel}"
    ).fetchdf()
    return set(map(tuple, df[KEY].to_numpy()))


def pairwise_sets(
    sets: dict[str, set[tuple]],
) -> list[dict[str, Any]]:
    names = list(sets)
    rows = []

    for i, a in enumerate(names):
        for b in names[i + 1:]:
            sa = sets[a]
            sb = sets[b]
            rows.append({
                "a": a,
                "b": b,
                "a_n": len(sa),
                "b_n": len(sb),
                "intersection": len(sa & sb),
                "a_only": len(sa - sb),
                "b_only": len(sb - sa),
                "a_subset_b": sa <= sb,
                "b_subset_a": sb <= sa,
                "jaccard": (
                    len(sa & sb) / len(sa | sb)
                    if (sa | sb)
                    else 1.0
                ),
            })

    return rows


def candidate_filters(
    con: duckdb.DuckDBPyConnection,
    name: str,
    path: Path,
    profile: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Enumerate counts for low-cardinality columns that look like status /
    inclusion / execution / acceptance fields.

    This does NOT select a canonical subset automatically. It only surfaces
    plausible lineage filters for human review.
    """
    if not profile.get("exists"):
        return []

    rel = relation(path)
    interesting_words = (
        "status", "state", "trade", "valid", "accepted", "executed",
        "selected", "include", "eligible", "pass", "filled", "active",
        "duplicate", "first", "signal", "reason",
    )

    rows = []

    for item in profile.get("low_cardinality_columns", []):
        col = item["column"]
        lname = col.lower()
        if not any(w in lname for w in interesting_words):
            continue

        qcol = '"' + col.replace('"', '""') + '"'
        try:
            vals = con.execute(
                f"""
                SELECT CAST({qcol} AS VARCHAR) AS value, COUNT(*) AS n
                FROM {rel}
                GROUP BY 1
                ORDER BY n DESC, value
                """
            ).fetchall()
        except Exception:
            continue

        rows.append({
            "source": name,
            "column": col,
            "values": [
                {"value": v, "n": int(n)}
                for v, n in vals
            ],
        })

    return rows


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    try:
        profiles = {
            name: profile_source(con, name, path)
            for name, path in CANDIDATES.items()
        }

        sets = {}
        for name, path in CANDIDATES.items():
            if profiles[name].get("exists") and not profiles[name].get("missing_key_fields"):
                sets[name] = load_key_set(con, path)

        pairs = pairwise_sets(sets)

        filters = []
        for name, path in CANDIDATES.items():
            filters.extend(
                candidate_filters(
                    con,
                    name,
                    path,
                    profiles[name],
                )
            )
    finally:
        con.close()

    report = {
        "audit_id": "DEEP_OVERSOLD_TRADE_LINEAGE_V01",
        "known_reference": {
            "previous_frozen_csi500_trade_count": 1365,
            "purpose": (
                "Identify the exact trade-grain lineage feeding the frozen "
                "Deep Oversold CSI500 economics before ret120 integration."
            ),
        },
        "profiles": profiles,
        "pairwise_trade_key_sets": pairs,
        "candidate_status_filters": filters,
        "automatic_selection": "NONE",
    }

    (OUT_DIR / "audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    pair_df = pd.DataFrame(pairs)
    pair_df.to_csv(
        OUT_DIR / "pairwise_trade_key_sets.csv",
        index=False,
        encoding="utf-8-sig",
    )

    filter_rows = []
    for item in filters:
        for v in item["values"]:
            filter_rows.append({
                "source": item["source"],
                "column": item["column"],
                "value": v["value"],
                "n": v["n"],
            })

    pd.DataFrame(filter_rows).to_csv(
        OUT_DIR / "candidate_status_filters.csv",
        index=False,
        encoding="utf-8-sig",
    )

    print("=" * 110)
    print("DEEP OVERSOLD CSI500 TRADE LINEAGE AUDIT V0.1")
    print("=" * 110)
    print("Known frozen CSI500 trade count reference: 1365")
    print()

    for name in CANDIDATES:
        p = profiles[name]
        print(name.upper())
        print("-" * 110)
        if not p.get("exists"):
            print("MISSING:", p["path"])
            print()
            continue

        print("Path:", p["path"])
        print("Rows:", p.get("row_count"))
        print("Distinct trade keys:", p.get("distinct_trade_keys"))
        print("Duplicate rows by trade key:", p.get("duplicate_rows_by_trade_key"))
        print("Symbols:", p.get("symbol_count"))
        print(
            "Signal range:",
            p.get("signal_date_min"),
            "->",
            p.get("signal_date_max"),
        )
        print("Columns:")
        print("  " + ", ".join(p.get("columns", [])))

        interesting = [
            x for x in filters if x["source"] == name
        ]
        if interesting:
            print("Candidate low-cardinality status fields:")
            for item in interesting:
                vals = ", ".join(
                    f"{v['value']}={v['n']}"
                    for v in item["values"]
                )
                print(f"  {item['column']}: {vals}")
        print()

    print("PAIRWISE CORE-KEY RELATIONSHIPS")
    print("-" * 110)
    for r in pairs:
        print(
            f"{r['a']} vs {r['b']}: "
            f"A={r['a_n']} B={r['b_n']} "
            f"intersection={r['intersection']} "
            f"A-only={r['a_only']} B-only={r['b_only']} "
            f"A⊆B={r['a_subset_b']} B⊆A={r['b_subset_a']} "
            f"Jaccard={r['jaccard']:.4f}"
        )

    print()
    print("Output:", OUT_DIR)
    print("No canonical subset was selected automatically.")
    print("No strategy, research panel, or production database was modified.")


if __name__ == "__main__":
    main()
