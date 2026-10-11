from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date, datetime, timedelta
import json
import math
from pathlib import Path
import sys
from typing import Any, Dict, Mapping, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from xtquant import xtdata

from research.forward_breakout import (
    ForwardPolicy,
    assert_state_policy,
    atomic_write_json,
    expected_next_session,
    fillability_at_open,
    forward_health,
    init_state,
    market_sessions_between,
    norm_date,
    payload_hash,
    policy_payload,
    recursive_match_rows,
    session_age_from_signal,
    trade_return,
    triggered_breakout_signals,
)
from research.forward_breakout_freshness import validate_strategy_snapshot_freshness
from research.strategy_matching import BreakoutRetestConfig


STATE_DIR = PROJECT_ROOT / "data" / "research" / "forward_breakout_v1"
STATE_PATH = STATE_DIR / "state.json"
LATEST_PATH = STATE_DIR / "latest.json"


def _df_for_symbol(raw: Any, symbol: str) -> pd.DataFrame:
    if isinstance(raw, dict):
        x = raw.get(symbol)
        if isinstance(x, pd.DataFrame):
            return x.copy()
    return pd.DataFrame()


def _normalize_history(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    x = df.copy()
    x.columns = [str(c).lower() for c in x.columns]
    idx = []
    for v in x.index:
        try:
            idx.append(pd.Timestamp(v).strftime("%Y%m%d"))
        except Exception:
            idx.append(norm_date(v))
    x.index = idx
    return x[~x.index.duplicated(keep="last")].sort_index()


def _download(symbol: str, start: str, end: str) -> None:
    try:
        xtdata.download_history_data(
            symbol, period="1d", start_time=start, end_time=end
        )
    except TypeError:
        try:
            xtdata.download_history_data(symbol, "1d", start, end)
        except Exception:
            pass
    except Exception:
        pass


def _raw_history(symbol: str, start: str, end: str) -> pd.DataFrame:
    _download(symbol, start, end)
    fields = ["open", "high", "low", "close", "volume"]
    try:
        raw = xtdata.get_market_data_ex(
            field_list=fields,
            stock_list=[symbol],
            period="1d",
            start_time=start,
            end_time=end,
            count=-1,
            dividend_type="none",
            fill_data=False,
        )
    except TypeError:
        raw = xtdata.get_market_data_ex(
            fields, [symbol], "1d", start, end, -1, "none", False
        )
    return _normalize_history(_df_for_symbol(raw, symbol))


def _market_dates(through: str, lookback_days: int = 220) -> list[str]:
    end_dt = datetime.strptime(through, "%Y%m%d").date()
    start = (end_dt - timedelta(days=lookback_days)).strftime("%Y%m%d")
    df = _raw_history("000001.SH", start, through)
    return [d for d in df.index if d <= through]


def _bar(df: pd.DataFrame, day: str) -> Optional[Dict[str, float]]:
    if df is None or df.empty or day not in df.index:
        return None
    row = df.loc[day]
    if isinstance(row, pd.DataFrame):
        row = row.iloc[-1]
    out = {}
    for k in ("open", "high", "low", "close", "volume"):
        if k in row:
            try:
                out[k] = float(row[k])
            except Exception:
                out[k] = math.nan
    return out


def _prev_close(df: pd.DataFrame, day: str) -> Optional[float]:
    if df is None or df.empty:
        return None
    prior = [d for d in df.index if d < day]
    if not prior:
        return None
    try:
        v = float(df.loc[prior[-1], "close"])
        return v if math.isfinite(v) and v > 0 else None
    except Exception:
        return None


def _instrument_detail(symbol: str) -> Dict[str, Any]:
    try:
        d = xtdata.get_instrument_detail(symbol)
        return dict(d or {})
    except Exception:
        return {}


def _instrument_name(detail: Mapping[str, Any]) -> Optional[str]:
    for k in ("InstrumentName", "StockName", "SecurityName", "Name"):
        if detail.get(k):
            return str(detail[k])
    return None


def _matcher_config_dict() -> Dict[str, Any]:
    try:
        return asdict(BreakoutRetestConfig())
    except Exception:
        cfg = BreakoutRetestConfig()
        return dict(vars(cfg))


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _save_latest(state, policy, day_result, market_asof, freshness):
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "BREAKOUT_FORWARD_SIMULATION",
        "version": "v1.2",
        "status": "OK",
        "safety": {
            "uses_broker": False,
            "production_db_opened": False,
            "creates_trading_plan": False,
            "can_submit_order": False,
            "writes_reference_db": False,
            "submits_any_order": False,
        },
        "market_latest_completed_session": market_asof,
        "strategy_snapshot_freshness": freshness,
        "day_result": dict(day_result),
        "health": forward_health(state, policy),
        "state_path": str(STATE_PATH),
    }
    atomic_write_json(LATEST_PATH, payload)


