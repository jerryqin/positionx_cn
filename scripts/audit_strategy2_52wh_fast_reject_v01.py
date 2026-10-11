from __future__ import annotations

import importlib.util
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = (
    ROOT / "data" / "research" / "edge_discovery"
    / "strategy2_52wh_fast_reject_v01"
)

ORIGIN_CANDIDATES = [
    ROOT / "scripts" / "run_deep_oversold_csi500_universe.py",
    ROOT / "payload" / "scripts" / "run_deep_oversold_csi500_universe.py",
]

START_DATE = "20200803"
END_DATE = "20260924"

LOOKBACK = 250
PRIMARY_HORIZON = 60
DIAGNOSTIC_HORIZONS = (20, 120)
ALL_HORIZONS = (20, 60, 120)

COST_MAIN = 0.002
COST_SHADOW = 0.005


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


def normalize_symbol(v: Any) -> str:
    s = str(v).strip().upper()
    if "." in s:
        return s
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) != 6:
        return s
    if digits.startswith(("4", "8")):
        return digits + ".BJ"
    if digits.startswith(("5", "6", "9")):
        return digits + ".SH"
    return digits + ".SZ"


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
        "positionx_deep_oversold_csi500_origin_for_52wh",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not import original script: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, path


def get_price(df: pd.DataFrame, date: str, col: str) -> float | None:
    if date not in df.index or col not in df.columns:
        return None
    v = df.at[date, col]
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(v) or v <= 0:
        return None
    return v


def summarize(values: pd.Series) -> dict[str, Any]:
    x = pd.to_numeric(values, errors="coerce")
    x = x[np.isfinite(x)]
    if len(x) == 0:
        return {
            "n": 0,
            "mean": None,
            "median": None,
            "trim5": None,
            "win_rate": None,
            "p10": None,
            "p90": None,
            "bottom10_mean": None,
            "top10_mean": None,
        }

    arr = np.sort(x.to_numpy(dtype=float))
    n = len(arr)
    trim = int(math.floor(n * 0.05))
    trimmed = arr[trim:n-trim] if trim > 0 and n - 2 * trim > 0 else arr

    k = max(1, int(math.ceil(n * 0.10)))
    return {
        "n": int(n),
        "mean": float(np.mean(arr)),
        "median": float(np.median(arr)),
        "trim5": float(np.mean(trimmed)),
        "win_rate": float(np.mean(arr > 0)),
        "p10": float(np.quantile(arr, 0.10)),
        "p90": float(np.quantile(arr, 0.90)),
        "bottom10_mean": float(np.mean(arr[:k])),
        "top10_mean": float(np.mean(arr[-k:])),
    }


def horizon_metrics(x: pd.DataFrame, h: int) -> dict[str, Any]:
    suffix = str(h)
    cols = {
        "gross": f"gross_{suffix}",
        "excess": f"excess_{suffix}",
        "net20": f"net20_{suffix}",
        "net50": f"net50_{suffix}",
    }
    out = {
        name: summarize(x[col])
        for name, col in cols.items()
    }

    valid = x[x[cols["excess"]].notna()].copy()
    if valid.empty:
        out["signal_date_equal_weight_excess"] = summarize(
            pd.Series(dtype=float)
        )
    else:
        daily = (
            valid.groupby("signal_date", as_index=False)[cols["excess"]]
            .mean()
        )
        out["signal_date_equal_weight_excess"] = summarize(
            daily[cols["excess"]]
        )

    return out


