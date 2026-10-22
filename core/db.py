from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .models import AccountSnapshot, PositionSnapshot, TradingPlanItem, OrderIntent, now_iso, broker_order_key


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;

CREATE TABLE IF NOT EXISTS market_daily (
    symbol TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    PRIMARY KEY(symbol, trade_date)
);

CREATE TABLE IF NOT EXISTS account_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    total_asset REAL NOT NULL,
    cash REAL NOT NULL,
    market_value REAL NOT NULL,
    frozen_cash REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS position_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    volume INTEGER NOT NULL,
    can_use_volume INTEGER NOT NULL,
    avg_price REAL NOT NULL,
    market_value REAL NOT NULL,
    last_price REAL NOT NULL,
    pnl REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS trading_plan (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_date TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    target_weight REAL NOT NULL,
    reason TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    audit_json TEXT,
    status TEXT NOT NULL DEFAULT 'PLANNED',
    created_at TEXT NOT NULL,
    UNIQUE(trade_date, symbol, strategy_name)
);

CREATE TABLE IF NOT EXISTS order_intents (
    intent_key TEXT PRIMARY KEY,
    trade_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    volume INTEGER NOT NULL,
    limit_price REAL NOT NULL,
    reason TEXT NOT NULL,
    target_weight REAL NOT NULL,
    current_weight REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'NEW',
    broker_order_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS broker_orders (
    order_key TEXT PRIMARY KEY,
    order_id INTEGER NOT NULL CHECK(order_id >= 0),
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    price REAL NOT NULL,
    volume INTEGER NOT NULL,
    traded_volume INTEGER NOT NULL,
    status TEXT NOT NULL,
    strategy_name TEXT,
    remark TEXT,
    raw_json TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS broker_orders_positive_id
ON broker_orders(order_id) WHERE order_id > 0;

CREATE TABLE IF NOT EXISTS broker_trades (
    trade_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    order_id INTEGER,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    price REAL NOT NULL,
    volume INTEGER NOT NULL,
    amount REAL NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS system_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT
);

CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self.connection() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn):
        """Small forward-only migrations for an existing live SQLite DB."""
        cols = {r[1] for r in conn.execute("PRAGMA table_info(broker_orders)").fetchall()}
        if "order_key" not in cols:
            # Exact preservation of every legacy positive-ID row and raw_json.
            # Preserve an existing zero-ID snapshot without inventing counter identity.
            # LEGACY_UNPROVEN:0 is not a native ID and can never match new COUNTER rows.
            if conn.execute("SELECT COUNT(*) FROM broker_orders WHERE order_id<0").fetchone()[0]:
                raise ValueError("LEGACY_NEGATIVE_ORDER_ID_REQUIRES_REVIEW")
            if conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE tbl_name='broker_orders' AND type IN ('trigger','index') AND sql IS NOT NULL AND name!='broker_orders_positive_id'").fetchone()[0]:
                raise ValueError("BROKER_ORDER_CUSTOM_SCHEMA_REQUIRES_REVIEW")
            conn.execute("SAVEPOINT broker_order_identity")
            try:
                conn.execute("""CREATE TABLE broker_orders_identity_v1 (
                    order_key TEXT PRIMARY KEY, order_id INTEGER NOT NULL CHECK(order_id>=0),
                    ts TEXT NOT NULL, symbol TEXT NOT NULL, direction TEXT NOT NULL,
                    price REAL NOT NULL, volume INTEGER NOT NULL, traded_volume INTEGER NOT NULL,
                    status TEXT NOT NULL, strategy_name TEXT, remark TEXT, raw_json TEXT NOT NULL)""")
                conn.execute("INSERT INTO broker_orders_identity_v1 SELECT CASE WHEN order_id=0 THEN 'LEGACY_UNPROVEN:0' ELSE 'LOCAL:' || order_id END, * FROM broker_orders")
                conn.execute("DROP TABLE broker_orders")
                conn.execute("ALTER TABLE broker_orders_identity_v1 RENAME TO broker_orders")
                conn.execute("CREATE UNIQUE INDEX broker_orders_positive_id ON broker_orders(order_id) WHERE order_id>0")
                conn.execute("RELEASE broker_order_identity")
            except Exception:
                conn.execute("ROLLBACK TO broker_order_identity")
                conn.execute("RELEASE broker_order_identity")
                raise
        cols = {r[1] for r in conn.execute("PRAGMA table_info(trading_plan)").fetchall()}
        if "audit_json" not in cols:
            conn.execute("ALTER TABLE trading_plan ADD COLUMN audit_json TEXT")

    @staticmethod
    def _plan_row(row) -> Dict[str, Any]:
        d = dict(row)
        raw = d.get("audit_json")
        try:
            d["audit"] = json.loads(raw) if raw else {}
        except Exception:
            d["audit"] = {}
        return d

    @contextmanager
    def connection(self):
        with self._lock:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    def save_daily_bars(self, symbol: str, rows: Iterable[Dict[str, Any]]):
        sql = """INSERT INTO market_daily(symbol,trade_date,open,high,low,close,volume)
                 VALUES(?,?,?,?,?,?,?)
                 ON CONFLICT(symbol,trade_date) DO UPDATE SET
                 open=excluded.open,high=excluded.high,low=excluded.low,
                 close=excluded.close,volume=excluded.volume"""
        with self.connection() as conn:
            conn.executemany(sql, [(symbol, r["trade_date"], r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in rows])

    def load_daily_bars(self, symbol: str, limit: int = 300) -> List[Dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM market_daily WHERE symbol=? ORDER BY trade_date DESC LIMIT ?", (symbol, limit)
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def save_account(self, s: AccountSnapshot):
        with self.connection() as conn:
            conn.execute("INSERT INTO account_snapshots(ts,total_asset,cash,market_value,frozen_cash) VALUES(?,?,?,?,?)",
                         (s.ts, s.total_asset, s.cash, s.market_value, s.frozen_cash))

    def save_positions(self, positions: Iterable[PositionSnapshot]):
        rows = list(positions)
        if not rows:
            return
        with self.connection() as conn:
            conn.executemany(
                "INSERT INTO position_snapshots(ts,symbol,volume,can_use_volume,avg_price,market_value,last_price,pnl) VALUES(?,?,?,?,?,?,?,?)",
                [(p.ts,p.symbol,p.volume,p.can_use_volume,p.avg_price,p.market_value,p.last_price,p.pnl) for p in rows]
            )

    def upsert_plan(self, item: TradingPlanItem):
        audit_json = json.dumps(item.audit or {}, ensure_ascii=False, default=str)
        with self.connection() as conn:
            conn.execute("""INSERT INTO trading_plan(plan_date,trade_date,symbol,target_weight,reason,strategy_name,audit_json,status,created_at)
                            VALUES(?,?,?,?,?,?,?, 'PLANNED', ?)
                            ON CONFLICT(trade_date,symbol,strategy_name) DO UPDATE SET
                            target_weight=excluded.target_weight, reason=excluded.reason,
                            plan_date=excluded.plan_date, audit_json=excluded.audit_json, status='PLANNED'""",
                         (item.plan_date,item.trade_date,item.symbol,item.target_weight,item.reason,item.strategy_name,audit_json,now_iso()))

    def plan_for_date(self, trade_date: str) -> List[Dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM trading_plan WHERE trade_date=? ORDER BY symbol", (trade_date,)).fetchall()
        return [self._plan_row(r) for r in rows]

    def pending_plan(self) -> List[Dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM trading_plan WHERE trade_date='NEXT_SESSION' AND status='PLANNED' ORDER BY symbol"
            ).fetchall()
        return [self._plan_row(r) for r in rows]

    def claim_pending_plan(self, trade_date: str) -> List[Dict[str, Any]]:
        """Atomically bind the latest pending plan to the actual live session.

        Plans are created after a completed daily bar and intentionally do not
        predict the next exchange holiday.  The first later live session claims
        NEXT_SESSION and becomes the immutable execution date.
        """
        with self.connection() as conn:
            pending = conn.execute(
                "SELECT * FROM trading_plan WHERE trade_date='NEXT_SESSION' AND status='PLANNED' ORDER BY id"
            ).fetchall()
            if not pending:
                rows = conn.execute(
                    "SELECT * FROM trading_plan WHERE trade_date=? ORDER BY symbol", (trade_date,)
                ).fetchall()
                return [self._plan_row(r) for r in rows]
            # A plan created today is never allowed to execute today.
            if any(str(r["plan_date"]) >= trade_date for r in pending):
                return []
            # If the same strategy/symbol already has a row for today, fail
            # closed rather than silently merging two plans.
            for r in pending:
                exists = conn.execute(
                    "SELECT 1 FROM trading_plan WHERE trade_date=? AND symbol=? AND strategy_name=?",
                    (trade_date, r["symbol"], r["strategy_name"])
                ).fetchone()
                if exists:
                    raise RuntimeError(
                        f"今日已存在计划: {trade_date} {r['symbol']} {r['strategy_name']}；拒绝自动合并"
                    )
            conn.execute(
                "UPDATE trading_plan SET trade_date=? WHERE trade_date='NEXT_SESSION' AND status='PLANNED'",
                (trade_date,)
            )
            rows = conn.execute(
                "SELECT * FROM trading_plan WHERE trade_date=? ORDER BY symbol", (trade_date,)
            ).fetchall()
        return [self._plan_row(r) for r in rows]

    def upcoming_plan(self, on_or_after: str) -> List[Dict[str, Any]]:
        # Prefer a plan already bound to today; otherwise show the pending plan
        # for the next *actual* live session.
        today = self.plan_for_date(on_or_after)
        if today:
            return today
        pending = self.pending_plan()
        if pending:
            return pending
        with self.connection() as conn:
            row = conn.execute(
                "SELECT MIN(trade_date) AS d FROM trading_plan WHERE trade_date>=? AND trade_date!='NEXT_SESSION'",
                (on_or_after,)
            ).fetchone()
            if not row or not row["d"]:
                return []
            rows = conn.execute("SELECT * FROM trading_plan WHERE trade_date=? ORDER BY symbol", (row["d"],)).fetchall()
        return [self._plan_row(r) for r in rows]

    def set_plan_status(self, trade_date: str, symbol: str, strategy_name: str, status: str):
        with self.connection() as conn:
            conn.execute("UPDATE trading_plan SET status=? WHERE trade_date=? AND symbol=? AND strategy_name=?",
                         (status, trade_date, symbol, strategy_name))

    def has_intent(self, intent_key: str) -> bool:
        with self.connection() as conn:
            return conn.execute("SELECT 1 FROM order_intents WHERE intent_key=?", (intent_key,)).fetchone() is not None

    def count_order_intents_for_date(self, trade_date: str) -> int:
        """Count persisted live intents for a date.

        Pilot mode deliberately counts every persisted intent regardless of later
        broker status.  An ambiguous/failed attempt therefore consumes the daily
        pilot quota until a human reviews it, which is safer than an automatic retry.
        """
        with self.connection() as conn:
            row = conn.execute("SELECT COUNT(*) FROM order_intents WHERE trade_date=?", (trade_date,)).fetchone()
        return int(row[0] if row else 0)

    def save_intent(self, intent: OrderIntent):
        with self.connection() as conn:
            conn.execute("""INSERT INTO order_intents(intent_key,trade_date,symbol,side,volume,limit_price,reason,target_weight,current_weight,status,created_at,updated_at)
                            VALUES(?,?,?,?,?,?,?,?,?,'NEW',?,?)""",
                         (intent.intent_key,intent.trade_date,intent.symbol,intent.side.value,intent.volume,intent.limit_price,
                          intent.reason,intent.target_weight,intent.current_weight,now_iso(),now_iso()))

    def update_intent_order(self, intent_key: str, status: str, broker_order_id: Optional[int] = None):
        with self.connection() as conn:
            conn.execute("UPDATE order_intents SET status=?,broker_order_id=COALESCE(?,broker_order_id),updated_at=? WHERE intent_key=?",
                         (status,broker_order_id,now_iso(),intent_key))

    def save_broker_orders(self, orders: Iterable[Dict[str, Any]]):
        orders = list(orders)
        keys = [broker_order_key(o) for o in orders]
        if len(set(keys)) != len(keys):
            raise ValueError("BROKER_ORDER_IDENTITY_DUPLICATE")
        with self.connection() as conn:
            for key, o in zip(keys, orders):
                existing = conn.execute("SELECT symbol FROM broker_orders WHERE order_key=?", (key,)).fetchone()
                if existing and existing[0] != o["symbol"]:
                    raise ValueError("BROKER_ORDER_IDENTITY_COLLISION")
                conn.execute("""INSERT INTO broker_orders(order_key,order_id,ts,symbol,direction,price,volume,traded_volume,status,strategy_name,remark,raw_json)
                                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                                ON CONFLICT(order_key) DO UPDATE SET ts=excluded.ts,traded_volume=excluded.traded_volume,
                                status=excluded.status,raw_json=excluded.raw_json""",
                             (key,o["order_id"],o.get("ts",now_iso()),o["symbol"],o["direction"],o.get("price",0),o.get("volume",0),
                              o.get("traded_volume",0),o.get("status",""),o.get("strategy_name",""),o.get("remark",""),json.dumps(o,ensure_ascii=False,default=str)))

    def save_broker_trades(self, trades: Iterable[Dict[str, Any]]):
        with self.connection() as conn:
            for t in trades:
                conn.execute("""INSERT INTO broker_trades(trade_id,ts,order_id,symbol,direction,price,volume,amount,raw_json)
                                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(trade_id) DO UPDATE SET raw_json=excluded.raw_json""",
                             (str(t["trade_id"]),t.get("ts",now_iso()),t.get("order_id"),t["symbol"],t["direction"],t.get("price",0),
                              t.get("volume",0),t.get("amount",0),json.dumps(t,ensure_ascii=False,default=str)))

    def event(self, level: str, event_type: str, message: str, payload: Optional[Dict[str, Any]] = None):
        with self.connection() as conn:
            conn.execute("INSERT INTO system_events(ts,level,event_type,message,payload_json) VALUES(?,?,?,?,?)",
                         (now_iso(),level,event_type,message,json.dumps(payload,ensure_ascii=False,default=str) if payload else None))

    def recent_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM system_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def set_state(self, key: str, value: str):
        with self.connection() as conn:
            conn.execute("""INSERT INTO app_state(key,value,updated_at) VALUES(?,?,?)
                            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
                         (key,value,now_iso()))

    def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default
