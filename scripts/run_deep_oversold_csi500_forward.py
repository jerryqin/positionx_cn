from __future__ import annotations

import argparse
from datetime import datetime
from dataclasses import asdict
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from core import p0_rq_input as rq_input

from research.forward_deep_oversold import (
    DeepOversoldForwardPolicy,
    choose_current_universe,
    entry_fillability,
    initial_state,
    normalize_history,
    public_health,
    ret20_at_close,
    review_not_before,
    session_distance,
    lot_rounded_shares,
)
from research.research_store import ResearchStore
from research.watchlist_adapter import AdapterConfig, XtDataWatchlistAdapter, save_watchlist_snapshot


DB_PATH = PROJECT_ROOT / "data/research/positionx_research.duckdb"
OUT_DIR = PROJECT_ROOT / "data/research/forward_deep_oversold_csi500_v1_1"
POLICY = DeepOversoldForwardPolicy()


def current_universe() -> dict:
    if rq_input.enabled():
        return rq_input.current_universe()
    from core.qmt_runtime import get_xtdata
    xtdata = get_xtdata()
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

    result = choose_current_universe(sector, weights, expected_size=500)
    result["sector_error"] = sector_error
    result["weight_error"] = weight_error
    return result


def load_histories(
    symbols: list[str],
    bars: int = 80,
    *,
    refresh: bool = False,
) -> dict[str, pd.DataFrame]:
    from core.qmt_runtime import get_xtdata
    xtdata = get_xtdata()
    if rq_input.enabled():
        frames = rq_input.strategy_histories(xtdata, symbols, bars, POLICY.dividend_type, 120)
        return {s: normalize_history(f) for s, f in frames.items()}
    adapter = XtDataWatchlistAdapter(
        xtdata,
        AdapterConfig(sector="", bars=bars, dividend_type=POLICY.dividend_type),
    )
    if refresh:
        adapter.refresh_history(symbols)
    raw = adapter.history(symbols)
    return {
        s: normalize_history(df)
        for s, df in raw.items()
        if df is not None and not df.empty
    }


def details(symbol: str) -> dict:
    from core.qmt_runtime import get_xtdata
    xtdata = get_xtdata()
    try:
        d = xtdata.get_instrument_detail(symbol)
        return dict(d or {})
    except Exception:
        return {}


def bar_rows(histories: dict[str, pd.DataFrame], trade_date: str) -> pd.DataFrame:
    rows = []
    for symbol, df in histories.items():
        if trade_date not in df.index:
            continue
        r = df.loc[trade_date]
        rows.append({
            "symbol": symbol,
            "trade_date": trade_date,
            "open": r.get("open"),
            "high": r.get("high"),
            "low": r.get("low"),
            "close": r.get("close"),
            "volume": r.get("volume"),
            "amount": r.get("amount"),
        })
    return pd.DataFrame(rows)


def open_price(histories, symbol, d):
    df = histories.get(symbol)
    if df is None or d not in df.index:
        return None
    v = df.at[d, "open"]
    return None if pd.isna(v) else float(v)


def close_price(histories, symbol, d):
    df = histories.get(symbol)
    if df is None or d not in df.index:
        return None
    v = df.at[d, "close"]
    return None if pd.isna(v) else float(v)


def prev_close_price(histories, symbol, d):
    df = histories.get(symbol)
    if df is None or d not in df.index or "close" not in df.columns:
        return None
    idx = list(df.index).index(d)
    if idx <= 0:
        return None
    v = df.iloc[idx - 1]["close"]
    return None if pd.isna(v) else float(v)


def day_field(histories, symbol, d, field):
    df = histories.get(symbol)
    if df is None or d not in df.index or field not in df.columns:
        return None
    v = df.at[d, field]
    return None if pd.isna(v) else float(v)


def market_dates(benchmark_df: pd.DataFrame) -> list[str]:
    return sorted(set(benchmark_df.index))


