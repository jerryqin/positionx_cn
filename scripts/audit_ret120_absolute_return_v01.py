from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from xtquant import xtdata


ROOT = Path(__file__).resolve().parents[1]
EDGE_DIR = ROOT / "data" / "research" / "edge_discovery"
TEMPORAL_DIR = EDGE_DIR / "ret120_temporal_v01"
OUT_DIR = EDGE_DIR / "ret120_absolute_return_v01"

UNIVERSES = ("csi300", "csi500")
HORIZON = 120
CUTOFF = "20260924"
CALENDAR_SYMBOL = "000001.SH"
ERA_BOUNDARY = "20210730"
COSTS = (0.0020, 0.0050)


def finite(x: Any) -> bool:
    try:
        return bool(np.isfinite(float(x)))
    except (TypeError, ValueError):
        return False


def normalize_date(v: Any) -> str | None:
    s = "".join(ch for ch in str(v) if ch.isdigit())
    if len(s) < 8:
        return None
    d = s[:8]
    try:
        datetime.strptime(d, "%Y%m%d")
    except ValueError:
        return None
    return d


def trim_mean(values, p: float = 0.05) -> float:
    arr = np.sort(np.asarray(list(values), dtype=float))
    arr = arr[np.isfinite(arr)]
    if len(arr) == 0:
        return math.nan
    k = int(math.floor(len(arr) * p))
    if k == 0:
        return float(arr.mean())
    if 2 * k >= len(arr):
        return math.nan
    return float(arr[k:len(arr)-k].mean())


def mean_ex_top(values, p: float = 0.05) -> float:
    arr = np.sort(np.asarray(list(values), dtype=float))
    arr = arr[np.isfinite(arr)]
    if len(arr) == 0:
        return math.nan
    k = int(math.floor(len(arr) * p))
    if k == 0:
        return float(arr.mean())
    remain = arr[:len(arr)-k]
    return float(remain.mean()) if len(remain) else math.nan


def summarize(values) -> dict[str, float | int]:
    arr = np.asarray(list(values), dtype=float)
    arr = arr[np.isfinite(arr)]
    if len(arr) == 0:
        return {
            "n_dates": 0,
            "mean": math.nan,
            "median": math.nan,
            "trim5": math.nan,
            "ex_top5": math.nan,
            "positive_ratio": math.nan,
        }
    return {
        "n_dates": int(len(arr)),
        "mean": float(arr.mean()),
        "median": float(np.median(arr)),
        "trim5": trim_mean(arr, 0.05),
        "ex_top5": mean_ex_top(arr, 0.05),
        "positive_ratio": float((arr > 0).mean()),
    }


def assign_quintiles(values: pd.Series) -> pd.Series:
    """
    Balanced cross-sectional quintiles using deterministic first-rank.
    Reconciled against frozen daily_buckets before any interpretation.
    """
    valid = values.notna()
    out = pd.Series(index=values.index, dtype="object")
    if valid.sum() < 5:
        return out

    ranks = values[valid].rank(method="first")
    labels = pd.qcut(
        ranks,
        5,
        labels=["Q1", "Q2", "Q3", "Q4", "Q5"],
    )
    out.loc[valid] = labels.astype(str).to_numpy()
    return out


def load_folds() -> dict[str, tuple[str, str]]:
    path = TEMPORAL_DIR / "audit.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = payload["folds"]
    return {
        k: (str(raw[k]["start"]), str(raw[k]["end"]))
        for k in ("F1", "F2", "F3")
    }


def assign_fold(date: str, folds: dict[str, tuple[str, str]]) -> str | None:
    for name in ("F1", "F2", "F3"):
        a, b = folds[name]
        if a <= date <= b:
            return name
    return None


def panel_path(universe: str) -> Path:
    return EDGE_DIR / f"trend_persistence_scan_{universe}_panel_v0221.parquet"


def bucket_path(universe: str) -> Path:
    return EDGE_DIR / f"trend_persistence_scan_{universe}_daily_buckets_v0221.parquet"


def load_panel(con: duckdb.DuckDBPyConnection, universe: str) -> pd.DataFrame:
    p = panel_path(universe)
    q = p.as_posix().replace("'", "''")
    df = con.execute(
        f"""
        SELECT
            signal_date,
            symbol,
            ret120,
            excess_120
        FROM read_parquet('{q}')
        WHERE signal_date <= '{CUTOFF}'
          AND ret120 IS NOT NULL
          AND excess_120 IS NOT NULL
          AND COALESCE(label_quality_reject_120, FALSE) = FALSE
        ORDER BY signal_date, symbol
        """
    ).fetchdf()
    df.insert(0, "universe", universe)
    df["signal_date"] = df["signal_date"].astype(str)
    df["symbol"] = df["symbol"].astype(str)
    return df


