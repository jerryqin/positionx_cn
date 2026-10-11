from __future__ import annotations

import argparse
import importlib.util
import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = (
    ROOT / "data" / "research" / "edge_discovery"
    / "strategy2_tsmom_fast_reject_v01"
)

ORIGIN_CANDIDATES = [
    ROOT / "scripts" / "run_deep_oversold_csi500_universe.py",
    ROOT / "payload" / "scripts" / "run_deep_oversold_csi500_universe.py",
]

SYMBOL = "510500.SH"
START_DATE = "20200803"
END_DATE = "20260924"
LOOKBACK = 250

ROUND_TRIP_MAIN = 0.002
ROUND_TRIP_SHADOW = 0.005


def finite(x: Any) -> bool:
    try:
        return bool(np.isfinite(float(x)))
    except (TypeError, ValueError):
        return False


def normalize_date(v: Any) -> str | None:
    if pd.isna(v):
        return None
    s = "".join(ch for ch in str(v) if ch.isdigit())
    if len(s) < 8:
        return None
    d = s[:8]
    try:
        datetime.strptime(d, "%Y%m%d")
    except ValueError:
        return None
    return d


def choose_origin_script() -> Path:
    existing = [p for p in ORIGIN_CANDIDATES if p.exists()]
    if not existing:
        raise FileNotFoundError(
            "run_deep_oversold_csi500_universe.py not found."
        )
    return existing[0]


