from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from research.pilot_capital_floor_v02 import (
    CapitalFloorV02Policy, board_of, diagnostics_by_group,
    empirical_floor_from_required, evaluate_nearest_sensitivity, evaluate_official,
    guaranteed_required_total_capital, policy_dict, summarize_point,
)

POLICY = CapitalFloorV02Policy()
CATALOG = Path(r"D:\positionx_data\catalog\market_data.duckdb")
RET_TRADES = Path(r"D:\positionx_data\research\ret120_vs_52wh_family_v0_2_frozen_signal\ret120_trades_20bps.csv")
DEEP_TRADES = Path(r"D:\positionx_data\research\alpha2_incremental_value_v0_1\deep_oversold_export\deep_oversold_20bps_trades.csv")
V01 = Path(r"D:\positionx_data\research\pilot_live_readiness\capital_floor_v0_1\capital_floor_summary_v01.json")
OUT = Path(r"D:\positionx_data\research\pilot_live_readiness\capital_floor_v0_2")


def write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_keys(path: Path, strategy: str) -> pd.DataFrame:
    if not path.exists():
        raise RuntimeError(f"FAIL_CLOSED missing trades {path}")
    x = pd.read_csv(path, dtype=str)
    need = {"symbol", "entry_date"}
    if not need.issubset(x.columns):
        raise RuntimeError(f"FAIL_CLOSED {strategy} trades missing {sorted(need-set(x.columns))}")
    x = x[["symbol", "entry_date"]].copy()
    x["symbol"] = x["symbol"].astype(str)
    x["entry_date"] = x["entry_date"].astype(str).str.replace("-", "", regex=False).str[:8]
    x["strategy"] = strategy
    return x.drop_duplicates(["symbol", "entry_date"]).reset_index(drop=True)


def attach_raw_open(keys: pd.DataFrame) -> pd.DataFrame:
    if not CATALOG.exists():
        raise RuntimeError(f"FAIL_CLOSED missing catalog {CATALOG}")
    import duckdb
    con = duckdb.connect(str(CATALOG), read_only=True)
    try:
        con.register("keys", keys)
        q = con.execute("""
          SELECT k.strategy,k.symbol,k.entry_date,b.open AS entry_open
          FROM keys k
          LEFT JOIN market.daily_bars_v1 b
            ON b.symbol=k.symbol
           AND replace(CAST(b.trade_date AS VARCHAR),'-','')=k.entry_date
        """).fetchdf()
    finally:
        con.close()
    q["entry_open"] = pd.to_numeric(q["entry_open"], errors="coerce")
    q["board"] = q["symbol"].map(board_of)
    return q


def strategy_summary(z: pd.DataFrame, key: str, error_col: str, blocked_col: str | None = None) -> dict[str, Any]:
    if key == "combined":
        q = z
    elif key == "deep":
        q = z[z.strategy == "DEEP_OVERSOLD"]
    else:
        q = z[z.strategy == "RET120"]
    return summarize_point(q, error_col=error_col, blocked_col=blocked_col)


def check() -> int:
    out = {
        "status": "PASS_PILOT_CAPITAL_FLOOR_V0_2_CHECK",
        "inputs": {
            "catalog": str(CATALOG), "catalog_exists": CATALOG.exists(),
            "deep_trades": str(DEEP_TRADES), "deep_trades_exists": DEEP_TRADES.exists(),
            "ret120_trades": str(RET_TRADES), "ret120_trades_exists": RET_TRADES.exists(),
            "v01_summary_exists": V01.exists(),
        },
        "policy": policy_dict(POLICY),
        "important_boundary": "Official G5 sizing remains round-down/never exceed target. Nearest-lot +/-25% is diagnostic only and cannot promote G5 without a separately frozen policy plus economic replay.",
        "no_qmt": True, "no_broker": True, "catalog_read_only": True,
    }
    if not (CATALOG.exists() and DEEP_TRADES.exists() and RET_TRADES.exists()):
        out["status"] = "BLOCKED_INPUT_MISSING"
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["status"].startswith("PASS_") else 3


