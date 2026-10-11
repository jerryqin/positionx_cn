from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
import json
import math
import re

import pandas as pd


REQUIRED_COLUMNS = (
    "trade_date",
    "symbol",
    "board",
    "exchange",
    "is_suspended",
    "pre_close",
    "open",
    "high",
    "low",
    "close",
    "up_limit",
    "down_limit",
    "is_one_price_up_limit",
    "is_open_at_up_limit",
    "can_buy_at_open",
    "st_status",
    "st_known",
    "limit_ratio",
    "limit_ratio_source",
    "new_listing_special",
)


@dataclass(frozen=True)
class FillabilityConfig:
    # Extra conservative bound for unknown main-board ST history. If the actual
    # open is already at/above a hypothetical 5% up-limit, block it even when
    # the source table classified the stock with the default 10% limit.
    conservative_unknown_st: bool = False
    price_tolerance: float = 0.005


def normalize_trade_date(value: Any) -> str:
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y%m%d")
    text = str(value).strip()
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 8:
        return digits[:8]
    return pd.Timestamp(value).strftime("%Y%m%d")


def file_sha256(path: str | Path) -> str:
    p = Path(path)
    h = sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _round2_exchange(x: float) -> float:
    return math.floor(float(x) * 100.0 + 0.5) / 100.0