def load_frozen_bucket_means(
    con: duckdb.DuckDBPyConnection,
    universe: str,
) -> pd.DataFrame:
    p = bucket_path(universe)
    q = p.as_posix().replace("'", "''")
    df = con.execute(
        f"""
        SELECT
            signal_date,
            bucket,
            n,
            mean_excess
        FROM read_parquet('{q}')
        WHERE feature = 'ret120'
          AND horizon = 120
          AND signal_date <= '{CUTOFF}'
        ORDER BY signal_date, bucket
        """
    ).fetchdf()
    df["signal_date"] = df["signal_date"].astype(str)
    return df


def reconstruct_and_reconcile_buckets(
    panel: pd.DataFrame,
    frozen: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, float | int]]:
    parts = []

    for _, x in panel.groupby("signal_date", sort=True):
        y = x.copy()
        y["bucket"] = assign_quintiles(y["ret120"])
        parts.append(y)

    out = pd.concat(parts, ignore_index=True)

    calc = (
        out.groupby(["signal_date", "bucket"], observed=True)
        .agg(
            n_calc=("symbol", "size"),
            mean_excess_calc=("excess_120", "mean"),
        )
        .reset_index()
    )

    chk = frozen.merge(
        calc,
        on=["signal_date", "bucket"],
        how="outer",
        validate="one_to_one",
    )

    chk["n_diff"] = chk["n_calc"] - chk["n"]
    chk["mean_diff"] = chk["mean_excess_calc"] - chk["mean_excess"]

    mismatch_n = int((chk["n_diff"].fillna(999999) != 0).sum())
    max_mean_abs_diff = float(
        chk["mean_diff"].abs().max()
    ) if len(chk) else math.nan

    reconciliation = {
        "rows": int(len(chk)),
        "count_mismatch_rows": mismatch_n,
        "max_mean_abs_diff": max_mean_abs_diff,
    }

    if mismatch_n != 0 or not finite(max_mean_abs_diff) or max_mean_abs_diff > 1e-12:
        raise RuntimeError(
            "Reconstructed quintiles do not match frozen daily_buckets. "
            f"Reconciliation={reconciliation}"
        )

    return out, reconciliation


def parse_market_ex(
    raw: Any,
    symbols: list[str],
    fields: tuple[str, ...],
) -> pd.DataFrame:
    if not isinstance(raw, dict):
        raise TypeError(f"Unexpected xtdata result: {type(raw)!r}")

    rows = []
    symbol_set = set(symbols)

    # Common shape: {symbol: DataFrame(fields)}
    symbol_keys = [k for k in raw.keys() if str(k) in symbol_set]
    if symbol_keys:
        for symbol in symbol_keys:
            obj = raw[symbol]
            if not isinstance(obj, pd.DataFrame):
                continue

            col_map = {str(c).lower(): c for c in obj.columns}
            if not all(f in col_map for f in fields):
                continue

            data = {
                "date": [normalize_date(x) for x in obj.index],
                "symbol": str(symbol),
            }
            for f in fields:
                data[f] = pd.to_numeric(
                    obj[col_map[f]],
                    errors="coerce",
                ).to_numpy()
            rows.append(pd.DataFrame(data))

    # Alternate shape: {field: DataFrame(index=date, columns=symbol)}
    if not rows and all(f in {str(k).lower() for k in raw.keys()} for f in fields):
        key_map = {str(k).lower(): k for k in raw.keys()}
        field_dfs = {f: raw[key_map[f]] for f in fields}
        first = field_dfs[fields[0]]

        if isinstance(first, pd.DataFrame):
            for symbol in first.columns:
                if str(symbol) not in symbol_set:
                    continue

                data = {
                    "date": [normalize_date(x) for x in first.index],
                    "symbol": str(symbol),
                }
                good = True
                for f in fields:
                    obj = field_dfs[f]
                    if not isinstance(obj, pd.DataFrame) or symbol not in obj.columns:
                        good = False
                        break
                    data[f] = pd.to_numeric(
                        obj[symbol],
                        errors="coerce",
                    ).to_numpy()

                if good:
                    rows.append(pd.DataFrame(data))

    if not rows:
        raise RuntimeError(
            "Could not normalize xtdata.get_market_data_ex result."
        )

    out = pd.concat(rows, ignore_index=True)
    out = out.dropna(subset=["date"])
    out = out.drop_duplicates(["date", "symbol"], keep="last")
    return out