def build_52wh_events(
    origin,
) -> tuple[pd.DataFrame, dict[str, Any], dict[str, pd.DataFrame], list[str]]:
    daily = origin.load_accepted_daily(
        origin.ACCEPTED_DIR,
        origin.CSI500,
    )
    membership = origin.event_membership(
        daily,
        START_DATE,
        END_DATE,
    )
    membership["symbol"] = membership["symbol"].map(normalize_symbol)
    membership["signal_date"] = membership["signal_date"].map(normalize_date)

    symbols = sorted(membership["symbol"].unique().tolist())

    adapter = origin.XtDataWatchlistAdapter(
        origin.xtdata,
        origin.AdapterConfig(
            sector="",
            bars=2200,
            dividend_type="front",
        ),
    )
    loaded = adapter.history(
        symbols + [origin.CSI500.expected_index]
    )
    loaded = {
        normalize_symbol(s): origin.normalize_history(df)
        for s, df in loaded.items()
        if df is not None and not df.empty
    }

    benchmark_symbol = normalize_symbol(origin.CSI500.expected_index)
    missing = [
        s
        for s in symbols + [benchmark_symbol]
        if s not in loaded
    ]
    if missing:
        raise RuntimeError(
            f"BLOCKED_INCOMPLETE_HISTORY missing={len(missing)} "
            f"sample={missing[:30]}"
        )

    benchmark = loaded[benchmark_symbol]
    stock_histories = {
        s: df for s, df in loaded.items()
        if s != benchmark_symbol
    }

    market_dates = [
        d
        for d in benchmark.index
        if START_DATE <= d <= END_DATE
    ]
    date_pos = {d: i for i, d in enumerate(market_dates)}

    member_by_symbol = {
        symbol: set(g["signal_date"].tolist())
        for symbol, g in membership.groupby("symbol", sort=False)
    }

    rows = []

    for idx, symbol in enumerate(symbols, 1):
        hist = stock_histories.get(symbol)
        if hist is None or hist.empty:
            continue
        if "close" not in hist.columns:
            continue

        h = hist.copy()
        h.index = [normalize_date(x) for x in h.index]
        h = h[~pd.isna(h.index)]
        h = h[~pd.Index(h.index).duplicated(keep="last")].sort_index()

        close = pd.to_numeric(h["close"], errors="coerce")
        prev250_max = close.shift(1).rolling(
            LOOKBACK,
            min_periods=LOOKBACK,
        ).max()

        member_dates = member_by_symbol.get(symbol, set())
        candidate_dates = [
            d
            for d in h.index
            if d in member_dates
            and d in date_pos
            and finite(close.loc[d])
            and finite(prev250_max.loc[d])
            and float(close.loc[d]) > float(prev250_max.loc[d])
        ]

        for signal_date in candidate_dates:
            p = date_pos[signal_date]

            # Primary S60 must be fully observable. S20 is therefore observable
            # too; S120 remains diagnostic and may be unavailable near cutoff.
            if p + PRIMARY_HORIZON >= len(market_dates):
                continue

            entry_date = market_dates[p + 1]
            exit60 = market_dates[p + PRIMARY_HORIZON]

            entry_open = get_price(h, entry_date, "open")
            entry_close = get_price(h, entry_date, "close")
            exit60_close = get_price(h, exit60, "close")

            bench_entry_open = get_price(benchmark, entry_date, "open")
            bench_entry_close = get_price(benchmark, entry_date, "close")
            bench_exit60_close = get_price(benchmark, exit60, "close")

            complete60 = all(
                finite(v)
                for v in (
                    entry_open,
                    entry_close,
                    exit60_close,
                    bench_entry_open,
                    bench_entry_close,
                    bench_exit60_close,
                )
            )
            if not complete60:
                continue

            gross60 = exit60_close / entry_open - 1.0
            bench60 = bench_exit60_close / bench_entry_open - 1.0
            excess60 = gross60 - bench60

            row = {
                "symbol": symbol,
                "signal_date": signal_date,
                "entry_date": entry_date,
                "exit_date": exit60,
                "signal_close": float(close.loc[signal_date]),
                "prev250_max_close": float(prev250_max.loc[signal_date]),
                "breakout_pct": (
                    float(close.loc[signal_date])
                    / float(prev250_max.loc[signal_date])
                    - 1.0
                ),
                # Compatibility fields for frozen STANDARD_OPEN_V1 helper.
                "signal_ret20": np.nan,
                "complete_outcome": True,
                "entry_open": entry_open,
                "entry_close": entry_close,
                "exit_close": exit60_close,
                "bench_entry_open": bench_entry_open,
                "bench_entry_close": bench_entry_close,
                "bench_exit_close": bench_exit60_close,
                "open_gross_return": gross60,
                "close_gross_return": (
                    exit60_close / entry_close - 1.0
                    if finite(entry_close)
                    else np.nan
                ),
                "open_excess_gross": excess60,
                "close_excess_gross": (
                    exit60_close / entry_close
                    - bench_exit60_close / bench_entry_close
                    if finite(entry_close) and finite(bench_entry_close)
                    else np.nan
                ),
                "open_net_20bps": gross60 - COST_MAIN,
                "open_net_50bps": gross60 - COST_SHADOW,
                "close_net_20bps": (
                    exit60_close / entry_close - 1.0 - COST_MAIN
                    if finite(entry_close)
                    else np.nan
                ),
                "close_net_50bps": (
                    exit60_close / entry_close - 1.0 - COST_SHADOW
                    if finite(entry_close)
                    else np.nan
                ),
            }

            for horizon in ALL_HORIZONS:
                if p + horizon >= len(market_dates):
                    row[f"exit_date_{horizon}"] = None
                    row[f"gross_{horizon}"] = np.nan
                    row[f"excess_{horizon}"] = np.nan
                    row[f"net20_{horizon}"] = np.nan
                    row[f"net50_{horizon}"] = np.nan
                    continue

                exit_date = market_dates[p + horizon]
                stock_exit = get_price(h, exit_date, "close")
                bench_exit = get_price(benchmark, exit_date, "close")
                if (
                    not finite(stock_exit)
                    or not finite(bench_exit)
                    or not finite(entry_open)
                    or not finite(bench_entry_open)
                ):
                    row[f"exit_date_{horizon}"] = exit_date
                    row[f"gross_{horizon}"] = np.nan
                    row[f"excess_{horizon}"] = np.nan
                    row[f"net20_{horizon}"] = np.nan
                    row[f"net50_{horizon}"] = np.nan
                    continue

                gross = stock_exit / entry_open - 1.0
                bench_ret = bench_exit / bench_entry_open - 1.0
                excess = gross - bench_ret

                row[f"exit_date_{horizon}"] = exit_date
                row[f"gross_{horizon}"] = gross
                row[f"excess_{horizon}"] = excess
                row[f"net20_{horizon}"] = gross - COST_MAIN
                row[f"net50_{horizon}"] = gross - COST_SHADOW

            rows.append(row)

    events = pd.DataFrame(rows)
    if events.empty:
        raise RuntimeError("No 52WH events generated.")

    events = events.sort_values(
        ["signal_date", "symbol"]
    ).reset_index(drop=True)

    receipt = {
        "pit_symbols": int(len(symbols)),
        "market_sessions": int(len(market_dates)),
        "raw_complete_s60_events": int(len(events)),
        "signal_dates": int(events["signal_date"].nunique()),
        "signal_min": str(events["signal_date"].min()),
        "signal_max": str(events["signal_date"].max()),
    }
    return events, receipt, stock_histories, market_dates