def get_positions(store: ResearchStore, ledger: str) -> list[dict]:
    cols = [
        "strategy_id","ledger","symbol","signal_date","entry_date","entry_price",
        "shares","entry_notional","entry_fee","last_price",
    ]
    rows = store.con.execute("""
    SELECT strategy_id, ledger, symbol, signal_date, entry_date, entry_price,
           shares, entry_notional, entry_fee, last_price
    FROM forward_positions WHERE strategy_id=? AND ledger=?
    ORDER BY symbol
    """, [POLICY.strategy_id, ledger]).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def pending_signals(store: ResearchStore) -> list[dict]:
    cols = ["symbol","signal_date","ret20","status"]
    rows = store.con.execute("""
    SELECT symbol, signal_date, ret20, status
    FROM forward_signals
    WHERE strategy_id=? AND status='PENDING_ENTRY'
    ORDER BY signal_date, symbol
    """, [POLICY.strategy_id]).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def symbol_busy(store: ResearchStore, symbol: str) -> bool:
    p = store.con.execute("""
    SELECT count(*) FROM forward_positions
    WHERE strategy_id=? AND symbol=?
    """, [POLICY.strategy_id, symbol]).fetchone()[0]
    q = store.con.execute("""
    SELECT count(*) FROM forward_signals
    WHERE strategy_id=? AND symbol=? AND status='PENDING_ENTRY'
    """, [POLICY.strategy_id, symbol]).fetchone()[0]
    return bool(p or q)


def ledger_snapshot(store: ResearchStore, ledger: str, state: dict):
    if ledger == "BASE20":
        return float(state["base_cash"]), POLICY.main_round_trip_cost
    return float(state["shadow_cash"]), POLICY.shadow_round_trip_cost


def set_ledger_cash(state: dict, ledger: str, cash: float):
    if ledger == "BASE20":
        state["base_cash"] = cash
    else:
        state["shadow_cash"] = cash