def get_market_calendar(start: str, end: str) -> list[str]:
    raw = xtdata.get_market_data_ex(
        field_list=["close"],
        stock_list=[CALENDAR_SYMBOL],
        period="1d",
        start_time=start,
        end_time=end,
        count=-1,
        dividend_type="none",
        fill_data=False,
    )
    df = parse_market_ex(raw, [CALENDAR_SYMBOL], ("close",))
    dates = sorted(
        d for d, c in zip(df["date"], df["close"])
        if d is not None and finite(c) and float(c) > 0
    )
    return dates


def session_map(
    signal_dates: list[str],
    calendar: list[str],
    horizon: int,
) -> pd.DataFrame:
    pos = {d: i for i, d in enumerate(calendar)}
    rows = []

    for d in sorted(set(signal_dates)):
        i = pos.get(d)
        if i is None:
            rows.append({
                "signal_date": d,
                "entry_date": None,
                "exit_date": None,
            })
            continue

        entry_i = i + 1
        exit_i = i + horizon

        rows.append({
            "signal_date": d,
            "entry_date": calendar[entry_i] if entry_i < len(calendar) else None,
            "exit_date": calendar[exit_i] if exit_i < len(calendar) else None,
        })

    return pd.DataFrame(rows)


def attach_raw_returns(
    panel: pd.DataFrame,
    market: pd.DataFrame,
    sessions: pd.DataFrame,
) -> pd.DataFrame:
    x = panel.merge(
        sessions,
        on="signal_date",
        how="left",
        validate="many_to_one",
    )

    open_px = market[["date", "symbol", "open"]].rename(
        columns={"date": "entry_date", "open": "entry_open"}
    )
    close_px = market[["date", "symbol", "close"]].rename(
        columns={"date": "exit_date", "close": "exit_close"}
    )

    x = x.merge(
        open_px,
        on=["entry_date", "symbol"],
        how="left",
        validate="many_to_one",
    )
    x = x.merge(
        close_px,
        on=["exit_date", "symbol"],
        how="left",
        validate="many_to_one",
    )

    valid = (
        x["entry_date"].notna()
        & x["exit_date"].notna()
        & np.isfinite(x["entry_open"])
        & np.isfinite(x["exit_close"])
        & (x["entry_open"] > 0)
        & (x["exit_close"] > 0)
    )

    x["raw_return_120"] = np.nan
    x.loc[valid, "raw_return_120"] = (
        x.loc[valid, "exit_close"] / x.loc[valid, "entry_open"] - 1.0
    )

    return x


def reconcile_excess_definition(x: pd.DataFrame) -> dict[str, Any]:
    work = x[np.isfinite(x["raw_return_120"])].copy()
    work["implied_benchmark_return"] = (
        work["raw_return_120"] - work["excess_120"]
    )

    daily = (
        work.groupby("signal_date")
        .agg(
            n=("symbol", "size"),
            benchmark_mean=("implied_benchmark_return", "mean"),
            benchmark_std=("implied_benchmark_return", "std"),
            benchmark_min=("implied_benchmark_return", "min"),
            benchmark_max=("implied_benchmark_return", "max"),
        )
        .reset_index()
    )
    daily["benchmark_range"] = daily["benchmark_max"] - daily["benchmark_min"]

    result = {
        "n_dates": int(len(daily)),
        "max_benchmark_range": float(daily["benchmark_range"].max()),
        "p99_benchmark_range": float(daily["benchmark_range"].quantile(0.99)),
        "max_benchmark_std": float(daily["benchmark_std"].fillna(0).max()),
    }

    if (
        result["max_benchmark_range"] > 1e-8
        or result["max_benchmark_std"] > 1e-8
    ):
        raise RuntimeError(
            "excess_120 does not reconcile to a single common benchmark return "
            f"within each date. {result}"
        )

    return result


