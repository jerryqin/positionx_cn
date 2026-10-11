from __future__ import annotations

r"""
PositionX — Deep Oversold frozen export for Alpha #2 Incremental Value V0.1

This script does NOT modify the frozen Deep Oversold implementation.
It imports the exact reviewed:
  D:\positionx_min_live\scripts\run_deep_oversold_csi500_universe.py

and replays its frozen CSI500 pipeline with:
  ret20 <= -25%
  T close -> T+1 exact OPEN -> S20 CLOSE
  STANDARD_OPEN_V1
  first signal while flat
  1% target / 30% max gross
  20bp round-trip

No --refresh-history is exposed. Local/QMT cached history only.
"""

import hashlib
import importlib.util
import json
import os
from datetime import datetime
from pathlib import Path
import sys

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TARGET = PROJECT_ROOT / "scripts" / "run_deep_oversold_csi500_universe.py"
OUT_ROOT = Path(r"D:\positionx_data\research\alpha2_incremental_value_v0_1\deep_oversold_export")

EXPECTED_TARGET_SHA256 = "e18792cb9f3676bc515d937fe555cab7d28e4f2e94f918385bace4f54e51e73c"

START_DATE = "20200803"
END_DATE = "20260924"
EXPECTED_TRADES = 1365
EXPECTED_CAGR = 0.11039928725435111
EXPECTED_MDD = -0.09624091257275003


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def write_json_atomic(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def load_target():
    if not TARGET.exists():
        raise RuntimeError(f"FAIL_CLOSED missing frozen target: {TARGET}")
    actual = sha256_file(TARGET)
    if actual != EXPECTED_TARGET_SHA256:
        raise RuntimeError(
            "FAIL_CLOSED frozen Deep Oversold script hash mismatch "
            f"expected={EXPECTED_TARGET_SHA256} actual={actual}"
        )
    spec = importlib.util.spec_from_file_location("px_deep_oversold_csi500_frozen", str(TARGET))
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load frozen target")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod, actual


def to_df(obj) -> pd.DataFrame:
    if isinstance(obj, pd.DataFrame):
        return obj.copy()
    if isinstance(obj, list):
        return pd.DataFrame(obj)
    if obj is None:
        return pd.DataFrame()
    return pd.DataFrame(obj)


def main() -> int:
    m, target_sha = load_target()

    # Exact frozen pipeline, intentionally without any download/refresh path.
    daily = m.load_accepted_daily(m.ACCEPTED_DIR, m.CSI500)
    membership = m.event_membership(daily, START_DATE, END_DATE)
    symbols = sorted(membership["symbol"].unique().tolist())

    adapter = m.XtDataWatchlistAdapter(
        m.xtdata,
        m.AdapterConfig(sector="", bars=2000, dividend_type="front"),
    )
    loaded = adapter.history(symbols + [m.CSI500.expected_index])
    loaded = {
        s: m.normalize_history(df)
        for s, df in loaded.items()
        if df is not None and not df.empty
    }

    missing = [s for s in symbols + [m.CSI500.expected_index] if s not in loaded]
    if missing:
        raise RuntimeError(
            f"BLOCKED_INCOMPLETE_LOCAL_HISTORY missing={len(missing)} sample={missing[:30]}. "
            "This export does not refresh/download history."
        )

    benchmark = loaded.pop(m.CSI500.expected_index)
    market_dates = [d for d in benchmark.index if START_DATE <= d <= END_DATE]
    if len(market_dates) < 500:
        raise RuntimeError(f"BLOCKED_SHORT_CSI500_CALENDAR sessions={len(market_dates)}")

    cfg = m.OversoldReplicationConfig()
    events = m.generate_events(loaded, benchmark, membership, market_dates, cfg)
    complete = events[events["complete_outcome"] == True].copy()

    status = m.load_status_for_events(m.STATUS_PARQUET, complete)
    screened = m.apply_standard_fillability(complete, status)
    if int(screened["status_found"].sum()) == 0:
        raise RuntimeError("BLOCKED_STATUS_JOIN_ZERO_MATCH")
    fillable = screened[screened["standard_fillable"] == True].copy()
    if fillable.empty:
        raise RuntimeError("no fillable CSI500 events")

    dedup = m.dedup_while_held(fillable)
    sim_dates = [
        d for d in market_dates
        if min(dedup["entry_date"]) <= d <= max(dedup["exit_date"])
    ]

    sim = m.simulate_shared_equity(
        events=dedup,
        histories=loaded,
        market_dates=sim_dates,
        profile=m.BASE_PROFILE,
        round_trip_cost=0.002,
    )
    compact = m.compact_result(sim)

    # Protect frozen lineage: do not proceed if today's replay no longer matches
    # the known frozen economics within tiny numerical tolerance.
    if int(compact["trades"]) != EXPECTED_TRADES:
        raise RuntimeError(
            f"FAIL_CLOSED frozen trade count drift expected={EXPECTED_TRADES} actual={compact['trades']}"
        )
    if abs(float(compact["cagr"]) - EXPECTED_CAGR) > 0.00002:
        raise RuntimeError(
            "FAIL_CLOSED frozen CAGR drift "
            f"expected≈{EXPECTED_CAGR} actual={compact['cagr']}"
        )
    if abs(float(compact["max_drawdown"]) - EXPECTED_MDD) > 0.00002:
        raise RuntimeError(
            "FAIL_CLOSED frozen MDD drift "
            f"expected≈{EXPECTED_MDD} actual={compact['max_drawdown']}"
        )

    curve = to_df(sim.get("curve"))
    trades = to_df(sim.get("trades"))

    if curve.empty or trades.empty:
        raise RuntimeError(
            f"FAIL_CLOSED simulator export empty curve={len(curve)} trades={len(trades)}"
        )

    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    curve_path = OUT_ROOT / "deep_oversold_20bps_curve.csv"
    trades_path = OUT_ROOT / "deep_oversold_20bps_trades.csv"
    events_path = OUT_ROOT / "deep_oversold_dedup_events.csv"

    curve.to_csv(curve_path, index=False, encoding="utf-8-sig")
    trades.to_csv(trades_path, index=False, encoding="utf-8-sig")
    dedup.to_csv(events_path, index=False, encoding="utf-8-sig")

    summary = {
        "status": "PASS_DEEP_OVERSOLD_FROZEN_EXPORT_V0_1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "frozen_script": str(TARGET),
        "frozen_script_sha256": target_sha,
        "start_date": START_DATE,
        "end_date": END_DATE,
        "counts": {
            "pit_symbols": len(symbols),
            "market_sessions": len(market_dates),
            "raw_events": int(len(events)),
            "fillable_events": int(len(fillable)),
            "dedup_events": int(len(dedup)),
            "sim_trades": int(compact["trades"]),
        },
        "frozen_metrics_20bps": compact,
        "export": {
            "curve": str(curve_path),
            "curve_rows": int(len(curve)),
            "curve_columns": list(curve.columns),
            "trades": str(trades_path),
            "trade_rows": int(len(trades)),
            "trade_columns": list(trades.columns),
            "dedup_events": str(events_path),
        },
        "safety": {
            "refresh_history_exposed": False,
            "network_download_called": False,
            "uses_broker": False,
            "production_db_opened": False,
            "creates_trading_plan": False,
            "can_submit_order": False,
            "writes_reference_db": False,
            "changes_strategy_parameters": False,
        },
    }
    write_json_atomic(OUT_ROOT / "deep_oversold_export_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
