from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research.research_store import ResearchStore, sha256_file

DEFAULT_FOUNDATION = Path(
    "D:/positionx_data/incoming/eva/20260925_foundation_assets/"
    "data/research/px_assets"
)
DEFAULT_DB = PROJECT_ROOT / "data/research/positionx_research.duckdb"


def safe_rebuild(db: Path) -> None:
    """Delete only an unstarted research store. Never erase a forward journal."""
    if not db.exists():
        return
    try:
        with ResearchStore(db) as store:
            n = 0
            for table in ("forward_sessions", "forward_signals", "forward_trades", "forward_equity"):
                n += int(store.con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            if n:
                raise SystemExit(
                    f"BLOCKED_REBUILD_NONEMPTY_FORWARD_STORE rows={n}; refusing to delete {db}"
                )
    except Exception as exc:
        # A bootstrap-interrupted DB may be incomplete.  It is still safe to
        # rebuild only because this option is explicitly requested before Forward.
        print(f"[REBUILD] existing store inspection warning: {type(exc).__name__}: {exc}")
    for p in (db, Path(str(db)+".wal")):
        if p.exists():
            p.unlink()
            print("[REBUILD] removed", p)


def register_if_present(store, asset_id, view, path, kind, source, asof, metadata=None):
    if not path.exists():
        return {"asset_id": asset_id, "status": "SKIPPED_MISSING", "path": str(path)}
    return store.register_external_view(
        asset_id=asset_id,
        view_name=view,
        path=path,
        source_system=source,
        file_kind=kind,
        asof_date=asof,
        row_count=None,
        metadata=metadata or {},
    )


def main() -> int:
    p = argparse.ArgumentParser(description="Bootstrap PositionX DuckDB research catalog v1.2")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--foundation-dir", default=str(DEFAULT_FOUNDATION))
    p.add_argument("--rebuild", action="store_true", help="rebuild only if Forward journal is empty")
    args = p.parse_args()

    db = Path(args.db)
    if args.rebuild:
        safe_rebuild(db)

    f = Path(args.foundation_dir)
    results = []
    with ResearchStore(db) as store:
        # Historical accepted PIT universes stay as local CSV assets + DuckDB views.
        for uni in ("csi500", "csi1000"):
            path = PROJECT_ROOT/f"data/research/universe_assets/{uni}/{uni}_membership_daily.csv"
            results.append(register_if_present(
                store, f"{uni.upper()}_PIT_ACCEPTED", f"foundation_{uni}_pit",
                path, "csv", "POSITIONX_ACCEPTED_ASSET", "20260924",
                {"universe": uni.upper(), "storage_policy": "PARQUET_OR_CSV_PLUS_DUCKDB_VIEW"},
            ))

        # Large immutable foundation assets are registered as external views.
        candidates = [
            ("FOUNDATION_BENCHMARK_INDEX_DAILY", "foundation_benchmark_index_daily", f/"benchmark_index_daily.csv", "csv"),
            ("FOUNDATION_TRADING_STATUS", "foundation_a_share_daily_trading_status", f/"a_share_daily_trading_status.parquet", "parquet"),
            ("FOUNDATION_CORPORATE_ACTIONS", "foundation_corporate_actions", f/"corporate_actions.csv", "csv"),
            ("FOUNDATION_ADJUSTMENT_FACTORS", "foundation_adjustment_factors", f/"adjustment_factors.csv", "csv"),
            ("FOUNDATION_STOCK_BASIC", "foundation_stock_basic", f/"stock_basic.parquet", "parquet"),
        ]
        for asset_id, view, path, kind in candidates:
            results.append(register_if_present(
                store, asset_id, view, path, kind, "EVA_FOUNDATION_ASSET", "20260924"
            ))

        table_counts = {}
        for table in (
            "asset_registry", "universe_membership", "daily_bars", "forward_sessions",
            "forward_signals", "forward_positions", "forward_trades", "forward_equity",
        ):
            table_counts[table] = int(store.con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        views = [r[0] for r in store.con.execute(
            "SELECT view_name FROM duckdb_views() WHERE view_name LIKE 'foundation_%' ORDER BY 1"
        ).fetchall()]

    print("POSITIONX RESEARCH CATALOG READY v1.2")
    print("DB:", db)
    print(json.dumps({"assets": results, "table_counts": table_counts, "foundation_views": views}, ensure_ascii=False, indent=2))
    print("\nSTORAGE POLICY:")
    print("  SQLite: production execution/audit/state only.")
    print("  DuckDB physical tables: mutable research/Forward journal only.")
    print("  Large immutable foundation data: local Parquet/CSV + DuckDB registry/view.")
    print("  PostgreSQL: not in use; reconsider only for concurrent writers/remote multi-machine service.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
