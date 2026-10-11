from __future__ import annotations

r"""
PositionX — ALPHA2_INCREMENTAL_VALUE_V0.1

Compare frozen Deep Oversold CSI500 vs EARNINGS_IMPROVING_H60_V0.1 over the
actual common history only.

This is a diversification / incremental-value audit, not an optimizer.

Primary combined diagnostic:
  fixed 50/50 initial-capital sleeves, no periodic rebalance between sleeves.
Each standalone daily return path is rebased to NAV=1 on the common period.
Combined NAV = 0.5 * DeepOversold_NAV + 0.5 * Earnings_NAV.

Why this construction:
- no weight search / optimization;
- keeps combined gross roughly comparable to a 30% strategy budget;
- measures diversification without pretending an exact future portfolio engine
  has already been built.

The audit also reports a "full-size overlay gross" diagnostic:
  gross_deep + gross_earnings
but does NOT call its return path a true shared-equity backtest.

Read-only.
"""

import hashlib
import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(r"D:\positionx_data\research\alpha2_incremental_value_v0_1")
DEEP_ROOT = ROOT / "deep_oversold_export"
DEEP_SUMMARY = DEEP_ROOT / "deep_oversold_export_summary.json"
DEEP_CURVE = DEEP_ROOT / "deep_oversold_20bps_curve.csv"
DEEP_TRADES = DEEP_ROOT / "deep_oversold_20bps_trades.csv"

EARN_ROOT = Path(r"D:\positionx_data\research\earnings_improving_h60_portfolio_v0_1")
EARN_SUMMARY = EARN_ROOT / "portfolio_summary.json"
EARN_CURVE = EARN_ROOT / "equity_curve_20bps.csv"
EARN_TRADES = EARN_ROOT / "trades_20bps.csv"

EXPECTED_DEEP_SCRIPT_SHA256 = "e18792cb9f3676bc515d937fe555cab7d28e4f2e94f918385bace4f54e51e73c"
EXPECTED_EARN_CURVE_SHA256 = "8389e7a3cc77ed91cc3ff0c0c96b46f96e7566f48bf539937a8aef1766cd283f"
EXPECTED_EARN_SUMMARY_SHA256 = "ae24ebf363cb74aca40f0fecf00f3f83afc36bc377a60017f7725787d1d208f8"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def normalize_curve(path: Path, label: str) -> pd.DataFrame:
    x = pd.read_csv(path)
    date_col = next((c for c in ("date", "trade_date", "session") if c in x.columns), None)
    equity_col = next((c for c in ("equity", "base_equity", "nav") if c in x.columns), None)
    gross_col = next((c for c in ("gross_weight", "avg_gross_weight", "gross") if c in x.columns), None)
    active_col = next((c for c in ("active_positions", "positions", "active") if c in x.columns), None)

    if date_col is None or equity_col is None:
        raise RuntimeError(
            f"FAIL_CLOSED {label} curve needs date+equity; columns={list(x.columns)}"
        )

    out = pd.DataFrame({
        "date": pd.to_datetime(x[date_col], errors="coerce"),
        f"{label}_equity": pd.to_numeric(x[equity_col], errors="coerce"),
    })
    if gross_col:
        out[f"{label}_gross"] = pd.to_numeric(x[gross_col], errors="coerce")
    else:
        out[f"{label}_gross"] = np.nan
    if active_col:
        out[f"{label}_active"] = pd.to_numeric(x[active_col], errors="coerce")
    else:
        out[f"{label}_active"] = np.nan

    # Prefer simulator-provided daily return when available, else derive.
    if "daily_return" in x.columns:
        r = pd.to_numeric(x["daily_return"], errors="coerce")
        out[f"{label}_return"] = r
    else:
        out[f"{label}_return"] = out[f"{label}_equity"].pct_change()

    out = out.dropna(subset=["date", f"{label}_equity"]).sort_values("date")
    out = out.drop_duplicates("date", keep="last").reset_index(drop=True)
    return out


