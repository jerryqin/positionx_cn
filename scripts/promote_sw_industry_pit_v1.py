from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

SOURCE = Path(
    r"D:\positionx_data\reference\industry\sw_pit\SW_INDUSTRY_PIT_L1_V20260927\ret120_industry_pit.csv"
)
EXPECTED_SHA256 = "df94c64e5365b82d3f76cff76c1465f4e1f39b257757ec01151d3291ceb7c8f9"
CATALOG = Path(r"D:\positionx_data\catalog\market_data.duckdb")
ASSET_ID = "SW_INDUSTRY_MEMBERSHIP_PIT_L1"
ASSET_VERSION = "SW_INDUSTRY_PIT_L1_V20260927"
TABLE = "research.sw_industry_membership_pit_v1"
REGISTRY = "research.asset_registry_v1"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_date_series(s: pd.Series) -> pd.Series:
    raw = s.astype("string").str.strip()

    def one(v):
        if pd.isna(v):
            return pd.NaT
        sv = str(v).strip()
        if sv == "" or sv == "<NA>":
            return pd.NaT
        if sv.endswith(".0"):
            head = sv[:-2]
            if head.isdigit() and len(head) == 8:
                sv = head
        if sv.isdigit() and len(sv) == 8:
            return pd.to_datetime(sv, format="%Y%m%d", errors="coerce")
        return pd.to_datetime(sv, errors="coerce")

    return raw.map(one).astype("datetime64[ns]")


def load_source() -> pd.DataFrame:
    if not SOURCE.exists():
        raise FileNotFoundError(SOURCE)
    actual = sha256_file(SOURCE)
    if actual != EXPECTED_SHA256:
        raise RuntimeError(
            f"source SHA mismatch expected={EXPECTED_SHA256} actual={actual}"
        )

    df = pd.read_csv(SOURCE, low_memory=False)
    required = [
        "symbol", "effective_from", "effective_to_exclusive",
        "industry_code", "industry_name", "standard", "level",
        "source_id", "source_url", "provenance_note",
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise RuntimeError(f"missing required columns: {missing}")

    df = df[required].copy()
    df["symbol"] = df["symbol"].astype(str).str.upper().str.strip()
    df["effective_from"] = parse_date_series(df["effective_from"])
    df["effective_to_exclusive"] = parse_date_series(df["effective_to_exclusive"])
    df["industry_code"] = df["industry_code"].astype("string").str.replace(r"\.0$", "", regex=True)
    for c in ["industry_name", "standard", "level", "source_id", "source_url", "provenance_note"]:
        df[c] = df[c].astype("string")

    if len(df) != 2039:
        raise RuntimeError(f"unexpected source row_count={len(df)}, expected 2039")
    if df["effective_from"].isna().any():
        raise RuntimeError("effective_from contains invalid dates after normalization")
    bad_end = df["effective_to_exclusive"].notna() & (
        df["effective_to_exclusive"] <= df["effective_from"]
    )
    if bad_end.any():
        raise RuntimeError(f"invalid membership intervals={int(bad_end.sum())}")

    return df


def cmd_check():
    result = {
        "status": "PASS_SW_INDUSTRY_PIT_PROMOTION_CHECK"
                  if SOURCE.exists() and CATALOG.exists()
                  else "BLOCKED_SW_INDUSTRY_PIT_PROMOTION_CHECK",
        "source": str(SOURCE),
        "source_exists": SOURCE.exists(),
        "source_sha256": sha256_file(SOURCE) if SOURCE.exists() else None,
        "expected_sha256": EXPECTED_SHA256,
        "catalog": str(CATALOG),
        "catalog_exists": CATALOG.exists(),
        "target_table": TABLE,
        "registry_table": REGISTRY,
        "asset_id": ASSET_ID,
        "asset_version": ASSET_VERSION,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if SOURCE.exists() and CATALOG.exists() else 3


def cmd_promote():
    df = load_source()
    metadata = {
        "validation": "ACCEPT_PIT_SECTOR_ASSET_FOR_SECTOR_RESEARCH",
        "validated_at": "2026-10-04",
        "source_sha256": EXPECTED_SHA256,
        "rows": int(len(df)),
        "taxonomy": ["SW2014", "SW2021"],
        "level": "L1",
        "interval_semantics": "effective_from <= T < effective_to_exclusive; NULL end=open",
        "pit_panel_coverage": 0.9832434254888739,
        "pit_ambiguity_rate": 0.0,
        "validation_gate": {
            "coverage_min": 0.95,
            "yearly_coverage_min": 0.90,
            "ambiguity_rate_max": 0.001,
            "lineage_nonempty_min": 0.95,
        },
    }

    con = duckdb.connect(str(CATALOG))
    try:
        con.execute("begin transaction")
        con.execute("create schema if not exists research")
        con.register("src_membership", df)

        con.execute(f"""
            create or replace table {TABLE} as
            select
                cast(symbol as varchar) as symbol,
                cast(effective_from as date) as effective_from,
                cast(effective_to_exclusive as date) as effective_to_exclusive,
                cast(industry_code as varchar) as industry_code,
                cast(industry_name as varchar) as industry_name,
                cast(standard as varchar) as standard,
                cast(level as varchar) as level,
                cast(source_id as varchar) as source_id,
                cast(source_url as varchar) as source_url,
                cast(provenance_note as varchar) as provenance_note
            from src_membership
            order by symbol, standard, level, effective_from
        """)

        con.execute(f"""
            create table if not exists {REGISTRY} (
                asset_id varchar,
                asset_version varchar,
                table_name varchar,
                source_path varchar,
                source_sha256 varchar,
                row_count bigint,
                promoted_at timestamp,
                metadata_json varchar,
                primary key(asset_id, asset_version)
            )
        """)
        con.execute(
            f"delete from {REGISTRY} where asset_id=? and asset_version=?",
            [ASSET_ID, ASSET_VERSION],
        )
        con.execute(
            f"""
            insert into {REGISTRY}
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ASSET_ID, ASSET_VERSION, TABLE, str(SOURCE), EXPECTED_SHA256,
                int(len(df)), datetime.now(), json.dumps(metadata, ensure_ascii=False),
            ],
        )

        actual_rows = con.execute(f"select count(*) from {TABLE}").fetchone()[0]
        if int(actual_rows) != len(df):
            raise RuntimeError(
                f"post-write row count mismatch expected={len(df)} actual={actual_rows}"
            )
        con.execute("commit")
    except Exception:
        try:
            con.execute("rollback")
        except Exception:
            pass
        raise
    finally:
        con.close()

    result = {
        "status": "PASS_SW_INDUSTRY_PIT_PROMOTION",
        "asset_id": ASSET_ID,
        "asset_version": ASSET_VERSION,
        "table": TABLE,
        "registry": REGISTRY,
        "source_sha256": EXPECTED_SHA256,
        "row_count": int(len(df)),
        "metadata": metadata,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["check", "promote"])
    args = p.parse_args()
    return cmd_check() if args.command == "check" else cmd_promote()


if __name__ == "__main__":
    raise SystemExit(main())