def daily_bucket_economics(x: pd.DataFrame) -> pd.DataFrame:
    work = x[np.isfinite(x["raw_return_120"])].copy()
    work["implied_benchmark_return"] = (
        work["raw_return_120"] - work["excess_120"]
    )

    rows = []

    for (signal_date, bucket), g in work.groupby(
        ["signal_date", "bucket"],
        sort=True,
        observed=True,
    ):
        bench = float(g["implied_benchmark_return"].mean())
        raw = float(g["raw_return_120"].mean())

        row = {
            "signal_date": signal_date,
            "bucket": bucket,
            "n": int(len(g)),
            "mean_raw_return": raw,
            "mean_excess_return": float(g["excess_120"].mean()),
            "benchmark_return": bench,
            "mean_raw_after_20bp": raw - COSTS[0],
            "mean_raw_after_50bp": raw - COSTS[1],
        }
        rows.append(row)

    return pd.DataFrame(rows)


def aggregate_segment(
    daily: pd.DataFrame,
    segment_dates: set[str],
) -> dict[str, Any]:
    x = daily[
        (daily["signal_date"].isin(segment_dates))
        & (daily["bucket"] == "Q5")
    ].copy()

    raw = summarize(x["mean_raw_return"])
    net20 = summarize(x["mean_raw_after_20bp"])
    net50 = summarize(x["mean_raw_after_50bp"])
    excess = summarize(x["mean_excess_return"])
    benchmark = summarize(x["benchmark_return"])

    return {
        "n_dates": int(len(x)),
        "q5_raw": raw,
        "q5_after_20bp": net20,
        "q5_after_50bp": net50,
        "q5_excess": excess,
        "benchmark": benchmark,
    }


def pct(x: Any) -> str:
    return "NA" if not finite(x) else f"{float(x) * 100:+.3f}%"


