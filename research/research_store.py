from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping

import pandas as pd


SCHEMA_VERSION = 2


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _duckdb():
    try:
        import duckdb
    except ImportError as exc:
        raise RuntimeError(
            "DuckDB is required for PositionX research storage. "
            "The existing PositionX research environment already uses DuckDB; "
            "do not fall back to SQLite for analytical assets."
        ) from exc
    return duckdb


class ResearchStore:
    """
    Durable local analytical/reference store.

    Deliberately separate from data/positionx.db:
    - SQLite remains the small transactional execution/audit state store.
    - DuckDB owns growing research/reference/time-series assets.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        duckdb = _duckdb()
        self.con = duckdb.connect(str(self.path))
        self.ensure_schema()

    def close(self) -> None:
        self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def ensure_schema(self) -> None:
        self.con.execute("""
        CREATE TABLE IF NOT EXISTS store_meta(
            key VARCHAR PRIMARY KEY,
            value VARCHAR NOT NULL
        );
        """)
        self.con.execute("""
        INSERT INTO store_meta VALUES ('schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, [str(SCHEMA_VERSION)])

        self.con.execute("""
        CREATE TABLE IF NOT EXISTS asset_registry(
            asset_id VARCHAR PRIMARY KEY,
            asset_kind VARCHAR NOT NULL,
            source_path VARCHAR,
            source_sha256 VARCHAR,
            source_system VARCHAR,
            status VARCHAR NOT NULL,
            asof_date VARCHAR,
            row_count BIGINT,
            imported_at TIMESTAMP DEFAULT current_timestamp,
            metadata_json VARCHAR
        );
        """)

        self.con.execute("""
        CREATE TABLE IF NOT EXISTS universe_membership(
            universe VARCHAR NOT NULL,
            trade_date VARCHAR NOT NULL,
            symbol VARCHAR NOT NULL,
            observation_mode VARCHAR NOT NULL,
            source_system VARCHAR NOT NULL,
            dataset_id VARCHAR,
            observed_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY(universe, trade_date, symbol, observation_mode)
        );
        """)

        self.con.execute("""
        CREATE TABLE IF NOT EXISTS daily_bars(
            symbol VARCHAR NOT NULL,
            trade_date VARCHAR NOT NULL,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            volume DOUBLE,
            amount DOUBLE,
            dividend_type VARCHAR NOT NULL,
            source_system VARCHAR NOT NULL,
            observed_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY(symbol, trade_date, dividend_type)
        );
        """)

        self.con.execute("""
        CREATE TABLE IF NOT EXISTS forward_sessions(
            strategy_id VARCHAR NOT NULL,
            trade_date VARCHAR NOT NULL,
            universe_count INTEGER,
            universe_source VARCHAR,
            benchmark_symbol VARCHAR,
            benchmark_close DOUBLE,
            processed_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY(strategy_id, trade_date)
        );
        """)

        self.con.execute("""
        CREATE TABLE IF NOT EXISTS forward_signals(
            strategy_id VARCHAR NOT NULL,
            symbol VARCHAR NOT NULL,
            signal_date VARCHAR NOT NULL,
            ret20 DOUBLE NOT NULL,
            status VARCHAR NOT NULL,
            reason VARCHAR,
            actual_entry_date VARCHAR,
            entry_open DOUBLE,
            up_stop_price DOUBLE,
            instrument_status INTEGER,
            created_at TIMESTAMP DEFAULT current_timestamp,
            updated_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY(strategy_id, symbol, signal_date)
        );
        """)

        self.con.execute("""
        CREATE TABLE IF NOT EXISTS forward_positions(
            strategy_id VARCHAR NOT NULL,
            ledger VARCHAR NOT NULL,
            symbol VARCHAR NOT NULL,
            signal_date VARCHAR NOT NULL,
            entry_date VARCHAR NOT NULL,
            entry_price DOUBLE NOT NULL,
            shares DOUBLE NOT NULL,
            entry_notional DOUBLE NOT NULL,
            entry_fee DOUBLE NOT NULL,
            last_price DOUBLE NOT NULL,
            PRIMARY KEY(strategy_id, ledger, symbol)
        );
        """)

        self.con.execute("""
        CREATE TABLE IF NOT EXISTS forward_trades(
            strategy_id VARCHAR NOT NULL,
            ledger VARCHAR NOT NULL,
            symbol VARCHAR NOT NULL,
            signal_date VARCHAR NOT NULL,
            entry_date VARCHAR NOT NULL,
            exit_date VARCHAR NOT NULL,
            entry_price DOUBLE NOT NULL,
            exit_price DOUBLE NOT NULL,
            entry_notional DOUBLE NOT NULL,
            net_pnl DOUBLE NOT NULL,
            net_return DOUBLE NOT NULL,
            round_trip_cost DOUBLE NOT NULL,
            PRIMARY KEY(strategy_id, ledger, symbol, signal_date)
        );
        """)

        self.con.execute("""
        CREATE TABLE IF NOT EXISTS forward_equity(
            strategy_id VARCHAR NOT NULL,
            ledger VARCHAR NOT NULL,
            trade_date VARCHAR NOT NULL,
            equity DOUBLE NOT NULL,
            cash DOUBLE NOT NULL,
            gross_market_value DOUBLE NOT NULL,
            gross_weight DOUBLE NOT NULL,
            active_positions INTEGER NOT NULL,
            PRIMARY KEY(strategy_id, ledger, trade_date)
        );
        """)

        self.con.execute("""
        CREATE TABLE IF NOT EXISTS forward_state(
            strategy_id VARCHAR PRIMARY KEY,
            policy_hash VARCHAR NOT NULL,
            state_json VARCHAR NOT NULL,
            updated_at TIMESTAMP DEFAULT current_timestamp
        );
        """)

    def register_asset(
        self,
        asset_id: str,
        asset_kind: str,
        source_path: str,
        source_sha256: str | None,
        source_system: str,
        status: str,
        asof_date: str | None,
        row_count: int | None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.con.execute("""
        INSERT INTO asset_registry(
          asset_id, asset_kind, source_path, source_sha256, source_system,
          status, asof_date, row_count, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(asset_id) DO UPDATE SET
          asset_kind=excluded.asset_kind,
          source_path=excluded.source_path,
          source_sha256=excluded.source_sha256,
          source_system=excluded.source_system,
          status=excluded.status,
          asof_date=excluded.asof_date,
          row_count=excluded.row_count,
          metadata_json=excluded.metadata_json,
          imported_at=now()
        """, [
            asset_id, asset_kind, source_path, source_sha256, source_system,
            status, asof_date, row_count,
            json.dumps(metadata or {}, ensure_ascii=False),
        ])

    def asset_sha_matches(self, asset_id: str, sha: str) -> bool:
        row = self.con.execute(
            "SELECT source_sha256 FROM asset_registry WHERE asset_id=?",
            [asset_id],
        ).fetchone()
        return bool(row and row[0] == sha)

    def import_csv_table(
        self,
        asset_id: str,
        table_name: str,
        path: Path,
        source_system: str,
        asof_date: str | None = None,
    ) -> Dict[str, Any]:
        sha = sha256_file(path)
        if self.asset_sha_matches(asset_id, sha):
            n = self.con.execute(f"SELECT count(*) FROM {table_name}").fetchone()[0]
            return {"asset_id": asset_id, "status": "ALREADY_CURRENT", "rows": int(n)}

        p = str(path).replace("\\", "/").replace("'", "''")
        self.con.execute(f"DROP TABLE IF EXISTS {table_name}")
        self.con.execute(
            f"CREATE TABLE {table_name} AS "
            f"SELECT * FROM read_csv_auto('{p}', header=true, sample_size=-1)"
        )
        n = int(self.con.execute(f"SELECT count(*) FROM {table_name}").fetchone()[0])
        self.register_asset(
            asset_id, "FOUNDATION_TABLE", str(path), sha, source_system,
            "READY", asof_date, n, {"table": table_name},
        )
        return {"asset_id": asset_id, "status": "IMPORTED", "rows": n}

    def import_parquet_table(
        self,
        asset_id: str,
        table_name: str,
        path: Path,
        source_system: str,
        asof_date: str | None = None,
    ) -> Dict[str, Any]:
        sha = sha256_file(path)
        if self.asset_sha_matches(asset_id, sha):
            n = self.con.execute(f"SELECT count(*) FROM {table_name}").fetchone()[0]
            return {"asset_id": asset_id, "status": "ALREADY_CURRENT", "rows": int(n)}

        p = str(path).replace("\\", "/").replace("'", "''")
        self.con.execute(f"DROP TABLE IF EXISTS {table_name}")
        self.con.execute(f"CREATE TABLE {table_name} AS SELECT * FROM read_parquet('{p}')")
        n = int(self.con.execute(f"SELECT count(*) FROM {table_name}").fetchone()[0])
        self.register_asset(
            asset_id, "FOUNDATION_TABLE", str(path), sha, source_system,
            "READY", asof_date, n, {"table": table_name},
        )
        return {"asset_id": asset_id, "status": "IMPORTED", "rows": n}

    def upsert_universe(
        self,
        universe: str,
        trade_date: str,
        symbols: Iterable[str],
        observation_mode: str,
        source_system: str,
        dataset_id: str | None = None,
    ) -> int:
        rows = sorted(set(map(str, symbols)))
        self.con.execute(
            "DELETE FROM universe_membership "
            "WHERE universe=? AND trade_date=? AND observation_mode=?",
            [universe, trade_date, observation_mode],
        )
        self.con.executemany("""
        INSERT INTO universe_membership(
          universe, trade_date, symbol, observation_mode, source_system, dataset_id
        ) VALUES (?, ?, ?, ?, ?, ?)
        """, [
            [universe, trade_date, s, observation_mode, source_system, dataset_id]
            for s in rows
        ])
        return len(rows)

    def get_universe(
        self,
        universe: str,
        trade_date: str,
        observation_mode: str,
    ) -> list[str]:
        return [
            r[0] for r in self.con.execute("""
            SELECT symbol FROM universe_membership
            WHERE universe=? AND trade_date=? AND observation_mode=?
            ORDER BY symbol
            """, [universe, trade_date, observation_mode]).fetchall()
        ]

    def upsert_bars(self, bars: pd.DataFrame, dividend_type: str, source_system: str) -> int:
        if bars.empty:
            return 0
        x = bars.copy()
        required = {"symbol", "trade_date", "open", "high", "low", "close"}
        missing = required - set(x.columns)
        if missing:
            raise ValueError(f"bars missing columns {sorted(missing)}")
        for c in ("volume", "amount"):
            if c not in x.columns:
                x[c] = None
        rows = []
        for r in x.to_dict("records"):
            rows.append([
                str(r["symbol"]), str(r["trade_date"]),
                None if pd.isna(r["open"]) else float(r["open"]),
                None if pd.isna(r["high"]) else float(r["high"]),
                None if pd.isna(r["low"]) else float(r["low"]),
                None if pd.isna(r["close"]) else float(r["close"]),
                None if pd.isna(r["volume"]) else float(r["volume"]),
                None if pd.isna(r["amount"]) else float(r["amount"]),
                dividend_type, source_system,
            ])
        self.con.executemany("""
        INSERT INTO daily_bars(
          symbol, trade_date, open, high, low, close, volume, amount,
          dividend_type, source_system
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, trade_date, dividend_type) DO UPDATE SET
          open=excluded.open, high=excluded.high, low=excluded.low,
          close=excluded.close, volume=excluded.volume, amount=excluded.amount,
          source_system=excluded.source_system, observed_at=now()
        """, rows)
        return len(rows)


    def register_external_view(
        self,
        asset_id: str,
        view_name: str,
        path: Path,
        source_system: str,
        file_kind: str,
        asof_date: str | None = None,
        row_count: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Register a durable local foundation file without physically copying it.

        Large immutable assets remain Parquet/CSV on disk; DuckDB stores provenance
        and exposes a view for analytical SQL.  This avoids multi-million-row
        bootstrap copies while keeping a single query surface.
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(path)
        sha = sha256_file(path)
        pp = str(path).replace("\\", "/").replace("'", "''")
        # Clean up a v1 physical copy or a prior external view without
        # issuing DROP VIEW against a table (or vice versa).
        obj = self.con.execute(
            "SELECT table_type FROM information_schema.tables "
            "WHERE table_schema='main' AND table_name=?",
            [view_name],
        ).fetchone()
        if obj:
            if str(obj[0]).upper() == "VIEW":
                self.con.execute(f"DROP VIEW {view_name}")
            else:
                self.con.execute(f"DROP TABLE {view_name}")
        kind = file_kind.lower()
        if kind == "parquet":
            self.con.execute(
                f"CREATE VIEW {view_name} AS SELECT * FROM read_parquet('{pp}')"
            )
        elif kind == "csv":
            self.con.execute(
                f"CREATE VIEW {view_name} AS "
                f"SELECT * FROM read_csv_auto('{pp}', header=true, sample_size=-1)"
            )
        else:
            raise ValueError(f"unsupported external view kind: {file_kind}")
        meta = dict(metadata or {})
        meta.update({"view": view_name, "storage": "EXTERNAL_VIEW", "file_kind": kind})
        self.register_asset(
            asset_id, "FOUNDATION_EXTERNAL_VIEW", str(path), sha,
            source_system, "READY", asof_date, row_count, meta,
        )
        return {
            "asset_id": asset_id,
            "status": "REGISTERED_EXTERNAL_VIEW",
            "view": view_name,
            "path": str(path),
            "sha256": sha,
            "rows": row_count,
        }

    def load_state(self, strategy_id: str) -> Dict[str, Any] | None:
        row = self.con.execute(
            "SELECT state_json FROM forward_state WHERE strategy_id=?",
            [strategy_id],
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save_state(self, strategy_id: str, policy_hash: str, state: Mapping[str, Any]) -> None:
        payload = json.dumps(dict(state), ensure_ascii=False)
        self.con.execute("""
        INSERT INTO forward_state(strategy_id, policy_hash, state_json)
        VALUES (?, ?, ?)
        ON CONFLICT(strategy_id) DO UPDATE SET
          policy_hash=excluded.policy_hash,
          state_json=excluded.state_json,
          updated_at=now()
        """, [strategy_id, policy_hash, payload])

    def policy_hash(self, strategy_id: str) -> str | None:
        row = self.con.execute(
            "SELECT policy_hash FROM forward_state WHERE strategy_id=?",
            [strategy_id],
        ).fetchone()
        return row[0] if row else None

    def processed_sessions(self, strategy_id: str) -> list[str]:
        return [
            r[0] for r in self.con.execute(
                "SELECT trade_date FROM forward_sessions WHERE strategy_id=? ORDER BY trade_date",
                [strategy_id],
            ).fetchall()
        ]

    def record_session(
        self, strategy_id: str, trade_date: str, universe_count: int,
        universe_source: str, benchmark_symbol: str, benchmark_close: float,
    ) -> None:
        self.con.execute("""
        INSERT INTO forward_sessions(
          strategy_id, trade_date, universe_count, universe_source,
          benchmark_symbol, benchmark_close
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(strategy_id, trade_date) DO NOTHING
        """, [
            strategy_id, trade_date, universe_count, universe_source,
            benchmark_symbol, float(benchmark_close),
        ])
