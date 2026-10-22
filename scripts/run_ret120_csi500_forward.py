from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from research.forward_ret120 import (
    Ret120ForwardPolicy,
    build_daily_snapshot,
    choose_current_universe,
    exit_action,
    initial_state,
    is_st_name,
    new_listing_special,
    norm_date,
    normalize_history,
    public_health,
    review_not_before,
    signal_day_eligibility,
    snapshot_sha256,
    standard_open_v1,
)
from research.research_store import ResearchStore

DB_PATH = PROJECT_ROOT / "data/research/positionx_research.duckdb"
OUT_DIR = PROJECT_ROOT / "data/research/forward_ret120_csi500_v1_0"
QUAL_DIR = Path(r"D:\positionx_data\research\strategy2_ret120_qualification_v0_1")
QUAL_JSON = QUAL_DIR / "strategy2_qualification_v01.json"
DESIGN_JSON = QUAL_DIR / "ret120_forward_protocol_design_v01.json"
SEMANTICS_JSON = Path(r"D:\positionx_data\research\strategy2_ret120_forward_semantics_v0_1\ret120_forward_execution_semantics_v01.json")
POLICY = Ret120ForwardPolicy()

EXPECTED_QUAL = "QUALIFIED_STRATEGY_2_HISTORICAL_EVIDENCE_V0_1"
EXPECTED_DESIGN = "FORWARD_PROTOCOL_DESIGN_FROZEN_PENDING_EXECUTION_SEMANTICS_AUDIT_V0_1"
EXPECTED_SEMANTICS = "PASS_RET120_FORWARD_EXECUTION_SEMANTICS_AUDIT_V0_1"
EXPECTED_FORWARD_READY = "READY_FOR_FORWARD_RUNNER_IMPLEMENTATION_NOT_STARTED"
EXPECTED_BLOCKERS = {
    "ENTRY_FILLABILITY_EQUIVALENCE": "PASS_ENTRY_FILLABILITY_EQUIVALENCE",
    "S120_EXIT_SUSPENSION_POLICY": "PASS_S120_DEFER_TO_NEXT_TRADABLE_CLOSE_ROBUSTNESS",
    "FORWARD_REVIEW_GATE": "FROZEN_RET120_REVIEW_GATE_V0_1",
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(f"FAIL_CLOSED missing frozen artifact: {path}")
    x = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(x, dict):
        raise RuntimeError(f"FAIL_CLOSED expected object JSON: {path}")
    return x


def write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def verify_frozen_prerequisites() -> dict[str, Any]:
    q = read_json(QUAL_JSON)
    d = read_json(DESIGN_JSON)
    s = read_json(SEMANTICS_JSON)
    if q.get("status") != EXPECTED_QUAL:
        raise RuntimeError(f"FAIL_CLOSED qualification status={q.get('status')}")
    if d.get("status") != EXPECTED_DESIGN:
        raise RuntimeError(f"FAIL_CLOSED design status={d.get('status')}")
    if s.get("status") != EXPECTED_SEMANTICS:
        raise RuntimeError(f"FAIL_CLOSED semantics status={s.get('status')}")
    if s.get("strategy2_status") != EXPECTED_QUAL:
        raise RuntimeError("FAIL_CLOSED semantics Strategy #2 status mismatch")
    if s.get("forward_protocol_status") != EXPECTED_FORWARD_READY:
        raise RuntimeError(f"FAIL_CLOSED forward_protocol_status={s.get('forward_protocol_status')}")
    blockers = s.get("resolved_blockers") or {}
    for k, expected in EXPECTED_BLOCKERS.items():
        got = (blockers.get(k) or {}).get("status")
        if got != expected:
            raise RuntimeError(f"FAIL_CLOSED blocker {k} status={got}")
    frozen_policy = s.get("frozen_forward_policy") or {}
    if frozen_policy.get("strategy_id") != POLICY.strategy_id:
        raise RuntimeError("FAIL_CLOSED forward strategy id mismatch")
    rg = frozen_policy.get("review_gate") or {}
    if int(rg.get("completed_trades_target", -1)) != POLICY.completed_trades_target:
        raise RuntimeError("FAIL_CLOSED completed-trades gate drift")
    if int(rg.get("calendar_months_target", -1)) != POLICY.review_calendar_months:
        raise RuntimeError("FAIL_CLOSED review-month gate drift")
    baseline_end = (((blockers.get("S120_EXIT_SUSPENSION_POLICY") or {}).get("main_20bps_baseline_exact") or {}).get("end"))
    if str(baseline_end).replace("-", "") != POLICY.historical_cutoff:
        raise RuntimeError(f"FAIL_CLOSED historical cutoff mismatch baseline_end={baseline_end}")
    return {
        "qualification_sha256": sha256_file(QUAL_JSON),
        "design_sha256": sha256_file(DESIGN_JSON),
        "semantics_sha256": sha256_file(SEMANTICS_JSON),
    }


def ensure_ret120_tables(store: ResearchStore) -> None:
    if rq_input.enabled():
        # This is a new-segment research audit table, never a broker fill record.
        store.con.execute('''CREATE TABLE IF NOT EXISTS ret120_price_basis(
          strategy_id VARCHAR, ledger VARCHAR, symbol VARCHAR, signal_date VARCHAR,
          phase VARCHAR, trade_date VARCHAR, raw_price DOUBLE, research_price DOUBLE,
          factor DOUBLE, raw_limit_up DOUBLE, raw_limit_down DOUBLE, price_tick DOUBLE,
          instrument_status INTEGER, available_at VARCHAR, source VARCHAR, version VARCHAR,
          PRIMARY KEY(strategy_id,ledger,symbol,signal_date,phase))''')
    store.con.execute("""
    CREATE TABLE IF NOT EXISTS ret120_forward_daily_snapshot(
      strategy_id VARCHAR NOT NULL,
      signal_date VARCHAR NOT NULL,
      symbol VARCHAR NOT NULL,
      ret120 DOUBLE,
      rank_first DOUBLE,
      quintile VARCHAR,
      selected_q5 BOOLEAN NOT NULL,
      feature_valid BOOLEAN NOT NULL,
      feature_reason VARCHAR NOT NULL,
      universe_source VARCHAR NOT NULL,
      observed_at TIMESTAMP DEFAULT current_timestamp,
      PRIMARY KEY(strategy_id, signal_date, symbol)
    )
    """)
    store.con.execute("""
    CREATE TABLE IF NOT EXISTS ret120_forward_snapshot_manifest(
      strategy_id VARCHAR NOT NULL,
      signal_date VARCHAR NOT NULL,
      universe_source VARCHAR NOT NULL,
      universe_count INTEGER NOT NULL,
      valid_feature_count INTEGER NOT NULL,
      q5_count INTEGER NOT NULL,
      snapshot_sha256 VARCHAR NOT NULL,
      observed_at TIMESTAMP DEFAULT current_timestamp,
      PRIMARY KEY(strategy_id, signal_date)
    )
    """)
    store.con.execute("""
    CREATE TABLE IF NOT EXISTS ret120_forward_signals(
      strategy_id VARCHAR NOT NULL,
      symbol VARCHAR NOT NULL,
      signal_date VARCHAR NOT NULL,
      ret120 DOUBLE NOT NULL,
      quintile VARCHAR NOT NULL,
      status VARCHAR NOT NULL,
      reason VARCHAR,
      signal_instrument_name VARCHAR,
      signal_open_date VARCHAR,
      signal_instrument_status INTEGER,
      signal_st_unknown BOOLEAN,
      actual_entry_date VARCHAR,
      entry_open DOUBLE,
      up_stop_price DOUBLE,
      entry_instrument_status INTEGER,
      entry_instrument_name VARCHAR,
      updated_at TIMESTAMP DEFAULT current_timestamp,
      created_at TIMESTAMP DEFAULT current_timestamp,
      PRIMARY KEY(strategy_id, symbol, signal_date)
    )
    """)


def orphan_count(store: ResearchStore) -> int:
    n = 0
    for table in ("forward_positions", "forward_trades", "forward_equity", "forward_sessions", "ret120_forward_daily_snapshot", "ret120_forward_signals"):
        try:
            n += int(store.con.execute(f"SELECT count(*) FROM {table} WHERE strategy_id=?", [POLICY.strategy_id]).fetchone()[0])
        except Exception:
            pass
    return n


def bootstrap(db_path: Path, out_dir: Path) -> int:
    artifacts = verify_frozen_prerequisites()
    fingerprint = POLICY.fingerprint(artifacts)
    with ResearchStore(db_path) as store:
        ensure_ret120_tables(store)
        existing_hash = store.policy_hash(POLICY.strategy_id)
        state = store.load_state(POLICY.strategy_id)
        if state is None:
            if orphan_count(store):
                raise RuntimeError("FAIL_CLOSED orphan RET120 Forward rows exist without state")
            state = initial_state(POLICY, artifacts)
            store.save_state(POLICY.strategy_id, fingerprint, state)
        else:
            if existing_hash != fingerprint:
                raise RuntimeError("FAIL_CLOSED policy/artifact fingerprint changed")
            if dict(state.get("artifact_hashes") or {}) != artifacts:
                raise RuntimeError("FAIL_CLOSED frozen artifact hash drift in stored state")
        health = public_health(state, POLICY)
    write_json_atomic(out_dir / "protocol.json", {"policy": asdict(POLICY), "artifact_hashes": artifacts, "policy_hash": fingerprint})
    write_json_atomic(out_dir / "state.json", state)
    write_json_atomic(out_dir / "latest.json", health)
    print(json.dumps({
        "status": "PASS_RET120_FORWARD_BOOTSTRAP_V1_0",
        "strategy_id": POLICY.strategy_id,
        "state": health,
        "research_store": str(db_path),
        "no_qmt": True,
        "no_broker": True,
        "production_db_opened": False,
    }, ensure_ascii=False, indent=2))
    return 0


def details(xtdata: Any, symbol: str) -> dict[str, Any]:
    try:
        try:
            d = xtdata.get_instrument_detail(symbol, False)
        except TypeError:
            d = xtdata.get_instrument_detail(symbol)
        return dict(d or {})
    except Exception:
        return {}


from core import p0_rq_input as rq_input


def current_universe(xtdata: Any) -> dict[str, Any]:
    if rq_input.enabled():
        return rq_input.current_universe()
    sector = set()
    weights = set()
    sector_error = None
    weight_error = None
    try:
        xtdata.download_sector_data()
    except Exception:
        pass
    try:
        sector = set(xtdata.get_stock_list_in_sector(POLICY.sector_name) or [])
    except Exception as exc:
        sector_error = f"{type(exc).__name__}: {exc}"
    try:
        if hasattr(xtdata, "download_index_weight"):
            xtdata.download_index_weight()
    except Exception:
        pass
    try:
        raw = xtdata.get_index_weight(POLICY.benchmark)
        if isinstance(raw, dict):
            weights = set(map(str, raw.keys()))
        elif isinstance(raw, (list, tuple, set)):
            weights = set(map(str, raw))
    except Exception as exc:
        weight_error = f"{type(exc).__name__}: {exc}"
    out = choose_current_universe(sector, weights, expected_size=500)
    out["sector_error"] = sector_error
    out["weight_error"] = weight_error
    return out


def qmt_sessions(xtdata: Any, start: str, end: str) -> list[str]:
    raw = xtdata.get_trading_dates("SH", start_time=start, end_time=end)
    out = []
    for v in raw or []:
        try:
            out.append(norm_date(v))
        except Exception:
            continue
    return sorted(set(out))


def refresh_history(xtdata: Any, symbols: list[str], target: str) -> None:
    if rq_input.enabled():
        # Sealed RQ input is ingested separately, never download a QMT stock pool.
        rq_input.open_inputs(target).require_contract(POLICY.dividend_type)
        return
    if not symbols:
        return
    start = (datetime.strptime(target, "%Y%m%d") - timedelta(days=650)).strftime("%Y%m%d")
    chunks = [symbols[i:i+200] for i in range(0, len(symbols), 200)]
    for chunk in chunks:
        try:
            xtdata.download_history_data2(chunk, "1d", start, target, callback=None, incrementally=True)
        except TypeError:
            xtdata.download_history_data2(chunk, "1d", start, target, None, True)


def load_histories(xtdata: Any, symbols: list[str], bars: int = 320) -> dict[str, pd.DataFrame]:
    if rq_input.enabled():
        frames = rq_input.strategy_histories(xtdata, symbols, bars, POLICY.dividend_type, 121)
        return {s: normalize_history(f) for s, f in frames.items()}
    from research.watchlist_adapter import AdapterConfig, XtDataWatchlistAdapter
    adapter = XtDataWatchlistAdapter(
        xtdata,
        AdapterConfig(sector="", bars=bars, dividend_type=POLICY.dividend_type),
    )
    raw = adapter.history(symbols)
    return {s: normalize_history(df) for s, df in raw.items() if df is not None and not df.empty}


def day_field(histories: dict[str, pd.DataFrame], symbol: str, d: str, field: str) -> float | None:
    df = histories.get(symbol)
    if df is None or d not in df.index or field not in df.columns:
        return None
    v = df.at[d, field]
    if pd.isna(v):
        return None
    try:
        f = float(v)
    except Exception:
        return None
    return f if math.isfinite(f) else None


def prev_close_price(histories: dict[str, pd.DataFrame], symbol: str, d: str) -> float | None:
    df = histories.get(symbol)
    if df is None or d not in df.index or "close" not in df.columns:
        return None
    loc = df.index.get_loc(d)
    if not isinstance(loc, int) or loc <= 0:
        return None
    v = df.iloc[loc-1]["close"]
    if pd.isna(v):
        return None
    return float(v)


def get_positions(store: ResearchStore, ledger: str) -> list[dict[str, Any]]:
    cols = ["strategy_id", "ledger", "symbol", "signal_date", "entry_date", "entry_price", "shares", "entry_notional", "entry_fee", "last_price"]
    rows = store.con.execute("""
      SELECT strategy_id,ledger,symbol,signal_date,entry_date,entry_price,shares,entry_notional,entry_fee,last_price
      FROM forward_positions WHERE strategy_id=? AND ledger=? ORDER BY symbol
    """, [POLICY.strategy_id, ledger]).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def pending_signals(store: ResearchStore) -> list[dict[str, Any]]:
    cols = ["symbol", "signal_date", "ret120", "quintile", "status"]
    rows = store.con.execute("""
      SELECT symbol,signal_date,ret120,quintile,status FROM ret120_forward_signals
      WHERE strategy_id=? AND status='PENDING_ENTRY' ORDER BY signal_date,symbol
    """, [POLICY.strategy_id]).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def symbol_busy(store: ResearchStore, symbol: str) -> bool:
    p = int(store.con.execute("SELECT count(*) FROM forward_positions WHERE strategy_id=? AND symbol=?", [POLICY.strategy_id, symbol]).fetchone()[0])
    q = int(store.con.execute("SELECT count(*) FROM ret120_forward_signals WHERE strategy_id=? AND symbol=? AND status='PENDING_ENTRY'", [POLICY.strategy_id, symbol]).fetchone()[0])
    return bool(p or q)


def ledger_cash(state: dict[str, Any], ledger: str) -> tuple[float, float]:
    return (float(state["base_cash"]), POLICY.main_round_trip_cost) if ledger == "BASE20" else (float(state["shadow_cash"]), POLICY.shadow_round_trip_cost)


def set_ledger_cash(state: dict[str, Any], ledger: str, cash: float) -> None:
    state["base_cash" if ledger == "BASE20" else "shadow_cash"] = cash


def record_price_basis(store, ledger, symbol, signal_date, target, phase, histories, research_price):
    if not rq_input.enabled():
        return
    b = rq_input.execution_bar(histories.get(symbol), target)
    field = 'open' if phase == 'ENTRY' else 'close'
    if not b['tradable'] or b[field] is None:
        raise rq_input.InputBlocked('NONTRADABLE_PRICE_BASIS_CANNOT_BE_RECORDED')
    store.con.execute('''INSERT INTO ret120_price_basis VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        [POLICY.strategy_id, ledger, symbol, signal_date, phase, target, b[field],
         research_price, b['factor'], b['up_stop_price'], b['down_stop_price'],
         b['price_tick'], b['instrument_status'], b['available_at'], b['source'], b['version']])


def process_entries(store: ResearchStore, xtdata: Any, histories: dict[str, pd.DataFrame], target: str, market_dates: list[str], state: dict[str, Any]) -> dict[str, int]:
    pending = pending_signals(store)
    if not pending:
        return {"due": 0, "fillable": 0, "entered": 0, "blocked": 0, "no_slot": 0}
    pos = {d:i for i,d in enumerate(market_dates)}
    if target not in pos or pos[target] == 0:
        raise RuntimeError("FAIL_CLOSED cannot resolve prior session")
    prior = market_dates[pos[target]-1]
    stale = [x for x in pending if x["signal_date"] != prior]
    if stale:
        raise RuntimeError(f"FAIL_CLOSED stale pending signals sample={stale[:10]}")
    due = pending
    fillable = []
    blocked = 0
    for sig in due:
        symbol = sig["symbol"]
        op = day_field(histories, symbol, target, "open")
        d = details(xtdata, symbol)
        execution = None
        if rq_input.enabled():
            execution = rq_input.execution_bar(histories.get(symbol), target)
            op = execution['open']
            d = dict(d, UpStopPrice=execution['up_stop_price'], DownStopPrice=execution['down_stop_price'],
                     PriceTick=execution['price_tick'], InstrumentStatus=execution['instrument_status'])
        name = d.get("InstrumentName")
        if is_st_name(name):
            f = {"fillable": False, "reason": "ENTRY_KNOWN_ST"}
        elif new_listing_special(symbol, d.get("OpenDate"), target, market_dates):
            f = {"fillable": False, "reason": "ENTRY_NEW_LISTING_SPECIAL"}
        else:
            raw_status = d.get("InstrumentStatus")
            try:
                status_i = int(raw_status) if raw_status is not None else None
            except Exception:
                status_i = None
            f = standard_open_v1(
                op, d.get("UpStopPrice"), status_i, d.get("PriceTick", 0.01),
                high_price=execution['high'] if execution is not None else day_field(histories, symbol, target, "high"),
                low_price=execution['low'] if execution is not None else day_field(histories, symbol, target, "low"),
                close_price=execution['close'] if execution is not None else day_field(histories, symbol, target, "close"),
                prev_close=execution['prev_close'] if execution is not None else prev_close_price(histories, symbol, target),
            )
        raw_status = d.get("InstrumentStatus")
        try:
            status_i = int(raw_status) if raw_status is not None else None
        except Exception:
            status_i = None
        up = d.get("UpStopPrice")
        upf = None
        try:
            upf = float(up) if up is not None and math.isfinite(float(up)) else None
        except Exception:
            upf = None
        if not f["fillable"]:
            blocked += 1
            store.con.execute("""
              UPDATE ret120_forward_signals SET status='BLOCKED_ENTRY',reason=?,actual_entry_date=?,entry_open=?,up_stop_price=?,entry_instrument_status=?,entry_instrument_name=?,updated_at=now()
              WHERE strategy_id=? AND symbol=? AND signal_date=?
            """, [f["reason"], target, op, upf, status_i, name, POLICY.strategy_id, symbol, sig["signal_date"]])
        else:
            fillable.append((sig, float(op), upf, status_i, name, f["reason"]))

    base_positions = get_positions(store, "BASE20")
    slots = max(0, POLICY.max_positions - len(base_positions))
    fillable.sort(key=lambda x: (x[0]["symbol"], x[0]["signal_date"]))
    selected = fillable[:slots]
    excess = fillable[slots:]
    for sig, op, upf, status_i, name, reason in excess:
        store.con.execute("""
          UPDATE ret120_forward_signals SET status='NO_SLOT',reason='NO_SLOT',actual_entry_date=?,entry_open=?,up_stop_price=?,entry_instrument_status=?,entry_instrument_name=?,updated_at=now()
          WHERE strategy_id=? AND symbol=? AND signal_date=?
        """, [target, op, upf, status_i, name, POLICY.strategy_id, sig["symbol"], sig["signal_date"]])

    if not selected:
        return {"due": len(due), "fillable": len(fillable), "entered": 0, "blocked": blocked, "no_slot": len(excess)}

    base_entered: set[str] = set()
    for ledger in ("BASE20", "SHADOW50"):
        cash, cost = ledger_cash(state, ledger)
        half = cost / 2.0
        positions = get_positions(store, ledger)
        open_mv = 0.0
        for p in positions:
            op = day_field(histories, p["symbol"], target, "open")
            px = float(op if op is not None and op > 0 else p["last_price"])
            open_mv += float(p["shares"]) * px
        equity_open = cash + open_mv
        remaining_gross = max(0.0, POLICY.max_target_gross * equity_open - open_mv)
        remaining_cash = max(0.0, cash / (1.0 + half))
        desired_each = POLICY.target_weight * equity_open
        desired_total = desired_each * len(selected)
        alloc_total = min(desired_total, remaining_gross, remaining_cash)
        scale = alloc_total / desired_total if desired_total > 0 else 0.0
        for sig, op, upf, status_i, name, fill_reason in selected:
            notional = desired_each * scale
            if notional <= 0:
                continue
            # Raw op was checked against raw limits/tick. Keep the frozen
            # adjusted research units for return, allocation, mark and net PnL.
            economic_open = day_field(histories, sig['symbol'], target, 'open') if rq_input.enabled() else op
            if economic_open is None or economic_open <= 0:
                raise RuntimeError('FAIL_CLOSED adjusted research open missing')
            shares = notional / economic_open  # research-only fractional units
            fee = notional * half
            cash -= notional + fee
            if ledger == "BASE20":
                base_entered.add(sig["symbol"])
            store.con.execute("""
              INSERT INTO forward_positions(strategy_id,ledger,symbol,signal_date,entry_date,entry_price,shares,entry_notional,entry_fee,last_price)
              VALUES (?,?,?,?,?,?,?,?,?,?)
            """, [POLICY.strategy_id, ledger, sig["symbol"], sig["signal_date"], target, economic_open, shares, notional, fee, economic_open])
            record_price_basis(store, ledger, sig['symbol'], sig['signal_date'], target, 'ENTRY', histories, economic_open)
        set_ledger_cash(state, ledger, cash)

    for sig, op, upf, status_i, name, fill_reason in selected:
        entered = sig["symbol"] in base_entered
        store.con.execute("""
          UPDATE ret120_forward_signals SET status=?,reason=?,actual_entry_date=?,entry_open=?,up_stop_price=?,entry_instrument_status=?,entry_instrument_name=?,updated_at=now()
          WHERE strategy_id=? AND symbol=? AND signal_date=?
        """, ["ENTERED" if entered else "NO_GROSS_OR_CASH_CAPACITY", fill_reason if entered else "NO_GROSS_OR_CASH_CAPACITY", target, op, upf, status_i, name, POLICY.strategy_id, sig["symbol"], sig["signal_date"]])
    return {"due": len(due), "fillable": len(fillable), "entered": len(base_entered), "blocked": blocked, "no_slot": len(excess)}


def process_exits_and_mark(store: ResearchStore, histories: dict[str, pd.DataFrame], target: str, market_dates: list[str], state: dict[str, Any]) -> dict[str, int]:
    idx = {d:i for i,d in enumerate(market_dates)}
    exited_base = 0
    deferred_base = 0
    for ledger in ("BASE20", "SHADOW50"):
        cash, cost = ledger_cash(state, ledger)
        half = cost / 2.0
        positions = get_positions(store, ledger)
        for p in positions:
            cp = day_field(histories, p["symbol"], target, "close")
            if rq_input.enabled():
                # A vendor-carried positive close may mark a position, but can
                # never execute its S120 exit. Keep the existing defer policy.
                b = rq_input.execution_bar(histories.get(p['symbol']), target)
                if not b['tradable']:
                    cp = None
            dist = None
            if p["signal_date"] in idx and target in idx:
                dist = idx[target] - idx[p["signal_date"]]
            action = exit_action(dist, cp, POLICY.exit_offset_sessions_from_signal)
            if action == "FAIL_SESSION_DISTANCE":
                raise RuntimeError(f"FAIL_CLOSED session distance {p['symbol']} {p['signal_date']}->{target}")
            if action == "EXIT_AT_CLOSE":
                record_price_basis(store, ledger, p['symbol'], p['signal_date'], target, 'EXIT', histories, cp)
                proceeds = float(p["shares"]) * float(cp)
                exit_fee = proceeds * half
                cash += proceeds - exit_fee
                pnl = proceeds - exit_fee - float(p["entry_notional"]) - float(p["entry_fee"])
                net_ret = pnl / float(p["entry_notional"]) if float(p["entry_notional"]) > 0 else 0.0
                store.con.execute("""
                  INSERT INTO forward_trades(strategy_id,ledger,symbol,signal_date,entry_date,exit_date,entry_price,exit_price,entry_notional,net_pnl,net_return,round_trip_cost)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """, [POLICY.strategy_id, ledger, p["symbol"], p["signal_date"], p["entry_date"], target, p["entry_price"], cp, p["entry_notional"], pnl, net_ret, cost])
                store.con.execute("DELETE FROM forward_positions WHERE strategy_id=? AND ledger=? AND symbol=?", [POLICY.strategy_id, ledger, p["symbol"]])
                if ledger == "BASE20":
                    exited_base += 1
            else:
                if action == "DEFER_NO_VALID_CLOSE" and ledger == "BASE20":
                    deferred_base += 1
                if cp is not None and cp > 0:
                    store.con.execute("UPDATE forward_positions SET last_price=? WHERE strategy_id=? AND ledger=? AND symbol=?", [cp, POLICY.strategy_id, ledger, p["symbol"]])
        set_ledger_cash(state, ledger, cash)

        remaining = get_positions(store, ledger)
        mv = 0.0
        for p in remaining:
            cp = day_field(histories, p["symbol"], target, "close")
            px = float(cp if cp is not None and cp > 0 else p["last_price"])
            mv += float(p["shares"]) * px
        equity = cash + mv
        gross = mv / equity if equity > 0 else 0.0
        store.con.execute("""
          INSERT INTO forward_equity(strategy_id,ledger,trade_date,equity,cash,gross_market_value,gross_weight,active_positions)
          VALUES (?,?,?,?,?,?,?,?)
          ON CONFLICT(strategy_id,ledger,trade_date) DO UPDATE SET equity=excluded.equity,cash=excluded.cash,gross_market_value=excluded.gross_market_value,gross_weight=excluded.gross_weight,active_positions=excluded.active_positions
        """, [POLICY.strategy_id, ledger, target, equity, cash, mv, gross, len(remaining)])
        if ledger == "BASE20":
            state["base_equity"] = equity
            hwm = max(float(state.get("high_water_base", equity)), equity)
            state["high_water_base"] = hwm
            state["max_drawdown_base"] = min(float(state.get("max_drawdown_base", 0.0)), equity / hwm - 1.0)
        else:
            state["shadow_equity"] = equity
    state["due_unresolved_positions"] = deferred_base
    return {"exited": exited_base, "deferred_due": deferred_base}


def persist_snapshot(store: ResearchStore, snapshot: pd.DataFrame, universe_source: str) -> str:
    d = str(snapshot.iloc[0]["signal_date"])
    existing = int(store.con.execute("SELECT count(*) FROM ret120_forward_daily_snapshot WHERE strategy_id=? AND signal_date=?", [POLICY.strategy_id, d]).fetchone()[0])
    if existing:
        raise RuntimeError(f"FAIL_CLOSED immutable RET120 snapshot already exists date={d} rows={existing}")
    rows = []
    for r in snapshot.to_dict("records"):
        rows.append([
            POLICY.strategy_id, str(r["signal_date"]), str(r["symbol"]),
            None if pd.isna(r["ret120"]) else float(r["ret120"]),
            None if pd.isna(r["rank_first"]) else float(r["rank_first"]),
            None if pd.isna(r["quintile"]) else str(r["quintile"]),
            bool(r["selected_q5"]), bool(r["feature_valid"]), str(r["feature_reason"]), universe_source,
        ])
    store.con.executemany("""
      INSERT INTO ret120_forward_daily_snapshot(strategy_id,signal_date,symbol,ret120,rank_first,quintile,selected_q5,feature_valid,feature_reason,universe_source)
      VALUES (?,?,?,?,?,?,?,?,?,?)
    """, rows)
    sha = snapshot_sha256(snapshot)
    valid = int(snapshot["feature_valid"].sum())
    q5 = int(snapshot["selected_q5"].sum())
    store.con.execute("""
      INSERT INTO ret120_forward_snapshot_manifest(strategy_id,signal_date,universe_source,universe_count,valid_feature_count,q5_count,snapshot_sha256)
      VALUES (?,?,?,?,?,?,?)
    """, [POLICY.strategy_id, d, universe_source, len(snapshot), valid, q5, sha])
    return sha


def detect_signals(store: ResearchStore, xtdata: Any, snapshot: pd.DataFrame, target: str, market_dates: list[str]) -> dict[str, int]:
    q5 = snapshot[snapshot["selected_q5"]].sort_values("symbol")
    pending = dup = excluded = 0
    for r in q5.to_dict("records"):
        symbol = r["symbol"]
        d = details(xtdata, symbol)
        e = signal_day_eligibility(symbol, d, target, market_dates)
        if not e["eligible"]:
            status = "EXCLUDED_SIGNAL_HYGIENE"
            reason = e["reason"]
            excluded += 1
        elif symbol_busy(store, symbol):
            status = "DUPLICATE_WHILE_HELD"
            reason = "DUPLICATE_WHILE_HELD"
            dup += 1
        else:
            status = "PENDING_ENTRY"
            reason = "RET120_Q5"
            pending += 1
        store.con.execute("""
          INSERT INTO ret120_forward_signals(strategy_id,symbol,signal_date,ret120,quintile,status,reason,signal_instrument_name,signal_open_date,signal_instrument_status,signal_st_unknown)
          VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, [POLICY.strategy_id, symbol, target, float(r["ret120"]), "Q5", status, reason, e.get("instrument_name", d.get("InstrumentName")), str(e.get("open_date") or d.get("OpenDate") or ""), e.get("instrument_status"), bool(e.get("st_unknown", d.get("InstrumentName") in (None, "")))])
    return {"q5": len(q5), "pending": pending, "duplicate": dup, "excluded": excluded}


def state_counts(store: ResearchStore, state: dict[str, Any]) -> None:
    state["active_positions"] = int(store.con.execute("SELECT count(*) FROM forward_positions WHERE strategy_id=? AND ledger='BASE20'", [POLICY.strategy_id]).fetchone()[0])
    state["pending_signals"] = int(store.con.execute("SELECT count(*) FROM ret120_forward_signals WHERE strategy_id=? AND status='PENDING_ENTRY'", [POLICY.strategy_id]).fetchone()[0])
    state["blocked_entries"] = int(store.con.execute("SELECT count(*) FROM ret120_forward_signals WHERE strategy_id=? AND status IN ('BLOCKED_ENTRY','NO_SLOT','NO_GROSS_OR_CASH_CAPACITY')", [POLICY.strategy_id]).fetchone()[0])
    state["completed_trades"] = int(store.con.execute("SELECT count(*) FROM forward_trades WHERE strategy_id=? AND ledger='BASE20'", [POLICY.strategy_id]).fetchone()[0])
    state["total_return_base"] = float(state["base_equity"]) / 1_000_000.0 - 1.0
    state["total_return_shadow"] = float(state["shadow_equity"]) / 1_000_000.0 - 1.0


def status(db_path: Path) -> int:
    artifacts = verify_frozen_prerequisites()
    with ResearchStore(db_path) as store:
        ensure_ret120_tables(store)
        state = store.load_state(POLICY.strategy_id)
        if state is None:
            payload = {"status": "NOT_BOOTSTRAPPED", "strategy_id": POLICY.strategy_id}
        else:
            state_counts(store, state)
            payload = {"status": "OK", "strategy_id": POLICY.strategy_id, "health": public_health(state, POLICY), "policy_hash": store.policy_hash(POLICY.strategy_id), "artifact_hashes_current": artifacts}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def run_forward(db_path: Path, out_dir: Path) -> int:
    artifacts = verify_frozen_prerequisites()
    fingerprint = POLICY.fingerprint(artifacts)
    from core.qmt_runtime import get_xtdata
    xtdata = get_xtdata()

    today = datetime.now().strftime("%Y%m%d")
    # Exchange calendar, not weekday logic.
    cal_start = (datetime.strptime(POLICY.historical_cutoff, "%Y%m%d") - timedelta(days=10)).strftime("%Y%m%d")
    sessions = qmt_sessions(xtdata, cal_start, today)
    if not sessions:
        raise RuntimeError("FAIL_CLOSED no SH trading calendar")

    with ResearchStore(db_path) as store:
        ensure_ret120_tables(store)
        state = store.load_state(POLICY.strategy_id)
        if state is None:
            raise RuntimeError("FAIL_CLOSED RET120 Forward is not bootstrapped; run bootstrap first")
        if store.policy_hash(POLICY.strategy_id) != fingerprint:
            raise RuntimeError("FAIL_CLOSED policy/artifact fingerprint changed")
        if dict(state.get("artifact_hashes") or {}) != artifacts:
            raise RuntimeError("FAIL_CLOSED frozen artifact hash drift")
        last = str(state["last_processed_date"])
        new_sessions = [d for d in sessions if d > last]
        if not new_sessions:
            state_counts(store, state)
            health = public_health(state, POLICY)
            print(json.dumps({"status": "NO_NEW_FORWARD_SESSION", "health": health, "no_broker": True}, ensure_ascii=False, indent=2))
            return 0
        if len(new_sessions) != 1:
            raise RuntimeError(f"FAIL_CLOSED MISSED_SESSION chain={new_sessions}")
        target = new_sessions[0]
        if target != today:
            raise RuntimeError(f"FAIL_CLOSED RETRO_SNAPSHOT_FORBIDDEN target={target} today={today}")
        if datetime.now().strftime("%H%M") < "1505":
            raise RuntimeError(f"FAIL_CLOSED CURRENT_SESSION_NOT_COMPLETED now={datetime.now().strftime('%H:%M:%S')}")

        u = current_universe(xtdata)
        if not u.get("ok"):
            raise RuntimeError("FAIL_CLOSED current universe " + json.dumps(u, ensure_ascii=False))
        members = list(u["members"])
        busy = sorted({r[0] for r in store.con.execute("""
          SELECT DISTINCT symbol FROM forward_positions WHERE strategy_id=?
          UNION SELECT DISTINCT symbol FROM ret120_forward_signals WHERE strategy_id=? AND status='PENDING_ENTRY'
        """, [POLICY.strategy_id, POLICY.strategy_id]).fetchall()})
        symbols = sorted(set(members) | set(busy) | {POLICY.benchmark})
        refresh_history(xtdata, symbols, target)
        histories = load_histories(xtdata, symbols, bars=320)
        missing_busy = [s for s in busy if s not in histories]
        if missing_busy:
            raise RuntimeError(f"FAIL_CLOSED busy histories missing {missing_busy[:30]}")
        if POLICY.benchmark not in histories or target not in histories[POLICY.benchmark].index:
            raise RuntimeError("FAIL_CLOSED benchmark exact T bar missing")

        # Full calendar must cover active positions' signal dates for S120 distance.
        earliest = target
        if busy:
            r = store.con.execute("SELECT min(signal_date) FROM forward_positions WHERE strategy_id=?", [POLICY.strategy_id]).fetchone()[0]
            if r:
                earliest = min(earliest, str(r))
        cal_full_start = (datetime.strptime(earliest, "%Y%m%d") - timedelta(days=10)).strftime("%Y%m%d")
        market_dates = qmt_sessions(xtdata, cal_full_start, target)
        feature_frames = rq_input.feature_histories(histories) if rq_input.enabled() else histories
        snapshot = build_daily_snapshot(feature_frames, members, target, POLICY.feature_min_valid)

        # One atomic DuckDB session: universe, observed bars, entries, exits, T-close snapshot/signals, state.
        store.con.execute("BEGIN TRANSACTION")
        try:
            store.upsert_universe(POLICY.universe, target, members, "RET120_FORWARD_OBSERVED", u["source"], dataset_id=None)
            bar_rows = []
            for s in symbols:
                df = histories.get(s)
                if df is None or target not in df.index:
                    continue
                r = df.loc[target]
                bar_rows.append({"symbol": s, "trade_date": target, "open": r.get("open"), "high": r.get("high"), "low": r.get("low"), "close": r.get("close"), "volume": r.get("volume"), "amount": r.get("amount")})
            if bar_rows:
                store.upsert_bars(pd.DataFrame(bar_rows), dividend_type=POLICY.dividend_type, source_system=rq_input.source_label(POLICY.dividend_type))

            entry_diag = process_entries(store, xtdata, histories, target, market_dates, state)
            exit_diag = process_exits_and_mark(store, histories, target, market_dates, state)
            snap_sha = persist_snapshot(store, snapshot, u["source"])
            signal_diag = detect_signals(store, xtdata, snapshot, target, market_dates)

            if state.get("started_at") is None:
                state["started_at"] = target
                state["review_not_before"] = review_not_before(target, POLICY.review_calendar_months)
            state["last_processed_date"] = target
            state["status"] = "COLLECTING"
            state["last_snapshot_sha256"] = snap_sha
            state["last_universe_source"] = u["source"]
            state_counts(store, state)
            bclose = day_field(histories, POLICY.benchmark, target, "close")
            if bclose is None:
                raise RuntimeError("FAIL_CLOSED benchmark close missing")
            store.record_session(POLICY.strategy_id, target, len(members), u["source"], POLICY.benchmark, bclose)
            store.save_state(POLICY.strategy_id, fingerprint, state)
            store.con.execute("COMMIT")
        except Exception:
            store.con.execute("ROLLBACK")
            raise

        health = public_health(state, POLICY)

    out_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(out_dir / "protocol.json", {"policy": asdict(POLICY), "artifact_hashes": artifacts, "policy_hash": fingerprint})
    write_json_atomic(out_dir / "state.json", state)
    write_json_atomic(out_dir / "latest.json", health)
    write_json_atomic(out_dir / f"session_{target}.json", {
        "status": "PASS_RET120_FORWARD_SESSION_V1_0",
        "trade_date": target,
        "universe_source": u["source"],
        "universe_count": len(members),
        "feature_valid_count": int(snapshot["feature_valid"].sum()),
        "q5_count": int(snapshot["selected_q5"].sum()),
        "snapshot_sha256": snap_sha,
        "entry": entry_diag,
        "exit": exit_diag,
        "signals": signal_diag,
        "health": health,
        "no_broker": True,
        "production_db_opened": False,
        "auto_execute": False,
    })
    print(json.dumps({
        "status": "PASS_RET120_FORWARD_SESSION_V1_0",
        "trade_date": target,
        "snapshot_sha256": snap_sha,
        "entry": entry_diag,
        "exit": exit_diag,
        "signals": signal_diag,
        "health": health,
        "research_store": str(db_path),
        "no_broker": True,
        "production_db_opened": False,
        "auto_execute": False,
    }, ensure_ascii=False, indent=2))
    return 0


def check() -> int:
    artifacts = verify_frozen_prerequisites()
    if not (PROJECT_ROOT / "research/research_store.py").exists():
        raise RuntimeError("FAIL_CLOSED missing research/research_store.py")
    print(json.dumps({
        "status": "PASS_RET120_CSI500_FORWARD_V1_0_CHECK",
        "strategy_id": POLICY.strategy_id,
        "historical_cutoff": POLICY.historical_cutoff,
        "policy": asdict(POLICY),
        "frozen_artifact_hashes": artifacts,
        "commands": ["check", "bootstrap", "status", "run"],
        "qmt_used_by_check": False,
        "broker": False,
        "production_db": False,
        "scheduler_created": False,
    }, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="PositionX RET120 CSI500 true-Forward research collector v1.0")
    p.add_argument("command", choices=["check", "bootstrap", "status", "run", "initialize-segment"])
    p.add_argument("--db", default=str(DB_PATH))
    p.add_argument("--output-dir", default=str(OUT_DIR))
    p.add_argument("--recovery-profile")
    p.add_argument("--authorize-initialize", action="store_true")
    a = p.parse_args()
    try:
        if a.recovery_profile:
            from scripts.p0_core_recovery_v01 import load_profile, initialize_segment, before_run, segment_status
            profile = load_profile(a.recovery_profile)
            if a.command == "initialize-segment":
                result = initialize_segment(profile, POLICY, verify_frozen_prerequisites(),
                                            authorized=a.authorize_initialize)
                print(json.dumps(result))
                return 0
            if a.command == "bootstrap":
                raise RuntimeError("FAIL_CLOSED USE_EXPLICIT_SEGMENT_INITIALIZATION")
            if a.command == "run":
                before_run(profile, datetime.now().strftime("%Y%m%d"))
                return run_forward(Path(profile["segment_db"]), Path(profile["output_dir"]) / "ret120")
            if a.command == "status":
                print(json.dumps(segment_status(profile), allow_nan=False))
                return 0
            return check()
        if a.command == "initialize-segment":
            raise RuntimeError("FAIL_CLOSED RECOVERY_PROFILE_REQUIRED")
        if a.command == "check":
            return check()
        if a.command == "bootstrap":
            return bootstrap(Path(a.db), Path(a.output_dir))
        if a.command == "status":
            return status(Path(a.db))
        return run_forward(Path(a.db), Path(a.output_dir))
    except Exception as exc:
        print(json.dumps({"status": "FAIL_CLOSED", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False, indent=2))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