def main() -> int:
    p = argparse.ArgumentParser(description="PositionX BreakoutRetest Forward Simulation v1.2")
    p.add_argument("--matches", default="data/research/strategy_matches_latest.json")
    args = p.parse_args()

    matches_path = PROJECT_ROOT / args.matches
    if not matches_path.exists():
        raise SystemExit(f"missing strategy match snapshot: {matches_path}")

    policy = ForwardPolicy()
    policy.validate()
    matcher_cfg = _matcher_config_dict()
    policy_data = policy_payload(policy, matcher_cfg)
    policy_hash = payload_hash(policy_data)

    today = date.today().strftime("%Y%m%d")
    market_dates = _market_dates(today)
    if not market_dates:
        raise SystemExit("BLOCKED_MARKET_DAILY_DATA_UNAVAILABLE")
    market_asof = market_dates[-1]

    snapshot = _load_json(matches_path)
    rows = recursive_match_rows(snapshot)
    mtime_date = datetime.fromtimestamp(matches_path.stat().st_mtime).strftime("%Y%m%d")
    freshness = validate_strategy_snapshot_freshness(
        snapshot=snapshot,
        rows=rows,
        market_asof=market_asof,
        local_today=today,
        file_mtime_date=mtime_date,
    )
    if not freshness["ok"]:
        raise SystemExit(
            "BLOCKED_STALE_STRATEGY_MATCH: "
            + json.dumps(freshness, ensure_ascii=False)
        )

    if STATE_PATH.exists():
        state = _load_json(STATE_PATH)
        assert_state_policy(state, policy_hash)
    else:
        state = init_state(
            policy_data,
            policy_hash,
            policy.historical_cutoff,
            policy.initial_capital,
        )

    if market_asof <= policy.historical_cutoff:
        atomic_write_json(STATE_PATH, state)
        day_result = {
            "asof": market_asof,
            "action": "ARMED_WAIT_NEXT_SESSION",
            "signals_ingested": [],
            "entries": [],
            "exits": [],
            "blocked_entries": [],
        }
        _save_latest(state, policy, day_result, market_asof, freshness)
        print(json.dumps({
            "uses_broker": False,
            "production_db_opened": False,
            "creates_trading_plan": False,
            "can_submit_order": False,
            "writes_reference_db": False,
            "submits_any_order": False,
        }, ensure_ascii=False, indent=2))
        print("\nSTRATEGY SNAPSHOT FRESHNESS:")
        print(json.dumps(freshness, ensure_ascii=False, indent=2))
        print("\nFORWARD JOURNAL ARMED")
        print(f"historical_cutoff={policy.historical_cutoff}")
        print(f"latest_completed_market_session={market_asof}")
        print("No historical-cutoff signal was admitted to the forward sample.")
        print(json.dumps(forward_health(state, policy), ensure_ascii=False, indent=2))
        print(f"\nSTATE:  {STATE_PATH}")
        print(f"LATEST: {LATEST_PATH}")
        return 0

    asof = market_asof
    if asof in set(state.get("processed_dates") or []):
        print("ALREADY_PROCESSED:", asof)
        print(json.dumps(forward_health(state, policy), ensure_ascii=False, indent=2))
        return 0

    last = str(state.get("last_processed_date") or policy.historical_cutoff)
    between = market_sessions_between(market_dates, last, asof)
    if len(between) != 1 or between[0] != asof:
        raise SystemExit(
            f"BLOCKED_MISSED_FORWARD_SESSION: last={last}, asof={asof}, "
            f"market_sessions_between={between}. Forward journal never backfills silently."
        )

    due_pending = list(state.get("pending") or [])
    if due_pending:
        expected = expected_next_session(market_dates, due_pending[0]["signal_date"])
        if expected != asof:
            raise SystemExit(
                f"BLOCKED_PENDING_ENTRY_DATE_MISMATCH: expected={expected}, asof={asof}"
            )
        if any(expected_next_session(market_dates, x["signal_date"]) != asof for x in due_pending):
            raise SystemExit("BLOCKED_MIXED_PENDING_ENTRY_DATES")

    active_symbols = sorted((state.get("positions") or {}).keys())
    pending_symbols = sorted({x["symbol"] for x in due_pending})
    needed = sorted(set(active_symbols + pending_symbols))

    hist_start = (
        datetime.strptime(asof, "%Y%m%d").date() - timedelta(days=120)
    ).strftime("%Y%m%d")
    histories = {s: _raw_history(s, hist_start, asof) for s in needed}

    positions = dict(state.get("positions") or {})
    cash = float(state.get("cash", policy.initial_capital))
    blocked_today, entered_today, exited_today = [], [], []
    buy_fee = policy.round_trip_cost / 2.0
    sell_fee = policy.round_trip_cost / 2.0

    open_values = {}
    for sym, pos in positions.items():
        bar = _bar(histories.get(sym, pd.DataFrame()), asof)
        px = bar.get("open") if bar else None
        if px is None or not math.isfinite(float(px)) or float(px) <= 0:
            px = pos.get("last_close")
        if px is None:
            raise SystemExit(f"BLOCKED_CANNOT_VALUE_OPEN: {sym} {asof}")
        open_values[sym] = float(px) * float(pos["shares"])

    pre_entry_equity = cash + sum(open_values.values())
    held_at_open = set(positions)

    fillable = []
    for signal in sorted(due_pending, key=lambda x: x["symbol"]):
        sym = signal["symbol"]
        if sym in held_at_open:
            blocked_today.append({**signal, "date": asof, "reason": "DUPLICATE_SYMBOL"})
            continue
        df = histories.get(sym, pd.DataFrame())
        bar = _bar(df, asof)
        prev = _prev_close(df, asof)
        detail = _instrument_detail(sym)
        decision = fillability_at_open(
            sym, bar, prev,
            instrument_name=_instrument_name(detail),
            instrument_status=detail.get("InstrumentStatus"),
        )
        if not decision["fillable"]:
            blocked_today.append({**signal, "date": asof, **decision})
            continue
        fillable.append({**signal, "_bar": bar, "_decision": decision})
        held_at_open.add(sym)

    free = max(0, policy.max_positions - len(positions))
    selected = fillable[:free]
    for x in fillable[free:]:
        blocked_today.append({
            **{k: v for k, v in x.items() if not k.startswith("_")},
            "date": asof,
            "reason": "NO_SLOT",
        })

    scaled = False
    if selected:
        existing_gross = sum(open_values.values())
        gross_budget = max(0.0, policy.max_total_weight * pre_entry_equity - existing_gross)
        cash_budget = max(0.0, cash) / (1.0 + buy_fee)
        desired_each = policy.target_weight * pre_entry_equity
        allowed_each = min(
            desired_each,
            gross_budget / len(selected),
            cash_budget / len(selected),
        )
        if allowed_each <= 1e-12:
            for x in selected:
                blocked_today.append({
                    **{k: v for k, v in x.items() if not k.startswith("_")},
                    "date": asof,
                    "reason": "RISK_BUDGET_BLOCK",
                })
            selected = []
        else:
            scaled = allowed_each < desired_each - 1e-9
            for x in selected:
                sym = x["symbol"]
                px = float(x["_bar"]["open"])
                fee = allowed_each * buy_fee
                shares = allowed_each / px
                cash -= allowed_each + fee
                positions[sym] = {
                    "symbol": sym,
                    "signal_date": x["signal_date"],
                    "breakout_date": x.get("breakout_date"),
                    "entry_date": asof,
                    "entry_price": px,
                    "entry_notional": allowed_each,
                    "entry_fee": fee,
                    "shares": shares,
                    "last_close": px,
                    "target_weight_at_entry": (
                        allowed_each / pre_entry_equity if pre_entry_equity else None
                    ),
                    "planned_exit_session_age": policy.holding_sessions_from_signal,
                }
                entered_today.append({
                    "symbol": sym,
                    "signal_date": x["signal_date"],
                    "entry_date": asof,
                    "entry_price": px,
                    "entry_notional": allowed_each,
                    "entry_weight": (
                        allowed_each / pre_entry_equity if pre_entry_equity else None
                    ),
                })

    for sym in list(positions):
        pos = positions[sym]
        age = session_age_from_signal(market_dates, pos["signal_date"], asof)
        if age < policy.holding_sessions_from_signal:
            continue
        df = histories.get(sym, pd.DataFrame())
        bar = _bar(df, asof)
        close = bar.get("close") if bar else None
        if close is None or not math.isfinite(float(close)) or float(close) <= 0:
            pos["exit_overdue"] = True
            pos["exit_due_session_age"] = age
            continue
        px = float(close)
        proceeds = float(pos["shares"]) * px
        fee = proceeds * sell_fee
        cash += proceeds - fee
        cost = float(pos["entry_notional"]) + float(pos["entry_fee"])
        pnl = proceeds - fee - cost
        trade = {
            "symbol": sym,
            "signal_date": pos["signal_date"],
            "entry_date": pos["entry_date"],
            "exit_date": asof,
            "entry_price": pos["entry_price"],
            "exit_price": px,
            "session_age_from_signal": age,
            "net_pnl_20bps": pnl,
            "net_return_20bps": trade_return(
                float(pos["entry_price"]), px, policy.round_trip_cost
            ),
            "net_return_50bps": trade_return(
                float(pos["entry_price"]), px, policy.shadow_round_trip_cost
            ),
        }
        state["closed_trades"].append(trade)
        exited_today.append(trade)
        del positions[sym]

    gross = 0.0
    for sym, pos in positions.items():
        df = histories.get(sym, pd.DataFrame())
        bar = _bar(df, asof)
        close = bar.get("close") if bar else None
        if close is not None and math.isfinite(float(close)) and float(close) > 0:
            pos["last_close"] = float(close)
        if pos.get("last_close") is None:
            raise SystemExit(f"BLOCKED_CANNOT_VALUE_CLOSE: {sym} {asof}")
        gross += float(pos["shares"]) * float(pos["last_close"])

    equity = cash + gross

    fresh = triggered_breakout_signals(
        rows, asof, historical_cutoff=policy.historical_cutoff
    )
    known = set(state.get("ingested_signal_keys") or [])
    new_pending = []
    for sig in fresh:
        if sig["signal_key"] in known:
            continue
        new_pending.append(sig)
        known.add(sig["signal_key"])

    state["cash"] = cash
    state["positions"] = positions
    state["pending"] = new_pending
    state["blocked_entries"].extend(blocked_today)
    state["ingested_signal_keys"] = sorted(known)
    state["last_processed_date"] = asof
    state["processed_dates"].append(asof)
    if not state.get("started_at"):
        state["started_at"] = asof

    daily_row = {
        "date": asof,
        "equity": equity,
        "cash": cash,
        "gross_market_value": gross,
        "gross_weight": gross / equity if equity > 0 else None,
        "active_positions": len(positions),
        "signals_ingested": len(new_pending),
        "entries": len(entered_today),
        "exits": len(exited_today),
        "blocked_entries": len(blocked_today),
        "scaled_entry_day": scaled,
    }
    state["daily"].append(daily_row)
    atomic_write_json(STATE_PATH, state)

    day_result = {
        "asof": asof,
        "signals_ingested": new_pending,
        "entries": entered_today,
        "exits": exited_today,
        "blocked_entries": blocked_today,
        "daily": daily_row,
    }
    _save_latest(state, policy, day_result, market_asof, freshness)

    print(json.dumps({
        "uses_broker": False,
        "production_db_opened": False,
        "creates_trading_plan": False,
        "can_submit_order": False,
        "writes_reference_db": False,
        "submits_any_order": False,
    }, ensure_ascii=False, indent=2))
    print("\nSTRATEGY SNAPSHOT FRESHNESS:")
    print(json.dumps(freshness, ensure_ascii=False, indent=2))
    print("\nFORWARD DAY:", asof)
    print(
        f"signals={len(new_pending)} entries={len(entered_today)} "
        f"exits={len(exited_today)} blocked={len(blocked_today)}"
    )
    print(
        f"equity={equity:.2f} cash={cash:.2f} gross_weight="
        f"{(gross/equity if equity else 0):.2%} active={len(positions)} "
        f"pending_for_next_session={len(new_pending)}"
    )
    print("\nHEALTH:")
    print(json.dumps(forward_health(state, policy), ensure_ascii=False, indent=2))
    print(f"\nSTATE:  {STATE_PATH}")
    print(f"LATEST: {LATEST_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
