from __future__ import annotations

import csv
import json
import zipfile
from pathlib import Path

import duckdb
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
EDGE_DIR = ROOT / "data" / "research" / "edge_discovery"
OUT_DIR = EDGE_DIR / "ret120_eva_industry_request_v01"
ZIP_PATH = EDGE_DIR / "ret120_eva_industry_request_v01.zip"

UNIVERSES = ("csi300", "csi500")
CUTOFF = "20260924"


def panel_path(universe: str) -> Path:
    return EDGE_DIR / f"trend_persistence_scan_{universe}_panel_v0221.parquet"


def load_request_universe(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    parts = []

    for universe in UNIVERSES:
        p = panel_path(universe)
        if not p.exists():
            raise FileNotFoundError(p)

        q = p.as_posix().replace("'", "''")
        df = con.execute(
            f"""
            SELECT
                symbol,
                MIN(signal_date) AS first_signal_date,
                MAX(signal_date) AS last_signal_date,
                COUNT(*) AS panel_rows
            FROM read_parquet('{q}')
            WHERE ret120 IS NOT NULL
              AND excess_120 IS NOT NULL
              AND COALESCE(label_quality_reject_120, FALSE) = FALSE
              AND signal_date <= '{CUTOFF}'
            GROUP BY symbol
            ORDER BY symbol
            """
        ).fetchdf()
        df.insert(1, "universe", universe)
        parts.append(df)

    long_df = pd.concat(parts, ignore_index=True)

    agg = (
        long_df.groupby("symbol", as_index=False)
        .agg(
            first_signal_date=("first_signal_date", "min"),
            last_signal_date=("last_signal_date", "max"),
            total_panel_rows=("panel_rows", "sum"),
        )
    )

    membership = (
        long_df.assign(value=True)
        .pivot_table(
            index="symbol",
            columns="universe",
            values="value",
            aggfunc="max",
            fill_value=False,
        )
        .reset_index()
    )

    for u in UNIVERSES:
        if u not in membership.columns:
            membership[u] = False

    out = agg.merge(membership, on="symbol", how="left")
    out = out.rename(
        columns={
            "csi300": "in_csi300_panel",
            "csi500": "in_csi500_panel",
        }
    )
    return out.sort_values("symbol").reset_index(drop=True)


REQUEST_MD = r"""# PositionX External Data Request — ret120 Historical PIT Industry Classification V0.1

## Purpose

PositionX has completed the `ret120` robustness, temporal-persistence, and
size-neutral audits for CSI300 / CSI500.

The remaining material exposure question is:

> Is the apparent ret120 S120 edge mainly an industry/sector concentration
> effect?

PositionX currently has no trustworthy historical point-in-time (PIT)
industry classification. Current industry classifications MUST NOT be
backfilled into earlier years.

Your task is DATA DELIVERY + PROVENANCE ONLY.
Do not evaluate the strategy, optimize parameters, or recommend trades.

## Requested population

Use `symbols.csv`.

The relevant research interval is the per-symbol
`first_signal_date .. last_signal_date` shown in that file.

## Preferred classification

Use ONE historically versioned industry classification consistently across
the requested period.

Preference order:
1. Shenwan (申万) Level 1, if historical effective membership can be supported.
2. CITIC Level 1, if historical effective membership can be supported.
3. GICS Sector, if historically versioned membership is materially more
   complete and better sourced.

Do NOT mix classification standards across symbols or dates.

If a classification standard itself changes version during the period,
document that fact explicitly. Do not silently remap old categories into a
new taxonomy unless the source itself provides a historical mapping.

## Required deliverable

A UTF-8 CSV named:

    ret120_industry_pit.csv

Required columns:

    symbol
    effective_from
    effective_to_exclusive
    industry_code
    industry_name
    standard
    level
    source_id
    source_url
    provenance_note

Semantics:

- `symbol`: PositionX code, e.g. `600000.SH`, `000001.SZ`.
- `effective_from`: YYYYMMDD, inclusive.
- `effective_to_exclusive`: YYYYMMDD, exclusive. Blank only if still open.
- `industry_code`: source-native code where available.
- `industry_name`: source-native name.
- `standard`: e.g. `SW2021`, `CITIC`, `GICS`.
- `level`: e.g. `L1` / `Sector`.
- `source_id`: stable source/document/dataset identifier.
- `source_url`: supporting URL when available.
- `provenance_note`: concise note describing how the interval was established.

## Hard constraints

1. No look-ahead.
   A historical row must represent classification that was actually effective
   for that historical interval.

2. No current-classification backfill.
   Do not take today's industry and apply it to 2020-2026.

3. No guessed change dates.
   If the exact effective date is unavailable, leave that interval missing
   rather than infer it.

4. No overlapping intervals for the same symbol.

5. Do not fabricate source URLs or citations.

6. Missing data is acceptable.
   Explicitly report gaps rather than filling them heuristically.

## Provenance summary

Also provide:

    SOURCE_NOTES.md

It should state:

- classification standard and version;
- primary source(s);
- whether data are official, vendor-derived, or independently reconstructed;
- how effective dates were determined;
- known gaps and limitations;
- approximate symbol/date coverage;
- any taxonomy-version transitions.

## Optional independent audit note

If you independently notice that the requested PIT reconstruction has
material reliability problems, state them clearly in `SOURCE_NOTES.md`.
Do not repair them by inference.

PositionX will independently validate the returned intervals and decide
whether they are usable.
"""


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    try:
        symbols = load_request_universe(con)
    finally:
        con.close()

    symbols.to_csv(
        OUT_DIR / "symbols.csv",
        index=False,
        encoding="utf-8-sig",
    )

    (OUT_DIR / "REQUEST.md").write_text(
        REQUEST_MD,
        encoding="utf-8",
    )

    schema = {
        "deliverable": "ret120_industry_pit.csv",
        "required_columns": [
            "symbol",
            "effective_from",
            "effective_to_exclusive",
            "industry_code",
            "industry_name",
            "standard",
            "level",
            "source_id",
            "source_url",
            "provenance_note",
        ],
        "date_format": "YYYYMMDD",
        "interval_semantics": "[effective_from, effective_to_exclusive)",
        "open_ended_effective_to": "blank allowed",
        "no_current_backfill": True,
        "no_guessed_dates": True,
        "one_standard_only": True,
    }
    (OUT_DIR / "schema.json").write_text(
        json.dumps(schema, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    manifest = {
        "request_id": "RET120_INDUSTRY_PIT_EVA_V01",
        "symbol_count": int(len(symbols)),
        "min_first_signal_date": str(symbols["first_signal_date"].min()),
        "max_last_signal_date": str(symbols["last_signal_date"].max()),
        "csi300_symbols": int(symbols["in_csi300_panel"].sum()),
        "csi500_symbols": int(symbols["in_csi500_panel"].sum()),
        "purpose": "historical PIT industry exposure audit only",
    }
    (OUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if ZIP_PATH.exists():
        ZIP_PATH.unlink()

    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as z:
        for name in ("REQUEST.md", "symbols.csv", "schema.json", "manifest.json"):
            z.write(OUT_DIR / name, name)

    print("=" * 100)
    print("RET120 EVA INDUSTRY REQUEST V0.1")
    print("=" * 100)
    print("Symbols:", len(symbols))
    print(
        "Research signal range:",
        symbols["first_signal_date"].min(),
        "->",
        symbols["last_signal_date"].max(),
    )
    print("Request ZIP:", ZIP_PATH)
    print()
    print("Give this ZIP to Eva and ask it to follow REQUEST.md exactly.")
    print("Do not ask Eva to evaluate or optimize ret120.")


if __name__ == "__main__":
    main()