def process_entries(store, histories, current_date, state, *, instrument_details=None, market_sessions=None):
    pending = [x for x in pending_signals(store) if x["signal_date"] < current_date]
    if not pending:
        return

    # Exact next market session only; if signal skipped a session, fail closed.
    all_dates = market_sessions if market_sessions is not None else market_dates(histories[POLICY.benchmark])
    prev_map = {all_dates[i]: all_dates[i-1] for i in range(1, len(all_dates))}
    prior = prev_map.get(current_date)
    due = [x for x in pending if x["signal_date"] == prior]
    stale = [x for x in pending if x["signal_date"] != prior]
    if stale:
        raise SystemExit(
            "BLOCKED_STALE_PENDING_SIGNAL "
            + json.dumps(stale[:20], ensure_ascii=False)
        )

    fillable = []
    for sig in due:
        symbol = sig["symbol"]
        op = open_price(histories, symbol, current_date)
        d = details(symbol) if instrument_details is None else instrument_details[symbol]
        up = d.get("UpStopPrice")
        status = d.get("InstrumentStatus")
        tick = d.get("PriceTick", 0.01)
        try:
            status_i = int(status) if status is not None else None
        except Exception:
            status_i = None
        f = entry_fillability(
            op, up, status_i, tick,
            high_price=day_field(histories, symbol, current_date, "high"),
            low_price=day_field(histories, symbol, current_date, "low"),
            close_price=day_field(histories, symbol, current_date, "close"),
            prev_close=prev_close_price(histories, symbol, current_date),
        )
        lot = int(d.get("MinLimitOrderVolume") or 100)
        if lot <= 0:
            lot = 100
        if not f["fillable"]:
            store.con.execute("""
            UPDATE forward_signals SET status='BLOCKED_ENTRY', reason=?,
              actual_entry_date=?, entry_open=?, up_stop_price=?,
              instrument_status=?, updated_at=now()
            WHERE strategy_id=? AND symbol=? AND signal_date=?
            """, [
                f["reason"], current_date, op, up, status_i,
                POLICY.strategy_id, symbol, sig["signal_date"],
            ])
            state["blocked_entries"] = int(state.get("blocked_entries", 0)) + 1
        else:
            fillable.append((sig, float(op), None if up is None else float(up), status_i, lot, f["reason"]))

    if not fillable:
        return

    # Same signals/scale for both ledgers.  Main ledger is executable-lot aware.
    main_entered = set()
    for ledger in ("BASE20", "SHADOW50"):
        cash, cost = ledger_snapshot(store, ledger, state)
        half = cost / 2.0
        positions = get_positions(store, ledger)

        open_mv = 0.0
        for pos in positions:
            op = open_price(histories, pos["symbol"], current_date)
            px = float(op if op is not None else pos["last_price"])
            open_mv += float(pos["shares"]) * px

        equity_open = cash + open_mv
        gross_cap = POLICY.max_total_weight * equity_open
        remaining_gross = max(0.0, gross_cap - open_mv)
        remaining_cash_notional = max(0.0, cash / (1.0 + half))
        desired_each = POLICY.target_weight * equity_open
        desired_total = desired_each * len(fillable)
        alloc_total = min(desired_total, remaining_gross, remaining_cash_notional)
        scale = alloc_total / desired_total if desired_total > 0 else 0.0

        for sig, op, up, status_i, lot, fill_reason in fillable:
            allocation = desired_each * scale
            shares = lot_rounded_shares(allocation, op, lot)
            if shares <= 0:
                continue
            notional = shares * op
            fee = notional * half
            cash -= notional + fee
            if ledger == "BASE20":
                main_entered.add(sig["symbol"])
            store.con.execute("""
            INSERT INTO forward_positions(
              strategy_id, ledger, symbol, signal_date, entry_date, entry_price,
              shares, entry_notional, entry_fee, last_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                POLICY.strategy_id, ledger, sig["symbol"], sig["signal_date"],
                current_date, op, shares, notional, fee, op,
            ])
        set_ledger_cash(state, ledger, cash)

    for sig, op, up, status_i, lot, fill_reason in fillable:
        entered = sig["symbol"] in main_entered
        status_text = "ENTERED" if entered else "BLOCKED_MIN_LOT_OR_CAPACITY"
        reason_text = fill_reason if entered else (
            f"LOT_ROUNDING_ZERO:min_lot={lot};target_weight={POLICY.target_weight:.4f}"
        )
        store.con.execute("""
        UPDATE forward_signals SET status=?, reason=?,
          actual_entry_date=?, entry_open=?, up_stop_price=?,
          instrument_status=?, updated_at=now()
        WHERE strategy_id=? AND symbol=? AND signal_date=?
        """, [
            status_text, reason_text, current_date, op, up, status_i,
            POLICY.strategy_id, sig["symbol"], sig["signal_date"],
        ])


def process_exits_and_mark(store, histories, current_date, state, *, market_sessions=None):
    dates = market_sessions if market_sessions is not None else market_dates(histories[POLICY.benchmark])
    for ledger, cost in (("BASE20", POLICY.main_round_trip_cost), ("SHADOW50", POLICY.shadow_round_trip_cost)):
        cash, _ = ledger_snapshot(store, ledger, state)
        half = cost / 2.0
        positions = get_positions(store, ledger)

        for pos in positions:
            cp = close_price(histories, pos["symbol"], current_date)
            if cp is None:
                raise SystemExit(f"BLOCKED_MISSING_CLOSE {pos['symbol']} {current_date}")
            dist = session_distance(dates, pos["signal_date"], current_date)
            if dist is None:
                raise SystemExit(f"BLOCKED_SESSION_DISTANCE {pos['symbol']} {pos['signal_date']} {current_date}")

            if dist >= POLICY.exit_offset_sessions_from_signal:
                proceeds = float(pos["shares"]) * cp
                exit_fee = proceeds * half
                cash += proceeds - exit_fee
                pnl = proceeds - exit_fee - float(pos["entry_notional"]) - float(pos["entry_fee"])
                net_ret = pnl / float(pos["entry_notional"])
                store.con.execute("""
                INSERT INTO forward_trades(
                  strategy_id, ledger, symbol, signal_date, entry_date, exit_date,
                  entry_price, exit_price, entry_notional, net_pnl, net_return,
                  round_trip_cost
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, [
                    POLICY.strategy_id, ledger, pos["symbol"], pos["signal_date"],
                    pos["entry_date"], current_date, pos["entry_price"], cp,
                    pos["entry_notional"], pnl, net_ret, cost,
                ])
                store.con.execute("""
                DELETE FROM forward_positions
                WHERE strategy_id=? AND ledger=? AND symbol=?
                """, [POLICY.strategy_id, ledger, pos["symbol"]])
            else:
                store.con.execute("""
                UPDATE forward_positions SET last_price=?
                WHERE strategy_id=? AND ledger=? AND symbol=?
                """, [cp, POLICY.strategy_id, ledger, pos["symbol"]])

        set_ledger_cash(state, ledger, cash)

        # End-of-day equity.
        remaining = get_positions(store, ledger)
        mv = 0.0
        for pos in remaining:
            cp = close_price(histories, pos["symbol"], current_date)
            if cp is None:
                cp = float(pos["last_price"])
            mv += float(pos["shares"]) * cp
        equity = cash + mv
        gross = mv / equity if equity > 0 else 0.0
        store.con.execute("""
        INSERT INTO forward_equity(
          strategy_id, ledger, trade_date, equity, cash,
          gross_market_value, gross_weight, active_positions
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(strategy_id, ledger, trade_date) DO UPDATE SET
          equity=excluded.equity, cash=excluded.cash,
          gross_market_value=excluded.gross_market_value,
          gross_weight=excluded.gross_weight,
          active_positions=excluded.active_positions
        """, [
            POLICY.strategy_id, ledger, current_date, equity, cash, mv, gross, len(remaining)
        ])

        if ledger == "BASE20":
            state["base_equity"] = equity
            hwm = max(float(state.get("high_water_base", equity)), equity)
            state["high_water_base"] = hwm
            state["max_drawdown_base"] = min(
                float(state.get("max_drawdown_base", 0.0)),
                equity / hwm - 1.0,
            )
        else:
            state["shadow_equity"] = equity


def detect_signals(store, histories, universe_members, current_date, state):
    new_count = 0
    for symbol in universe_members:
        df = histories.get(symbol)
        r20 = ret20_at_close(df, current_date) if df is not None else None
        if r20 is None or r20 > POLICY.ret20_threshold:
            continue
        if symbol_busy(store, symbol):
            continue
        store.con.execute("""
        INSERT INTO forward_signals(
          strategy_id, symbol, signal_date, ret20, status, reason
        ) VALUES (?, ?, ?, ?, 'PENDING_ENTRY', 'RET20_LE_-25PCT')
        ON CONFLICT(strategy_id, symbol, signal_date) DO NOTHING
        """, [POLICY.strategy_id, symbol, current_date, r20])
        new_count += 1
    return new_count


def state_counts(store, state):
    state["active_positions"] = int(store.con.execute("""
    SELECT count(*) FROM forward_positions
    WHERE strategy_id=? AND ledger='BASE20'
    """, [POLICY.strategy_id]).fetchone()[0])
    state["pending_signals"] = int(store.con.execute("""
    SELECT count(*) FROM forward_signals
    WHERE strategy_id=? AND status='PENDING_ENTRY'
    """, [POLICY.strategy_id]).fetchone()[0])
    state["blocked_entries"] = int(store.con.execute("""
    SELECT count(*) FROM forward_signals
    WHERE strategy_id=? AND status='BLOCKED_ENTRY'
    """, [POLICY.strategy_id]).fetchone()[0])
    state["completed_trades"] = int(store.con.execute("""
    SELECT count(*) FROM forward_trades
    WHERE strategy_id=? AND ledger='BASE20'
    """, [POLICY.strategy_id]).fetchone()[0])
    state["total_return_base"] = float(state["base_equity"]) / 1_000_000.0 - 1.0
    state["total_return_shadow"] = float(state["shadow_equity"]) / 1_000_000.0 - 1.0


def _run_main() -> int:
    p = argparse.ArgumentParser(description="CSI500 Deep Oversold frozen forward v1")
    p.add_argument("--db", default=str(DB_PATH))
    p.add_argument("--output-dir", default=str(OUT_DIR))
    p.add_argument("--segment-profile")
    args = p.parse_args()
    segment=None
    from core.qmt_runtime import is_full
    if args.segment_profile or is_full():
        from scripts.deep_forward_segment_v01 import load_profile
        segment=load_profile(args.segment_profile or PROJECT_ROOT/"config/ops/deep_forward_segment_v01.json")
        if datetime.now().strftime("%Y%m%d")<segment["start_date"]:raise SystemExit("DEEP_SEGMENT_NOT_STARTED")
        args.db=segment["segment_db"];args.output_dir=segment["output_dir"]
        if not Path(args.db).is_file():raise SystemExit("DEEP_SEGMENT_NOT_INITIALIZED")

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    save_watchlist_snapshot(out_dir/"protocol.json", asdict(POLICY))

    with ResearchStore(args.db) as store:
        existing_hash = store.policy_hash(POLICY.strategy_id)
        if existing_hash and existing_hash != POLICY.fingerprint():
            raise SystemExit("BLOCKED_POLICY_FINGERPRINT_CHANGED")

        state = store.load_state(POLICY.strategy_id) or initial_state(POLICY)

        if segment is not None:
            from scripts.deep_forward_segment_v01 import require_state
            require_state(segment,state,con=store.con)

        # Load benchmark first; this establishes completed market sessions.
        bench_hist = load_histories([POLICY.benchmark], bars=120, refresh=True).get(POLICY.benchmark)
        if bench_hist is None or bench_hist.empty:
            raise SystemExit("BLOCKED_BENCHMARK_HISTORY_MISSING")
        latest = max(bench_hist.index)
        dates = market_dates(bench_hist)
        new_sessions = [d for d in dates if d > state["last_processed_date"]]

        # Before the first true post-cutoff session, simply arm.
        if latest <= POLICY.historical_cutoff or not new_sessions:
            state_counts(store, state)
            health = public_health(state, POLICY)
            store.save_state(POLICY.strategy_id, POLICY.fingerprint(), state)
            save_watchlist_snapshot(out_dir/"latest.json", health)
            save_watchlist_snapshot(out_dir/"state.json", state)
            print("DEEP OVERSOLD CSI500 FORWARD ARMED")
            print(json.dumps(health, ensure_ascii=False, indent=2))
            return 0

        # True-forward integrity rule:
        # if more than one completed post-cutoff session is pending, an earlier
        # session was missed. Do not reconstruct it later from today's data.
        if len(new_sessions) != 1:
            raise SystemExit(
                "BLOCKED_MISSED_FORWARD_SESSION "
                f"pending_sessions={new_sessions}. "
                "True-forward backfill is forbidden."
            )

        current_date = new_sessions[0]
        today = datetime.now().strftime("%Y%m%d")
        now_hm = datetime.now().strftime("%H%M")

        # The CSI500 PIT snapshot must be observed on the same calendar day as
        # the completed market session. StartWhenAvailable on a later day must
        # not silently backfill a missed observation.
        if current_date != today:
            raise SystemExit(
                f"BLOCKED_FORWARD_SESSION_NOT_TODAY "
                f"session={current_date} today={today}. "
                "True-forward universe reconstruction is forbidden."
            )
        if now_hm < "1505":
            raise SystemExit("BLOCKED_CURRENT_SESSION_NOT_COMPLETED")

        u = current_universe()
        if not u["ok"]:
            raise SystemExit("BLOCKED_CURRENT_UNIVERSE " + json.dumps(u, ensure_ascii=False))

        # Build all external observations before writing anything to DuckDB.
        busy = [
            r[0] for r in store.con.execute("""
            SELECT DISTINCT symbol FROM forward_positions WHERE strategy_id=?
            UNION
            SELECT DISTINCT symbol FROM forward_signals
            WHERE strategy_id=? AND status='PENDING_ENTRY'
            """, [POLICY.strategy_id, POLICY.strategy_id]).fetchall()
        ]
        members = list(u["members"])
        symbols = sorted(set(members) | set(busy) | {POLICY.benchmark})

        # Explicitly refresh the daily cache after the close. Without this, a
        # stale local QMT cache could incorrectly look like "no new session".
        histories = load_histories(symbols, bars=120, refresh=True)
        missing = [sym for sym in symbols if sym not in histories]
        if missing:
            raise SystemExit(
                f"BLOCKED_INCOMPLETE_FORWARD_HISTORY date={current_date} "
                f"missing={len(missing)} sample={missing[:30]}"
            )

        missing_date = [
            sym for sym in symbols
            if current_date not in histories[sym].index
        ]
        if missing_date:
            raise SystemExit(
                f"BLOCKED_FORWARD_DATE_MISSING date={current_date} "
                f"missing={len(missing_date)} sample={missing_date[:30]}"
            )

        observed_bars = bar_rows(histories, current_date)
        if len(observed_bars) != len(symbols):
            raise SystemExit(
                f"BLOCKED_FORWARD_BAR_COUNT date={current_date} "
                f"expected={len(symbols)} actual={len(observed_bars)}"
            )

        # Atomically commit the session. A crash must not leave behind a PIT
        # universe snapshot that could later be mistaken for a completed
        # forward observation.
        state_before = dict(state)
        store.con.execute("BEGIN TRANSACTION")
        try:
            store.upsert_universe(
                "CSI500", current_date, members, "FORWARD_OBSERVED",
                u["source"], dataset_id=None,
            )
            stored_members = store.get_universe(
                "CSI500", current_date, "FORWARD_OBSERVED"
            )
            if len(stored_members) != 500:
                raise RuntimeError(
                    f"BLOCKED_FORWARD_UNIVERSE_COUNT "
                    f"date={current_date} count={len(stored_members)}"
                )

            store.upsert_bars(
                observed_bars,
                dividend_type=POLICY.dividend_type,
                source_system=rq_input.source_label(POLICY.dividend_type),
            )

            # Chronology: T+1 open entries -> close exits/mark -> T-close signals.
            process_entries(store, histories, current_date, state)
            process_exits_and_mark(store, histories, current_date, state)
            detect_signals(store, histories, stored_members, current_date, state)

            if state["started_at"] is None:
                state["started_at"] = current_date
                state["review_not_before"] = review_not_before(
                    current_date, POLICY.review_calendar_months
                )
            state["last_processed_date"] = current_date
            state["last_universe_snapshot"] = current_date
            state["status"] = "COLLECTING"

            bclose = close_price(histories, POLICY.benchmark, current_date)
            store.record_session(
                POLICY.strategy_id, current_date, len(stored_members), u["source"],
                POLICY.benchmark, bclose,
            )

            state_counts(store, state)
            health = public_health(state, POLICY)
            store.save_state(POLICY.strategy_id, POLICY.fingerprint(), state)
            store.con.execute("COMMIT")
        except Exception:
            store.con.execute("ROLLBACK")
            state.clear()
            state.update(state_before)
            raise

        save_watchlist_snapshot(out_dir/"state.json", state)
        save_watchlist_snapshot(out_dir/"latest.json", health)

        print(json.dumps({
            "uses_broker": False,
            "production_db_opened": False,
            "creates_trading_plan": False,
            "can_submit_order": False,
            "writes_sqlite": False,
            "research_store": str(args.db),
            "history_refresh": "EXPLICIT_BATCH_REFRESH",
            "forward_commit": "ATOMIC_SINGLE_SESSION",
        }, ensure_ascii=False, indent=2))
        print("\nDEEP OVERSOLD CSI500 FORWARD v1.1 — OPS HARDENED")
        print(json.dumps(health, ensure_ascii=False, indent=2))
        print("\nDATA:")
        print(f"  universe source={u['source']} count={len(members)}")
        print(f"  DuckDB={args.db}")
        print(f"  last_processed={state['last_processed_date']}")
        print(f"STATE: {out_dir/'state.json'}")
        print(f"LATEST: {out_dir/'latest.json'}")

    return 0

def main() -> int:
    from core.qmt_runtime import is_full
    if not is_full():return _run_main()
    from core.ops_runtime import TaskLock
    from scripts.deep_forward_segment_v01 import load_profile,DEFAULT_PROFILE
    parser=argparse.ArgumentParser(add_help=False);parser.add_argument("--segment-profile")
    args,_=parser.parse_known_args()
    segment=load_profile(args.segment_profile or DEFAULT_PROFILE)
    if datetime.now().strftime("%Y%m%d")<segment["start_date"]:raise SystemExit("DEEP_SEGMENT_NOT_STARTED")
    with TaskLock(Path(segment["segment_root"])/"locks","DEEP_FORWARD",datetime.now().strftime("%Y%m%d")):
        return _run_main()

if __name__ == "__main__":
    raise SystemExit(main())
