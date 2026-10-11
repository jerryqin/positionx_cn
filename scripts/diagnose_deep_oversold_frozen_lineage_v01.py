from __future__ import annotations

r"""
PositionX — Deep Oversold Frozen Lineage Diagnostic V0.1

Read-only diagnosis for:
  frozen expected trades = 1365
  current replay trades  = 1360

Goals:
1) prove the frozen runner itself is unchanged;
2) identify whether current event keys drift from previously persisted frozen artifacts;
3) inspect existing persisted equity/trade artifacts that may already contain the exact
   frozen 1365-trade 20bp curve, so Alpha2 comparison can use immutable evidence instead
   of re-deriving history from mutable QMT front-adjusted data.

No refresh/download. No broker/order/DB/catalog mutation.
"""

import hashlib
import importlib.util
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER = PROJECT_ROOT / "scripts" / "run_deep_oversold_csi500_universe.py"
EXPECTED_RUNNER_SHA256 = "e18792cb9f3676bc515d937fe555cab7d28e4f2e94f918385bace4f54e51e73c"

FROZEN_JSON = PROJECT_ROOT / "data/research/deep_oversold_csi500_universe_latest.json"
FROZEN_EVENTS = PROJECT_ROOT / "data/research/deep_oversold_replication_events_latest.csv"

RET120_ROOT = PROJECT_ROOT / "data/research/edge_discovery/ret120_shared_equity_v01"
CANDIDATE_CURVE = RET120_ROOT / "csi500_20bp_equity_curve.csv"
CANDIDATE_TRADES = RET120_ROOT / "csi500_20bp_trades.csv"
CANDIDATE_AUDIT = RET120_ROOT / "audit.json"

OUT_ROOT = Path(r"D:\positionx_data\research\alpha2_incremental_value_v0_1\lineage_diagnostic")

EXPECTED_TRADES = 1365
EXPECTED_CAGR = 0.11039928725435111
EXPECTED_MDD = -0.09624091257275003
EXPECTED_SHARPE = 1.042

KEY = ["symbol", "signal_date", "entry_date", "exit_date"]


