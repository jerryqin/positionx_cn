from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd


FEATURE = "ret120"
UNIVERSES = ("csi300", "csi500")
HORIZONS = (20, 60, 120)

ROOT = Path(__file__).resolve().parents[1]
EDGE_DIR = ROOT / "data" / "research" / "edge_discovery"
OUTPUT_DIR = EDGE_DIR / "ret120_robustness_v01"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def finite_array(values) -> np.ndarray:
    arr = np.asarray(list(values), dtype=float)
    return arr[np.isfinite(arr)]


def trimmed_mean(values, proportion: float = 0.05) -> float:
    arr = np.sort(finite_array(values))
    n = len(arr)
    if n == 0:
        return math.nan

    k = int(math.floor(n * proportion))
    if k == 0:
        return float(arr.mean())

    if 2 * k >= n:
        return math.nan

    return float(arr[k:n - k].mean())


def mean_without_top(values, proportion: float) -> float:
    arr = np.sort(finite_array(values))
    n = len(arr)
    if n == 0:
        return math.nan

    k = int(math.floor(n * proportion))
    if k == 0:
        return float(arr.mean())

    remain = arr[:n - k]
    if len(remain) == 0:
        return math.nan

    return float(remain.mean())


def mean_without_bottom(values, proportion: float) -> float:
    arr = np.sort(finite_array(values))
    n = len(arr)
    if n == 0:
        return math.nan

    k = int(math.floor(n * proportion))
    if k == 0:
        return float(arr.mean())

    remain = arr[k:]
    if len(remain) == 0:
        return math.nan

    return float(remain.mean())


def distribution_stats(values) -> dict:
    arr = finite_array(values)

    if len(arr) == 0:
        return {
            "n_dates": 0,
            "mean": math.nan,
            "median": math.nan,
            "trim5_mean": math.nan,
            "std": math.nan,
            "p10": math.nan,
            "p25": math.nan,
            "p50": math.nan,
            "p75": math.nan,
            "p90": math.nan,
            "positive_date_ratio": math.nan,
            "mean_ex_top1": math.nan,
            "mean_ex_top5": math.nan,
            "mean_ex_top10": math.nan,
            "mean_ex_bottom1": math.nan,
            "mean_ex_bottom5": math.nan,
            "mean_ex_bottom10": math.nan,
        }

    return {
        "n_dates": int(len(arr)),
        "mean": float(arr.mean()),
        "median": float(np.median(arr)),
        "trim5_mean": trimmed_mean(arr, 0.05),
        "std": float(arr.std(ddof=1)) if len(arr) > 1 else 0.0,
        "p10": float(np.quantile(arr, 0.10)),
        "p25": float(np.quantile(arr, 0.25)),
        "p50": float(np.quantile(arr, 0.50)),
        "p75": float(np.quantile(arr, 0.75)),
        "p90": float(np.quantile(arr, 0.90)),
        "positive_date_ratio": float((arr > 0).mean()),
        "mean_ex_top1": mean_without_top(arr, 0.01),
        "mean_ex_top5": mean_without_top(arr, 0.05),
        "mean_ex_top10": mean_without_top(arr, 0.10),
        "mean_ex_bottom1": mean_without_bottom(arr, 0.01),
        "mean_ex_bottom5": mean_without_bottom(arr, 0.05),
        "mean_ex_bottom10": mean_without_bottom(arr, 0.10),
    }


def classify_edge_shape(stats: dict) -> str:
    """
    Sign-based descriptive classification only.
    This is NOT yet a Strategy gate.
    """
    mean = stats["mean"]
    median = stats["median"]
    trim5 = stats["trim5_mean"]
    ex_top5 = stats["mean_ex_top5"]

    if not np.isfinite(mean) or mean <= 0:
        return "NO_POSITIVE_DATE_EQUAL_EDGE"

    if (
        np.isfinite(median)
        and np.isfinite(trim5)
        and np.isfinite(ex_top5)
        and median > 0
        and trim5 > 0
        and ex_top5 > 0
    ):
        return "BROAD_BASED"

    return "TAIL_SENSITIVE"


def classify_selection(
    q5_vs_universe: float,
    q1_vs_universe: float,
) -> str:
    q5_pos = np.isfinite(q5_vs_universe) and q5_vs_universe > 0
    q1_neg = np.isfinite(q1_vs_universe) and q1_vs_universe < 0

    if q5_pos and q1_neg:
        return "POSITIVE_SELECTION_AND_AVOIDANCE"

    if q5_pos:
        return "POSITIVE_SELECTION"

    if q1_neg:
        return "AVOIDANCE_ONLY"

    return "NO_CLEAR_SELECTION_EDGE"


