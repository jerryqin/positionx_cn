from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
import re
import sqlite3

import pandas as pd


@dataclass(frozen=True)
class PITCandidateConfig:
    min_price: float = 2.0
    min_avg_amount20: float = 50_000_000.0
    liquidity_lookback: int = 20


def normalize_trade_date(value: Any) -> str:
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y%m%d")
    text = str(value).strip()
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 8:
        return digits[:8]
    try:
        return pd.Timestamp(value).strftime("%Y%m%d")
    except Exception as exc:
        raise ValueError(f"cannot normalize trade date: {value!r}") from exc


class CSI300ReferenceUniverse:
    """CSI300 point-in-time universe backed by PositionX reference SQLite.

    Interval semantics are half-open:
        valid_from <= d < valid_to_exclusive
    where NULL valid_to_exclusive means active through the dataset cutoff.
    """

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        self._validate_db()
        self.source_sha256 = self._interval_fingerprint()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        return conn

    def _validate_db(self) -> None:
        required = {
            "dataset_registry",
            "csi300_membership_daily",
            "csi300_membership_interval",
        }
        with self._connect() as conn:
            tables = {
                r["name"]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            missing = sorted(required - tables)
            if missing:
                raise ValueError(f"reference DB missing tables: {missing}")

            row = conn.execute(
                """SELECT dataset_id,status,coverage_start,coverage_end
                   FROM dataset_registry
                   WHERE name='CSI300 Point-in-Time Canonical Membership'
                   ORDER BY created_at DESC LIMIT 1"""
            ).fetchone()
            if row is None:
                raise ValueError("CSI300 dataset registry entry missing")
            if row["status"] != "READY_FOR_RESEARCH":
                raise ValueError(f"CSI300 dataset not ready: {row['status']}")

            bad_counts = conn.execute(
                """SELECT COUNT(*) FROM (
                       SELECT trade_date, COUNT(*) AS n
                       FROM csi300_membership_daily
                       GROUP BY trade_date
                       HAVING n <> 300
                   )"""
            ).fetchone()[0]
            if bad_counts:
                raise ValueError(f"CSI300 daily membership count gate failed: {bad_counts} bad dates")

            self.dataset_id = str(row["dataset_id"])
            self.coverage_start = str(row["coverage_start"])
            self.coverage_end = str(row["coverage_end"])

    def _interval_fingerprint(self) -> str:
        h = sha256()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT symbol,valid_from,COALESCE(valid_to_exclusive,'') AS valid_to_exclusive
                   FROM csi300_membership_interval
                   ORDER BY symbol,valid_from"""
            )
            for r in rows:
                h.update(f"{r['symbol']}|{r['valid_from']}|{r['valid_to_exclusive']}\n".encode())
        return h.hexdigest()

    @property
    def interval_count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM csi300_membership_interval").fetchone()[0])

    @property
    def current_member_count(self) -> int:
        with self._connect() as conn:
            latest = conn.execute("SELECT MAX(trade_date) FROM csi300_membership_daily").fetchone()[0]
            return int(conn.execute(
                "SELECT COUNT(*) FROM csi300_membership_daily WHERE trade_date=?", (latest,)
            ).fetchone()[0])

    @property
    def distinct_symbol_count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(DISTINCT symbol) FROM csi300_membership_daily"
            ).fetchone()[0])

    @property
    def trade_date_count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(DISTINCT trade_date) FROM csi300_membership_daily"
            ).fetchone()[0])

    def validate_requested_range(self, start_date: Any, end_date: Any) -> None:
        start = normalize_trade_date(start_date)
        end = normalize_trade_date(end_date)
        if end < start:
            raise ValueError("end_date must be >= start_date")
        with self._connect() as conn:
            first = conn.execute(
                "SELECT MIN(trade_date) FROM csi300_membership_daily WHERE trade_date>=?",
                (start,),
            ).fetchone()[0]
            last = conn.execute(
                "SELECT MAX(trade_date) FROM csi300_membership_daily WHERE trade_date<=?",
                (end,),
            ).fetchone()[0]
        if first is None or last is None:
            raise ValueError(f"requested range outside CSI300 PIT coverage: {start}..{end}")

    def members_at(self, when: Any) -> set[str]:
        d = normalize_trade_date(when)
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT symbol FROM csi300_membership_interval
                   WHERE valid_from<=?
                     AND (valid_to_exclusive IS NULL OR valid_to_exclusive>?)""",
                (d, d),
            ).fetchall()
        return {str(r["symbol"]) for r in rows}

    def contains(self, symbol: str, when: Any) -> bool:
        d = normalize_trade_date(when)
        with self._connect() as conn:
            row = conn.execute(
                """SELECT 1 FROM csi300_membership_interval
                   WHERE symbol=? AND valid_from<=?
                     AND (valid_to_exclusive IS NULL OR valid_to_exclusive>?)
                   LIMIT 1""",
                (str(symbol), d, d),
            ).fetchone()
        return row is not None

    def union_members(self, start_date: Any, end_date: Any) -> List[str]:
        start = normalize_trade_date(start_date)
        end = normalize_trade_date(end_date)
        if end < start:
            raise ValueError("end_date must be >= start_date")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT DISTINCT symbol FROM csi300_membership_interval
                   WHERE valid_from<=?
                     AND (valid_to_exclusive IS NULL OR valid_to_exclusive>?)
                   ORDER BY symbol""",
                (end, start),
            ).fetchall()
        return [str(r["symbol"]) for r in rows]


def _aligned_frame(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame):
        raise TypeError("history must be pandas.DataFrame")
    x = df.copy()
    x.columns = [str(c).lower() for c in x.columns]
    required = ["open", "high", "low", "close", "volume"]
    if not set(required).issubset(x.columns):
        return pd.DataFrame()
    numeric = list(dict.fromkeys(required + (["amount"] if "amount" in x.columns else [])))
    for c in numeric:
        x[c] = pd.to_numeric(x[c], errors="coerce")
    return x.dropna(subset=required).copy()


def candidate_metrics_at_signal(
    history: pd.DataFrame,
    signal_pos: int,
    config: Optional[PITCandidateConfig] = None,
) -> Dict[str, Any]:
    cfg = config or PITCandidateConfig()
    x = _aligned_frame(history)
    if x.empty:
        return {"ok": False, "reason": "MISSING_OHLCV"}
    i = int(signal_pos)
    if i < 0 or i >= len(x):
        return {"ok": False, "reason": "INVALID_SIGNAL_POS"}
    lookback = int(cfg.liquidity_lookback)
    if i + 1 < lookback:
        return {"ok": False, "reason": "INSUFFICIENT_LIQUIDITY_HISTORY", "bars_available": i + 1}

    close = x["close"]
    volume = x["volume"]
    last_close = float(close.iloc[i])
    proxy = close * volume
    window = slice(i - lookback + 1, i + 1)
    if "amount" in x.columns and x["amount"].iloc[window].notna().any():
        amount = x["amount"].fillna(proxy)
        amount_source = "amount"
    else:
        amount = proxy
        amount_source = "close_x_volume_proxy"
    avg_amount20 = float(amount.iloc[window].mean())
    price_pass = bool(last_close >= cfg.min_price)
    liquidity_pass = bool(avg_amount20 >= cfg.min_avg_amount20)
    eligible = bool(price_pass and liquidity_pass)
    reason = "ELIGIBLE" if eligible else ("PRICE_FAIL" if not price_pass else "LIQUIDITY_FAIL")
    return {
        "ok": True,
        "eligible": eligible,
        "reason": reason,
        "close": last_close,
        "avg_amount20": avg_amount20,
        "amount_source": amount_source,
        "price_pass": price_pass,
        "liquidity_pass": liquidity_pass,
    }


def filter_events_point_in_time(
    history: Mapping[str, pd.DataFrame],
    events: Sequence[Dict[str, Any]],
    universe: CSI300ReferenceUniverse,
    config: Optional[PITCandidateConfig] = None,
    *,
    start_date: Optional[Any] = None,
    end_date: Optional[Any] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    cfg = config or PITCandidateConfig()
    start = normalize_trade_date(start_date) if start_date is not None else None
    end = normalize_trade_date(end_date) if end_date is not None else None

    accepted: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    reason_counts: Dict[str, int] = {}

    for event in events:
        symbol = str(event.get("symbol", ""))
        signal_date = normalize_trade_date(event.get("signal_date"))
        reason = None
        metrics: Dict[str, Any] = {}

        if start is not None and signal_date < start:
            reason = "BEFORE_START_DATE"
        elif end is not None and signal_date > end:
            reason = "AFTER_END_DATE"
        elif not universe.contains(symbol, signal_date):
            reason = "NOT_CSI300_MEMBER"
        else:
            df = history.get(symbol)
            if df is None or df.empty:
                reason = "MISSING_HISTORY"
            else:
                metrics = candidate_metrics_at_signal(df, int(event.get("signal_pos", -1)), cfg)
                if not metrics.get("ok"):
                    reason = str(metrics.get("reason", "CANDIDATE_GATE_ERROR"))
                elif not metrics.get("eligible"):
                    reason = str(metrics.get("reason", "CANDIDATE_GATE_FAIL"))

        if reason is None:
            item = dict(event)
            item["signal_date"] = signal_date
            item["pit_candidate"] = {
                "csi300_member": True,
                "close": metrics["close"],
                "avg_amount20": metrics["avg_amount20"],
                "amount_source": metrics["amount_source"],
                "price_pass": metrics["price_pass"],
                "liquidity_pass": metrics["liquidity_pass"],
            }
            accepted.append(item)
        else:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
            rejected.append({
                "symbol": symbol,
                "strategy": event.get("strategy"),
                "signal_date": signal_date,
                "reason": reason,
                "candidate_metrics": metrics,
            })

    return accepted, {
        "events_input": len(events),
        "events_accepted": len(accepted),
        "events_rejected": len(rejected),
        "rejection_counts": dict(sorted(reason_counts.items())),
        "rejected": rejected,
        "candidate_config": asdict(cfg),
    }