def main():
    folds = load_folds()
    con = duckdb.connect()

    try:
        panels = {}
        frozen = {}
        all_symbols = set()
        all_signal_dates = set()

        for universe in UNIVERSES:
            p = load_panel(con, universe)
            b = load_frozen_bucket_means(con, universe)
            p, rec = reconstruct_and_reconcile_buckets(p, b)

            panels[universe] = p
            frozen[universe] = rec
            all_symbols.update(p["symbol"].unique().tolist())
            all_signal_dates.update(p["signal_date"].unique().tolist())

    finally:
        con.close()

    min_signal = min(all_signal_dates)
    calendar = get_market_calendar(min_signal, CUTOFF)
    sessions = session_map(
        sorted(all_signal_dates),
        calendar,
        HORIZON,
    )

    if sessions["entry_date"].isna().any() or sessions["exit_date"].isna().any():
        bad = sessions[
            sessions["entry_date"].isna() | sessions["exit_date"].isna()
        ]
        raise RuntimeError(
            f"Trading-session mapping incomplete. Sample={bad.head(10).to_dict('records')}"
        )

    start_market = sessions["entry_date"].min()
    end_market = sessions["exit_date"].max()
    symbols = sorted(all_symbols)

    print("=" * 110)
    print("RET120 ABSOLUTE RETURN AUDIT V0.1")
    print("=" * 110)
    print("Symbols:", len(symbols))
    print("Signal dates:", min_signal, "->", max(all_signal_dates))
    print("Entry/exit market range:", start_market, "->", end_market)
    print("Price representation: QMT back_ratio")
    print()

    raw_market = xtdata.get_market_data_ex(
        field_list=["open", "close"],
        stock_list=symbols,
        period="1d",
        start_time=start_market,
        end_time=end_market,
        count=-1,
        dividend_type="back_ratio",
        fill_data=False,
    )
    market = parse_market_ex(raw_market, symbols, ("open", "close"))

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    payload = {
        "audit_id": "RET120_ABSOLUTE_RETURN_V01",
        "cutoff": CUTOFF,
        "horizon": HORIZON,
        "entry": "S1/T+1 OPEN",
        "exit": "S120 CLOSE",
        "price_representation": "QMT back_ratio",
        "costs": {
            "20bp": COSTS[0],
            "50bp": COSTS[1],
            "interpretation": "round-trip return haircut per cohort",
        },
        "universes": {},
        "notes": [
            "This is a cohort economic fast-reject, not a shared-equity portfolio.",
            "Daily cohort observations overlap heavily at a 120-session horizon.",
            "Quintiles are reconstructed and must exactly reconcile to frozen daily_buckets before use.",
            "excess_120 must reconcile to one common implied benchmark return per universe-date.",
        ],
    }

    summary_rows = []

    for universe in UNIVERSES:
        p = attach_raw_returns(
            panels[universe],
            market,
            sessions,
        )

        coverage = float(np.isfinite(p["raw_return_120"]).mean())

        missing = p[~np.isfinite(p["raw_return_120"])][
            ["signal_date", "symbol", "entry_date", "exit_date", "entry_open", "exit_close"]
        ]
        missing.head(50000).to_csv(
            OUT_DIR / f"{universe}_missing_raw_return_sample.csv",
            index=False,
            encoding="utf-8-sig",
        )

        if coverage < 0.995:
            raise RuntimeError(
                f"{universe} raw-return coverage {coverage:.4%} < 99.5%. "
                "Failing closed."
            )

        excess_rec = reconcile_excess_definition(p)
        daily = daily_bucket_economics(p)
        daily.to_csv(
            OUT_DIR / f"{universe}_daily_bucket_economics.csv",
            index=False,
            encoding="utf-8-sig",
        )

        all_dates = set(daily["signal_date"].unique().tolist())
        segment_map = {
            "ALL": all_dates,
            "F1": {d for d in all_dates if folds["F1"][0] <= d <= folds["F1"][1]},
            "F2": {d for d in all_dates if folds["F2"][0] <= d <= folds["F2"][1]},
            "F3": {d for d in all_dates if folds["F3"][0] <= d <= folds["F3"][1]},
            "ERA_SW2014": {d for d in all_dates if d < ERA_BOUNDARY},
            "ERA_SW2021": {d for d in all_dates if d >= ERA_BOUNDARY},
        }

        uni_result = {
            "bucket_reconciliation": frozen[universe],
            "raw_return_coverage": coverage,
            "excess_reconciliation": excess_rec,
            "segments": {},
        }

        for segment, dates in segment_map.items():
            agg = aggregate_segment(daily, dates)
            uni_result["segments"][segment] = agg

            summary_rows.append({
                "universe": universe,
                "segment": segment,
                "n_dates": agg["n_dates"],
                "q5_raw_mean": agg["q5_raw"]["mean"],
                "q5_raw_median": agg["q5_raw"]["median"],
                "q5_raw_trim5": agg["q5_raw"]["trim5"],
                "q5_raw_ex_top5": agg["q5_raw"]["ex_top5"],
                "q5_raw_positive_ratio": agg["q5_raw"]["positive_ratio"],
                "q5_after_20bp_mean": agg["q5_after_20bp"]["mean"],
                "q5_after_50bp_mean": agg["q5_after_50bp"]["mean"],
                "q5_excess_mean": agg["q5_excess"]["mean"],
                "q5_excess_median": agg["q5_excess"]["median"],
                "benchmark_mean": agg["benchmark"]["mean"],
            })

        payload["universes"][universe] = uni_result

    pd.DataFrame(summary_rows).to_csv(
        OUT_DIR / "summary.csv",
        index=False,
        encoding="utf-8-sig",
    )

    (OUT_DIR / "audit.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    for universe in UNIVERSES:
        u = payload["universes"][universe]
        print(universe.upper())
        print("-" * 110)
        print(
            "Bucket reconciliation:",
            f"count_mismatch={u['bucket_reconciliation']['count_mismatch_rows']}",
            f"max_mean_diff={u['bucket_reconciliation']['max_mean_abs_diff']:.3e}",
        )
        print(
            "Raw-return coverage:",
            f"{u['raw_return_coverage']:.4%}",
            "| excess common-benchmark max range:",
            f"{u['excess_reconciliation']['max_benchmark_range']:.3e}",
        )

        for segment in ("ALL", "F1", "F2", "F3", "ERA_SW2014", "ERA_SW2021"):
            a = u["segments"][segment]
            q = a["q5_raw"]
            print(
                f"{segment}: "
                f"Q5 raw={pct(q['mean'])} "
                f"(med={pct(q['median'])}, trim5={pct(q['trim5'])}, "
                f"exTop5={pct(q['ex_top5'])}, pos={q['positive_ratio']:.1%}) | "
                f"20bp={pct(a['q5_after_20bp']['mean'])} | "
                f"50bp={pct(a['q5_after_50bp']['mean'])} | "
                f"Q5 excess={pct(a['q5_excess']['mean'])} | "
                f"benchmark={pct(a['benchmark']['mean'])}"
            )
        print()

    print("Output:", OUT_DIR)
    print("No shared-equity portfolio was constructed.")
    print("No production database was modified.")


if __name__ == "__main__":
    main()