def load_daily_buckets(
    con: duckdb.DuckDBPyConnection,
    path: Path,
) -> pd.DataFrame:
    p = path.as_posix().replace("'", "''")

    sql = f"""
        SELECT
            signal_date,
            feature,
            horizon,
            bucket,
            n,
            mean_excess,
            median_excess
        FROM read_parquet('{p}')
        WHERE feature = '{FEATURE}'
          AND horizon IN (20, 60, 120)
          AND bucket IN ('Q1', 'Q2', 'Q3', 'Q4', 'Q5')
        ORDER BY signal_date, horizon, bucket
    """

    return con.execute(sql).fetchdf()


def load_panel_daily(
    con: duckdb.DuckDBPyConnection,
    path: Path,
    horizon: int,
) -> pd.DataFrame:
    p = path.as_posix().replace("'", "''")

    excess_col = f"excess_{horizon}"
    reject_col = f"label_quality_reject_{horizon}"

    sql = f"""
        SELECT
            signal_date,
            COUNT(*) AS panel_n,
            AVG({excess_col}) AS panel_mean_excess
        FROM read_parquet('{p}')
        WHERE ret120 IS NOT NULL
          AND {excess_col} IS NOT NULL
          AND COALESCE({reject_col}, FALSE) = FALSE
        GROUP BY signal_date
        ORDER BY signal_date
    """

    return con.execute(sql).fetchdf()


