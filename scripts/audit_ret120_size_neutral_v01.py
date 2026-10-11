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
CAPITAL_DIR = EDGE_DIR / "ret120_size_pit_source_v01"
TEMPORAL_DIR = EDGE_DIR / "ret120_temporal_v01"
OUT_DIR = EDGE_DIR / "ret120_size_neutral_v01"

CUTOFF = "20260924"
UNIVERSES = ("csi300", "csi500")


def finite(x: Any) -> bool:
    try:
        return bool(np.isfinite(float(x)))
    except (TypeError, ValueError):
        return False


def zscore(values: np.ndarray) -> np.ndarray:
    x = np.asarray(values, dtype=float)
    out = np.full(len(x), np.nan, dtype=float)
    mask = np.isfinite(x)
    if mask.sum() < 2:
        return out
    sd = x[mask].std(ddof=0)
    if not np.isfinite(sd) or sd <= 0:
        return out
    out[mask] = (x[mask] - x[mask].mean()) / sd
    return out


def ols_beta(y: np.ndarray, x_cols: list[np.ndarray]) -> np.ndarray:
    y = np.asarray(y, dtype=float)
    xs = [np.asarray(x, dtype=float) for x in x_cols]

    mask = np.isfinite(y)
    for x in xs:
        mask &= np.isfinite(x)

    if mask.sum() <= len(xs) + 2:
        return np.full(len(xs) + 1, np.nan)

    yy = y[mask]
    xx = np.column_stack([np.ones(mask.sum())] + [x[mask] for x in xs])

    try:
        beta, *_ = np.linalg.lstsq(xx, yy, rcond=None)
    except np.linalg.LinAlgError:
        return np.full(len(xs) + 1, np.nan)

    return beta


def residualize(x: np.ndarray, control: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    z = np.asarray(control, dtype=float)
    out = np.full(len(x), np.nan, dtype=float)
    beta = ols_beta(x, [z])

    if not np.all(np.isfinite(beta)):
        return out

    mask = np.isfinite(x) & np.isfinite(z)
    out[mask] = x[mask] - (beta[0] + beta[1] * z[mask])
    return out


def spearman_corr(a: np.ndarray, b: np.ndarray) -> float:
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    mask = np.isfinite(a) & np.isfinite(b)
    if mask.sum() < 3:
        return math.nan

    aa = pd.Series(a[mask]).rank(method="average").to_numpy(dtype=float)
    bb = pd.Series(b[mask]).rank(method="average").to_numpy(dtype=float)

    if aa.std(ddof=0) <= 0 or bb.std(ddof=0) <= 0:
        return math.nan

    return float(np.corrcoef(aa, bb)[0, 1])


def quintile_spread(score: np.ndarray, y: np.ndarray) -> tuple[float, float, float, int, int]:
    score = np.asarray(score, dtype=float)
    y = np.asarray(y, dtype=float)
    mask = np.isfinite(score) & np.isfinite(y)

    if mask.sum() < 20:
        return math.nan, math.nan, math.nan, 0, 0

    s = score[mask]
    yy = y[mask]

    # Percentile rank avoids qcut failures when ties occur.
    pct = pd.Series(s).rank(method="average", pct=True).to_numpy()
    q1 = pct <= 0.20
    q5 = pct > 0.80

    if q1.sum() == 0 or q5.sum() == 0:
        return math.nan, math.nan, math.nan, int(q1.sum()), int(q5.sum())

    q1_mean = float(np.mean(yy[q1]))
    q5_mean = float(np.mean(yy[q5]))
    return q5_mean - q1_mean, q5_mean, q1_mean, int(q5.sum()), int(q1.sum())


def trim_mean(values, p: float = 0.05) -> float:
    arr = np.asarray(list(values), dtype=float)
    arr = np.sort(arr[np.isfinite(arr)])
    if len(arr) == 0:
        return math.nan
    k = int(math.floor(len(arr) * p))
    if k == 0:
        return float(arr.mean())
    if 2 * k >= len(arr):
        return math.nan
    return float(arr[k:len(arr)-k].mean())


def mean_ex_top(values, p: float = 0.05) -> float:
    arr = np.asarray(list(values), dtype=float)
    arr = np.sort(arr[np.isfinite(arr)])
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


def normalize_date_index(v: Any) -> str | None:
    s = str(v)
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) < 8:
        return None
    d = digits[:8]
    try:
        datetime.strptime(d, "%Y%m%d")
    except ValueError:
        return None
    return d