def apply_frozen_fillability_and_dedup(
    origin,
    events: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    status = origin.load_status_for_events(
        origin.STATUS_PARQUET,
        events,
    )

    screened = origin.apply_standard_fillability(
        events.copy(),
        status,
    )

    if int(screened["status_found"].sum()) == 0:
        raise RuntimeError("BLOCKED_STATUS_JOIN_ZERO_MATCH")

    fillable = screened[
        screened["standard_fillable"] == True  # noqa: E712
    ].copy()

    if fillable.empty:
        raise RuntimeError("No STANDARD_OPEN_V1 fillable 52WH events.")

    dedup = origin.dedup_while_held(fillable).copy()
    dedup = dedup.sort_values(
        ["signal_date", "symbol"]
    ).reset_index(drop=True)

    receipt = {
        "screened": int(len(screened)),
        "status_found": int(screened["status_found"].sum()),
        "fillable": int(len(fillable)),
        "dedup_s60_first_signal_while_flat": int(len(dedup)),
        "unique_symbols": int(dedup["symbol"].nunique()),
        "signal_dates": int(dedup["signal_date"].nunique()),
    }
    return fillable, dedup, receipt


def fold_metrics(
    x: pd.DataFrame,
    folds: list[tuple[str, str, str]],
) -> list[dict[str, Any]]:
    rows = []
    for name, start, end in folds:
        y = x[
            (x["signal_date"] >= str(start))
            & (x["signal_date"] <= str(end))
        ].copy()
        m = horizon_metrics(y, PRIMARY_HORIZON)
        rows.append({
            "fold": str(name),
            "start": str(start),
            "end": str(end),
            "events": int(len(y)),
            "signal_dates": int(y["signal_date"].nunique()),
            "gross_mean": m["gross"]["mean"],
            "excess_mean": m["excess"]["mean"],
            "excess_median": m["excess"]["median"],
            "excess_trim5": m["excess"]["trim5"],
            "date_equal_excess_mean":
                m["signal_date_equal_weight_excess"]["mean"],
            "net20_mean": m["net20"]["mean"],
            "net50_mean": m["net50"]["mean"],
        })
    return rows


def yearly_metrics(x: pd.DataFrame) -> list[dict[str, Any]]:
    y = x.copy()
    y["year"] = y["signal_date"].str[:4]
    rows = []
    for year, g in y.groupby("year", sort=True):
        m = horizon_metrics(g, PRIMARY_HORIZON)
        rows.append({
            "year": str(year),
            "events": int(len(g)),
            "signal_dates": int(g["signal_date"].nunique()),
            "excess_mean": m["excess"]["mean"],
            "excess_median": m["excess"]["median"],
            "excess_trim5": m["excess"]["trim5"],
            "date_equal_excess_mean":
                m["signal_date_equal_weight_excess"]["mean"],
            "net20_mean": m["net20"]["mean"],
        })
    return rows


def fast_reject_gate(
    primary: dict[str, Any],
    folds: list[dict[str, Any]],
) -> dict[str, Any]:
    checks = {
        "enough_events_ge_300":
            primary["excess"]["n"] >= 300,
        "s60_mean_excess_positive":
            finite(primary["excess"]["mean"])
            and primary["excess"]["mean"] > 0,
        "s60_trim5_excess_positive":
            finite(primary["excess"]["trim5"])
            and primary["excess"]["trim5"] > 0,
        "s60_signal_date_equal_mean_excess_positive":
            finite(primary["signal_date_equal_weight_excess"]["mean"])
            and primary["signal_date_equal_weight_excess"]["mean"] > 0,
        "s60_net20_mean_positive":
            finite(primary["net20"]["mean"])
            and primary["net20"]["mean"] > 0,
        "s60_net50_mean_positive":
            finite(primary["net50"]["mean"])
            and primary["net50"]["mean"] > 0,
        "positive_excess_folds_ge_2_of_3":
            sum(
                1
                for r in folds
                if finite(r["excess_mean"])
                and r["excess_mean"] > 0
            ) >= 2,
        "positive_date_equal_folds_ge_2_of_3":
            sum(
                1
                for r in folds
                if finite(r["date_equal_excess_mean"])
                and r["date_equal_excess_mean"] > 0
            ) >= 2,
    }

    passed = all(checks.values())
    return {
        "checks": checks,
        "decision": (
            "PASS_TO_SHARED_EQUITY"
            if passed
            else "REJECT_FAST"
        ),
        "meaning": (
            "PASS_TO_SHARED_EQUITY means only that the naked S60 event edge "
            "deserves portfolio simulation. It does NOT validate Strategy #2."
        ),
    }


def pct(v: Any) -> str:
    return "NA" if not finite(v) else f"{float(v):+.2%}"


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    origin, origin_path = load_origin_module()
    events, build_receipt, _, _ = build_52wh_events(origin)
    fillable, dedup, fill_receipt = apply_frozen_fillability_and_dedup(
        origin,
        events,
    )

    # Diagnostics are evaluated on the SAME S60-deduplicated event population.
    # We do not re-deduplicate separately for S20/S120; that would silently
    # create three different strategies.
    horizons = {
        str(h): horizon_metrics(dedup, h)
        for h in ALL_HORIZONS
    }

    folds = [
        (str(name), str(start), str(end))
        for name, start, end in origin.FOLDS
    ]
    fmetrics = fold_metrics(dedup, folds)
    ymetrics = yearly_metrics(dedup)

    gate = fast_reject_gate(
        horizons[str(PRIMARY_HORIZON)],
        fmetrics,
    )

    event_cols = [
        "symbol",
        "signal_date",
        "entry_date",
        "exit_date",
        "signal_close",
        "prev250_max_close",
        "breakout_pct",
        "standard_fillable",
        "status_found",
    ]
    for h in ALL_HORIZONS:
        event_cols += [
            f"exit_date_{h}",
            f"gross_{h}",
            f"excess_{h}",
            f"net20_{h}",
            f"net50_{h}",
        ]

    dedup[event_cols].to_csv(
        OUT_DIR / "events_dedup_s60.csv",
        index=False,
        encoding="utf-8-sig",
    )
    fillable[event_cols].to_csv(
        OUT_DIR / "events_fillable.csv",
        index=False,
        encoding="utf-8-sig",
    )
    pd.DataFrame(fmetrics).to_csv(
        OUT_DIR / "fold_metrics.csv",
        index=False,
        encoding="utf-8-sig",
    )
    pd.DataFrame(ymetrics).to_csv(
        OUT_DIR / "yearly_metrics.csv",
        index=False,
        encoding="utf-8-sig",
    )

    report = {
        "audit_id": "STRATEGY2_52WH_FAST_REJECT_V01",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "origin_infrastructure": str(origin_path),
        "status": "OK",
        "frozen_hypothesis": {
            "universe": "CSI500 PIT",
            "signal": (
                "T close > max close of prior 250 trading observations, "
                "excluding T"
            ),
            "signal_time": "T CLOSE",
            "entry": "T+1 exact market-session OPEN",
            "primary_exit": "S60 CLOSE",
            "diagnostic_exits": ["S20 CLOSE", "S120 CLOSE"],
            "fillability": "STANDARD_OPEN_V1 via original Deep Oversold helper",
            "duplicate": (
                "first signal while flat using S60 holding window, "
                "via original dedup_while_held"
            ),
            "cost_main": COST_MAIN,
            "cost_shadow": COST_SHADOW,
            "no_volume_filter": True,
            "no_ma_filter": True,
            "no_ret120": True,
            "no_regime_filter": True,
            "no_stop_loss": True,
            "no_take_profit": True,
            "no_retest_confirmation": True,
            "no_parameter_sweep": True,
        },
        "counts": {
            **build_receipt,
            **fill_receipt,
        },
        "horizons": horizons,
        "folds": fmetrics,
        "years": ymetrics,
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
    print("POSITIONX STRATEGY #2 — 52WH BREAKOUT FAST-REJECT V0.1")
    print("=" * 110)
    print("Infrastructure:", origin_path)
    print(
        "Hypothesis: CSI500 PIT | close > prior-250d max close | "
        "T+1 OPEN | PRIMARY S60 CLOSE"
    )
    print(
        "Counts:",
        f"rawCompleteS60={build_receipt['raw_complete_s60_events']} "
        f"fillable={fill_receipt['fillable']} "
        f"dedupS60={fill_receipt['dedup_s60_first_signal_while_flat']} "
        f"symbols={fill_receipt['unique_symbols']} "
        f"signalDates={fill_receipt['signal_dates']}"
    )
    print()

    for h in ALL_HORIZONS:
        m = horizons[str(h)]
        ex = m["excess"]
        de = m["signal_date_equal_weight_excess"]
        gr = m["gross"]
        print(
            f"S{h:03d} "
            + ("[PRIMARY]" if h == PRIMARY_HORIZON else "[DIAGNOSTIC]")
        )
        print("-" * 110)
        print(
            f"n={ex['n']} "
            f"grossMean={pct(gr['mean'])} "
            f"excessMean={pct(ex['mean'])} "
            f"excessMedian={pct(ex['median'])} "
            f"trim5={pct(ex['trim5'])} "
            f"win={ex['win_rate']:.1%} "
            f"dateEqMean={pct(de['mean'])} "
            f"net20Mean={pct(m['net20']['mean'])} "
            f"net50Mean={pct(m['net50']['mean'])}"
        )
        print(
            f"p10={pct(ex['p10'])} "
            f"p90={pct(ex['p90'])} "
            f"bottom10={pct(ex['bottom10_mean'])} "
            f"top10={pct(ex['top10_mean'])}"
        )
        print()

    print("S60 FOLDS")
    print("-" * 110)
    for r in fmetrics:
        print(
            f"{r['fold']}: events={r['events']} "
            f"excess={pct(r['excess_mean'])} "
            f"median={pct(r['excess_median'])} "
            f"trim5={pct(r['excess_trim5'])} "
            f"dateEq={pct(r['date_equal_excess_mean'])} "
            f"net20={pct(r['net20_mean'])}"
        )
    print()

    print("S60 YEARS")
    print("-" * 110)
    for r in ymetrics:
        print(
            f"{r['year']}: events={r['events']} "
            f"excess={pct(r['excess_mean'])} "
            f"median={pct(r['excess_median'])} "
            f"trim5={pct(r['excess_trim5'])} "
            f"dateEq={pct(r['date_equal_excess_mean'])}"
        )
    print()

    print("FAST-REJECT GATE")
    print("-" * 110)
    for k, v in gate["checks"].items():
        print(f"{k}: {'PASS' if v else 'FAIL'}")
    print("DECISION:", gate["decision"])
    print()
    print("Output:", OUT_DIR)
    print("S20/S120 are diagnostics only; S60 is the frozen primary horizon.")
    print("No shared-equity portfolio was run.")
    print("No factor/filter/stop/regime parameter was added.")
    print("No production database or execution code was modified.")


if __name__ == "__main__":
    main()
