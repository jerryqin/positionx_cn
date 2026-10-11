from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import duckdb
import pandas as pd
from xtquant import xtdata

from research.deep_oversold_replication import (
    OversoldReplicationConfig,
    apply_standard_fillability,
    expand_membership,
    generate_events,
    normalize_history,
    replication_verdict,
    summarize_variant,
)
from research.watchlist_adapter import AdapterConfig, XtDataWatchlistAdapter, save_watchlist_snapshot


REFERENCE_DB = PROJECT_ROOT / "data" / "research" / "positionx_reference.db"
DEFAULT_STATUS = Path(
    "D:/positionx_data/incoming/eva/20260925_foundation_assets/"
    "data/research/px_assets/a_share_daily_trading_status.parquet"
)


def load_reference_intervals() -> tuple[pd.DataFrame, dict]:
    if not REFERENCE_DB.exists():
        raise SystemExit(f"missing reference DB: {REFERENCE_DB}")
    with sqlite3.connect(REFERENCE_DB) as conn:
        intervals = pd.read_sql_query(
            """
            SELECT symbol, valid_from, valid_to_exclusive
            FROM csi300_membership_interval
            ORDER BY symbol, valid_from
            """,
            conn,
        )
        registry_cols = [
            r[1] for r in conn.execute("PRAGMA table_info(dataset_registry)").fetchall()
        ]
        meta = {"registry_columns": registry_cols}
        if registry_cols:
            rows = conn.execute("SELECT * FROM dataset_registry").fetchall()
            meta["registry_rows"] = [list(r) for r in rows[-5:]]
    if intervals.empty:
        raise SystemExit("canonical PIT intervals are empty")
    return intervals, meta


def refresh_symbol(symbol: str, start: str, end: str):
    try:
        xtdata.download_history_data(
            symbol, period="1d", start_time=start, end_time=end
        )
    except TypeError:
        xtdata.download_history_data(symbol, "1d", start, end)


def load_status_for_events(parquet_path: Path, events: pd.DataFrame) -> pd.DataFrame:
    if not parquet_path.exists():
        raise SystemExit(f"missing fillability parquet: {parquet_path}")

    keys = events[["entry_date", "symbol"]].drop_duplicates().copy()
    if keys.empty:
        return pd.DataFrame()

    con = duckdb.connect()
    try:
        con.register("keys", keys)
        path = str(parquet_path).replace("\\", "/").replace("'", "''")
        q = f"""
        SELECT
          strftime(CAST(s.trade_date AS DATE), '%Y%m%d') AS trade_date,
          s.symbol,
          s.is_suspended,
          s.new_listing_special,
          s.can_buy_at_open,
          s.st_known
        FROM read_parquet('{path}') s
        INNER JOIN keys k
          ON strftime(CAST(s.trade_date AS DATE), '%Y%m%d') = k.entry_date
         AND s.symbol = k.symbol
        """
        return con.execute(q).fetchdf()
    finally:
        con.close()


