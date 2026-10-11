from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


DEFAULT_RELATIVE_DIR = Path("data") / "research" / "forward_breakout_v1"


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"__read_error__": f"{type(exc).__name__}: {exc}"}
    if not isinstance(value, dict):
        return {"__read_error__": "JSON root is not an object"}
    return value


def _pct(v: Any) -> Optional[float]:
    try:
        return float(v)
    except Exception:
        return None


def load_forward_view(project_root: Path | str = ".") -> Dict[str, Any]:
    """
    Read-only adapter for the forward journal.

    It never imports research strategy code and never recomputes strategy
    signals, PnL, risk, or portfolio decisions. The GUI displays persisted
    forward evidence only.
    """
    root = Path(project_root)
    base = root / DEFAULT_RELATIVE_DIR
    state_path = base / "state.json"
    latest_path = base / "latest.json"

    state = _load_json(state_path)
    latest = _load_json(latest_path)

    errors = []
    for name, payload in (("state.json", state), ("latest.json", latest)):
        if isinstance(payload, dict) and payload.get("__read_error__"):
            errors.append(f"{name}: {payload['__read_error__']}")

    if state is None and latest is None:
        return {
            "available": False,
            "status": "NOT_INITIALIZED",
            "message": "Forward journal 尚未初始化",
            "state_path": str(state_path),
            "latest_path": str(latest_path),
            "errors": [],
            "health": {},
            "policy": {},
            "positions": [],
            "pending": [],
            "closed_trades": [],
            "blocked_entries": [],
            "daily": [],
        }

    if errors:
        return {
            "available": False,
            "status": "READ_ERROR",
            "message": "Forward journal 读取失败",
            "state_path": str(state_path),
            "latest_path": str(latest_path),
            "errors": errors,
            "health": {},
            "policy": {},
            "positions": [],
            "pending": [],
            "closed_trades": [],
            "blocked_entries": [],
            "daily": [],
        }

    state = state or {}
    latest = latest or {}
    health = dict(latest.get("health") or {})
    stored_policy = state.get("policy") or {}
    policy = dict(stored_policy.get("policy") or stored_policy or {})

    positions_obj = state.get("positions") or {}
    if isinstance(positions_obj, dict):
        positions = []
        for symbol, row in sorted(positions_obj.items()):
            item = dict(row or {})
            item.setdefault("symbol", symbol)
            positions.append(item)
    else:
        positions = list(positions_obj) if isinstance(positions_obj, list) else []

    pending = list(state.get("pending") or [])
    closed = list(state.get("closed_trades") or [])
    blocked = list(state.get("blocked_entries") or [])
    daily = list(state.get("daily") or [])

    last_daily = daily[-1] if daily else {}
    status = str(
        health.get("status")
        or latest.get("status")
        or ("COLLECTING" if state.get("started_at") else "ARMED_WAIT_NEXT_SESSION")
    )

    return {
        "available": True,
        "status": status,
        "message": "",
        "state_path": str(state_path),
        "latest_path": str(latest_path),
        "errors": [],
        "health": health,
        "policy": policy,
        "historical_cutoff": (
            health.get("historical_cutoff")
            or state.get("historical_cutoff")
            or policy.get("historical_cutoff")
        ),
        "started_at": health.get("started_at") or state.get("started_at"),
        "last_processed_date": (
            health.get("last_processed_date")
            or state.get("last_processed_date")
        ),
        "review_not_before": health.get("review_not_before"),
        "completed_trades": int(
            health.get("completed_trades", len(closed)) or 0
        ),
        "completed_trades_target": int(
            health.get(
                "completed_trades_target",
                policy.get("min_completed_trades_for_review", 60),
            )
            or 60
        ),
        "equity": _pct(
            health.get(
                "equity",
                last_daily.get("equity", policy.get("initial_capital")),
            )
        ),
        "total_return": _pct(health.get("total_return")),
        "max_drawdown": _pct(health.get("max_drawdown")),
        "gross_weight": _pct(last_daily.get("gross_weight")),
        "active_positions": int(
            health.get("active_positions", len(positions)) or 0
        ),
        "pending_signals": int(
            health.get("pending_signals", len(pending)) or 0
        ),
        "blocked_count": int(
            health.get("blocked_entries", len(blocked)) or 0
        ),
        "base_20bps": dict(health.get("base_20bps") or {}),
        "shadow_50bps": dict(health.get("shadow_50bps") or {}),
        "positions": positions,
        "pending": pending,
        "closed_trades": list(reversed(closed[-50:])),
        "blocked_entries": list(reversed(blocked[-50:])),
        "daily": daily,
    }
