from __future__ import annotations

r"""
PositionX — Deep Oversold Frozen Export V0.1.3 (economic-equivalence gate)

Fixes a lineage mistake in V0.1:
- 1365 is the frozen *executed portfolio trade count*, not the deduplicated event count.
- the frozen runner JSON says first-signal-while-flat events = 1468.
- use the persisted 1468-row frozen dedup event artifact as the signal lineage,
  then replay the unchanged frozen simulator against current local QMT history.

This separates:
  signal-lineage drift
from:
  market-history / mark-to-market drift.

No download/refresh. Read-only source data.
"""

import hashlib
import importlib.util
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER = PROJECT_ROOT / "scripts" / "run_deep_oversold_csi500_universe.py"
FROZEN_JSON = PROJECT_ROOT / "data/research/deep_oversold_csi500_universe_latest.json"

# Exact persisted 1468-row first-signal-while-flat artifact found by the prior
# PositionX provenance audit.
FROZEN_DEDUP = PROJECT_ROOT / "data/research/deep_oversold_csi500_regime_events_v1.parquet"
FROZEN_DEDUP_CROSSCHECK = (
    PROJECT_ROOT
    / "data/research/edge_discovery/deep_oversold_trade_lineage_v03"
    / "exit_then_signal_accepted.parquet"
)

OUT_ROOT = Path(r"D:\positionx_data\research\alpha2_incremental_value_v0_1\deep_oversold_export")
DIAG_ROOT = Path(r"D:\positionx_data\research\alpha2_incremental_value_v0_1\lineage_v011")

EXPECTED_RUNNER_SHA256 = "e18792cb9f3676bc515d937fe555cab7d28e4f2e94f918385bace4f54e51e73c"
EXPECTED_FROZEN_DEDUP_ROWS = 1468
EXPECTED_EXECUTED_TRADES = 1365
EXPECTED_CAGR = 0.11039928725435111
EXPECTED_MDD = -0.09624091257275003
EXPECTED_SHARPE = 1.0418115217631059

KEY = ["symbol", "signal_date", "entry_date", "exit_date"]