def main() -> int:
    p = argparse.ArgumentParser(
        description="PositionX Edge Candidate #1 — Deep Oversold Replication Gate v1"
    )
    p.add_argument("--start-date", default="20200803")
    p.add_argument("--end-date", default="20260924")
    p.add_argument("--benchmark", default="000300.SH")
    p.add_argument("--refresh-history", action="store_true")
    p.add_argument("--status-parquet", default=str(DEFAULT_STATUS))
    p.add_argument(
        "--output",
        default="data/research/deep_oversold_replication_latest.json",
    )
    args = p.parse_args()

    cfg = OversoldReplicationConfig()
    cfg.validate()
    intervals, registry_meta = load_reference_intervals()
    symbols = sorted(intervals["symbol"].astype(str).unique().tolist())

    # Reuse the same tested adapter as PIT/Edge Discovery. 2000 bars covers
    # the full research window plus the 20-day lookback.
    if args.refresh_history:
        dl_start = (
            datetime.strptime(args.start_date, "%Y%m%d") - timedelta(days=120)
        ).strftime("%Y%m%d")
        for i, symbol in enumerate(symbols + [args.benchmark], start=1):
            print(f"[DATA] refresh {i}/{len(symbols)+1} {symbol}")
            try:
                refresh_symbol(symbol, dl_start, args.end_date)
            except Exception as exc:
                print(f"[DATA] warning {symbol}: {type(exc).__name__}: {exc}")

    adapter = XtDataWatchlistAdapter(
        xtdata,
        AdapterConfig(sector="", bars=2000, dividend_type="front"),
    )
    history = adapter.history(symbols + [args.benchmark])
    history = {
        s: normalize_history(df)
        for s, df in history.items()
        if df is not None and not df.empty
    }

    if args.benchmark not in history:
        raise SystemExit(f"benchmark history missing: {args.benchmark}")
    benchmark = history.pop(args.benchmark)
    missing = sorted(s for s in symbols if s not in history)
    if missing:
        raise SystemExit(
            f"BLOCKED_INCOMPLETE_HISTORY missing={len(missing)} sample={missing[:20]}"
        )

    market_dates = [
        d for d in benchmark.index
        if args.start_date <= d <= args.end_date
    ]
    if len(market_dates) < 500:
        raise SystemExit(f"insufficient market calendar: {len(market_dates)} sessions")

    membership = expand_membership(intervals, market_dates)
    counts = membership.groupby("signal_date")["symbol"].nunique()
    bad = counts[(counts < 295) | (counts > 305)]
    if len(bad):
        raise SystemExit(
            "BLOCKED_PIT_MEMBERSHIP_COUNT "
            + json.dumps(bad.head(20).to_dict(), ensure_ascii=False)
        )

    events = generate_events(history, benchmark, membership, market_dates, cfg)
    if events.empty:
        raise SystemExit("no oversold events generated")

    complete = events[events["complete_outcome"] == True].copy()
    status = load_status_for_events(Path(args.status_parquet), complete)
    screened = apply_standard_fillability(complete, status)

    matched = int(screened["status_found"].sum())
    if matched == 0:
        sample_keys = complete[["entry_date","symbol"]].head(10).to_dict("records")
        raise SystemExit(
            "BLOCKED_STATUS_JOIN_ZERO_MATCH: status parquet date-key join failed. "
            f"sample_event_keys={sample_keys}"
        )
    fillable = screened[screened["standard_fillable"] == True].copy()
    if fillable.empty:
        raise SystemExit(
            f"BLOCKED_ALL_EVENTS_UNFILLABLE: matched_status_rows={matched} "
            f"complete_events={len(complete)}"
        )

    open_summary = summarize_variant(fillable, "open", cfg)
    close_summary = summarize_variant(fillable, "close", cfg)
    verdict = replication_verdict(open_summary)

    paired_delta = {
        "events": int(len(fillable)),
        "open_minus_close_avg_net_20bps": float(
            (fillable["open_net_20bps"] - fillable["close_net_20bps"]).mean()
        ),
        "open_minus_close_avg_excess": float(
            (fillable["open_excess_gross"] - fillable["close_excess_gross"]).mean()
        ),
    }

    safety = {
        "uses_broker": False,
        "production_db_opened": False,
        "creates_trading_plan": False,
        "can_submit_order": False,
        "writes_reference_db": False,
        "changes_strategy_parameters": False,
    }

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "DEEP_OVERSOLD_REPLICATION_V1",
        "status": "OK",
        "safety": safety,
        "hypothesis": {
            "name": "DEEP_OVERSOLD_REVERSAL",
            "frozen_event": "adjusted close(T)/close(T-20)-1 <= -25%",
            "pit_universe": "CSI300 canonical PIT membership on signal date",
            "main_entry": "exact T+1 market-session open",
            "eva_comparator_entry": "exact T+1 market-session close",
            "exit": "exact signal+20 market-session close",
            "no_extra_filters": True,
            "no_stop_loss": True,
            "no_regime_filter": True,
            "no_parameter_sweep": True,
            "note": (
                "The close-entry variant is a paired methodology comparator to Eva. "
                "The open-entry variant is the executable main hypothesis."
            ),
        },
        "input": {
            "start_date": args.start_date,
            "end_date": args.end_date,
            "benchmark": args.benchmark,
            "pit_union_symbols": len(symbols),
            "history_loaded": len(history),
            "market_sessions": len(market_dates),
            "reference_registry": registry_meta,
            "status_parquet": str(Path(args.status_parquet)),
        },
        "counts": {
            "raw_events": int(len(events)),
            "complete_outcome_events": int(len(complete)),
            "status_matched": int(screened["status_found"].sum()),
            "standard_fillable_events": int(len(fillable)),
            "standard_blocked_events": int(
                len(screened) - screened["standard_fillable"].sum()
            ),
            "unique_signal_dates_fillable": int(fillable["signal_date"].nunique()),
        },
        "open_entry_main": open_summary,
        "close_entry_eva_comparator": close_summary,
        "paired_entry_timing": paired_delta,
        "verdict": verdict,
        "interpretation_rule": (
            "REPLICATED_CORE means the open-entry executable variant has positive "
            "mean excess return, positive 5%-trimmed excess return, and positive "
            "average excess in at least 2 of 3 predeclared time folds. It does NOT "
            "mean the strategy is ready for portfolio use."
        ),
    }

    out_path = PROJECT_ROOT / args.output
    out_path.parent.mkdir(parents=True, exist_ok=True)
    save_watchlist_snapshot(out_path, out)

    events_path = out_path.with_name("deep_oversold_replication_events_latest.csv")
    cols = [
        "symbol", "signal_date", "entry_date", "exit_date", "signal_ret20",
        "standard_fillable", "open_gross_return", "open_excess_gross",
        "open_net_20bps", "open_net_50bps", "close_gross_return",
        "close_excess_gross", "close_net_20bps", "close_net_50bps",
        "st_known",
    ]
    screened[[c for c in cols if c in screened.columns]].to_csv(
        events_path, index=False, encoding="utf-8-sig"
    )

    print(json.dumps(safety, ensure_ascii=False, indent=2))
    print("\nDEEP OVERSOLD REPLICATION v1")
    print(
        f"event: ret20 <= {cfg.threshold_20d:.0%} | "
        f"PIT symbols={len(symbols)} | sessions={len(market_dates)}"
    )
    print(
        f"raw={len(events)} complete={len(complete)} "
        f"fillable={len(fillable)} blocked={len(screened)-len(fillable)}"
    )

    def show(name, s):
        a = s["excess_gross"]
        b = s["net_20bps"]
        c = s["net_50bps"]
        print(f"\n{name}")
        print(
            f"  excess: n={a['n']} avg={a['avg']:+.4%} "
            f"median={a['median']:+.4%} win={a['win_rate']:.2%} "
            f"trim5={a['trimmed_mean_5pct']:+.4%}"
        )
        print(
            f"  net20 : avg={b['avg']:+.4%} median={b['median']:+.4%} "
            f"PF={b['profit_factor']}"
        )
        print(
            f"  net50 : avg={c['avg']:+.4%} median={c['median']:+.4%} "
            f"PF={c['profit_factor']}"
        )
        for f in s["folds"]:
            print(
                f"  {f['fold']} events={f['events']} "
                f"avg_excess={f['avg_excess']:+.4%} "
                f"avg_net20={f['avg_net_20bps']:+.4%}"
            )

    show("A) T+1 OPEN — EXECUTABLE MAIN", open_summary)
    show("B) T+1 CLOSE — EVA COMPARATOR", close_summary)

    print("\nPAIRED ENTRY TIMING")
    print(
        f"  open-close avg net20 = "
        f"{paired_delta['open_minus_close_avg_net_20bps']:+.4%}"
    )
    print(
        f"  open-close avg excess = "
        f"{paired_delta['open_minus_close_avg_excess']:+.4%}"
    )

    print("\nVERDICT")
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    print("\nNOTE: no stop, PB, low-volume, low-volatility or regime filter was added.")
    print(f"\nJSON: {out_path}")
    print(f"CSV : {events_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