def _bool_value(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    try:
        if pd.isna(v):
            return default
    except Exception:
        pass
    return bool(v)


def validate_status_asset(
    parquet_path: str | Path,
    metadata_path: Optional[str | Path] = None,
    verification_path: Optional[str | Path] = None,
) -> Dict[str, Any]:
    pq = Path(parquet_path)
    if not pq.exists():
        raise FileNotFoundError(pq)

    try:
        import duckdb
    except ImportError as exc:
        raise RuntimeError("duckdb is required to read the external Parquet fillability asset") from exc
    con = duckdb.connect()
    try:
        desc = con.execute(
            "DESCRIBE SELECT * FROM read_parquet(?)", [str(pq)]
        ).fetchdf()
        cols = [str(x) for x in desc["column_name"].tolist()]
        missing = [c for c in REQUIRED_COLUMNS if c not in cols]
        if missing:
            raise ValueError(f"fillability parquet missing columns: {missing}")

        stats = con.execute(
            """
            SELECT COUNT(*) AS rows,
                   COUNT(DISTINCT trade_date) AS trading_days,
                   COUNT(DISTINCT symbol) AS symbols,
                   MIN(trade_date) AS start_date,
                   MAX(trade_date) AS end_date
            FROM read_parquet(?)
            """,
            [str(pq)],
        ).fetchdf().iloc[0].to_dict()
    finally:
        con.close()

    meta = None
    verify = None
    if metadata_path and Path(metadata_path).exists():
        meta = json.loads(Path(metadata_path).read_text(encoding="utf-8"))
    if verification_path and Path(verification_path).exists():
        verify = json.loads(Path(verification_path).read_text(encoding="utf-8"))

    expected_rows = None
    if verify and "rows" in verify:
        expected_rows = int(verify["rows"])
    elif meta and isinstance(meta.get("verification"), dict) and "rows" in meta["verification"]:
        expected_rows = int(meta["verification"]["rows"])

    if expected_rows is not None and int(stats["rows"]) != expected_rows:
        raise ValueError(
            f"fillability parquet row count mismatch: actual={int(stats['rows'])} expected={expected_rows}"
        )

    return {
        "parquet": str(pq),
        "sha256": file_sha256(pq),
        "columns": cols,
        "rows": int(stats["rows"]),
        "trading_days": int(stats["trading_days"]),
        "symbols": int(stats["symbols"]),
        "start_date": normalize_trade_date(stats["start_date"]),
        "end_date": normalize_trade_date(stats["end_date"]),
        "metadata_loaded": meta is not None,
        "verification_loaded": verify is not None,
        "metadata": meta,
        "verification": verify,
    }


def load_event_status_rows(
    events: Sequence[Mapping[str, Any]],
    parquet_path: str | Path,
) -> pd.DataFrame:
    rows = []
    for idx, e in enumerate(events):
        rows.append({
            "event_idx": int(idx),
            "symbol": str(e["symbol"]),
            "entry_date": normalize_trade_date(e["entry_date"]),
        })
    keys = pd.DataFrame(rows)
    if keys.empty:
        return pd.DataFrame(columns=["event_idx", "symbol", "entry_date", *REQUIRED_COLUMNS])

    try:
        import duckdb
    except ImportError as exc:
        raise RuntimeError("duckdb is required to join event keys to the external Parquet asset") from exc
    con = duckdb.connect()
    try:
        con.register("event_keys", keys)
        query = f"""
            SELECT e.event_idx,
                   e.symbol AS event_symbol,
                   e.entry_date,
                   s.*
            FROM event_keys e
            LEFT JOIN read_parquet(?) s
              ON s.symbol = e.symbol
             AND CAST(s.trade_date AS DATE) = strptime(e.entry_date, '%Y%m%d')::DATE
            ORDER BY e.event_idx
        """
        out = con.execute(query, [str(parquet_path)]).fetchdf()
    finally:
        con.close()

    # More than one match for an event key means the asset itself is ambiguous.
    dup = out[out["trade_date"].notna()].groupby("event_idx").size()
    bad = dup[dup > 1]
    if len(bad):
        raise ValueError(f"duplicate fillability rows for event_idx: {bad.index.tolist()[:20]}")
    return out


def classify_entry(row: Mapping[str, Any], config: FillabilityConfig) -> Tuple[bool, str, Dict[str, Any]]:
    # A missing joined row is fail-closed.
    trade_date = row.get("trade_date")
    if trade_date is None or (isinstance(trade_date, float) and math.isnan(trade_date)) or pd.isna(trade_date):
        return False, "MISSING_STATUS_ROW", {}

    is_suspended = _bool_value(row.get("is_suspended"))
    if is_suspended:
        return False, "SUSPENDED", {}

    new_listing_special = _bool_value(row.get("new_listing_special"))
    if new_listing_special:
        # BreakoutRetest should normally never enter here because it needs
        # lookback history. Treat an occurrence as data/model uncertainty.
        return False, "NEW_LISTING_SPECIAL", {}

    up = row.get("up_limit")
    dn = row.get("down_limit")
    high = row.get("high")
    low = row.get("low")
    close = row.get("close")
    tol = float(config.price_tolerance)

    def finite(v: Any) -> bool:
        try:
            return v is not None and not pd.isna(v) and math.isfinite(float(v))
        except Exception:
            return False

    # Fail closed on event-day rows whose observed prices violate the supplied
    # limit surface. The source verification found a tiny global number of such
    # rows, so we do not silently trust them.
    if finite(up):
        if finite(high) and float(high) > float(up) + tol:
            return False, "LIMIT_DATA_INCONSISTENT", {"side": "UP", "field": "high"}
        if finite(close) and float(close) > float(up) + tol:
            return False, "LIMIT_DATA_INCONSISTENT", {"side": "UP", "field": "close"}
    if finite(dn):
        if finite(low) and float(low) < float(dn) - tol:
            return False, "LIMIT_DATA_INCONSISTENT", {"side": "DOWN", "field": "low"}
        if finite(close) and float(close) < float(dn) - tol:
            return False, "LIMIT_DATA_INCONSISTENT", {"side": "DOWN", "field": "close"}

    can_buy = row.get("can_buy_at_open")
    if can_buy is None or pd.isna(can_buy):
        return False, "CAN_BUY_UNKNOWN", {}
    if not bool(can_buy):
        if _bool_value(row.get("is_one_price_up_limit")):
            return False, "ONE_PRICE_UP_LIMIT", {}
        if _bool_value(row.get("is_open_at_up_limit")):
            return False, "OPEN_AT_UP_LIMIT", {}
        return False, "CANNOT_BUY_AT_OPEN", {}

    if config.conservative_unknown_st:
        board = str(row.get("board") or "")
        st_known = _bool_value(row.get("st_known"))
        pre_close = row.get("pre_close")
        open_price = row.get("open")
        if board == "MAIN" and not st_known and finite(pre_close) and finite(open_price):
            hypothetical_st_up = _round2_exchange(float(pre_close) * 1.05)
            if float(open_price) >= hypothetical_st_up - 1e-12:
                return False, "UNKNOWN_MAIN_ST_AT_OR_ABOVE_5PCT", {
                    "hypothetical_st_up_limit": hypothetical_st_up,
                }

    return True, "FILLABLE_AT_OPEN", {}


def apply_fillability_filter(
    events: Sequence[Mapping[str, Any]],
    status_rows: pd.DataFrame,
    config: Optional[FillabilityConfig] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    cfg = config or FillabilityConfig()
    by_idx = {int(r.event_idx): r._asdict() for r in status_rows.itertuples(index=False)}

    kept: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []
    reason_counts: Dict[str, int] = {}

    for idx, event in enumerate(events):
        status = by_idx.get(idx, {"trade_date": None})
        ok, reason, extra = classify_entry(status, cfg)
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

        row = dict(event)
        row["fillability"] = {
            "ok": bool(ok),
            "reason": reason,
            "mode": "CONSERVATIVE_UNKNOWN_ST" if cfg.conservative_unknown_st else "STANDARD",
            "entry_date": normalize_trade_date(event["entry_date"]),
            "board": None if pd.isna(status.get("board")) else status.get("board"),
            "st_known": None if pd.isna(status.get("st_known")) else _bool_value(status.get("st_known")),
            "limit_ratio_source": None if pd.isna(status.get("limit_ratio_source")) else status.get("limit_ratio_source"),
            "open": None if pd.isna(status.get("open")) else status.get("open"),
            "pre_close": None if pd.isna(status.get("pre_close")) else status.get("pre_close"),
            "up_limit": None if pd.isna(status.get("up_limit")) else status.get("up_limit"),
            **extra,
        }
        if ok:
            kept.append(row)
        else:
            blocked.append(row)

    return kept, {
        "input_events": len(events),
        "kept_events": len(kept),
        "blocked_events": len(blocked),
        "blocked_share": (len(blocked) / len(events)) if events else None,
        "reason_counts": dict(sorted(reason_counts.items())),
        "blocked_events_detail": blocked,
    }


def outcome_stats(events: Iterable[Mapping[str, Any]], horizon: int = 20) -> Dict[str, Any]:
    vals = []
    for e in events:
        v = e.get(f"return_{horizon}d")
        if v is None:
            continue
        try:
            fv = float(v)
        except Exception:
            continue
        if math.isfinite(fv):
            vals.append(fv)
    if not vals:
        return {"n": 0, "avg": None, "median": None, "win_rate": None}
    s = pd.Series(vals, dtype=float)
    return {
        "n": int(len(s)),
        "avg": float(s.mean()),
        "median": float(s.median()),
        "win_rate": float((s > 0).mean()),
    }