def sha256_file(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def qpath(p: Path) -> str:
    return str(p).replace("\\", "/").replace("'", "''")


def write_json_atomic(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def load_runner():
    if not RUNNER.exists():
        raise RuntimeError(f"FAIL_CLOSED runner missing: {RUNNER}")
    actual = sha256_file(RUNNER)
    if actual != EXPECTED_RUNNER_SHA256:
        raise RuntimeError(
            f"FAIL_CLOSED runner hash mismatch expected={EXPECTED_RUNNER_SHA256} actual={actual}"
        )
    spec = importlib.util.spec_from_file_location("px_deep_frozen_v011", str(RUNNER))
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load frozen runner")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod, actual


def read_parquet(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise RuntimeError(f"FAIL_CLOSED parquet missing: {path}")
    con = duckdb.connect(":memory:")
    try:
        return con.execute(
            f"SELECT * FROM read_parquet('{qpath(path)}')"
        ).df()
    finally:
        con.close()


def norm_keys(df: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in KEY if c not in df.columns]
    if missing:
        raise RuntimeError(f"FAIL_CLOSED key columns missing={missing}")
    x = df[KEY].copy()
    x["symbol"] = x["symbol"].astype(str)
    for c in KEY[1:]:
        s = x[c].astype(str)
        s = s.str.replace("-", "", regex=False)
        s = s.str.replace(".0", "", regex=False)
        x[c] = s.str.slice(0, 8)
    return x.drop_duplicates().sort_values(KEY).reset_index(drop=True)


def load_current_pipeline(m):
    start, end = "20200803", "20260924"
    daily = m.load_accepted_daily(m.ACCEPTED_DIR, m.CSI500)
    membership = m.event_membership(daily, start, end)
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
            f"BLOCKED_INCOMPLETE_LOCAL_HISTORY missing={len(missing)} sample={missing[:30]}"
        )

    benchmark = loaded.pop(m.CSI500.expected_index)
    market_dates = [d for d in benchmark.index if start <= d <= end]
    cfg = m.OversoldReplicationConfig()

    events = m.generate_events(loaded, benchmark, membership, market_dates, cfg)
    complete = events[events["complete_outcome"] == True].copy()
    status = m.load_status_for_events(m.STATUS_PARQUET, complete)
    screened = m.apply_standard_fillability(complete, status)
    fillable = screened[screened["standard_fillable"] == True].copy()
    current_dedup = m.dedup_while_held(fillable)

    return {
        "loaded": loaded,
        "benchmark": benchmark,
        "market_dates": market_dates,
        "raw": events,
        "fillable": fillable,
        "dedup": current_dedup,
    }


def compare_event_keys(frozen: pd.DataFrame, current: pd.DataFrame) -> dict:
    fk = norm_keys(frozen)
    ck = norm_keys(current)
    fs = set(map(tuple, fk[KEY].to_numpy()))
    cs = set(map(tuple, ck[KEY].to_numpy()))
    frozen_only = sorted(fs - cs)
    current_only = sorted(cs - fs)

    rows = []
    for r in frozen_only:
        rows.append(dict(zip(KEY, r)) | {"side": "FROZEN_ONLY"})
    for r in current_only:
        rows.append(dict(zip(KEY, r)) | {"side": "CURRENT_ONLY"})
    DIAG_ROOT.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=KEY+["side"]).to_csv(
        DIAG_ROOT / "current_vs_frozen_dedup_diff.csv",
        index=False,
        encoding="utf-8-sig",
    )
    return {
        "frozen_keys": len(fs),
        "current_keys": len(cs),
        "intersection": len(fs & cs),
        "frozen_only": len(frozen_only),
        "current_only": len(current_only),
        "frozen_only_sample": frozen_only[:30],
        "current_only_sample": current_only[:30],
        "diff_csv": str(DIAG_ROOT / "current_vs_frozen_dedup_diff.csv"),
    }


def compare_crosscheck(frozen: pd.DataFrame) -> dict:
    if not FROZEN_DEDUP_CROSSCHECK.exists():
        return {"exists": False}
    x = read_parquet(FROZEN_DEDUP_CROSSCHECK)
    fk = set(map(tuple, norm_keys(frozen)[KEY].to_numpy()))
    xk = set(map(tuple, norm_keys(x)[KEY].to_numpy()))
    return {
        "exists": True,
        "path": str(FROZEN_DEDUP_CROSSCHECK),
        "sha256": sha256_file(FROZEN_DEDUP_CROSSCHECK),
        "rows": int(len(x)),
        "key_count": int(len(xk)),
        "same_key_set_as_primary_frozen_dedup": fk == xk,
        "primary_only": len(fk - xk),
        "crosscheck_only": len(xk - fk),
    }


def event_price_drift(frozen: pd.DataFrame, histories: dict) -> dict:
    cols = set(frozen.columns)
    required = {"symbol","entry_date","exit_date","entry_open","exit_close"}
    if not required.issubset(cols):
        return {
            "available": False,
            "reason": f"required columns missing={sorted(required-cols)}",
        }

    rows = []
    checked = 0
    for r in frozen.itertuples(index=False):
        sym = str(getattr(r, "symbol"))
        entry = str(getattr(r, "entry_date")).replace("-","")[:8]
        exit_ = str(getattr(r, "exit_date")).replace("-","")[:8]
        if sym not in histories:
            continue
        h = histories[sym]
        if entry not in h.index or exit_ not in h.index:
            continue
        try:
            old_open = float(getattr(r, "entry_open"))
            old_close = float(getattr(r, "exit_close"))
            new_open = float(h.loc[entry, "open"])
            new_close = float(h.loc[exit_, "close"])
        except Exception:
            continue
        checked += 1

        def rel(a,b):
            return abs(a-b) / max(abs(a), abs(b), 1.0)

        ro = rel(old_open,new_open)
        rc = rel(old_close,new_close)
        old_ret = old_close / old_open - 1.0 if old_open > 0 else np.nan
        new_ret = new_close / new_open - 1.0 if new_open > 0 else np.nan
        rr = abs(old_ret-new_ret) if np.isfinite(old_ret) and np.isfinite(new_ret) else np.nan

        if ro > 1e-10 or rc > 1e-10 or (np.isfinite(rr) and rr > 1e-10):
            rows.append({
                "symbol": sym,
                "entry_date": entry,
                "exit_date": exit_,
                "old_entry_open": old_open,
                "current_entry_open": new_open,
                "old_exit_close": old_close,
                "current_exit_close": new_close,
                "entry_rel_diff": ro,
                "exit_rel_diff": rc,
                "old_gross_return": old_ret,
                "current_gross_return": new_ret,
                "gross_return_abs_diff": rr,
            })

    drift = pd.DataFrame(rows)
    DIAG_ROOT.mkdir(parents=True, exist_ok=True)
    path = DIAG_ROOT / "frozen_event_price_drift.csv"
    drift.to_csv(path, index=False, encoding="utf-8-sig")
    return {
        "available": True,
        "checked_events": checked,
        "changed_price_events": int(len(drift)),
        "changed_gross_return_gt_1bp": int(
            (drift["gross_return_abs_diff"] > 0.0001).sum()
        ) if len(drift) else 0,
        "changed_gross_return_gt_10bp": int(
            (drift["gross_return_abs_diff"] > 0.001).sum()
        ) if len(drift) else 0,
        "max_gross_return_abs_diff": (
            float(drift["gross_return_abs_diff"].max()) if len(drift) else 0.0
        ),
        "csv": str(path),
    }


def replay_frozen_events(m, frozen: pd.DataFrame, loaded: dict, market_dates: list):
    sim_dates = [
        d for d in market_dates
        if min(frozen["entry_date"]) <= d <= max(frozen["exit_date"])
    ]
    sim = m.simulate_shared_equity(
        events=frozen,
        histories=loaded,
        market_dates=sim_dates,
        profile=m.BASE_PROFILE,
        round_trip_cost=0.002,
    )
    compact = m.compact_result(sim)
    return sim, compact


def main() -> int:
    m, runner_sha = load_runner()

    if not FROZEN_JSON.exists():
        raise RuntimeError(f"FAIL_CLOSED frozen JSON missing: {FROZEN_JSON}")
    frozen_json = json.loads(FROZEN_JSON.read_text(encoding="utf-8"))
    frozen_counts = frozen_json.get("counts") or {}
    frozen_metrics = (frozen_json.get("base_shared_equity") or {}).get("20bps") or {}

    if int(frozen_counts.get("first_signal_while_flat_events") or 0) != EXPECTED_FROZEN_DEDUP_ROWS:
        raise RuntimeError(
            "FAIL_CLOSED frozen JSON dedup count is not 1468 "
            f"actual={frozen_counts.get('first_signal_while_flat_events')}"
        )
    if int(frozen_metrics.get("trades") or 0) != EXPECTED_EXECUTED_TRADES:
        raise RuntimeError("FAIL_CLOSED frozen JSON portfolio trades is not 1365")

    frozen = read_parquet(FROZEN_DEDUP)
    if len(frozen) != EXPECTED_FROZEN_DEDUP_ROWS:
        raise RuntimeError(
            f"FAIL_CLOSED persisted frozen dedup rows expected=1468 actual={len(frozen)}"
        )

    current = load_current_pipeline(m)
    key_diff = compare_event_keys(frozen, current["dedup"])
    crosscheck = compare_crosscheck(frozen)
    price_drift = event_price_drift(frozen, current["loaded"])

    sim, compact = replay_frozen_events(
        m, frozen, current["loaded"], current["market_dates"]
    )

    # Sensitivity replay using today's regenerated 1470-event lineage. This does
    # not replace the frozen strategy; it only tells us whether the tiny 4/6-key
    # signal drift changes the portfolio-level conclusion.
    current_sim_dates = [
        d for d in current["market_dates"]
        if min(current["dedup"]["entry_date"]) <= d <= max(current["dedup"]["exit_date"])
    ]
    current_sim = m.simulate_shared_equity(
        events=current["dedup"],
        histories=current["loaded"],
        market_dates=current_sim_dates,
        profile=m.BASE_PROFILE,
        round_trip_cost=0.002,
    )
    current_compact = m.compact_result(current_sim)

    metric_delta = {
        "trades": int(compact["trades"]) - EXPECTED_EXECUTED_TRADES,
        "cagr": float(compact["cagr"]) - EXPECTED_CAGR,
        "max_drawdown": float(compact["max_drawdown"]) - EXPECTED_MDD,
        "sharpe": float(compact["sharpe"]) - EXPECTED_SHARPE,
    }

    exact_reproduction = bool(
        int(compact["trades"]) == EXPECTED_EXECUTED_TRADES
        and abs(metric_delta["cagr"]) <= 0.00002
        and abs(metric_delta["max_drawdown"]) <= 0.00002
        and abs(metric_delta["sharpe"]) <= 0.002
    )

    # The exact frozen market-data snapshot was not persisted. Current QMT
    # front-adjusted history has a small number of retrospective changes.
    # For the diversification audit we allow a separately labelled economic-
    # equivalence path, with deliberately tight tolerances:
    #   trade-count drift <= 1
    #   CAGR / MDD drift <= 1bp absolute
    #   Sharpe drift <= 0.005
    #   frozen 1468-event lineage must itself be exact and cross-checked.
    economic_equivalence = bool(
        crosscheck.get("same_key_set_as_primary_frozen_dedup") is True
        and abs(int(compact["trades"]) - EXPECTED_EXECUTED_TRADES) <= 1
        and abs(metric_delta["cagr"]) <= 0.0001
        and abs(metric_delta["max_drawdown"]) <= 0.0001
        and abs(metric_delta["sharpe"]) <= 0.005
    )

    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    curve = sim.get("curve")
    trades_obj = sim.get("trades")
    trades_rows_obj = sim.get("trades_df")

    if isinstance(curve, pd.DataFrame):
        curve_df = curve.copy()
    elif isinstance(curve, (list, tuple, dict)):
        curve_df = pd.DataFrame(curve)
    else:
        raise RuntimeError(
            f"FAIL_CLOSED unsupported sim['curve'] type={type(curve).__name__}"
        )

    # In the frozen Deep Oversold simulator, sim["trades"] is the executed trade
    # COUNT (int), not a row-level trade table. V0.1.1 incorrectly attempted to
    # pass that integer into pd.DataFrame(), which caused:
    #   ValueError: DataFrame constructor not properly called!
    #
    # Do not invent executed trade rows. Export the exact frozen 1468-row dedup
    # event lineage separately and let the incremental-value audit use it only as
    # a conservative same-symbol holding-overlap UPPER BOUND.
    exact_trade_rows_available = False
    if isinstance(trades_rows_obj, pd.DataFrame):
        trades_df = trades_rows_obj.copy()
        exact_trade_rows_available = True
    elif isinstance(trades_rows_obj, (list, tuple)):
        trades_df = pd.DataFrame(trades_rows_obj)
        exact_trade_rows_available = True
    elif isinstance(trades_obj, pd.DataFrame):
        trades_df = trades_obj.copy()
        exact_trade_rows_available = True
    elif isinstance(trades_obj, (list, tuple)):
        trades_df = pd.DataFrame(trades_obj)
        exact_trade_rows_available = True
    else:
        trades_df = pd.DataFrame()

    # Always preserve the replay evidence. It becomes the Alpha2 comparison input
    # only if exact_enough is true.
    curve_path = OUT_ROOT / "deep_oversold_20bps_curve.csv"
    trades_path = OUT_ROOT / "deep_oversold_20bps_trades.csv"
    current_curve_path = OUT_ROOT / "deep_oversold_current_regenerated_20bps_curve.csv"
    frozen_csv_path = OUT_ROOT / "deep_oversold_frozen_dedup_events.csv"
    frozen_path = OUT_ROOT / "deep_oversold_frozen_dedup_events.parquet"

    curve_df.to_csv(curve_path, index=False, encoding="utf-8-sig")
    if exact_trade_rows_available and not trades_df.empty:
        trades_df.to_csv(trades_path, index=False, encoding="utf-8-sig")
    elif trades_path.exists():
        trades_path.unlink()
    frozen.to_csv(frozen_csv_path, index=False, encoding="utf-8-sig")

    current_curve_obj = current_sim.get("curve")
    current_curve_df = (
        current_curve_obj.copy()
        if isinstance(current_curve_obj, pd.DataFrame)
        else pd.DataFrame(current_curve_obj)
    )
    current_curve_df.to_csv(
        current_curve_path, index=False, encoding="utf-8-sig"
    )

    con = duckdb.connect(":memory:")
    try:
        con.register("_frozen", frozen)
        con.execute(
            f"COPY _frozen TO '{qpath(frozen_path)}' "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )
    finally:
        con.close()

    if exact_reproduction:
        status = "PASS_DEEP_OVERSOLD_EXACT_REPLAY_V0_1_3"
    elif economic_equivalence:
        status = "PASS_DEEP_OVERSOLD_ECONOMIC_EQUIVALENCE_V0_1_3"
    else:
        status = "HOLD_DEEP_OVERSOLD_MARKET_INPUT_DRIFT_V0_1_3"

    summary = {
        "status": status,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "runner": {
            "path": str(RUNNER),
            "sha256": runner_sha,
            "unchanged": True,
        },
        "frozen_lineage": {
            "frozen_json": str(FROZEN_JSON),
            "frozen_json_sha256": sha256_file(FROZEN_JSON),
            "frozen_dedup": str(FROZEN_DEDUP),
            "frozen_dedup_sha256": sha256_file(FROZEN_DEDUP),
            "frozen_dedup_rows": int(len(frozen)),
            "crosscheck": crosscheck,
            "frozen_expected_portfolio_metrics": {
                "trades": EXPECTED_EXECUTED_TRADES,
                "cagr": EXPECTED_CAGR,
                "max_drawdown": EXPECTED_MDD,
                "sharpe": EXPECTED_SHARPE,
            },
        },
        "current_signal_generation": {
            "raw_events": int(len(current["raw"])),
            "fillable_events": int(len(current["fillable"])),
            "dedup_events": int(len(current["dedup"])),
            "key_diff_vs_frozen_1468": key_diff,
        },
        "market_history_drift": price_drift,
        "frozen_event_replay_on_current_local_history": {
            "metrics": compact,
            "delta_vs_frozen": metric_delta,
            "exact_reproduction": exact_reproduction,
            "economic_equivalence_for_alpha2": economic_equivalence,
            "economic_equivalence_tolerances": {
                "trade_count_abs_delta_max": 1,
                "cagr_abs_delta_max": 0.0001,
                "max_drawdown_abs_delta_max": 0.0001,
                "sharpe_abs_delta_max": 0.005,
            },
        },
        "current_regenerated_lineage_sensitivity": {
            "metrics": current_compact,
            "delta_vs_frozen_event_replay": {
                "trades": int(current_compact["trades"]) - int(compact["trades"]),
                "cagr": float(current_compact["cagr"]) - float(compact["cagr"]),
                "max_drawdown": float(current_compact["max_drawdown"]) - float(compact["max_drawdown"]),
                "sharpe": float(current_compact["sharpe"]) - float(compact["sharpe"]),
            },
            "curve": str(current_curve_path),
        },
        "sim_output_shape": {
            "keys": sorted(str(k) for k in sim.keys()),
            "types": {str(k): type(v).__name__ for k, v in sim.items()},
            "trades_df_rows": (
                int(len(trades_rows_obj))
                if isinstance(trades_rows_obj, pd.DataFrame)
                else None
            ),
            "sim_trades_value": (
                int(trades_obj)
                if isinstance(trades_obj, (int, np.integer))
                else None
            ),
            "exact_trade_rows_available": bool(exact_trade_rows_available),
        },
        "exports": {
            "curve": str(curve_path),
            "curve_rows": int(len(curve_df)),
            "exact_trade_rows_available": bool(exact_trade_rows_available),
            "trades": (
                str(trades_path)
                if exact_trade_rows_available and not trades_df.empty
                else None
            ),
            "trade_rows": int(len(trades_df)),
            "frozen_dedup_csv": str(frozen_csv_path),
            "frozen_dedup_copy": str(frozen_path),
            "current_regenerated_curve": str(current_curve_path),
            "holding_overlap_note": (
                "If exact trade rows are unavailable, frozen dedup event intervals "
                "are used only as a conservative upper-bound proxy for same-symbol "
                "holding overlap; return/capital metrics use the exact replay curve."
            ),
        },
        "next": (
            "RUN_ALPHA2_INCREMENTAL_VALUE_V0_1_3"
            if (exact_reproduction or economic_equivalence)
            else "STOP_AND_INSPECT_MARKET_INPUT_DRIFT"
        ),
        "safety": {
            "refresh_history_called": False,
            "download_called": False,
            "uses_broker": False,
            "production_db_opened": False,
            "database_mutation": False,
            "frozen_runner_modified": False,
        },
    }
    write_json_atomic(OUT_ROOT / "deep_oversold_export_summary_v013.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if (exact_reproduction or economic_equivalence) else 3


if __name__ == "__main__":
    raise SystemExit(main())