def run() -> int:
    ret = load_keys(RET_TRADES, "RET120")
    deep = load_keys(DEEP_TRADES, "DEEP_OVERSOLD")
    keys = pd.concat([deep, ret], ignore_index=True)
    px = attach_raw_open(keys)
    missing = px[px.entry_open.isna() | (px.entry_open <= 0)].copy()
    valid = px[px.entry_open.notna() & (px.entry_open > 0)].copy()
    missing_rate = len(missing) / max(1, len(px))
    if missing_rate > 0.01:
        raise RuntimeError(f"FAIL_CLOSED raw OPEN missing >1% rows={len(missing)}/{len(px)}")

    frac = POLICY.portfolio_target_fraction
    valid["guaranteed_required_total_capital"] = valid.apply(
        lambda r: guaranteed_required_total_capital(
            float(r.entry_open), str(r.board), frac, POLICY.max_relative_target_error
        ), axis=1
    )

    robust = {}
    for label, q in (
        ("combined", valid),
        ("deep", valid[valid.strategy == "DEEP_OVERSOLD"]),
        ("ret120", valid[valid.strategy == "RET120"]),
    ):
        required = pd.to_numeric(q["guaranteed_required_total_capital"], errors="coerce")
        robust[label] = {
            "n": int(len(q)),
            "robust_floor_for_90pct_fidelity": empirical_floor_from_required(required, POLICY.fidelity_min),
            "required_capital_median": float(required.median()) if len(q) else None,
            "required_capital_p90": float(required.quantile(.90)) if len(q) else None,
            "required_capital_p95": float(required.quantile(.95)) if len(q) else None,
            "required_capital_max": float(required.max()) if len(q) else None,
        }

    point_results = {}
    detail_rows = []
    sensitivity_rows = []
    for cap in POLICY.point_capitals:
        official_rows = []
        nearest_rows = []
        for r in valid.itertuples(index=False):
            sw = POLICY.deep_sleeve_weight if r.strategy == "DEEP_OVERSOLD" else POLICY.ret120_sleeve_weight
            off = evaluate_official(float(r.entry_open), cap, sw, POLICY.strategy_target_weight,
                                    str(r.board), POLICY.max_relative_target_error)
            row = {"capital": cap, "strategy": r.strategy, "symbol": r.symbol,
                   "entry_date": r.entry_date, "entry_open": float(r.entry_open), "board": r.board, **off}
            official_rows.append(row); detail_rows.append(row)

            sen = evaluate_nearest_sensitivity(float(r.entry_open), cap, sw, POLICY.strategy_target_weight,
                                               str(r.board), POLICY.max_relative_target_error)
            srow = {"capital": cap, "strategy": r.strategy, "symbol": r.symbol,
                    "entry_date": r.entry_date, "entry_open": float(r.entry_open), "board": r.board, **sen}
            nearest_rows.append(srow); sensitivity_rows.append(srow)

        odf = pd.DataFrame(official_rows)
        ndf = pd.DataFrame(nearest_rows)
        point_results[str(cap)] = {
            "official_round_down": {
                k: strategy_summary(odf, k, "relative_underweight", "blocked_one_lot")
                for k in ("combined", "deep", "ret120")
            },
            "nearest_lot_sensitivity_DIAGNOSTIC_ONLY": {
                k: strategy_summary(ndf, k, "absolute_relative_error", None)
                for k in ("combined", "deep", "ret120")
            },
        }

    observed_g5_pass_capitals = []
    for cap in POLICY.point_capitals:
        x = point_results[str(cap)]["official_round_down"]
        if x["deep"]["fidelity"] >= POLICY.fidelity_min and x["ret120"]["fidelity"] >= POLICY.fidelity_min:
            observed_g5_pass_capitals.append(cap)

    v01 = {}
    if V01.exists():
        try: v01 = json.loads(V01.read_text(encoding="utf-8-sig"))
        except Exception: v01 = {}

    result = {
        "status": "PASS_PILOT_CAPITAL_FLOOR_FORENSICS_V0_2",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "policy": policy_dict(POLICY),
        "official_g5_rule_unchanged": {
            "sizing": "round down / never exceed target",
            "fidelity_pass": "shares>0 and relative underweight<=25%",
            "required": "Deep>=90% AND RET120>=90%",
        },
        "inputs": {
            "deep_trade_keys": int(len(deep)), "ret120_trade_keys": int(len(ret)),
            "joined": int(len(px)), "valid_open": int(len(valid)), "missing_open": int(len(missing)),
            "missing_open_fraction": float(missing_rate),
        },
        "v01_reference": {
            "found": bool(v01),
            "minimum_tested_capital_both_ge_90pct": v01.get("minimum_tested_capital_both_ge_90pct") if v01 else None,
        },
        "diagnostics": diagnostics_by_group(valid),
        "robust_monotone_floor": robust,
        "point_results": point_results,
        "observed_official_g5_pass_capitals_in_grid": observed_g5_pass_capitals,
        "minimum_observed_official_g5_pass_capital_in_grid": min(observed_g5_pass_capitals) if observed_g5_pass_capitals else None,
        "nearest_lot_sensitivity_boundary": "DIAGNOSTIC_ONLY. It may exceed target by up to 25%; do not use it to PASS G5 unless a new policy is frozen BEFORE an economic lot-size replay.",
        "interpretation": "Use robust_monotone_floor to understand structural capital needs. Point fidelity can wiggle with lot boundaries, so a single passing capital point is not by itself a stable floor.",
        "no_qmt": True, "no_broker": True, "catalog_read_only": True,
    }

    OUT.mkdir(parents=True, exist_ok=True)
    valid.to_csv(OUT / "capital_requirement_by_trade.csv", index=False)
    missing.to_csv(OUT / "missing_open_trade_keys.csv", index=False)
    pd.DataFrame(detail_rows).to_csv(OUT / "official_point_fidelity_detail.csv", index=False)
    pd.DataFrame(sensitivity_rows).to_csv(OUT / "nearest_lot_sensitivity_detail.csv", index=False)
    write_json_atomic(OUT / "capital_floor_forensics_v02.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("OUTPUT:", OUT)
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["check", "run"])
    a = p.parse_args()
    try:
        return check() if a.command == "check" else run()
    except Exception as exc:
        print(json.dumps({"status": "FAIL_CLOSED", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False, indent=2))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
