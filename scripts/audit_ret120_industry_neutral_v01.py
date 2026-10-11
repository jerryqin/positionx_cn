from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
EDGE_DIR = ROOT / "data" / "research" / "edge_discovery"
TEMPORAL_DIR = EDGE_DIR / "ret120_temporal_v01"
OUT_DIR = EDGE_DIR / "ret120_industry_neutral_v01"
DB_PATH = ROOT / "data" / "research" / "positionx_research.duckdb"

UNIVERSES = ("csi300", "csi500")
EXPECTED_ASSET_ID = "SW_INDUSTRY_PIT_L1_V20260927"
CUTOFF = "20260924"


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


def industry_demean(
    values: np.ndarray,
    groups: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Date-local within-industry transformation.

    Returns:
      residuals
      group_sizes

    Singleton industries get a mathematical residual of 0 but are explicitly
    excluded from industry-neutral beta/spread calculations downstream.
    """
    s = pd.Series(np.asarray(values, dtype=float))
    g = pd.Series(np.asarray(groups, dtype=object))

    means = s.groupby(g, sort=False).transform("mean")
    sizes = s.groupby(g, sort=False).transform("size").to_numpy(dtype=int)
    resid = (s - means).to_numpy(dtype=float)
    return resid, sizes


def quintile_spread(
    score: np.ndarray,
    y: np.ndarray,
) -> tuple[float, float, float, int, int, np.ndarray]:
    score = np.asarray(score, dtype=float)
    y = np.asarray(y, dtype=float)
    mask = np.isfinite(score) & np.isfinite(y)

    selected = np.zeros(len(score), dtype=bool)

    if mask.sum() < 20:
        return math.nan, math.nan, math.nan, 0, 0, selected

    s = score[mask]
    yy = y[mask]
    pct = pd.Series(s).rank(method="average", pct=True).to_numpy()

    q1 = pct <= 0.20
    q5 = pct > 0.80

    if q1.sum() == 0 or q5.sum() == 0:
        return math.nan, math.nan, math.nan, int(q5.sum()), int(q1.sum()), selected

    q1_mean = float(np.mean(yy[q1]))
    q5_mean = float(np.mean(yy[q5]))

    mask_idx = np.flatnonzero(mask)
    selected[mask_idx[q5]] = True

    return (
        q5_mean - q1_mean,
        q5_mean,
        q1_mean,
        int(q5.sum()),
        int(q1.sum()),
        selected,
    )


def active_industry_share(
    groups: np.ndarray,
    selected: np.ndarray,
    eligible: np.ndarray | None = None,
) -> tuple[float, float, float, float]:
    groups = np.asarray(groups, dtype=object)
    selected = np.asarray(selected, dtype=bool)

    if eligible is None:
        eligible = np.ones(len(groups), dtype=bool)
    else:
        eligible = np.asarray(eligible, dtype=bool)

    base_groups = groups[eligible]
    sel_groups = groups[selected & eligible]

    if len(base_groups) == 0 or len(sel_groups) == 0:
        return math.nan, math.nan, math.nan, math.nan

    base = pd.Series(base_groups).value_counts(normalize=True)
    sel = pd.Series(sel_groups).value_counts(normalize=True)

    keys = base.index.union(sel.index)
    base = base.reindex(keys, fill_value=0.0)
    sel = sel.reindex(keys, fill_value=0.0)

    diff = sel - base
    active_share = float(0.5 * np.abs(diff).sum())
    max_overweight = float(diff.max())
    sel_hhi = float((sel**2).sum())
    base_hhi = float((base**2).sum())

    return active_share, max_overweight, sel_hhi, base_hhi


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
        start, end = folds[name]
        if start <= date <= end:
            return name
    return None


def verify_reference_asset(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    schemas = {
        r[0]
        for r in con.execute(
            "SELECT schema_name FROM information_schema.schemata"
        ).fetchall()
    }
    if "reference" not in schemas:
        raise RuntimeError("DuckDB reference schema not found.")

    row = con.execute("""
        SELECT
            asset_id,
            row_count,
            symbol_count,
            source_csv_sha256,
            taxonomy_boundary,
            crosswalk_policy
        FROM reference.sw_industry_pit_assets
        ORDER BY asset_version DESC, ingested_at DESC
        LIMIT 1
    """).fetchone()

    if row is None:
        raise RuntimeError("No Shenwan PIT asset registered.")

    asset = {
        "asset_id": row[0],
        "row_count": int(row[1]),
        "symbol_count": int(row[2]),
        "source_csv_sha256": row[3],
        "taxonomy_boundary": row[4],
        "crosswalk_policy": row[5],
    }

    if asset["asset_id"] != EXPECTED_ASSET_ID:
        raise RuntimeError(
            f"Unexpected active Shenwan asset: {asset['asset_id']}"
        )

    if asset["crosswalk_policy"] != (
        "NO_SILENT_CROSSWALK; DATE_LOCAL_OR_ERA_STRATIFIED_ONLY"
    ):
        raise RuntimeError(
            "Reference asset crosswalk policy differs from the audited contract."
        )

    return asset


def panel_path(universe: str) -> Path:
    return EDGE_DIR / f"trend_persistence_scan_{universe}_panel_v0221.parquet"


def load_joined_panel(
    con: duckdb.DuckDBPyConnection,
    universe: str,
) -> pd.DataFrame:
    p = panel_path(universe)
    if not p.exists():
        raise FileNotFoundError(p)

    q = p.as_posix().replace("'", "''")

    df = con.execute(
        f"""
        WITH panel AS (
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
        ),
        joined AS (
            SELECT
                p.signal_date,
                p.symbol,
                p.ret120,
                p.excess_120,
                i.industry_code,
                i.industry_name,
                i.standard,
                i.asset_id,
                COUNT(i.symbol) OVER (
                    PARTITION BY p.signal_date, p.symbol
                ) AS industry_match_count
            FROM panel p
            LEFT JOIN reference.sw_industry_pit_l1_current i
              ON i.symbol = p.symbol
             AND CAST(strptime(p.signal_date, '%Y%m%d') AS DATE)
                    >= i.effective_from
             AND (
                    i.effective_to_exclusive IS NULL
                    OR CAST(strptime(p.signal_date, '%Y%m%d') AS DATE)
                        < i.effective_to_exclusive
                 )
        )
        SELECT *
        FROM joined
        ORDER BY signal_date, symbol
        """
    ).fetchdf()

    if df.empty:
        raise RuntimeError(f"No joined rows for {universe}.")

    bad = df[df["industry_match_count"] != 1]
    if not bad.empty:
        sample = bad.head(20).to_dict(orient="records")
        raise RuntimeError(
            f"Industry PIT join is not exactly-one for {len(bad)} rows. "
            f"Sample={sample}"
        )

    if df[["industry_code", "industry_name", "standard"]].isna().any().any():
        raise RuntimeError("Missing industry fields after supposedly complete PIT join.")

    df.insert(0, "universe", universe)
    df["signal_date"] = df["signal_date"].astype(str)
    df["symbol"] = df["symbol"].astype(str)
    df["industry_key"] = (
        df["standard"].astype(str)
        + "|"
        + df["industry_code"].astype(str)
    )

    return df


def per_date_metrics(x: pd.DataFrame) -> dict[str, Any]:
    standards = sorted(x["standard"].astype(str).unique().tolist())
    if len(standards) != 1:
        raise RuntimeError(
            f"More than one taxonomy generation on one date: {standards}"
        )

    ret = x["ret120"].to_numpy(dtype=float)
    y = x["excess_120"].to_numpy(dtype=float)
    groups = x["industry_key"].to_numpy(dtype=object)

    raw_beta = ols_beta(y, [zscore(ret)])
    raw_spread, _, _, _, _, raw_q5 = quintile_spread(ret, y)

    ret_w, sizes = industry_demean(ret, groups)
    y_w, _ = industry_demean(y, groups)

    eligible = (
        (sizes >= 2)
        & np.isfinite(ret_w)
        & np.isfinite(y_w)
    )

    ind_beta = ols_beta(
        y_w[eligible],
        [zscore(ret_w[eligible])],
    )

    ind_spread, _, _, _, _, ind_q5_local = quintile_spread(
        ret_w[eligible],
        y_w[eligible],
    )

    ind_q5 = np.zeros(len(x), dtype=bool)
    ind_q5[np.flatnonzero(eligible)] = ind_q5_local

    raw_active, raw_max_ow, raw_hhi, universe_hhi = active_industry_share(
        groups,
        raw_q5,
    )
    ind_active, ind_max_ow, ind_hhi, _ = active_industry_share(
        groups,
        ind_q5,
        eligible=eligible,
    )

    return {
        "standard": standards[0],
        "n": int(len(x)),
        "n_industries": int(pd.Series(groups).nunique()),
        "eligible_rows": int(eligible.sum()),
        "singleton_rows": int((sizes < 2).sum()),
        "raw_beta": raw_beta[1] if len(raw_beta) > 1 else math.nan,
        "industry_control_beta":
            ind_beta[1] if len(ind_beta) > 1 else math.nan,
        "raw_q5_q1_spread": raw_spread,
        "industry_neutral_q5_q1_spread": ind_spread,
        "raw_q5_industry_active_share": raw_active,
        "raw_q5_max_industry_overweight": raw_max_ow,
        "raw_q5_industry_hhi": raw_hhi,
        "universe_industry_hhi": universe_hhi,
        "neutral_q5_industry_active_share": ind_active,
        "neutral_q5_max_industry_overweight": ind_max_ow,
        "neutral_q5_industry_hhi": ind_hhi,
    }


def aggregate_segment(x: pd.DataFrame) -> dict[str, Any]:
    raw_beta = summarize(x["raw_beta"])
    ind_beta = summarize(x["industry_control_beta"])
    raw_spread = summarize(x["raw_q5_q1_spread"])
    neutral_spread = summarize(x["industry_neutral_q5_q1_spread"])

    raw_mean = raw_beta["mean"]
    ind_mean = ind_beta["mean"]

    return {
        "n_dates": int(len(x)),
        "mean_cross_section_n":
            float(x["n"].mean()) if len(x) else math.nan,
        "mean_industries":
            float(x["n_industries"].mean()) if len(x) else math.nan,
        "singleton_row_ratio":
            float(x["singleton_rows"].sum() / x["n"].sum())
            if len(x) and x["n"].sum() > 0
            else math.nan,
        "raw_beta": raw_beta,
        "industry_control_beta": ind_beta,
        "beta_retention": (
            ind_mean / raw_mean
            if finite(raw_mean) and abs(float(raw_mean)) > 1e-12
            else math.nan
        ),
        "raw_q5_q1_spread": raw_spread,
        "industry_neutral_q5_q1_spread": neutral_spread,
        "raw_q5_industry_active_share_mean":
            float(x["raw_q5_industry_active_share"].mean())
            if len(x) else math.nan,
        "raw_q5_max_industry_overweight_mean":
            float(x["raw_q5_max_industry_overweight"].mean())
            if len(x) else math.nan,
        "raw_q5_industry_hhi_mean":
            float(x["raw_q5_industry_hhi"].mean())
            if len(x) else math.nan,
        "universe_industry_hhi_mean":
            float(x["universe_industry_hhi"].mean())
            if len(x) else math.nan,
        "neutral_q5_industry_active_share_mean":
            float(x["neutral_q5_industry_active_share"].mean())
            if len(x) else math.nan,
    }


def pct(x: Any) -> str:
    return "NA" if not finite(x) else f"{float(x) * 100:+.3f}%"


def ratio(x: Any) -> str:
    return "NA" if not finite(x) else f"{float(x):+.3f}"


def main():
    folds = load_folds()
    con = duckdb.connect(str(DB_PATH), read_only=True)

    try:
        asset = verify_reference_asset(con)
        frames = [
            load_joined_panel(con, universe)
            for universe in UNIVERSES
        ]
    finally:
        con.close()

    panel = pd.concat(frames, ignore_index=True)
    panel["fold"] = panel["signal_date"].map(lambda d: assign_fold(d, folds))

    if panel["fold"].isna().any():
        raise RuntimeError("Rows outside frozen F1/F2/F3 found.")

    daily_rows = []

    for (universe, signal_date), x in panel.groupby(
        ["universe", "signal_date"],
        sort=True,
    ):
        row = {
            "universe": universe,
            "signal_date": signal_date,
            "fold": x["fold"].iloc[0],
        }
        row.update(per_date_metrics(x))
        daily_rows.append(row)

    daily = pd.DataFrame(daily_rows)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    daily.to_csv(
        OUT_DIR / "daily_industry_neutral_metrics.csv",
        index=False,
        encoding="utf-8-sig",
    )

    payload_results = {}
    summary_rows = []

    for universe in UNIVERSES:
        payload_results[universe] = {}

        segments = ["ALL", "F1", "F2", "F3", "ERA_SW2014", "ERA_SW2021"]

        for segment in segments:
            if segment == "ALL":
                x = daily[daily["universe"] == universe]
            elif segment.startswith("ERA_"):
                std = segment.replace("ERA_", "")
                x = daily[
                    (daily["universe"] == universe)
                    & (daily["standard"] == std)
                ]
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
                "mean_industries": agg["mean_industries"],
                "singleton_row_ratio": agg["singleton_row_ratio"],
                "raw_beta_mean": agg["raw_beta"]["mean"],
                "industry_control_beta_mean":
                    agg["industry_control_beta"]["mean"],
                "industry_control_beta_median":
                    agg["industry_control_beta"]["median"],
                "industry_control_beta_trim5":
                    agg["industry_control_beta"]["trim5"],
                "industry_control_beta_ex_top5":
                    agg["industry_control_beta"]["ex_top5"],
                "industry_control_beta_positive_ratio":
                    agg["industry_control_beta"]["positive_ratio"],
                "beta_retention": agg["beta_retention"],
                "raw_q5_q1_mean":
                    agg["raw_q5_q1_spread"]["mean"],
                "industry_neutral_q5_q1_mean":
                    agg["industry_neutral_q5_q1_spread"]["mean"],
                "industry_neutral_q5_q1_median":
                    agg["industry_neutral_q5_q1_spread"]["median"],
                "industry_neutral_q5_q1_trim5":
                    agg["industry_neutral_q5_q1_spread"]["trim5"],
                "industry_neutral_q5_q1_ex_top5":
                    agg["industry_neutral_q5_q1_spread"]["ex_top5"],
                "raw_q5_industry_active_share_mean":
                    agg["raw_q5_industry_active_share_mean"],
                "raw_q5_max_industry_overweight_mean":
                    agg["raw_q5_max_industry_overweight_mean"],
                "neutral_q5_industry_active_share_mean":
                    agg["neutral_q5_industry_active_share_mean"],
            })

    pd.DataFrame(summary_rows).to_csv(
        OUT_DIR / "summary.csv",
        index=False,
        encoding="utf-8-sig",
    )

    audit = {
        "audit_id": "RET120_INDUSTRY_NEUTRAL_V01",
        "reference_asset": asset,
        "cutoff": CUTOFF,
        "results": payload_results,
        "method": {
            "factor": "ret120",
            "outcome": "excess_120",
            "taxonomy": "Shenwan/SWS L1",
            "taxonomy_usage":
                "date-local current-at-T membership; no cross-era name mapping",
            "primary_control":
                "Frisch-Waugh style date-local within-industry demeaning",
            "industry_neutral_beta":
                "within-industry excess_120 ~ z(within-industry ret120)",
            "industry_neutral_spread":
                "Q5-Q1 of within-industry excess_120 ranked by "
                "within-industry ret120 residual across the date",
            "singleton_policy":
                "industry groups with n<2 excluded from neutral beta/spread",
            "aggregation": "date-equal",
            "transaction_cost": "not_applied",
            "portfolio_construction": "not_applied",
            "significance_test":
                "not_run_due_to_overlapping_120-session forward labels",
        },
        "notes": [
            "SW2014 and SW2021 are never crosswalked.",
            "Industry names are only used within the same signal date.",
            "Taxonomy transition cannot create a cross-date name-difference artifact in the within-date fixed-effect calculation.",
            "This is an exposure audit, not a strategy backtest.",
        ],
    }

    (OUT_DIR / "audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("=" * 110)
    print("RET120 INDUSTRY-NEUTRAL AUDIT V0.1")
    print("=" * 110)
    print("Reference asset:", asset["asset_id"])
    print("Rows:", len(panel))
    print("Symbols:", panel["symbol"].nunique())
    print("Date range:", panel["signal_date"].min(), "->", panel["signal_date"].max())
    print("Taxonomy generations:", sorted(panel["standard"].unique().tolist()))
    print()

    for universe in UNIVERSES:
        print(universe.upper())
        print("-" * 110)

        for segment in ("ALL", "F1", "F2", "F3", "ERA_SW2014", "ERA_SW2021"):
            a = payload_results[universe][segment]
            b = a["industry_control_beta"]
            s = a["industry_neutral_q5_q1_spread"]

            print(
                f"{segment}: "
                f"raw beta={pct(a['raw_beta']['mean'])} | "
                f"industry-ctl beta={pct(b['mean'])} "
                f"(med={pct(b['median'])}, trim5={pct(b['trim5'])}, "
                f"exTop5={pct(b['ex_top5'])}, pos={b['positive_ratio']:.1%}) | "
                f"beta retention={ratio(a['beta_retention'])} | "
                f"neutral Q5-Q1={pct(s['mean'])} "
                f"(med={pct(s['median'])}, trim5={pct(s['trim5'])}, "
                f"exTop5={pct(s['ex_top5'])}) | "
                f"rawQ5 industry active={a['raw_q5_industry_active_share_mean']:.1%} | "
                f"singleton rows={a['singleton_row_ratio']:.2%}"
            )

        print()

    print("Output:", OUT_DIR)
    print("No SW2014<->SW2021 crosswalk was used.")
    print("No transaction cost or portfolio construction was applied.")
    print("No production database was modified.")


if __name__ == "__main__":
    main()