def parse_market_data_ex(raw: Any, symbols: list[str]) -> pd.DataFrame:
    """
    Normalize the common xtdata.get_market_data_ex return shapes into:
        signal_date, symbol, close
    """
    rows: list[pd.DataFrame] = []

    if not isinstance(raw, dict):
        raise TypeError(f"Unexpected market data type: {type(raw)!r}")

    symbol_set = set(symbols)

    # Shape A: {symbol: DataFrame(index=date, columns=[close,...])}
    symbol_keys = [k for k in raw.keys() if str(k) in symbol_set]
    if symbol_keys:
        for symbol in symbol_keys:
            obj = raw[symbol]

            if isinstance(obj, pd.Series):
                df = obj.to_frame(name="close")
            elif isinstance(obj, pd.DataFrame):
                df = obj.copy()
            else:
                continue

            close_col = None
            for c in df.columns:
                if str(c).lower() == "close":
                    close_col = c
                    break

            if close_col is None:
                continue

            tmp = pd.DataFrame({
                "signal_date": [normalize_date_index(x) for x in df.index],
                "symbol": str(symbol),
                "close": pd.to_numeric(df[close_col], errors="coerce").to_numpy(),
            })
            rows.append(tmp)

    # Shape B: {"close": DataFrame(index=date, columns=symbols)}
    if not rows:
        close_obj = None
        for k, v in raw.items():
            if str(k).lower() == "close":
                close_obj = v
                break

        if isinstance(close_obj, pd.DataFrame):
            for symbol in close_obj.columns:
                if str(symbol) not in symbol_set:
                    continue
                tmp = pd.DataFrame({
                    "signal_date": [
                        normalize_date_index(x) for x in close_obj.index
                    ],
                    "symbol": str(symbol),
                    "close": pd.to_numeric(
                        close_obj[symbol], errors="coerce"
                    ).to_numpy(),
                })
                rows.append(tmp)

    if not rows:
        raise RuntimeError(
            "Could not normalize xtdata.get_market_data_ex result. "
            f"Top-level keys sample: {list(raw.keys())[:10]}"
        )

    out = pd.concat(rows, ignore_index=True)
    out = out.dropna(subset=["signal_date", "close"])
    out = out[np.isfinite(out["close"]) & (out["close"] > 0)]
    out = out.drop_duplicates(["signal_date", "symbol"], keep="last")
    return out