def compute_horizon(
    daily: pd.DataFrame,
    panel_daily: pd.DataFrame,
    horizon: int,
):
    x = daily[daily["horizon"] == horizon].copy()

    if x.empty:
        raise RuntimeError(f"No rows for horizon={horizon}")

    mean_pivot = x.pivot(
        index="signal_date",
        columns="bucket",
        values="mean_excess",
    )

    median_pivot = x.pivot(
        index="signal_date",
        columns="bucket",
        values="median_excess",
    )

    n_pivot = x.pivot(
        index="signal_date",
        columns="bucket",
        values="n",
    )

    required = {"Q1", "Q2", "Q3", "Q4", "Q5"}

    if not required.issubset(mean_pivot.columns):
        missing = sorted(required - set(mean_pivot.columns))
        raise RuntimeError(
            f"horizon={horizon}: missing buckets {missing}"
        )

    # Only dates with all five frozen quintiles.
    valid_dates = (
        mean_pivot[["Q1", "Q2", "Q3", "Q4", "Q5"]]
        .dropna()
        .index
    )

    mean_pivot = mean_pivot.loc[valid_dates]
    median_pivot = median_pivot.loc[valid_dates]
    n_pivot = n_pivot.loc[valid_dates]

    daily_out = pd.DataFrame(index=valid_dates)

    daily_out["q1_mean_excess"] = mean_pivot["Q1"]
    daily_out["q2_mean_excess"] = mean_pivot["Q2"]
    daily_out["q3_mean_excess"] = mean_pivot["Q3"]
    daily_out["q4_mean_excess"] = mean_pivot["Q4"]
    daily_out["q5_mean_excess"] = mean_pivot["Q5"]

    daily_out["q5_q1_spread"] = (
        daily_out["q5_mean_excess"]
        - daily_out["q1_mean_excess"]
    )

    # Difference between the within-bucket constituent medians.
    # Secondary robustness diagnostic; not the primary spread.
    daily_out["q5_q1_bucket_median_spread"] = (
        median_pivot["Q5"]
        - median_pivot["Q1"]
    )

    total_n = n_pivot[["Q1", "Q2", "Q3", "Q4", "Q5"]].sum(axis=1)

    weighted_sum = (
        mean_pivot[["Q1", "Q2", "Q3", "Q4", "Q5"]]
        * n_pivot[["Q1", "Q2", "Q3", "Q4", "Q5"]]
    ).sum(axis=1)

    daily_out["bucket_n"] = total_n
    daily_out["universe_ew_excess"] = weighted_sum / total_n

    daily_out["q5_minus_universe"] = (
        daily_out["q5_mean_excess"]
        - daily_out["universe_ew_excess"]
    )

    daily_out["q1_minus_universe"] = (
        daily_out["q1_mean_excess"]
        - daily_out["universe_ew_excess"]
    )

    daily_out = (
        daily_out.reset_index()
        .merge(
            panel_daily,
            on="signal_date",
            how="left",
        )
    )

    daily_out["count_diff"] = (
        daily_out["bucket_n"] - daily_out["panel_n"]
    )

    daily_out["universe_mean_diff"] = (
        daily_out["universe_ew_excess"]
        - daily_out["panel_mean_excess"]
    )

    spread_stats = distribution_stats(
        daily_out["q5_q1_spread"]
    )

    bucket_median_spread_stats = distribution_stats(
        daily_out["q5_q1_bucket_median_spread"]
    )

    q5_vs_universe_stats = distribution_stats(
        daily_out["q5_minus_universe"]
    )

    q1_vs_universe_stats = distribution_stats(
        daily_out["q1_minus_universe"]
    )

    bucket_rows = []

    for bucket in ["Q1", "Q2", "Q3", "Q4", "Q5"]:
        vals = mean_pivot[bucket]

        bucket_rows.append(
            {
                "horizon": horizon,
                "bucket": bucket,
                "date_equal_mean_excess": float(vals.mean()),
                "date_equal_median_of_daily_mean_excess":
                    float(vals.median()),
                "positive_date_ratio": float((vals > 0).mean()),
                "n_dates": int(vals.notna().sum()),
            }
        )

    bucket_df = pd.DataFrame(bucket_rows)

    ordered_means = (
        bucket_df
        .set_index("bucket")
        .loc[
            ["Q1", "Q2", "Q3", "Q4", "Q5"],
            "date_equal_mean_excess",
        ]
        .to_numpy()
    )

    monotonicity = float(
        np.mean(np.diff(ordered_means) > 0)
    )

    mismatch_count = int(
        (daily_out["count_diff"].fillna(0) != 0).sum()
    )

    mean_diff_abs_max = float(
        daily_out["universe_mean_diff"]
        .abs()
        .max()
    )

    summary = {
        "horizon": horizon,
        "n_dates": int(len(daily_out)),
        "spread": spread_stats,
        "bucket_median_spread": bucket_median_spread_stats,
        "q5_vs_universe": q5_vs_universe_stats,
        "q1_vs_universe": q1_vs_universe_stats,
        "edge_shape": classify_edge_shape(spread_stats),
        "selection_type": classify_selection(
            q5_vs_universe_stats["mean"],
            q1_vs_universe_stats["mean"],
        ),
        "date_equal_bucket_monotonicity": monotonicity,
        "panel_bucket_count_mismatch_dates": mismatch_count,
        "panel_bucket_universe_mean_abs_max_diff":
            mean_diff_abs_max,
    }

    return summary, daily_out, bucket_df


def pct(x):
    if x is None or not np.isfinite(x):
        return "NA"
    return f"{x * 100:+.3f}%"