def metric_from_returns(dates: pd.Series, r: pd.Series) -> dict:
    z = pd.DataFrame({"date": pd.to_datetime(dates), "r": pd.to_numeric(r, errors="coerce")}).dropna()
    if z.empty:
        raise RuntimeError("empty return path")

    nav = (1.0 + z["r"]).cumprod()
    d0, d1 = z["date"].iloc[0], z["date"].iloc[-1]
    years = max((d1 - d0).days, 1) / 365.25
    cagr = float(nav.iloc[-1] ** (1.0 / years) - 1.0)

    dd = nav / nav.cummax() - 1.0
    mdd = float(dd.min())

    annual_sessions = len(z) / years
    sd = float(z["r"].std(ddof=1))
    sharpe = (
        float(z["r"].mean() / sd * math.sqrt(annual_sessions))
        if sd > 0 else None
    )
    return {
        "sessions": int(len(z)),
        "start": str(d0.date()),
        "end": str(d1.date()),
        "total_return": float(nav.iloc[-1] - 1.0),
        "cagr": cagr,
        "max_drawdown": mdd,
        "sharpe": sharpe,
        "annual_sessions_observed": float(annual_sessions),
        "nav": nav,
        "drawdown": dd,
    }


def normalize_trade_intervals(path: Path, label: str) -> pd.DataFrame:
    x = pd.read_csv(path)
    if "symbol" not in x.columns:
        raise RuntimeError(f"FAIL_CLOSED {label} trades missing symbol")
    entry_col = next((c for c in ("entry_date", "buy_date") if c in x.columns), None)
    exit_col = next((c for c in ("exit_date", "sell_date") if c in x.columns), None)
    if entry_col is None or exit_col is None:
        raise RuntimeError(
            f"FAIL_CLOSED {label} trades missing entry/exit; columns={list(x.columns)}"
        )
    out = pd.DataFrame({
        "symbol": x["symbol"].astype(str),
        "entry_date": pd.to_datetime(x[entry_col], errors="coerce"),
        "exit_date": pd.to_datetime(x[exit_col], errors="coerce"),
    }).dropna()
    out = out[out["exit_date"] >= out["entry_date"]].copy()
    return out


def holding_overlap(deep: pd.DataFrame, earn: pd.DataFrame, dates: pd.Series) -> dict:
    deep_by_day = {}
    earn_by_day = {}
    for d in pd.to_datetime(dates):
        ds = set(
            deep.loc[
                (deep["entry_date"] <= d) & (deep["exit_date"] >= d),
                "symbol",
            ]
        )
        es = set(
            earn.loc[
                (earn["entry_date"] <= d) & (earn["exit_date"] >= d),
                "symbol",
            ]
        )
        deep_by_day[d] = ds
        earn_by_day[d] = es

    rows = []
    same_symbol_days = 0
    total_same_symbol_positions = 0
    for d in pd.to_datetime(dates):
        ds, es = deep_by_day[d], earn_by_day[d]
        inter = ds & es
        union = ds | es
        if inter:
            same_symbol_days += 1
        total_same_symbol_positions += len(inter)
        rows.append({
            "date": d,
            "deep_active_symbols": len(ds),
            "earn_active_symbols": len(es),
            "same_symbol_active": len(inter),
            "symbol_jaccard": (len(inter) / len(union)) if union else 0.0,
        })

    rdf = pd.DataFrame(rows)
    return {
        "days": int(len(rdf)),
        "days_with_same_symbol_active": int(same_symbol_days),
        "fraction_days_with_same_symbol_active": float(same_symbol_days / len(rdf)),
        "mean_same_symbol_active": float(rdf["same_symbol_active"].mean()),
        "max_same_symbol_active": int(rdf["same_symbol_active"].max()),
        "mean_symbol_jaccard": float(rdf["symbol_jaccard"].mean()),
        "daily": rdf,
    }