def load_folds() -> dict[str, tuple[str, str]]:
    path = TEMPORAL_DIR / "audit.json"
    if not path.exists():
        raise FileNotFoundError(path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = payload.get("folds") or {}
    folds = {}

    for name in ("F1", "F2", "F3"):
        item = raw.get(name) or {}
        start = str(item.get("start") or "")
        end = str(item.get("end") or "")
        if len(start) != 8 or len(end) != 8:
            raise RuntimeError(f"Invalid fold {name}: {item}")
        folds[name] = (start, end)

    return folds


def assign_fold(date: str, folds: dict[str, tuple[str, str]]) -> str | None:
    for name in ("F1", "F2", "F3"):
        start, end = folds[name]
        if start <= date <= end:
            return name
    return None


def load_panel(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    parts = []

    for universe in UNIVERSES:
        path = EDGE_DIR / f"trend_persistence_scan_{universe}_panel_v0221.parquet"
        if not path.exists():
            raise FileNotFoundError(path)

        q = path.as_posix().replace("'", "''")
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
            """
        ).fetchdf()
        df.insert(0, "universe", universe)
        parts.append(df)

    panel = pd.concat(parts, ignore_index=True)
    panel["signal_date"] = panel["signal_date"].astype(str)
    panel["symbol"] = panel["symbol"].astype(str)
    return panel


def attach_capital_asof(panel: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    events = events.copy()
    events["symbol"] = events["symbol"].astype(str)
    events["announce_date"] = events["announce_date"].astype(str)

    out_parts = []

    cap_groups = {
        symbol: g.sort_values("announce_date")
        for symbol, g in events.groupby("symbol", sort=False)
    }

    for symbol, left in panel.groupby("symbol", sort=False):
        left = left.copy()
        cap = cap_groups.get(symbol)

        if cap is None or cap.empty:
            left["capital_announce_date"] = None
            left["total_capital"] = np.nan
            left["circulating_capital"] = np.nan
            out_parts.append(left)
            continue

        cap_dates = cap["announce_date"].to_numpy(dtype=str)
        total = cap["total_capital"].to_numpy(dtype=float)
        circ = cap["circulating_capital"].to_numpy(dtype=float)

        dates = left["signal_date"].to_numpy(dtype=str)
        pos = np.searchsorted(cap_dates, dates, side="right") - 1
        ok = pos >= 0

        ann = np.full(len(left), None, dtype=object)
        total_out = np.full(len(left), np.nan, dtype=float)
        circ_out = np.full(len(left), np.nan, dtype=float)

        ann[ok] = cap_dates[pos[ok]]
        total_out[ok] = total[pos[ok]]
        circ_out[ok] = circ[pos[ok]]

        left["capital_announce_date"] = ann
        left["total_capital"] = total_out
        left["circulating_capital"] = circ_out
        out_parts.append(left)

    return pd.concat(out_parts, ignore_index=True)


def per_date_metrics(x: pd.DataFrame) -> dict[str, float | int]:
    ret = x["ret120"].to_numpy(dtype=float)
    y = x["excess_120"].to_numpy(dtype=float)
    log_total = x["log_total_mcap"].to_numpy(dtype=float)
    log_float = x["log_float_mcap"].to_numpy(dtype=float)

    zret = zscore(ret)
    ztotal = zscore(log_total)
    zfloat = zscore(log_float)

    raw_beta = ols_beta(y, [zret])
    total_beta = ols_beta(y, [zret, ztotal])
    float_beta = ols_beta(y, [zret, zfloat])

    residual_total = residualize(ret, log_total)
    residual_float = residualize(ret, log_float)

    total_spread, total_q5, total_q1, total_q5n, total_q1n = quintile_spread(
        residual_total, y
    )
    float_spread, float_q5, float_q1, float_q5n, float_q1n = quintile_spread(
        residual_float, y
    )

    raw_spread, raw_q5, raw_q1, raw_q5n, raw_q1n = quintile_spread(ret, y)

    return {
        "n": int(len(x)),
        "ret_size_spearman_total": spearman_corr(ret, log_total),
        "ret_size_spearman_float": spearman_corr(ret, log_float),
        "raw_ret_beta": raw_beta[1] if len(raw_beta) > 1 else math.nan,
        "ret_beta_control_total": (
            total_beta[1] if len(total_beta) > 1 else math.nan
        ),
        "size_beta_total": total_beta[2] if len(total_beta) > 2 else math.nan,
        "ret_beta_control_float": (
            float_beta[1] if len(float_beta) > 1 else math.nan
        ),
        "size_beta_float": float_beta[2] if len(float_beta) > 2 else math.nan,
        "raw_q5_q1_spread": raw_spread,
        "raw_q5_mean": raw_q5,
        "raw_q1_mean": raw_q1,
        "raw_q5_n": raw_q5n,
        "raw_q1_n": raw_q1n,
        "resid_total_q5_q1_spread": total_spread,
        "resid_total_q5_mean": total_q5,
        "resid_total_q1_mean": total_q1,
        "resid_total_q5_n": total_q5n,
        "resid_total_q1_n": total_q1n,
        "resid_float_q5_q1_spread": float_spread,
        "resid_float_q5_mean": float_q5,
        "resid_float_q1_mean": float_q1,
        "resid_float_q5_n": float_q5n,
        "resid_float_q1_n": float_q1n,
    }


def aggregate_segment(x: pd.DataFrame) -> dict[str, Any]:
    raw = summarize(x["raw_ret_beta"])
    ctl_total = summarize(x["ret_beta_control_total"])
    ctl_float = summarize(x["ret_beta_control_float"])
    resid_total = summarize(x["resid_total_q5_q1_spread"])
    resid_float = summarize(x["resid_float_q5_q1_spread"])
    raw_spread = summarize(x["raw_q5_q1_spread"])

    raw_mean = raw["mean"]
    total_mean = ctl_total["mean"]
    float_mean = ctl_float["mean"]

    return {
        "n_dates": int(len(x)),
        "mean_cross_section_n": float(x["n"].mean()) if len(x) else math.nan,
        "mean_ret_size_spearman_total":
            float(x["ret_size_spearman_total"].mean()) if len(x) else math.nan,
        "mean_ret_size_spearman_float":
            float(x["ret_size_spearman_float"].mean()) if len(x) else math.nan,
        "raw_ret_beta": raw,
        "ret_beta_control_total": ctl_total,
        "ret_beta_control_float": ctl_float,
        "beta_retention_total": (
            total_mean / raw_mean
            if finite(raw_mean) and abs(float(raw_mean)) > 1e-12
            else math.nan
        ),
        "beta_retention_float": (
            float_mean / raw_mean
            if finite(raw_mean) and abs(float(raw_mean)) > 1e-12
            else math.nan
        ),
        "raw_q5_q1_spread": raw_spread,
        "resid_total_q5_q1_spread": resid_total,
        "resid_float_q5_q1_spread": resid_float,
    }


def pct(x: Any) -> str:
    return "NA" if not finite(x) else f"{float(x) * 100:+.3f}%"


def num(x: Any) -> str:
    return "NA" if not finite(x) else f"{float(x):+.3f}"


def main():
    capital_path = CAPITAL_DIR / "capital_events.csv"
    if not capital_path.exists():
        raise FileNotFoundError(
            f"Missing Capital source audit output: {capital_path}"
        )

    folds = load_folds()
    con = duckdb.connect()

    try:
        panel = load_panel(con)
    finally:
        con.close()

    events = pd.read_csv(
        capital_path,
        dtype={"symbol": str, "announce_date": str, "report_date": str},
    )

    symbols = sorted(panel["symbol"].unique().tolist())
    start = str(panel["signal_date"].min())
    end = str(panel["signal_date"].max())

    print("=" * 100)
    print("RET120 SIZE-NEUTRAL AUDIT V0.1")
    print("=" * 100)
    print("Panel rows:", len(panel))
    print("Symbols:", len(symbols))
    print("Date range:", start, "->", end)
    print("Market data: QMT 1d close, dividend_type='none', fill_data=False")
    print()

    raw_market = xtdata.get_market_data_ex(
        field_list=["close"],
        stock_list=symbols,
        period="1d",
        start_time=start,
        end_time=end,
        count=-1,
        dividend_type="none",
        fill_data=False,
    )
    closes = parse_market_data_ex(raw_market, symbols)

    merged = panel.merge(
        closes,
        on=["signal_date", "symbol"],
        how="left",
        validate="many_to_one",
    )
    merged = attach_capital_asof(merged, events)

    merged["total_mcap"] = merged["close"] * merged["total_capital"]
    merged["float_mcap"] = merged["close"] * merged["circulating_capital"]

    valid_total = (
        np.isfinite(merged["total_mcap"])
        & (merged["total_mcap"] > 0)
    )
    valid_float = (
        np.isfinite(merged["float_mcap"])
        & (merged["float_mcap"] > 0)
    )
    close_ok = np.isfinite(merged["close"]) & (merged["close"] > 0)

    data_quality = {
        "panel_rows": int(len(merged)),
        "close_coverage": float(close_ok.mean()),
        "total_mcap_coverage": float(valid_total.mean()),
        "float_mcap_coverage": float(valid_float.mean()),
    }

    print(
        "Coverage:",
        f"close={data_quality['close_coverage']:.4%}",
        f"total_mcap={data_quality['total_mcap_coverage']:.4%}",
        f"float_mcap={data_quality['float_mcap_coverage']:.4%}",
    )

    if (
        data_quality["close_coverage"] < 0.995
        or data_quality["total_mcap_coverage"] < 0.995
        or data_quality["float_mcap_coverage"] < 0.995
    ):
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        missing = merged[
            ~(close_ok & valid_total & valid_float)
        ][
            [
                "universe", "signal_date", "symbol",
                "close", "total_capital", "circulating_capital",
            ]
        ]
        missing.head(50000).to_csv(
            OUT_DIR / "missing_coverage_sample.csv",
            index=False,
            encoding="utf-8-sig",
        )
        raise RuntimeError(
            "Size audit coverage < 99.5%. Failing closed. "
            "See missing_coverage_sample.csv."
        )

    work = merged[close_ok & valid_total & valid_float].copy()
    work["log_total_mcap"] = np.log(work["total_mcap"])
    work["log_float_mcap"] = np.log(work["float_mcap"])
    work["fold"] = work["signal_date"].map(lambda d: assign_fold(d, folds))

    if work["fold"].isna().any():
        raise RuntimeError("Rows outside frozen F1/F2/F3 detected.")

    daily_rows = []
    for (universe, signal_date), x in work.groupby(
        ["universe", "signal_date"], sort=True
    ):
        if len(x) < 100:
            continue
        row = {
            "universe": universe,
            "signal_date": signal_date,
            "fold": x["fold"].iloc[0],
        }
        row.update(per_date_metrics(x))
        daily_rows.append(row)

    daily = pd.DataFrame(daily_rows)

    if daily.empty:
        raise RuntimeError("No daily cross-sectional metrics produced.")

    summary_rows = []
    payload_results = {}

    for universe in UNIVERSES:
        payload_results[universe] = {}
        for segment in ("ALL", "F1", "F2", "F3"):
            if segment == "ALL":
                x = daily[daily["universe"] == universe]
            else:
                x = daily[
                    (daily["universe"] == universe)
                    & (daily["fold"] == segment)
                ]

            agg = aggregate_segment(x)
            payload_results[universe][segment] = agg

            summary_rows.append({
                "universe": universe,
                "segment": segment,
                "n_dates": agg["n_dates"],
                "mean_cross_section_n": agg["mean_cross_section_n"],
                "ret_size_corr_total":
                    agg["mean_ret_size_spearman_total"],
                "ret_size_corr_float":
                    agg["mean_ret_size_spearman_float"],
                "raw_ret_beta_mean":
                    agg["raw_ret_beta"]["mean"],
                "ctl_total_ret_beta_mean":
                    agg["ret_beta_control_total"]["mean"],
                "ctl_total_ret_beta_median":
                    agg["ret_beta_control_total"]["median"],
                "ctl_total_ret_beta_trim5":
                    agg["ret_beta_control_total"]["trim5"],
                "ctl_total_ret_beta_ex_top5":
                    agg["ret_beta_control_total"]["ex_top5"],
                "ctl_total_ret_beta_pos_ratio":
                    agg["ret_beta_control_total"]["positive_ratio"],
                "beta_retention_total":
                    agg["beta_retention_total"],
                "ctl_float_ret_beta_mean":
                    agg["ret_beta_control_float"]["mean"],
                "beta_retention_float":
                    agg["beta_retention_float"],
                "raw_q5_q1_mean":
                    agg["raw_q5_q1_spread"]["mean"],
                "resid_total_q5_q1_mean":
                    agg["resid_total_q5_q1_spread"]["mean"],
                "resid_total_q5_q1_median":
                    agg["resid_total_q5_q1_spread"]["median"],
                "resid_total_q5_q1_trim5":
                    agg["resid_total_q5_q1_spread"]["trim5"],
                "resid_total_q5_q1_ex_top5":
                    agg["resid_total_q5_q1_spread"]["ex_top5"],
                "resid_float_q5_q1_mean":
                    agg["resid_float_q5_q1_spread"]["mean"],
            })

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    daily.to_csv(
        OUT_DIR / "daily_size_neutral_metrics.csv",
        index=False,
        encoding="utf-8-sig",
    )
    pd.DataFrame(summary_rows).to_csv(
        OUT_DIR / "summary.csv",
        index=False,
        encoding="utf-8-sig",
    )

    payload = {
        "audit_id": "RET120_SIZE_NEUTRAL_V01",
        "cutoff": CUTOFF,
        "data_quality": data_quality,
        "results": payload_results,
        "method": {
            "outcome": "excess_120",
            "factor": "ret120",
            "primary_size": "log(close_none * latest-announced total_capital)",
            "secondary_size":
                "log(close_none * latest-announced circulating_capital)",
            "pit_rule": "capital.m_anntime <= signal_date",
            "daily_beta":
                "cross-sectional OLS of excess_120 on z(ret120) and z(log size)",
            "residual_spread":
                "Q5-Q1 excess_120 after residualizing ret120 on log size",
            "aggregation": "date-equal",
            "significance_test": "not_run_due_to_overlapping_120d_labels",
            "industry_control": "deferred_no_historical_pit_source",
        },
        "notes": [
            "This is an exposure audit, not a strategy backtest.",
            "No transaction cost is applied.",
            "No portfolio construction is applied.",
            "Daily observations overlap because the outcome horizon is 120 sessions.",
        ],
    }
    (OUT_DIR / "audit.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print()
    for universe in UNIVERSES:
        print(universe.upper())
        print("-" * 100)
        for segment in ("ALL", "F1", "F2", "F3"):
            a = payload_results[universe][segment]
            b = a["ret_beta_control_total"]
            r = a["resid_total_q5_q1_spread"]

            print(
                f"{segment}: "
                f"ret~size rho={num(a['mean_ret_size_spearman_total'])} | "
                f"raw beta={pct(a['raw_ret_beta']['mean'])} | "
                f"size-ctl beta={pct(b['mean'])} "
                f"(med={pct(b['median'])}, trim5={pct(b['trim5'])}, "
                f"exTop5={pct(b['ex_top5'])}, pos={b['positive_ratio']:.1%}) | "
                f"beta retention={num(a['beta_retention_total'])} | "
                f"resid Q5-Q1={pct(r['mean'])} "
                f"(med={pct(r['median'])}, trim5={pct(r['trim5'])}, "
                f"exTop5={pct(r['ex_top5'])})"
            )
        print()

    print("Output:", OUT_DIR)
    print("Industry control remains deferred: no historical PIT industry source.")
    print("No transaction cost or portfolio construction was applied.")
    print("No production database was modified.")


if __name__ == "__main__":
    main()