def print_horizon_summary(universe: str, summary: dict):
    s = summary["spread"]
    q5 = summary["q5_vs_universe"]
    q1 = summary["q1_vs_universe"]

    print()
    print(
        f"{universe.upper()} | "
        f"ret120 | S{summary['horizon']}"
    )
    print("-" * 78)

    print(
        "Q5-Q1 date-equal:"
        f" mean={pct(s['mean'])}"
        f" median={pct(s['median'])}"
        f" trim5={pct(s['trim5_mean'])}"
        f" exTop5={pct(s['mean_ex_top5'])}"
        f" posDays={s['positive_date_ratio']:.1%}"
    )

    print(
        "Tail:"
        f" exTop1={pct(s['mean_ex_top1'])}"
        f" exTop10={pct(s['mean_ex_top10'])}"
        f" exBottom5={pct(s['mean_ex_bottom5'])}"
    )

    print(
        "Selection:"
        f" Q5-Universe={pct(q5['mean'])}"
        f" Q1-Universe={pct(q1['mean'])}"
    )

    print(
        "Shape:",
        summary["edge_shape"],
        "| Type:",
        summary["selection_type"],
    )

    print(
        "Bucket monotonicity:",
        f"{summary['date_equal_bucket_monotonicity']:.2f}",
    )

    print(
        "Data reconciliation:"
        f" count_mismatch_dates="
        f"{summary['panel_bucket_count_mismatch_dates']}"
        f" mean_abs_max_diff="
        f"{summary['panel_bucket_universe_mean_abs_max_diff']:.3e}"
    )


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()

    report = {
        "audit_id": "RET120_ROBUSTNESS_V01",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "feature": FEATURE,
        "universes": list(UNIVERSES),
        "horizons": list(HORIZONS),
        "method": {
            "primary_unit": "signal_date",
            "primary_stat": "date-equal Q5-Q1 mean_excess spread",
            "trim": 0.05,
            "tail_exclusion": [0.01, 0.05, 0.10],
            "re_bucket": False,
            "cost_applied": False,
            "strategy_gate": False,
        },
        "inputs": {},
        "results": {},
    }

    all_daily = []
    all_buckets = []
    all_summary_rows = []

    try:
        for universe in UNIVERSES:
            daily_path = (
                EDGE_DIR
                / f"trend_persistence_scan_{universe}"
                  f"_daily_buckets_v0221.parquet"
            )

            panel_path = (
                EDGE_DIR
                / f"trend_persistence_scan_{universe}"
                  f"_panel_v0221.parquet"
            )

            if not daily_path.exists():
                raise FileNotFoundError(daily_path)

            if not panel_path.exists():
                raise FileNotFoundError(panel_path)

            report["inputs"][universe] = {
                "daily_buckets": str(daily_path),
                "daily_buckets_sha256":
                    sha256_file(daily_path),
                "panel": str(panel_path),
                "panel_sha256":
                    sha256_file(panel_path),
            }

            daily = load_daily_buckets(
                con,
                daily_path,
            )

            report["results"][universe] = {}

            for horizon in HORIZONS:
                panel_daily = load_panel_daily(
                    con,
                    panel_path,
                    horizon,
                )

                summary, daily_out, bucket_df = (
                    compute_horizon(
                        daily,
                        panel_daily,
                        horizon,
                    )
                )

                report["results"][universe][
                    str(horizon)
                ] = summary

                print_horizon_summary(
                    universe,
                    summary,
                )

                daily_out.insert(
                    0,
                    "universe",
                    universe,
                )
                daily_out.insert(
                    1,
                    "horizon",
                    horizon,
                )

                bucket_df.insert(
                    0,
                    "universe",
                    universe,
                )

                all_daily.append(daily_out)
                all_buckets.append(bucket_df)

                all_summary_rows.append(
                    {
                        "universe": universe,
                        "horizon": horizon,
                        "n_dates": summary["n_dates"],
                        "spread_mean":
                            summary["spread"]["mean"],
                        "spread_median":
                            summary["spread"]["median"],
                        "spread_trim5":
                            summary["spread"]["trim5_mean"],
                        "spread_ex_top5":
                            summary["spread"]["mean_ex_top5"],
                        "positive_date_ratio":
                            summary["spread"]
                            ["positive_date_ratio"],
                        "q5_minus_universe":
                            summary["q5_vs_universe"]["mean"],
                        "q1_minus_universe":
                            summary["q1_vs_universe"]["mean"],
                        "monotonicity":
                            summary[
                                "date_equal_bucket_monotonicity"
                            ],
                        "edge_shape":
                            summary["edge_shape"],
                        "selection_type":
                            summary["selection_type"],
                        "count_mismatch_dates":
                            summary[
                                "panel_bucket_count_mismatch_dates"
                            ],
                        "universe_mean_abs_max_diff":
                            summary[
                                "panel_bucket_universe_mean_abs_max_diff"
                            ],
                    }
                )

    finally:
        con.close()

    pd.concat(
        all_daily,
        ignore_index=True,
    ).to_csv(
        OUTPUT_DIR / "daily_metrics.csv",
        index=False,
    )

    pd.concat(
        all_buckets,
        ignore_index=True,
    ).to_csv(
        OUTPUT_DIR / "bucket_summary.csv",
        index=False,
    )

    pd.DataFrame(
        all_summary_rows
    ).to_csv(
        OUTPUT_DIR / "summary.csv",
        index=False,
    )

    with (
        OUTPUT_DIR / "audit.json"
    ).open(
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            report,
            f,
            ensure_ascii=False,
            indent=2,
        )

    print()
    print("=" * 78)
    print("RET120 ROBUSTNESS V0.1 COMPLETE")
    print("=" * 78)
    print("Output:", OUTPUT_DIR)
    print("No source parquet was modified.")
    print("No strategy gate was applied.")
    print("No transaction cost was applied.")


if __name__ == "__main__":
    main()
