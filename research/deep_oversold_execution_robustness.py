from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Sequence

import pandas as pd


def norm_date(v: Any) -> str:
    s = "".join(ch for ch in str(v).replace("-", "").replace("/", "") if ch.isdigit())
    if len(s) < 8:
        raise ValueError(f"invalid date {v!r}")
    return s[:8]


@dataclass(frozen=True)
class ExitPolicy:
    name: str
    block_one_price_down: bool
    block_close_at_down_limit: bool
    max_defer_sessions: int = 20

    def validate(self) -> None:
        if self.max_defer_sessions < 0:
            raise ValueError("max_defer_sessions must be non-negative")


BASELINE = ExitPolicy(
    "INTENDED_CLOSE",
    block_one_price_down=False,
    block_close_at_down_limit=False,
    max_defer_sessions=0,
)
STANDARD = ExitPolicy(
    "STANDARD_DEFERRED_CLOSE",
    block_one_price_down=True,
    block_close_at_down_limit=False,
    max_defer_sessions=20,
)
CONSERVATIVE = ExitPolicy(
    "CONSERVATIVE_DEFERRED_CLOSE",
    block_one_price_down=True,
    block_close_at_down_limit=True,
    max_defer_sessions=20,
)


def _bool(v: Any, missing: bool = True) -> bool:
    if pd.isna(v):
        return missing
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "t", "yes", "y"}
    return bool(v)


def exit_blocked(row: Mapping[str, Any], policy: ExitPolicy) -> bool:
    if policy.name == "INTENDED_CLOSE":
        return False
    if _bool(row.get("is_suspended"), True):
        return True
    if policy.block_one_price_down and _bool(row.get("is_one_price_down_limit"), True):
        return True
    if policy.block_close_at_down_limit and _bool(row.get("is_close_at_down_limit"), True):
        return True
    return False


def resolve_exit_date(
    intended_exit: str,
    symbol: str,
    market_dates: Sequence[str],
    status_map: Mapping[tuple[str, str], Mapping[str, Any]],
    policy: ExitPolicy,
) -> Dict[str, Any]:
    """
    Resolve an intended close exit to the first later close that is sellable
    under the declared policy. Missing status is fail-closed and therefore
    treated as blocked.
    """
    policy.validate()
    intended_exit = norm_date(intended_exit)
    dates = [norm_date(d) for d in market_dates]
    pos = {d: i for i, d in enumerate(dates)}
    if intended_exit not in pos:
        return {
            "resolved": False,
            "reason": "INTENDED_EXIT_NOT_IN_MARKET_CALENDAR",
            "intended_exit": intended_exit,
            "actual_exit": None,
            "defer_sessions": None,
        }

    start_i = pos[intended_exit]
    max_i = min(len(dates) - 1, start_i + policy.max_defer_sessions)

    for i in range(start_i, max_i + 1):
        d = dates[i]
        row = status_map.get((d, str(symbol)))
        if policy.name == "INTENDED_CLOSE":
            return {
                "resolved": True,
                "reason": "BASELINE_INTENDED_CLOSE",
                "intended_exit": intended_exit,
                "actual_exit": intended_exit,
                "defer_sessions": 0,
            }
        if row is None:
            continue
        if not exit_blocked(row, policy):
            return {
                "resolved": True,
                "reason": "INTENDED_SELLABLE" if i == start_i else "DEFERRED_TO_SELLABLE_CLOSE",
                "intended_exit": intended_exit,
                "actual_exit": d,
                "defer_sessions": i - start_i,
            }

    return {
        "resolved": False,
        "reason": "NO_SELLABLE_CLOSE_WITHIN_DEFER_WINDOW",
        "intended_exit": intended_exit,
        "actual_exit": None,
        "defer_sessions": None,
    }


def apply_exit_policy(
    raw_fillable_events: pd.DataFrame,
    market_dates: Sequence[str],
    status_rows: pd.DataFrame,
    policy: ExitPolicy,
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    """
    Apply exit execution first, then enforce "first signal while actually flat".

    This ordering matters: if an intended exit is deferred, subsequent same-symbol
    signals during the extension must remain blocked.
    """
    x = raw_fillable_events.copy()
    for c in ("signal_date", "entry_date", "exit_date"):
        x[c] = x[c].map(norm_date)
    x["symbol"] = x["symbol"].astype(str)

    s = status_rows.copy()
    s["trade_date"] = s["trade_date"].map(norm_date)
    s["symbol"] = s["symbol"].astype(str)
    status_map = {
        (r["trade_date"], r["symbol"]): r
        for r in s.to_dict("records")
    }

    kept = []
    unresolved = []
    last_actual_exit: Dict[str, str] = {}
    deferred = []

    for row in x.sort_values(["signal_date", "symbol"]).to_dict("records"):
        symbol = str(row["symbol"])
        signal_date = str(row["signal_date"])
        prior_exit = last_actual_exit.get(symbol)
        if prior_exit is not None and signal_date < prior_exit:
            continue

        rr = resolve_exit_date(
            intended_exit=str(row["exit_date"]),
            symbol=symbol,
            market_dates=market_dates,
            status_map=status_map,
            policy=policy,
        )
        if not rr["resolved"]:
            unresolved.append({
                "symbol": symbol,
                "signal_date": signal_date,
                **rr,
            })
            continue

        actual_exit = str(rr["actual_exit"])
        out = dict(row)
        out["intended_exit_date"] = str(row["exit_date"])
        out["exit_date"] = actual_exit
        out["exit_defer_sessions"] = int(rr["defer_sessions"])
        out["exit_resolution_reason"] = rr["reason"]
        kept.append(out)
        last_actual_exit[symbol] = actual_exit
        if rr["defer_sessions"] > 0:
            deferred.append(out)

    out_df = pd.DataFrame(kept)
    delays = pd.Series(
        [int(r["exit_defer_sessions"]) for r in deferred],
        dtype=float,
    )
    audit = {
        "policy": policy.name,
        "input_fillable_stock_day_events": int(len(x)),
        "first_signal_while_actual_flat_events": int(len(out_df)),
        "unresolved_events": int(len(unresolved)),
        "deferred_exit_events": int(len(deferred)),
        "deferred_exit_rate": float(len(deferred) / len(out_df)) if len(out_df) else None,
        "defer_sessions_mean": float(delays.mean()) if len(delays) else 0.0,
        "defer_sessions_p90": float(delays.quantile(0.90)) if len(delays) else 0.0,
        "defer_sessions_max": int(delays.max()) if len(delays) else 0,
        "unresolved_sample": unresolved[:20],
    }
    return out_df, audit


def robustness_verdict(
    baseline20: Mapping[str, Any],
    standard20: Mapping[str, Any],
    conservative20: Mapping[str, Any],
    conservative50: Mapping[str, Any],
    audits: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    checks = {
        "baseline_positive": baseline20["cagr"] > 0,
        "standard_cagr_positive": standard20["cagr"] > 0,
        "standard_maxdd_within_15pct": standard20["max_drawdown"] >= -0.15,
        "conservative_cagr_positive": conservative20["cagr"] > 0,
        "conservative_50bps_cagr_positive": conservative50["cagr"] > 0,
        "conservative_maxdd_within_20pct": conservative20["max_drawdown"] >= -0.20,
        "standard_unresolved_zero": audits["STANDARD_DEFERRED_CLOSE"]["unresolved_events"] == 0,
        "conservative_unresolved_zero": audits["CONSERVATIVE_DEFERRED_CLOSE"]["unresolved_events"] == 0,
    }
    checks["execution_robust_candidate"] = all(checks.values())
    return checks
