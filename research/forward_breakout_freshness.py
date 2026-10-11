from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Mapping, Optional, Sequence

from research.forward_breakout import norm_date


def _recursive_values(obj: Any, key: str):
    out = []
    if isinstance(obj, dict):
        if key in obj:
            out.append(obj[key])
        for v in obj.values():
            out.extend(_recursive_values(v, key))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_recursive_values(v, key))
    return out


def _date_or_none(v: Any) -> Optional[str]:
    try:
        return norm_date(v)
    except Exception:
        return None


def generated_calendar_date(
    snapshot: Mapping[str, Any],
    file_mtime_date: Optional[str] = None,
) -> Optional[str]:
    raw = snapshot.get("generated_at")
    if raw:
        d = _date_or_none(raw)
        if d:
            return d
    return _date_or_none(file_mtime_date) if file_mtime_date else None


def explicit_market_asof_dates(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """
    Only fields whose semantics are unambiguously 'latest market observation'.

    Deliberately excludes:
      - date: overloaded by current matcher output (may be breakout date)
      - signal_date: strategy event date, not snapshot freshness
      - breakout_date: obviously not snapshot freshness
    """
    out = []
    for row in rows:
        for key in ("latest_date", "asof_date", "trade_date"):
            if row.get(key):
                d = _date_or_none(row[key])
                if d:
                    out.append(d)
                    break
    return out


def validate_strategy_snapshot_freshness(
    snapshot: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    market_asof: str,
    local_today: str,
    file_mtime_date: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Holiday-safe and fail-closed freshness check.

    A strategy snapshot is considered fresh when:
      1) the snapshot itself was generated today (generated_at, or file mtime fallback);
      2) if explicit latest/asof/trade dates exist on rows, their max equals the
         latest completed market session;
      3) otherwise the snapshot must explicitly say history_refreshed=true.

    This avoids treating payload generated_at (a calendar date) as if it were
    the market data date.
    """
    generated = generated_calendar_date(snapshot, file_mtime_date)
    if generated != local_today:
        return {
            "ok": False,
            "reason": "SNAPSHOT_NOT_GENERATED_TODAY",
            "snapshot_generated_date": generated,
            "local_today": local_today,
            "market_asof": market_asof,
        }

    explicit = explicit_market_asof_dates(rows)
    if explicit:
        observed = max(explicit)
        if observed != market_asof:
            return {
                "ok": False,
                "reason": "EXPLICIT_MARKET_ASOF_MISMATCH",
                "snapshot_generated_date": generated,
                "explicit_market_asof": observed,
                "market_asof": market_asof,
            }
        return {
            "ok": True,
            "mode": "GENERATED_TODAY_PLUS_EXPLICIT_MARKET_ASOF",
            "snapshot_generated_date": generated,
            "explicit_market_asof": observed,
            "market_asof": market_asof,
        }

    refreshed_values = _recursive_values(snapshot, "history_refreshed")
    refreshed = any(v is True for v in refreshed_values)
    if not refreshed:
        return {
            "ok": False,
            "reason": "NO_EXPLICIT_MARKET_ASOF_AND_HISTORY_NOT_CONFIRMED_REFRESHED",
            "snapshot_generated_date": generated,
            "market_asof": market_asof,
        }

    return {
        "ok": True,
        "mode": "GENERATED_TODAY_PLUS_HISTORY_REFRESHED",
        "snapshot_generated_date": generated,
        "explicit_market_asof": None,
        "market_asof": market_asof,
    }