def sha256_file(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def hash_dir_manifest(root: Path) -> dict:
    rows = []
    if not root.exists():
        return {"exists": False, "files": 0, "manifest_sha256": None}
    for p in sorted(x for x in root.rglob("*") if x.is_file()):
        rows.append((str(p.relative_to(root)).replace("\\", "/"), sha256_file(p), p.stat().st_size))
    payload = "\n".join(f"{a}|{b}|{c}" for a,b,c in rows).encode("utf-8")
    return {
        "exists": True,
        "files": len(rows),
        "manifest_sha256": hashlib.sha256(payload).hexdigest(),
        "sample": rows[:10],
    }


def load_runner():
    if not RUNNER.exists():
        raise RuntimeError(f"missing runner: {RUNNER}")
    actual = sha256_file(RUNNER)
    if actual != EXPECTED_RUNNER_SHA256:
        raise RuntimeError(
            f"FAIL_CLOSED runner hash mismatch expected={EXPECTED_RUNNER_SHA256} actual={actual}"
        )
    spec = importlib.util.spec_from_file_location("px_deep_diag_runner", str(RUNNER))
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load runner")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod, actual


def normalize_key_df(df: pd.DataFrame) -> pd.DataFrame | None:
    if not all(c in df.columns for c in KEY):
        return None
    x = df[KEY].copy()
    x["symbol"] = x["symbol"].astype(str)
    for c in ("signal_date", "entry_date", "exit_date"):
        x[c] = (
            x[c].astype(str)
            .str.replace("-", "", regex=False)
            .str.replace(".0", "", regex=False)
            .str.slice(0, 8)
        )
    return x.drop_duplicates().sort_values(KEY).reset_index(drop=True)


def metric_from_curve(path: Path) -> dict:
    if not path.exists():
        return {"exists": False}
    x = pd.read_csv(path)
    out = {
        "exists": True,
        "path": str(path),
        "sha256": sha256_file(path),
        "rows": int(len(x)),
        "columns": list(x.columns),
    }

    date_col = next((c for c in ("date","trade_date","session") if c in x.columns), None)
    eq_col = next((c for c in ("equity","nav","base_equity") if c in x.columns), None)
    if date_col is None or eq_col is None or len(x) < 2:
        return out

    d = pd.to_datetime(x[date_col], errors="coerce")
    eq = pd.to_numeric(x[eq_col], errors="coerce")
    z = pd.DataFrame({"date": d, "equity": eq}).dropna().sort_values("date")
    if len(z) < 2:
        return out

    total = float(z["equity"].iloc[-1] / z["equity"].iloc[0] - 1.0)
    years = max((z["date"].iloc[-1] - z["date"].iloc[0]).days, 1) / 365.25
    cagr = float((z["equity"].iloc[-1] / z["equity"].iloc[0]) ** (1.0/years) - 1.0)
    dd = z["equity"] / z["equity"].cummax() - 1.0
    ret = z["equity"].pct_change().dropna()
    ann = (len(z)-1) / years
    sharpe = None
    if len(ret) > 1 and float(ret.std(ddof=1)) > 0:
        sharpe = float(ret.mean() / ret.std(ddof=1) * np.sqrt(ann))

    out["computed"] = {
        "start": str(z["date"].iloc[0].date()),
        "end": str(z["date"].iloc[-1].date()),
        "total_return": total,
        "cagr": cagr,
        "max_drawdown": float(dd.min()),
        "sharpe": sharpe,
    }
    out["matches_frozen_metrics_roughly"] = bool(
        abs(cagr - EXPECTED_CAGR) <= 0.001
        and abs(float(dd.min()) - EXPECTED_MDD) <= 0.001
        and (sharpe is None or abs(sharpe - EXPECTED_SHARPE) <= 0.05)
    )
    return out


def inspect_trades(path: Path) -> dict:
    if not path.exists():
        return {"exists": False}
    x = pd.read_csv(path)
    return {
        "exists": True,
        "path": str(path),
        "sha256": sha256_file(path),
        "rows": int(len(x)),
        "columns": list(x.columns),
        "row_count_matches_1365": int(len(x)) == EXPECTED_TRADES,
    }


def replay_current(m):
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
        raise RuntimeError(f"BLOCKED_INCOMPLETE_LOCAL_HISTORY missing={len(missing)} sample={missing[:30]}")

    benchmark = loaded.pop(m.CSI500.expected_index)
    market_dates = [d for d in benchmark.index if start <= d <= end]

    cfg = m.OversoldReplicationConfig()
    events = m.generate_events(loaded, benchmark, membership, market_dates, cfg)
    complete = events[events["complete_outcome"] == True].copy()
    status = m.load_status_for_events(m.STATUS_PARQUET, complete)
    screened = m.apply_standard_fillability(complete, status)
    fillable = screened[screened["standard_fillable"] == True].copy()
    dedup = m.dedup_while_held(fillable)

    return {
        "symbols": len(symbols),
        "market_sessions": len(market_dates),
        "raw": events,
        "complete": complete,
        "fillable": fillable,
        "dedup": dedup,
    }


def main() -> int:
    m, runner_sha = load_runner()
    current = replay_current(m)

    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    frozen_json_info = {"exists": FROZEN_JSON.exists()}
    if FROZEN_JSON.exists():
        frozen_json_info["sha256"] = sha256_file(FROZEN_JSON)
        j = json.loads(FROZEN_JSON.read_text(encoding="utf-8"))
        frozen_json_info["status"] = j.get("status")
        frozen_json_info["counts"] = j.get("counts")
        frozen_json_info["base_20bps"] = (j.get("base_shared_equity") or {}).get("20bps")

    frozen_events_info = {"exists": FROZEN_EVENTS.exists()}
    diff_info = {"comparable": False}
    if FROZEN_EVENTS.exists():
        fe = pd.read_csv(FROZEN_EVENTS)
        frozen_events_info.update({
            "sha256": sha256_file(FROZEN_EVENTS),
            "rows": int(len(fe)),
            "columns": list(fe.columns),
        })
        fk = normalize_key_df(fe)
        ck = normalize_key_df(current["dedup"])
        if fk is not None and ck is not None:
            diff_info["comparable"] = True
            frozen_set = set(map(tuple, fk[KEY].to_numpy()))
            current_set = set(map(tuple, ck[KEY].to_numpy()))
            frozen_only = sorted(frozen_set - current_set)
            current_only = sorted(current_set - frozen_set)

            rows = []
            for r in frozen_only:
                rows.append(dict(zip(KEY, r)) | {"side": "FROZEN_ONLY"})
            for r in current_only:
                rows.append(dict(zip(KEY, r)) | {"side": "CURRENT_ONLY"})
            pd.DataFrame(rows).to_csv(
                OUT_ROOT / "current_vs_frozen_event_diff.csv",
                index=False, encoding="utf-8-sig",
            )
            diff_info.update({
                "frozen_unique_keys": len(frozen_set),
                "current_unique_keys": len(current_set),
                "frozen_only": len(frozen_only),
                "current_only": len(current_only),
                "frozen_only_sample": frozen_only[:20],
                "current_only_sample": current_only[:20],
                "diff_csv": str(OUT_ROOT / "current_vs_frozen_event_diff.csv"),
            })

    candidate_curve = metric_from_curve(CANDIDATE_CURVE)
    candidate_trades = inspect_trades(CANDIDATE_TRADES)

    status_info = {
        "path": str(m.STATUS_PARQUET),
        "exists": m.STATUS_PARQUET.exists(),
        "sha256": sha256_file(m.STATUS_PARQUET) if m.STATUS_PARQUET.exists() else None,
    }
    pit_manifest = hash_dir_manifest(m.ACCEPTED_DIR)

    diagnosis = {
        "status": "PASS_DEEP_OVERSOLD_FROZEN_LINEAGE_DIAGNOSTIC_V0_1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "runner": {
            "path": str(RUNNER),
            "sha256": runner_sha,
            "unchanged": True,
        },
        "current_replay": {
            "symbols": current["symbols"],
            "market_sessions": current["market_sessions"],
            "raw_events": int(len(current["raw"])),
            "complete_events": int(len(current["complete"])),
            "fillable_events": int(len(current["fillable"])),
            "dedup_events": int(len(current["dedup"])),
            "expected_frozen_dedup": EXPECTED_TRADES,
            "drift": int(len(current["dedup"])) - EXPECTED_TRADES,
        },
        "persisted_frozen_json": frozen_json_info,
        "persisted_frozen_events": frozen_events_info,
        "current_vs_persisted_event_keys": diff_info,
        "candidate_existing_curve": candidate_curve,
        "candidate_existing_trades": candidate_trades,
        "source_fingerprints_now": {
            "status_parquet": status_info,
            "accepted_csi500_dir": pit_manifest,
            "qmt_history_note": (
                "QMT front-adjusted history is read dynamically via dividend_type='front'; "
                "this diagnostic cannot compare it with a historical hash because the original "
                "runner did not persist a market-history manifest."
            ),
        },
        "decision_hints": {
            "use_existing_frozen_curve_if": (
                "candidate_existing_curve.matches_frozen_metrics_roughly=true AND "
                "candidate_existing_trades.row_count_matches_1365=true"
            ),
            "otherwise": (
                "Use current_vs_frozen_event_diff to identify the exact five-event drift before "
                "building any Alpha2 comparison. Do not relax expected=1365 to 1360."
            ),
        },
        "safety": {
            "network_download_called": False,
            "refresh_history_called": False,
            "uses_broker": False,
            "production_db_opened": False,
            "database_mutation": False,
        },
    }

    out = OUT_ROOT / "deep_frozen_lineage_diagnostic.json"
    write = json.dumps(diagnosis, ensure_ascii=False, indent=2, default=str)
    out.write_text(write, encoding="utf-8")
    print(write)
    print(f"OUTPUT: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
