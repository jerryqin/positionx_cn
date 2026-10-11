from __future__ import annotations
import sqlite3
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "research" / "positionx_reference.db"


def connect_reference(db_path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def csi300_members_at(trade_date: str, db_path: str | Path = DEFAULT_DB) -> list[str]:
    """Exact trading-date membership. Raises if that date is not stored."""
    with connect_reference(db_path) as conn:
        exists = conn.execute(
            "SELECT 1 FROM csi300_membership_daily WHERE trade_date=? LIMIT 1",
            (trade_date,),
        ).fetchone()
        if not exists:
            raise ValueError(f"CSI300 PIT date not available: {trade_date}")
        rows = conn.execute(
            "SELECT symbol FROM csi300_membership_daily WHERE trade_date=? ORDER BY symbol",
            (trade_date,),
        ).fetchall()
    return [r["symbol"] for r in rows]


def csi300_members_asof(yyyymmdd: str, db_path: str | Path = DEFAULT_DB) -> tuple[str, list[str]]:
    """Use the latest stored trading date <= yyyymmdd."""
    with connect_reference(db_path) as conn:
        row = conn.execute(
            "SELECT MAX(trade_date) AS d FROM csi300_membership_daily WHERE trade_date<=?",
            (yyyymmdd,),
        ).fetchone()
        if row is None or row["d"] is None:
            raise ValueError(f"No CSI300 PIT data on/before: {yyyymmdd}")
        d = row["d"]
        rows = conn.execute(
            "SELECT symbol FROM csi300_membership_daily WHERE trade_date=? ORDER BY symbol",
            (d,),
        ).fetchall()
    return d, [r["symbol"] for r in rows]


def csi300_is_member(symbol: str, trade_date: str, db_path: str | Path = DEFAULT_DB) -> bool:
    with connect_reference(db_path) as conn:
        row = conn.execute(
            """SELECT 1 FROM csi300_membership_interval
               WHERE symbol=? AND valid_from<=?
                 AND (valid_to_exclusive IS NULL OR valid_to_exclusive>?)
               LIMIT 1""",
            (symbol, trade_date, trade_date),
        ).fetchone()
    return row is not None