def main() -> int:
    for p in (DEEP_SUMMARY, DEEP_CURVE, DEEP_TRADES, EARN_SUMMARY, EARN_CURVE, EARN_TRADES):
        if not p.exists():
            raise RuntimeError(f"FAIL_CLOSED missing input: {p}")

    dsum = json.loads(DEEP_SUMMARY.read_text(encoding="utf-8"))
    esum = json.loads(EARN_SUMMARY.read_text(encoding="utf-8"))

    if dsum.get("status") != "PASS_DEEP_OVERSOLD_FROZEN_EXPORT_V0_1":
        raise RuntimeError(f"bad Deep export status={dsum.get('status')}")
    if dsum.get("frozen_script_sha256") != EXPECTED_DEEP_SCRIPT_SHA256:
        raise RuntimeError("Deep frozen script lineage mismatch")

    if sha256_file(EARN_SUMMARY) != EXPECTED_EARN_SUMMARY_SHA256:
        raise RuntimeError("Earnings portfolio_summary hash mismatch")
    if sha256_file(EARN_CURVE) != EXPECTED_EARN_CURVE_SHA256:
        raise RuntimeError("Earnings equity curve hash mismatch")
    if esum.get("standalone_gate", {}).get("status") != "PASS_STANDALONE_PORTFOLIO_GATE":
        raise RuntimeError("Earnings standalone gate is not PASS")

    deep = normalize_curve(DEEP_CURVE, "deep")
    earn = normalize_curve(EARN_CURVE, "earn")

    common = deep.merge(earn, on="date", how="inner")
    if len(common) < 500:
        raise RuntimeError(f"FAIL_CLOSED common sessions too short: {len(common)}")

    # Recompute same-period paths only. No pre-2024 Deep Oversold history gets
    # credit in this comparison.
    common = common.sort_values("date").reset_index(drop=True)
    common["deep_return"] = pd.to_numeric(common["deep_return"], errors="coerce").fillna(0.0)
    common["earn_return"] = pd.to_numeric(common["earn_return"], errors="coerce").fillna(0.0)

    deep_m = metric_from_returns(common["date"], common["deep_return"])
    earn_m = metric_from_returns(common["date"], common["earn_return"])

    common["deep_nav"] = (1.0 + common["deep_return"]).cumprod()
    common["earn_nav"] = (1.0 + common["earn_return"]).cumprod()

    # Fixed initial 50/50 sleeves, no periodic rebalance between the strategies.
    common["combo_nav"] = 0.5 * common["deep_nav"] + 0.5 * common["earn_nav"]
    common["combo_return"] = common["combo_nav"].pct_change()
    common.loc[0, "combo_return"] = common.loc[0, "combo_nav"] - 1.0
    combo_m = metric_from_returns(common["date"], common["combo_return"])

    # Sleeve-weighted gross as sleeve NAVs drift.
    denom = common["combo_nav"]
    deep_sleeve_value = 0.5 * common["deep_nav"]
    earn_sleeve_value = 0.5 * common["earn_nav"]
    common["combo_gross"] = (
        deep_sleeve_value * common["deep_gross"].fillna(0.0)
        + earn_sleeve_value * common["earn_gross"].fillna(0.0)
    ) / denom
    common["full_overlay_gross_diagnostic"] = (
        common["deep_gross"].fillna(0.0)
        + common["earn_gross"].fillna(0.0)
    )

    # Drawdown diagnostics.
    common["deep_dd"] = common["deep_nav"] / common["deep_nav"].cummax() - 1.0
    common["earn_dd"] = common["earn_nav"] / common["earn_nav"].cummax() - 1.0
    common["combo_dd"] = common["combo_nav"] / common["combo_nav"].cummax() - 1.0

    both_underwater = (common["deep_dd"] < 0) & (common["earn_dd"] < 0)
    both_dd2 = (common["deep_dd"] <= -0.02) & (common["earn_dd"] <= -0.02)
    return_corr = float(common["deep_return"].corr(common["earn_return"]))
    dd_corr = float(common["deep_dd"].corr(common["earn_dd"]))

    # Capital overlap.
    deep_active = common["deep_gross"].fillna(0.0) > 0.005
    earn_active = common["earn_gross"].fillna(0.0) > 0.005
    both_capital_active = deep_active & earn_active
    min_gross = np.minimum(
        common["deep_gross"].fillna(0.0),
        common["earn_gross"].fillna(0.0),
    )

    # Trade/symbol holding overlap.
    deep_trades = normalize_trade_intervals(DEEP_TRADES, "deep")
    earn_trades = normalize_trade_intervals(EARN_TRADES, "earn")
    hs = holding_overlap(deep_trades, earn_trades, common["date"])
    holding_daily = hs.pop("daily")

    OUT = ROOT
    OUT.mkdir(parents=True, exist_ok=True)
    common.to_csv(OUT / "common_daily.csv", index=False, encoding="utf-8-sig")
    holding_daily.to_csv(OUT / "holding_overlap_daily.csv", index=False, encoding="utf-8-sig")

    result = {
        "status": "PASS_ALPHA2_INCREMENTAL_VALUE_AUDIT_V0_1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "comparison": {
            "strategy_1": "DEEP_OVERSOLD_CSI500",
            "alpha_2": "EARNINGS_IMPROVING_H60_V0_1",
            "cost_basis": "20bp round-trip each standalone strategy",
            "common_period_only": True,
            "common_start": str(common["date"].min().date()),
            "common_end": str(common["date"].max().date()),
            "common_sessions": int(len(common)),
        },
        "lineage": {
            "deep_frozen_script_sha256": EXPECTED_DEEP_SCRIPT_SHA256,
            "earnings_summary_sha256": EXPECTED_EARN_SUMMARY_SHA256,
            "earnings_curve_sha256": EXPECTED_EARN_CURVE_SHA256,
        },
        "common_period_metrics": {
            "deep_oversold": {k:v for k,v in deep_m.items() if k not in ("nav","drawdown")},
            "earnings_improving_h60": {k:v for k,v in earn_m.items() if k not in ("nav","drawdown")},
            "fixed_50_50_sleeves": {k:v for k,v in combo_m.items() if k not in ("nav","drawdown")},
        },
        "incremental_delta_fixed_50_50": {
            "vs_deep": {
                "cagr": combo_m["cagr"] - deep_m["cagr"],
                "max_drawdown": combo_m["max_drawdown"] - deep_m["max_drawdown"],
                "sharpe": (
                    combo_m["sharpe"] - deep_m["sharpe"]
                    if None not in (combo_m["sharpe"], deep_m["sharpe"]) else None
                ),
            },
            "vs_earnings": {
                "cagr": combo_m["cagr"] - earn_m["cagr"],
                "max_drawdown": combo_m["max_drawdown"] - earn_m["max_drawdown"],
                "sharpe": (
                    combo_m["sharpe"] - earn_m["sharpe"]
                    if None not in (combo_m["sharpe"], earn_m["sharpe"]) else None
                ),
            },
        },
        "correlation_and_drawdown_overlap": {
            "daily_return_correlation": return_corr,
            "drawdown_correlation": dd_corr,
            "both_underwater_days": int(both_underwater.sum()),
            "both_underwater_fraction": float(both_underwater.mean()),
            "both_drawdown_le_2pct_days": int(both_dd2.sum()),
            "both_drawdown_le_2pct_fraction": float(both_dd2.mean()),
        },
        "capital_overlap": {
            "both_active_fraction": float(both_capital_active.mean()),
            "mean_min_gross_weight": float(min_gross.mean()),
            "deep_mean_gross_common": float(common["deep_gross"].mean()),
            "earn_mean_gross_common": float(common["earn_gross"].mean()),
            "fixed_50_50_mean_gross": float(common["combo_gross"].mean()),
            "fixed_50_50_max_gross": float(common["combo_gross"].max()),
            "full_size_overlay_mean_gross_diagnostic": float(
                common["full_overlay_gross_diagnostic"].mean()
            ),
            "full_size_overlay_p95_gross_diagnostic": float(
                common["full_overlay_gross_diagnostic"].quantile(0.95)
            ),
            "full_size_overlay_max_gross_diagnostic": float(
                common["full_overlay_gross_diagnostic"].max()
            ),
        },
        "holding_overlap": hs,
        "interpretation_guardrail": (
            "The fixed 50/50 result is a non-optimized sleeve diversification diagnostic, "
            "not the final Strategy Portfolio engine. Full-size overlay gross is capital "
            "usage only; no exact full-overlay shared-equity return is claimed."
        ),
        "outputs": [
            "incremental_value_summary.json",
            "common_daily.csv",
            "holding_overlap_daily.csv",
        ],
        "no_network": True,
        "no_database_mutation": True,
    }

    write_json_atomic(OUT / "incremental_value_summary.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