def load_origin_module():
    path = choose_origin_script()
    spec = importlib.util.spec_from_file_location(
        "positionx_origin_for_tsmom",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not import original script: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, path


def load_history(origin, refresh: bool) -> pd.DataFrame:
    if refresh:
        dl_start = (
            datetime.strptime(START_DATE, "%Y%m%d")
            - timedelta(days=500)
        ).strftime("%Y%m%d")
        if not hasattr(origin, "refresh_symbol"):
            raise RuntimeError(
                "Original infrastructure does not expose refresh_symbol()."
            )
        origin.refresh_symbol(SYMBOL, dl_start, END_DATE)

    adapter = origin.XtDataWatchlistAdapter(
        origin.xtdata,
        origin.AdapterConfig(
            sector="",
            bars=2600,
            dividend_type="front",
        ),
    )
    loaded = adapter.history([SYMBOL])
    if SYMBOL not in loaded or loaded[SYMBOL] is None or loaded[SYMBOL].empty:
        raise RuntimeError(
            f"Missing QMT history for {SYMBOL}. "
            "Retry with --refresh-history."
        )

    df = origin.normalize_history(loaded[SYMBOL]).copy()
    df.index = [normalize_date(x) for x in df.index]
    df = df[~pd.isna(df.index)]
    df = df[~pd.Index(df.index).duplicated(keep="last")].sort_index()

    for c in ("open", "close"):
        if c not in df.columns:
            raise RuntimeError(f"{SYMBOL} history missing {c!r}.")
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df[
        df["open"].map(finite)
        & df["close"].map(finite)
    ].copy()

    if len(df) < 800:
        raise RuntimeError(
            f"Insufficient {SYMBOL} history rows={len(df)}; "
            "need enough pre-start lookback and evaluation history."
        )

    return df


def month_end_dates(eval_dates: list[str]) -> list[str]:
    if not eval_dates:
        return []
    s = pd.Series(eval_dates, dtype="object")
    month = s.str[:6]
    out = []
    for _, g in s.groupby(month, sort=True):
        out.append(str(g.iloc[-1]))
    return out


def build_decisions(df: pd.DataFrame) -> pd.DataFrame:
    all_dates = list(df.index)
    pos = {d: i for i, d in enumerate(all_dates)}

    eval_dates = [
        d for d in all_dates
        if START_DATE <= d <= END_DATE
    ]
    me = month_end_dates(eval_dates)

    rows = []
    for signal_date in me:
        i = pos[signal_date]
        if i < LOOKBACK:
            continue
        if i + 1 >= len(all_dates):
            continue

        ref_date = all_dates[i - LOOKBACK]
        signal_close = float(df.at[signal_date, "close"])
        ref_close = float(df.at[ref_date, "close"])
        desired_long = bool(signal_close > ref_close)

        rows.append({
            "signal_date": signal_date,
            "entry_date": all_dates[i + 1],
            "reference_date": ref_date,
            "signal_close": signal_close,
            "reference_close_250": ref_close,
            "tsmom_250": signal_close / ref_close - 1.0,
            "desired_long": desired_long,
        })

    out = pd.DataFrame(rows)
    if out.empty:
        raise RuntimeError("No eligible month-end TSMOM decisions.")
    return out


def simulate(
    df: pd.DataFrame,
    decisions: pd.DataFrame,
    round_trip_cost: float,
    buy_hold: bool,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """
    Long/cash portfolio, no leverage, no shorting.

    round_trip_cost is split equally across entry and exit so a completed
    long->cash round trip pays exactly the frozen total cost.
    """
    one_way = round_trip_cost / 2.0

    decision_map = {
        r.entry_date: bool(r.desired_long)
        for r in decisions.itertuples(index=False)
    }

    first_exec = str(decisions.iloc[0]["entry_date"])
    dates = [
        d for d in df.index
        if first_exec <= d <= END_DATE
    ]
    if len(dates) < 500:
        raise RuntimeError(
            f"Evaluation span too short: {len(dates)} sessions."
        )

    cash = 1.0
    shares = 0.0
    long_state = False
    switches = 0
    turnover_one_way = 0.0
    rows = []

    for j, d in enumerate(dates):
        op = float(df.at[d, "open"])
        cl = float(df.at[d, "close"])

        if buy_hold:
            desired = True
        else:
            desired = decision_map.get(d, long_state)

        if desired != long_state:
            if desired:
                # Buy at OPEN, pay one-way cost.
                investable = cash * (1.0 - one_way)
                shares = investable / op
                cash = 0.0
                long_state = True
            else:
                # Sell at OPEN, pay one-way cost.
                cash = shares * op * (1.0 - one_way)
                shares = 0.0
                long_state = False

            switches += 1
            turnover_one_way += 1.0

        equity = shares * cl if long_state else cash
        rows.append({
            "date": d,
            "equity": float(equity),
            "long": int(long_state),
            "close": cl,
        })

    # Terminal liquidation at the final close so both strategy and benchmark
    # carry a complete round-trip cost convention.
    if long_state:
        last = rows[-1]
        final_close = float(last["close"])
        final_cash = shares * final_close * (1.0 - one_way)
        last["equity"] = float(final_cash)
        last["long"] = 0
        cash = final_cash
        shares = 0.0
        long_state = False
        switches += 1
        turnover_one_way += 1.0

    curve = pd.DataFrame(rows)
    curve["daily_return"] = curve["equity"].pct_change().fillna(0.0)

    years = (
        datetime.strptime(curve.iloc[-1]["date"], "%Y%m%d")
        - datetime.strptime(curve.iloc[0]["date"], "%Y%m%d")
    ).days / 365.25

    final_equity = float(curve.iloc[-1]["equity"])
    cagr = (
        final_equity ** (1.0 / years) - 1.0
        if years > 0 and final_equity > 0
        else np.nan
    )

    peak = curve["equity"].cummax()
    dd = curve["equity"] / peak - 1.0
    max_dd = float(dd.min())

    dr = curve["daily_return"].iloc[1:]
    vol = float(dr.std(ddof=1))
    sharpe = (
        float(np.sqrt(252.0) * dr.mean() / vol)
        if vol > 0
        else np.nan
    )

    calmar = (
        float(cagr / abs(max_dd))
        if finite(cagr) and max_dd < 0
        else np.nan
    )

    metrics = {
        "round_trip_cost": round_trip_cost,
        "one_way_cost": one_way,
        "start_date": str(curve.iloc[0]["date"]),
        "end_date": str(curve.iloc[-1]["date"]),
        "sessions": int(len(curve)),
        "years": float(years),
        "final_equity": final_equity,
        "total_return": final_equity - 1.0,
        "cagr": float(cagr),
        "max_drawdown": max_dd,
        "sharpe": sharpe,
        "calmar": calmar,
        "avg_long_exposure": float(curve["long"].mean()),
        "switches_including_terminal_liquidation": int(switches),
        "one_way_turnover": float(turnover_one_way),
        "annualized_one_way_turnover": (
            float(turnover_one_way / years)
            if years > 0
            else np.nan
        ),
    }
    return curve, metrics


def slice_metrics(curve: pd.DataFrame, start: str, end: str) -> dict[str, Any]:
    x = curve[
        (curve["date"] >= start)
        & (curve["date"] <= end)
    ].copy()
    if len(x) < 2:
        return {
            "sessions": int(len(x)),
            "return": None,
            "max_drawdown": None,
            "sharpe": None,
        }

    equity = x["equity"] / float(x.iloc[0]["equity"])
    ret = float(equity.iloc[-1] - 1.0)
    dd = equity / equity.cummax() - 1.0
    dr = x["daily_return"].iloc[1:]
    vol = float(dr.std(ddof=1))
    sharpe = (
        float(np.sqrt(252.0) * dr.mean() / vol)
        if vol > 0
        else np.nan
    )
    return {
        "sessions": int(len(x)),
        "return": ret,
        "max_drawdown": float(dd.min()),
        "sharpe": sharpe,
    }


def fold_comparison(
    origin,
    strat_curve: pd.DataFrame,
    bh_curve: pd.DataFrame,
) -> list[dict[str, Any]]:
    rows = []
    for name, start, end in origin.FOLDS:
        s = slice_metrics(strat_curve, str(start), str(end))
        b = slice_metrics(bh_curve, str(start), str(end))
        rows.append({
            "fold": str(name),
            "start": str(start),
            "end": str(end),
            "strategy": s,
            "buy_hold": b,
            "return_delta": (
                s["return"] - b["return"]
                if s["return"] is not None and b["return"] is not None
                else None
            ),
            "sharpe_delta": (
                s["sharpe"] - b["sharpe"]
                if finite(s["sharpe"]) and finite(b["sharpe"])
                else None
            ),
            "mdd_improvement": (
                abs(b["max_drawdown"]) - abs(s["max_drawdown"])
                if (
                    finite(s["max_drawdown"])
                    and finite(b["max_drawdown"])
                )
                else None
            ),
        })
    return rows


def rolling_3y_comparison(
    strat_curve: pd.DataFrame,
    bh_curve: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    s = strat_curve.set_index("date")
    b = bh_curve.set_index("date")
    dates = list(s.index)

    rows = []
    # Evaluate every ~21 sessions after a 756-session warmup.
    for end_i in range(755, len(dates), 21):
        start_i = end_i - 755
        start = dates[start_i]
        end = dates[end_i]

        sm = slice_metrics(strat_curve, start, end)
        bm = slice_metrics(bh_curve, start, end)

        calendar_years = (
            datetime.strptime(end, "%Y%m%d")
            - datetime.strptime(start, "%Y%m%d")
        ).days / 365.25

        s0 = float(s.loc[start, "equity"])
        s1 = float(s.loc[end, "equity"])
        b0 = float(b.loc[start, "equity"])
        b1 = float(b.loc[end, "equity"])

        scagr = (
            (s1 / s0) ** (1.0 / calendar_years) - 1.0
            if calendar_years > 0 and s0 > 0 and s1 > 0
            else np.nan
        )
        bcagr = (
            (b1 / b0) ** (1.0 / calendar_years) - 1.0
            if calendar_years > 0 and b0 > 0 and b1 > 0
            else np.nan
        )

        rows.append({
            "start": start,
            "end": end,
            "strategy_cagr": scagr,
            "buy_hold_cagr": bcagr,
            "cagr_delta": scagr - bcagr,
            "strategy_sharpe": sm["sharpe"],
            "buy_hold_sharpe": bm["sharpe"],
            "sharpe_delta": (
                sm["sharpe"] - bm["sharpe"]
                if finite(sm["sharpe"]) and finite(bm["sharpe"])
                else np.nan
            ),
            "strategy_mdd": sm["max_drawdown"],
            "buy_hold_mdd": bm["max_drawdown"],
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df, {
            "windows": 0,
            "strategy_cagr_ge_buy_hold_ratio": None,
            "strategy_sharpe_ge_buy_hold_ratio": None,
            "strategy_lower_abs_mdd_ratio": None,
        }

    summary = {
        "windows": int(len(df)),
        "strategy_cagr_ge_buy_hold_ratio":
            float((df["cagr_delta"] >= 0).mean()),
        "strategy_sharpe_ge_buy_hold_ratio":
            float((df["sharpe_delta"] >= 0).mean()),
        "strategy_lower_abs_mdd_ratio":
            float(
                (
                    df["strategy_mdd"].abs()
                    <= df["buy_hold_mdd"].abs()
                ).mean()
            ),
    }
    return df, summary


def fast_reject_gate(
    strat20: dict[str, Any],
    bh20: dict[str, Any],
    strat50: dict[str, Any],
    bh50: dict[str, Any],
    rolling20: dict[str, Any],
) -> dict[str, Any]:
    """
    Ex-ante gate for a risk-control-oriented long/cash strategy.

    The strategy is allowed to sacrifice some CAGR, but not destroy it.
    """
    bh_cagr = bh20["cagr"]
    cagr_floor = (
        0.75 * bh_cagr
        if bh_cagr > 0
        else bh_cagr
    )

    checks = {
        "strategy20_cagr_positive":
            finite(strat20["cagr"])
            and strat20["cagr"] > 0,
        "strategy20_sharpe_above_buy_hold":
            finite(strat20["sharpe"])
            and finite(bh20["sharpe"])
            and strat20["sharpe"] > bh20["sharpe"],
        "strategy20_mdd_at_least_25pct_better":
            finite(strat20["max_drawdown"])
            and finite(bh20["max_drawdown"])
            and abs(strat20["max_drawdown"])
                <= 0.75 * abs(bh20["max_drawdown"]),
        "strategy20_cagr_retains_at_least_75pct_of_bh":
            finite(strat20["cagr"])
            and finite(cagr_floor)
            and strat20["cagr"] >= cagr_floor,
        "strategy50_cagr_positive":
            finite(strat50["cagr"])
            and strat50["cagr"] > 0,
        "strategy50_sharpe_above_buy_hold":
            finite(strat50["sharpe"])
            and finite(bh50["sharpe"])
            and strat50["sharpe"] > bh50["sharpe"],
        "rolling3y_sharpe_not_worse_in_majority":
            rolling20["windows"] > 0
            and rolling20["strategy_sharpe_ge_buy_hold_ratio"] >= 0.50,
        "rolling3y_mdd_not_worse_in_majority":
            rolling20["windows"] > 0
            and rolling20["strategy_lower_abs_mdd_ratio"] >= 0.50,
    }

    passed = all(checks.values())
    return {
        "checks": checks,
        "buy_hold_20bp_cagr": bh_cagr,
        "strategy_required_cagr_floor": cagr_floor,
        "decision": (
            "PASS_TO_ROBUSTNESS"
            if passed
            else "REJECT_FAST"
        ),
        "meaning": (
            "PASS_TO_ROBUSTNESS means the frozen monthly 12m long/cash "
            "TSMOM is economically interesting enough for robustness and "
            "Deep-Oversold-combination work. It does not validate Strategy #2."
        ),
    }


def pct(v: Any) -> str:
    return "NA" if not finite(v) else f"{float(v):+.2%}"


def num(v: Any, digits: int = 3) -> str:
    return "NA" if not finite(v) else f"{float(v):.{digits}f}"


def main():
    p = argparse.ArgumentParser(
        description="PositionX Strategy #2 TSMOM Fast-Reject v0.1"
    )
    p.add_argument("--refresh-history", action="store_true")
    args = p.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    origin, origin_path = load_origin_module()
    df = load_history(origin, args.refresh_history)
    decisions = build_decisions(df)

    results = {}
    curves = {}
    rolling_frames = {}
    fold_frames = {}

    for label, cost in (
        ("20bps", ROUND_TRIP_MAIN),
        ("50bps", ROUND_TRIP_SHADOW),
    ):
        scurve, sm = simulate(
            df, decisions, cost, buy_hold=False
        )
        bcurve, bm = simulate(
            df, decisions, cost, buy_hold=True
        )

        folds = fold_comparison(
            origin,
            scurve,
            bcurve,
        )
        rolling_df, rolling_summary = rolling_3y_comparison(
            scurve,
            bcurve,
        )

        results[label] = {
            "strategy": sm,
            "buy_hold": bm,
            "folds": folds,
            "rolling_3y": rolling_summary,
        }
        curves[label] = (scurve, bcurve)
        rolling_frames[label] = rolling_df
        fold_frames[label] = folds

    gate = fast_reject_gate(
        results["20bps"]["strategy"],
        results["20bps"]["buy_hold"],
        results["50bps"]["strategy"],
        results["50bps"]["buy_hold"],
        results["20bps"]["rolling_3y"],
    )

    decisions.to_csv(
        OUT_DIR / "monthly_decisions.csv",
        index=False,
        encoding="utf-8-sig",
    )

    for label, (scurve, bcurve) in curves.items():
        scurve.to_csv(
            OUT_DIR / f"strategy_curve_{label}.csv",
            index=False,
            encoding="utf-8-sig",
        )
        bcurve.to_csv(
            OUT_DIR / f"buy_hold_curve_{label}.csv",
            index=False,
            encoding="utf-8-sig",
        )
        rolling_frames[label].to_csv(
            OUT_DIR / f"rolling_3y_{label}.csv",
            index=False,
            encoding="utf-8-sig",
        )
        pd.DataFrame(fold_frames[label]).to_json(
            OUT_DIR / f"folds_{label}.json",
            orient="records",
            force_ascii=False,
            indent=2,
        )

    report = {
        "audit_id": "STRATEGY2_TSMOM_FAST_REJECT_V01",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "infrastructure": str(origin_path),
        "status": "OK",
        "frozen_hypothesis": {
            "instrument": SYMBOL,
            "signal_observation": "calendar-month last trading session CLOSE",
            "signal": "close[T] > close[T-250]",
            "execution": "next market session OPEN after month-end signal",
            "long_state": "100% long 510500.SH",
            "cash_state": "0% market exposure; cash return fixed at 0%",
            "shorting": False,
            "leverage": False,
            "round_trip_cost_main": ROUND_TRIP_MAIN,
            "round_trip_cost_shadow": ROUND_TRIP_SHADOW,
            "cost_application": "half round-trip cost on each entry/exit side",
            "terminal_liquidation": "final CLOSE with one-way exit cost",
            "no_ma_variant": True,
            "no_vol_target": True,
            "no_regime_filter": True,
            "no_parameter_sweep": True,
        },
        "history": {
            "available_min": str(df.index.min()),
            "available_max": str(df.index.max()),
            "rows": int(len(df)),
        },
        "decisions": {
            "rows": int(len(decisions)),
            "long_months": int(decisions["desired_long"].sum()),
            "cash_months": int((~decisions["desired_long"]).sum()),
            "first_signal_date": str(decisions["signal_date"].min()),
            "last_signal_date": str(decisions["signal_date"].max()),
        },
        "results": results,
        "fast_reject_gate": gate,
        "safety": {
            "uses_broker": False,
            "production_db_opened": False,
            "creates_trading_plan": False,
            "can_submit_order": False,
            "changes_strategy_parameters": False,
        },
    }

    (OUT_DIR / "audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("=" * 110)
    print("POSITIONX STRATEGY #2 — CSI500 ETF TSMOM FAST-REJECT V0.1")
    print("=" * 110)
    print("Infrastructure:", origin_path)
    print(
        f"Instrument={SYMBOL} | month-end CLOSE | "
        f"close[T] > close[T-{LOOKBACK}] | next OPEN | long/cash"
    )
    print(
        f"History={df.index.min()}->{df.index.max()} rows={len(df)} | "
        f"decisions={len(decisions)} "
        f"longMonths={int(decisions['desired_long'].sum())} "
        f"cashMonths={int((~decisions['desired_long']).sum())}"
    )
    print("Cash return: 0%. No short / leverage / MA / vol-target / regime.")
    print()

    for label in ("20bps", "50bps"):
        s = results[label]["strategy"]
        b = results[label]["buy_hold"]
        r = results[label]["rolling_3y"]

        print(label.upper())
        print("-" * 110)
        print(
            "TSMOM     "
            f"CAGR={pct(s['cagr'])} "
            f"MDD={pct(s['max_drawdown'])} "
            f"Sharpe={num(s['sharpe'])} "
            f"Calmar={num(s['calmar'])} "
            f"AvgLong={pct(s['avg_long_exposure'])} "
            f"Switches={s['switches_including_terminal_liquidation']} "
            f"AnnTurn={num(s['annualized_one_way_turnover'])}"
        )
        print(
            "BUY&HOLD  "
            f"CAGR={pct(b['cagr'])} "
            f"MDD={pct(b['max_drawdown'])} "
            f"Sharpe={num(b['sharpe'])} "
            f"Calmar={num(b['calmar'])}"
        )
        print(
            "DELTA      "
            f"CAGR={pct(s['cagr'] - b['cagr'])} "
            f"MDD improvement={pct(abs(b['max_drawdown']) - abs(s['max_drawdown']))} "
            f"Sharpe={num(s['sharpe'] - b['sharpe'])}"
        )
        rc = (
            "NA"
            if r["strategy_cagr_ge_buy_hold_ratio"] is None
            else f"{r['strategy_cagr_ge_buy_hold_ratio']:.1%}"
        )
        rs = (
            "NA"
            if r["strategy_sharpe_ge_buy_hold_ratio"] is None
            else f"{r['strategy_sharpe_ge_buy_hold_ratio']:.1%}"
        )
        rm = (
            "NA"
            if r["strategy_lower_abs_mdd_ratio"] is None
            else f"{r['strategy_lower_abs_mdd_ratio']:.1%}"
        )
        print(
            "ROLLING 3Y "
            f"windows={r['windows']} "
            f"CAGR>=BH={rc} "
            f"Sharpe>=BH={rs} "
            f"|MDD|<=BH={rm}"
        )
        print()

        print("FOLDS")
        for f in results[label]["folds"]:
            print(
                f"  {f['fold']}: "
                f"TS ret={pct(f['strategy']['return'])} "
                f"MDD={pct(f['strategy']['max_drawdown'])} "
                f"Sharpe={num(f['strategy']['sharpe'])} | "
                f"BH ret={pct(f['buy_hold']['return'])} "
                f"MDD={pct(f['buy_hold']['max_drawdown'])} "
                f"Sharpe={num(f['buy_hold']['sharpe'])}"
            )
        print()

    print("FAST-REJECT GATE")
    print("-" * 110)
    for k, v in gate["checks"].items():
        print(f"{k}: {'PASS' if v else 'FAIL'}")
    print(
        "20bp Buy&Hold CAGR:",
        pct(gate["buy_hold_20bp_cagr"]),
        "| required TSMOM floor:",
        pct(gate["strategy_required_cagr_floor"]),
    )
    print("DECISION:", gate["decision"])
    print()
    print("Output:", OUT_DIR)
    print("No Deep Oversold rule was changed.")
    print("No production database or execution code was modified.")


if __name__ == "__main__":
    main()
