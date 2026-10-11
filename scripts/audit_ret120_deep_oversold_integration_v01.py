from __future__ import annotations

import argparse
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
RESEARCH_DIR = ROOT / "data" / "research"
EDGE_DIR = RESEARCH_DIR / "edge_discovery"
TEMPORAL_DIR = EDGE_DIR / "ret120_temporal_v01"
OUT_DIR = EDGE_DIR / "ret120_deep_oversold_integration_v01"

TREND_PANEL = EDGE_DIR / "trend_persistence_scan_csi500_panel_v0221.parquet"
TREND_BUCKETS = EDGE_DIR / "trend_persistence_scan_csi500_daily_buckets_v0221.parquet"

CUTOFF = "20260924"
ERA_BOUNDARY = "20210730"
CALENDAR_SYMBOL = "000001.SH"

ALIASES = {
    "symbol": [
        "symbol", "stock", "stock_code", "code", "security", "ticker",
    ],
    "signal_date": [
        "signal_date", "signal_day", "signal_dt", "date", "trade_signal_date",
    ],
    "entry_date": [
        "entry_date", "entry_day", "entry_dt", "buy_date", "open_date",
    ],
    "exit_date": [
        "exit_date", "exit_day", "exit_dt", "sell_date", "close_date",
    ],
}


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


def assign_quintiles(values: pd.Series) -> pd.Series:
    valid = values.notna()
    out = pd.Series(index=values.index, dtype="object")
    if valid.sum() < 5:
        return out

    ranks = values[valid].rank(method="first")
    out.loc[valid] = pd.qcut(
        ranks,
        5,
        labels=["Q1", "Q2", "Q3", "Q4", "Q5"],
    ).astype(str).to_numpy()
    return out


def load_folds() -> dict[str, tuple[str, str]]:
    path = TEMPORAL_DIR / "audit.json"
    if not path.exists():
        raise FileNotFoundError(path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = payload["folds"]

    return {
        k: (str(raw[k]["start"]), str(raw[k]["end"]))
        for k in ("F1", "F2", "F3")
    }


def assign_fold(d: str, folds: dict[str, tuple[str, str]]) -> str | None:
    for name in ("F1", "F2", "F3"):
        a, b = folds[name]
        if a <= d <= b:
            return name
    return None


def table_columns_from_file(
    con: duckdb.DuckDBPyConnection,
    path: Path,
) -> list[str]:
    q = path.as_posix().replace("'", "''")
    if path.suffix.lower() == ".parquet":
        rel = f"read_parquet('{q}')"
    elif path.suffix.lower() == ".csv":
        rel = f"read_csv_auto('{q}', header=true, sample_size=-1)"
    else:
        return []

    rows = con.execute(
        f"DESCRIBE SELECT * FROM {rel}"
    ).fetchall()
    return [str(r[0]) for r in rows]


def resolve_alias(columns: list[str], logical: str) -> str | None:
    cmap = {c.lower(): c for c in columns}
    for candidate in ALIASES[logical]:
        if candidate.lower() in cmap:
            return cmap[candidate.lower()]
    return None


def score_trade_candidate(path: Path, columns: list[str]) -> tuple[int, dict[str, str | None]]:
    mapping = {
        logical: resolve_alias(columns, logical)
        for logical in ALIASES
    }

    score = 0
    for logical, weight in [
        ("symbol", 6),
        ("signal_date", 6),
        ("entry_date", 5),
        ("exit_date", 5),
    ]:
        if mapping[logical]:
            score += weight

    name = path.as_posix().lower()
    if "deep" in name:
        score += 3
    if "oversold" in name:
        score += 4
    if "csi500" in name or "zz500" in name:
        score += 3
    if "trade" in name:
        score += 3
    if "forward" in name:
        score -= 4

    return score, mapping


def discover_trade_source() -> tuple[Path, dict[str, str], list[dict[str, Any]]]:
    """
    Canonical source policy for THIS audit:

      target strategy = Deep Oversold CSI500

    Preferred event source:
      deep_oversold_csi500_regime_events_v1.parquet

    Cross-check source:
      deep_oversold_csi500_capacity_events_v1.parquet

    A generic replication_events_latest file is NOT accepted as canonical for
    this task unless explicitly supplied by --trades, because it may belong to
    another universe / replication lineage.
    """
    candidates = []

    con = duckdb.connect()
    try:
        for path in RESEARCH_DIR.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".parquet", ".csv"}:
                continue

            lname = path.as_posix().lower()
            if "deep" not in lname or "oversold" not in lname:
                continue

            try:
                cols = table_columns_from_file(con, path)
            except Exception:
                continue

            score, mapping = score_trade_candidate(path, cols)

            if all(mapping[x] for x in ("symbol", "signal_date", "entry_date", "exit_date")):
                fname = path.name.lower()

                role = "other"
                if fname == "deep_oversold_csi500_regime_events_v1.parquet":
                    role = "canonical_csi500_regime"
                elif fname == "deep_oversold_csi500_capacity_events_v1.parquet":
                    role = "derived_csi500_capacity"
                elif "csi500" in fname and "regime_events" in fname:
                    role = "csi500_regime"
                elif "csi500" in fname and "capacity_events" in fname:
                    role = "csi500_capacity"
                elif "replication_events_latest" in fname:
                    role = "generic_replication_latest"
                elif "replication" in fname:
                    role = "generic_replication"

                candidates.append({
                    "path": path,
                    "score": score,
                    "mapping": mapping,
                    "columns": cols,
                    "role": role,
                })
    finally:
        con.close()

    if not candidates:
        raise RuntimeError(
            "Could not discover any Deep Oversold event table with "
            "symbol + signal_date + entry_date + exit_date."
        )

    exact = [
        x for x in candidates
        if x["role"] == "canonical_csi500_regime"
    ]

    if len(exact) != 1:
        profiles = [
            {
                "path": str(x["path"]),
                "role": x["role"],
                "score": int(x["score"]),
                "mapping": {
                    k: (str(v) if v is not None else None)
                    for k, v in x["mapping"].items()
                },
            }
            for x in candidates
        ]
        raise RuntimeError(
            "Expected exactly one canonical CSI500 regime event file "
            "'deep_oversold_csi500_regime_events_v1.parquet'. "
            "Use --trades explicitly if the project lineage changed.\n"
            + json.dumps(profiles, ensure_ascii=False, indent=2)
        )

    best = exact[0]

    audit_candidates = [
        {
            "path": str(x["path"]),
            "role": x["role"],
            "score": int(x["score"]),
            "mapping": {
                k: (str(v) if v is not None else None)
                for k, v in x["mapping"].items()
            },
        }
        for x in candidates
    ]

    return best["path"], {
        k: str(v)
        for k, v in best["mapping"].items()
        if v is not None
    }, audit_candidates

def trade_key_frame(df: pd.DataFrame) -> pd.DataFrame:
    cols = ["symbol", "signal_date", "entry_date", "exit_date"]
    return (
        df[cols]
        .drop_duplicates()
        .sort_values(cols)
        .reset_index(drop=True)
    )


def crosscheck_derived_trade_sources(
    canonical_path: Path,
    canonical_trades: pd.DataFrame,
    discovered_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    For the CSI500 Deep Oversold lineage, compare canonical regime events to
    the CSI500 capacity event table on the immutable trade identity keys.

    The capacity table is not allowed to define the population; it only
    verifies that downstream research used the same trades.
    """
    canonical_keys = trade_key_frame(canonical_trades)
    results = []

    for item in discovered_candidates:
        p = Path(item["path"])
        if p.resolve() == canonical_path.resolve():
            continue

        role = item.get("role")
        if role not in {"derived_csi500_capacity", "csi500_capacity"}:
            continue

        try:
            derived, _ = load_trades(
                p,
                {
                    k: v
                    for k, v in item["mapping"].items()
                    if v is not None
                },
            )
            derived_keys = trade_key_frame(derived)

            canon_set = set(map(tuple, canonical_keys.to_numpy()))
            deriv_set = set(map(tuple, derived_keys.to_numpy()))

            only_canonical = canon_set - deriv_set
            only_derived = deriv_set - canon_set

            result = {
                "path": str(p),
                "role": role,
                "canonical_key_rows": int(len(canonical_keys)),
                "derived_key_rows": int(len(derived_keys)),
                "only_canonical": int(len(only_canonical)),
                "only_derived": int(len(only_derived)),
                "exact_key_match": (
                    len(only_canonical) == 0 and len(only_derived) == 0
                ),
            }
            results.append(result)

        except Exception as exc:
            results.append({
                "path": str(p),
                "role": role,
                "crosscheck_error": repr(exc),
                "exact_key_match": False,
            })

    if not results:
        raise RuntimeError(
            "CSI500 capacity event table was not found for lineage cross-check."
        )

    bad = [x for x in results if not x.get("exact_key_match", False)]
    if bad:
        raise RuntimeError(
            "CSI500 regime/capacity event identities are inconsistent. "
            "Failing closed before ret120 integration.\n"
            + json.dumps(bad, ensure_ascii=False, indent=2)
        )

    return results

def load_trades(
    path: Path,
    explicit_mapping: dict[str, str] | None = None,
) -> tuple[pd.DataFrame, dict[str, str]]:
    con = duckdb.connect()
    try:
        columns = table_columns_from_file(con, path)
        mapping = explicit_mapping or {
            logical: resolve_alias(columns, logical)
            for logical in ALIASES
        }

        required = ("symbol", "signal_date", "entry_date", "exit_date")
        missing = [x for x in required if not mapping.get(x)]
        if missing:
            raise RuntimeError(
                f"Trade file is missing required logical fields {missing}. "
                f"Columns={columns}"
            )

        q = path.as_posix().replace("'", "''")
        if path.suffix.lower() == ".parquet":
            rel = f"read_parquet('{q}')"
        elif path.suffix.lower() == ".csv":
            rel = f"read_csv_auto('{q}', header=true, sample_size=-1)"
        else:
            raise RuntimeError(f"Unsupported trade source: {path}")

        df = con.execute(
            f"""
            SELECT
                "{mapping['symbol']}" AS symbol,
                "{mapping['signal_date']}" AS signal_date,
                "{mapping['entry_date']}" AS entry_date,
                "{mapping['exit_date']}" AS exit_date
            FROM {rel}
            """
        ).fetchdf()
    finally:
        con.close()

    for c in ("signal_date", "entry_date", "exit_date"):
        df[c] = df[c].map(normalize_date)

    df["symbol"] = df["symbol"].map(normalize_symbol)

    invalid = df[
        df[["signal_date", "entry_date", "exit_date"]].isna().any(axis=1)
    ]
    if not invalid.empty:
        raise RuntimeError(
            f"Invalid trade dates in {len(invalid)} rows. "
            f"Sample={invalid.head(20).to_dict('records')}"
        )

    df = df[df["signal_date"] <= CUTOFF].copy()
    df = df.drop_duplicates(
        ["symbol", "signal_date", "entry_date", "exit_date"],
        keep="first",
    )
    df = df.sort_values(["signal_date", "symbol"]).reset_index(drop=True)

    return df, mapping


def load_ret120_tags() -> tuple[pd.DataFrame, dict[str, Any]]:
    if not TREND_PANEL.exists():
        raise FileNotFoundError(TREND_PANEL)

    con = duckdb.connect()
    try:
        qp = TREND_PANEL.as_posix().replace("'", "''")
        panel = con.execute(
            f"""
            SELECT
                signal_date,
                symbol,
                ret120,
                excess_120
            FROM read_parquet('{qp}')
            WHERE signal_date <= '{CUTOFF}'
              AND ret120 IS NOT NULL
            ORDER BY signal_date, symbol
            """
        ).fetchdf()

        panel["signal_date"] = panel["signal_date"].map(normalize_date)
        panel["symbol"] = panel["symbol"].map(normalize_symbol)

        if panel["signal_date"].isna().any():
            raise RuntimeError("Invalid signal_date found in ret120 panel.")

        parts = []
        for _, x in panel.groupby("signal_date", sort=True):
            y = x.copy()
            y["ret120_bucket"] = assign_quintiles(y["ret120"])
            parts.append(y)

        tagged = pd.concat(parts, ignore_index=True)

        reconciliation = {
            "checked_dates": 0,
            "count_mismatch_rows": 0,
            "max_mean_abs_diff": math.nan,
        }

        if TREND_BUCKETS.exists():
            qb = TREND_BUCKETS.as_posix().replace("'", "''")
            frozen = con.execute(
                f"""
                SELECT signal_date, bucket, n, mean_excess
                FROM read_parquet('{qb}')
                WHERE feature = 'ret120'
                  AND horizon = 120
                  AND signal_date <= '{CUTOFF}'
                ORDER BY signal_date, bucket
                """
            ).fetchdf()
            frozen["signal_date"] = frozen["signal_date"].map(normalize_date)

            calc = (
                tagged[tagged["excess_120"].notna()]
                .groupby(
                    ["signal_date", "ret120_bucket"],
                    observed=True,
                )
                .agg(
                    n_calc=("symbol", "size"),
                    mean_excess_calc=("excess_120", "mean"),
                )
                .reset_index()
                .rename(columns={"ret120_bucket": "bucket"})
            )

            chk = frozen.merge(
                calc,
                on=["signal_date", "bucket"],
                how="inner",
                validate="one_to_one",
            )
            chk["n_diff"] = chk["n_calc"] - chk["n"]
            chk["mean_diff"] = chk["mean_excess_calc"] - chk["mean_excess"]

            reconciliation = {
                "checked_dates": int(chk["signal_date"].nunique()),
                "count_mismatch_rows": int((chk["n_diff"] != 0).sum()),
                "max_mean_abs_diff": (
                    float(chk["mean_diff"].abs().max())
                    if len(chk)
                    else math.nan
                ),
            }

            if (
                reconciliation["count_mismatch_rows"] != 0
                or not finite(reconciliation["max_mean_abs_diff"])
                or reconciliation["max_mean_abs_diff"] > 1e-12
            ):
                raise RuntimeError(
                    "ret120 bucket reconstruction diverges from frozen "
                    f"daily_buckets: {reconciliation}"
                )

    finally:
        con.close()

    return (
        tagged[
            ["signal_date", "symbol", "ret120", "ret120_bucket"]
        ].copy(),
        reconciliation,
    )


def parse_market_ex(
    raw: Any,
    symbols: list[str],
) -> pd.DataFrame:
    if not isinstance(raw, dict):
        raise TypeError(type(raw))

    rows = []
    symbol_set = set(symbols)

    symbol_keys = [k for k in raw.keys() if str(k) in symbol_set]
    if symbol_keys:
        for symbol in symbol_keys:
            obj = raw[symbol]
            if not isinstance(obj, pd.DataFrame):
                continue

            cmap = {str(c).lower(): c for c in obj.columns}
            if "open" not in cmap or "close" not in cmap:
                continue

            rows.append(pd.DataFrame({
                "date": [normalize_date(x) for x in obj.index],
                "symbol": str(symbol),
                "open": pd.to_numeric(
                    obj[cmap["open"]], errors="coerce"
                ).to_numpy(),
                "close": pd.to_numeric(
                    obj[cmap["close"]], errors="coerce"
                ).to_numpy(),
            }))

    if not rows:
        key_map = {str(k).lower(): k for k in raw.keys()}
        if "open" in key_map and "close" in key_map:
            op = raw[key_map["open"]]
            cl = raw[key_map["close"]]

            if isinstance(op, pd.DataFrame) and isinstance(cl, pd.DataFrame):
                for symbol in op.columns:
                    if str(symbol) not in symbol_set or symbol not in cl.columns:
                        continue

                    rows.append(pd.DataFrame({
                        "date": [normalize_date(x) for x in op.index],
                        "symbol": str(symbol),
                        "open": pd.to_numeric(
                            op[symbol], errors="coerce"
                        ).to_numpy(),
                        "close": pd.to_numeric(
                            cl[symbol], errors="coerce"
                        ).to_numpy(),
                    }))

    if not rows:
        raise RuntimeError("Could not normalize QMT market-data response.")

    out = pd.concat(rows, ignore_index=True)
    out = out.dropna(subset=["date"])
    out = out.drop_duplicates(["date", "symbol"], keep="last")
    return out


def attach_trade_returns(trades: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    symbols = sorted(trades["symbol"].unique().tolist())
    start = trades["entry_date"].min()
    end = trades["exit_date"].max()

    raw = xtdata.get_market_data_ex(
        field_list=["open", "close"],
        stock_list=symbols,
        period="1d",
        start_time=start,
        end_time=end,
        count=-1,
        dividend_type="back_ratio",
        fill_data=False,
    )
    market = parse_market_ex(raw, symbols)

    entry = market[["date", "symbol", "open"]].rename(
        columns={"date": "entry_date", "open": "entry_open"}
    )
    exit_ = market[["date", "symbol", "close"]].rename(
        columns={"date": "exit_date", "close": "exit_close"}
    )

    x = trades.merge(
        entry,
        on=["entry_date", "symbol"],
        how="left",
        validate="many_to_one",
    )
    x = x.merge(
        exit_,
        on=["exit_date", "symbol"],
        how="left",
        validate="many_to_one",
    )

    valid = (
        np.isfinite(x["entry_open"])
        & np.isfinite(x["exit_close"])
        & (x["entry_open"] > 0)
        & (x["exit_close"] > 0)
    )

    x["gross_return"] = np.nan
    x.loc[valid, "gross_return"] = (
        x.loc[valid, "exit_close"] / x.loc[valid, "entry_open"] - 1.0
    )
    x["net_20bp"] = x["gross_return"] - 0.0020
    x["net_50bp"] = x["gross_return"] - 0.0050

    receipt = {
        "trade_rows": int(len(x)),
        "raw_return_coverage": float(valid.mean()),
        "missing_raw_return_rows": int((~valid).sum()),
    }

    if receipt["raw_return_coverage"] < 0.995:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        x.loc[
            ~valid,
            [
                "symbol", "signal_date", "entry_date", "exit_date",
                "entry_open", "exit_close",
            ],
        ].to_csv(
            OUT_DIR / "missing_trade_return_sample.csv",
            index=False,
            encoding="utf-8-sig",
        )
        raise RuntimeError(
            f"Trade return coverage {receipt['raw_return_coverage']:.4%} "
            "< 99.5%. Failing closed."
        )

    return x, receipt


def mean_bottom(values, frac: float = 0.10) -> float:
    arr = np.sort(np.asarray(list(values), dtype=float))
    arr = arr[np.isfinite(arr)]
    if len(arr) == 0:
        return math.nan
    k = max(1, int(math.ceil(len(arr) * frac)))
    return float(arr[:k].mean())


def summarize_returns(values) -> dict[str, float | int]:
    arr = np.asarray(list(values), dtype=float)
    arr = arr[np.isfinite(arr)]

    if len(arr) == 0:
        return {
            "n": 0,
            "mean": math.nan,
            "median": math.nan,
            "win_rate": math.nan,
            "p10": math.nan,
            "p25": math.nan,
            "p75": math.nan,
            "p90": math.nan,
            "bottom10_mean": math.nan,
        }

    return {
        "n": int(len(arr)),
        "mean": float(arr.mean()),
        "median": float(np.median(arr)),
        "win_rate": float((arr > 0).mean()),
        "p10": float(np.quantile(arr, 0.10)),
        "p25": float(np.quantile(arr, 0.25)),
        "p75": float(np.quantile(arr, 0.75)),
        "p90": float(np.quantile(arr, 0.90)),
        "bottom10_mean": mean_bottom(arr, 0.10),
    }


def adjacent_monotonicity(means: dict[str, float]) -> float:
    ordered = [means.get(f"Q{i}", math.nan) for i in range(1, 6)]
    comparisons = []
    for a, b in zip(ordered[:-1], ordered[1:]):
        if finite(a) and finite(b):
            comparisons.append(float(b) >= float(a))
    return (
        float(np.mean(comparisons))
        if comparisons
        else math.nan
    )


def segment_analysis(x: pd.DataFrame) -> dict[str, Any]:
    bucket_results = {}

    for q in ("Q1", "Q2", "Q3", "Q4", "Q5"):
        y = x[x["ret120_bucket"] == q]
        bucket_results[q] = {
            "gross": summarize_returns(y["gross_return"]),
            "net20": summarize_returns(y["net_20bp"]),
            "net50": summarize_returns(y["net_50bp"]),
        }

    q1 = x[x["ret120_bucket"] == "Q1"]
    rest = x[x["ret120_bucket"] != "Q1"]

    q1_stats = summarize_returns(q1["gross_return"])
    rest_stats = summarize_returns(rest["gross_return"])
    baseline = summarize_returns(x["gross_return"])

    means = {
        q: bucket_results[q]["gross"]["mean"]
        for q in bucket_results
    }

    return {
        "n": int(len(x)),
        "baseline": baseline,
        "buckets": bucket_results,
        "q1_vs_rest": {
            "q1_n": q1_stats["n"],
            "q1_share": (
                q1_stats["n"] / len(x)
                if len(x)
                else math.nan
            ),
            "q1_mean": q1_stats["mean"],
            "rest_mean": rest_stats["mean"],
            "mean_gap_q1_minus_rest": (
                q1_stats["mean"] - rest_stats["mean"]
                if finite(q1_stats["mean"]) and finite(rest_stats["mean"])
                else math.nan
            ),
            "q1_median": q1_stats["median"],
            "rest_median": rest_stats["median"],
            "median_gap_q1_minus_rest": (
                q1_stats["median"] - rest_stats["median"]
                if finite(q1_stats["median"]) and finite(rest_stats["median"])
                else math.nan
            ),
            "q1_win_rate": q1_stats["win_rate"],
            "rest_win_rate": rest_stats["win_rate"],
            "win_rate_gap_q1_minus_rest": (
                q1_stats["win_rate"] - rest_stats["win_rate"]
                if finite(q1_stats["win_rate"]) and finite(rest_stats["win_rate"])
                else math.nan
            ),
            "q1_bottom10_mean": q1_stats["bottom10_mean"],
            "rest_bottom10_mean": rest_stats["bottom10_mean"],
        },
        "bucket_mean_monotonicity": adjacent_monotonicity(means),
    }


def pct(x: Any) -> str:
    return "NA" if not finite(x) else f"{float(x) * 100:+.2f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--trades",
        default=None,
        help="Optional explicit frozen Deep Oversold trade CSV/parquet.",
    )
    args = ap.parse_args()

    discovered_candidates = []

    if args.trades:
        trade_path = Path(args.trades)
        trades, mapping = load_trades(trade_path)
    else:
        trade_path, mapping, discovered_candidates = discover_trade_source()
        trades, _ = load_trades(trade_path, mapping)

    derived_crosscheck = (
        crosscheck_derived_trade_sources(
            trade_path,
            trades,
            discovered_candidates,
        )
        if discovered_candidates
        else []
    )

    frozen_trade_identity_rows = len(
        trade_key_frame(trades)
    )
    frozen_trade_symbols = int(trades["symbol"].nunique())
    frozen_trade_signal_min = str(trades["signal_date"].min())
    frozen_trade_signal_max = str(trades["signal_date"].max())

    folds = load_folds()

    tags, bucket_reconciliation = load_ret120_tags()
    trades, return_receipt = attach_trade_returns(trades)

    tag_min_date = str(tags["signal_date"].min())
    tag_max_date = str(tags["signal_date"].max())

    trade_date_set = set(trades["signal_date"].unique().tolist())
    tag_date_set = set(tags["signal_date"].unique().tolist())
    trade_symbol_set = set(trades["symbol"].unique().tolist())
    tag_symbol_set = set(tags["symbol"].unique().tolist())

    date_intersection = trade_date_set & tag_date_set
    symbol_intersection = trade_symbol_set & tag_symbol_set

    overlap_mask = (
        (trades["signal_date"] >= tag_min_date)
        & (trades["signal_date"] <= tag_max_date)
    )

    overlap_trades = trades.loc[overlap_mask].copy()
    out_of_range_trades = trades.loc[~overlap_mask].copy()

    merged_overlap = overlap_trades.merge(
        tags,
        on=["signal_date", "symbol"],
        how="left",
        validate="many_to_one",
    )

    overlap_tag_coverage = float(
        merged_overlap["ret120_bucket"].notna().mean()
    ) if len(merged_overlap) else 0.0

    diagnostics = {
        "trade_rows_total": int(len(trades)),
        "trade_signal_date_min": str(trades["signal_date"].min()),
        "trade_signal_date_max": str(trades["signal_date"].max()),
        "trade_symbol_count": int(trades["symbol"].nunique()),
        "ret120_tag_date_min": tag_min_date,
        "ret120_tag_date_max": tag_max_date,
        "ret120_tag_symbol_count": int(tags["symbol"].nunique()),
        "trade_date_count": int(len(trade_date_set)),
        "tag_date_count": int(len(tag_date_set)),
        "date_intersection_count": int(len(date_intersection)),
        "trade_symbol_intersection_count": int(len(symbol_intersection)),
        "overlap_trade_rows": int(len(overlap_trades)),
        "out_of_range_trade_rows": int(len(out_of_range_trades)),
        "overlap_tag_coverage": overlap_tag_coverage,
        "trade_symbol_sample": sorted(list(trade_symbol_set))[:10],
        "tag_symbol_sample": sorted(list(tag_symbol_set))[:10],
        "trade_date_sample": sorted(list(trade_date_set))[:10],
        "tag_date_sample": sorted(list(tag_date_set))[:10],
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if len(out_of_range_trades):
        out_of_range_trades.to_csv(
            OUT_DIR / "out_of_ret120_panel_range_trades.csv",
            index=False,
            encoding="utf-8-sig",
        )

    missing_overlap = merged_overlap[
        merged_overlap["ret120_bucket"].isna()
    ].copy()

    if len(missing_overlap):
        missing_overlap.head(50000).to_csv(
            OUT_DIR / "missing_ret120_tag_within_overlap_sample.csv",
            index=False,
            encoding="utf-8-sig",
        )

    # Within the historical overlap, exact tagging should be nearly complete.
    # Out-of-range recent trades are reported separately and do not count
    # against this source-integrity threshold.
    if overlap_tag_coverage < 0.995:
        raise RuntimeError(
            "ret120 tag coverage within the shared historical date range "
            f"is {overlap_tag_coverage:.2%} < 99.5%. "
            "This indicates a key/source mismatch, not merely the known "
            "future-label cutoff. Diagnostics:\n"
            + json.dumps(diagnostics, ensure_ascii=False, indent=2)
        )

    work = merged_overlap[
        merged_overlap["ret120_bucket"].notna()
        & np.isfinite(merged_overlap["gross_return"])
    ].copy()

    tag_coverage = overlap_tag_coverage

    work["fold"] = work["signal_date"].map(lambda d: assign_fold(d, folds))
    work["era"] = np.where(
        work["signal_date"] < ERA_BOUNDARY,
        "SW2014",
        "SW2021",
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    work.to_parquet(
        OUT_DIR / "deep_oversold_trades_tagged_ret120.parquet",
        index=False,
    )

    segment_defs = {
        "ALL": np.ones(len(work), dtype=bool),
        "F1": work["fold"].eq("F1").to_numpy(),
        "F2": work["fold"].eq("F2").to_numpy(),
        "F3": work["fold"].eq("F3").to_numpy(),
        "ERA_SW2021": work["era"].eq("SW2021").to_numpy(),
    }

    results = {}
    rows = []

    for name, mask in segment_defs.items():
        x = work.loc[mask].copy()
        a = segment_analysis(x)
        results[name] = a

        for q in ("Q1", "Q2", "Q3", "Q4", "Q5"):
            g = a["buckets"][q]["gross"]
            n20 = a["buckets"][q]["net20"]
            n50 = a["buckets"][q]["net50"]
            rows.append({
                "segment": name,
                "bucket": q,
                "n": g["n"],
                "gross_mean": g["mean"],
                "gross_median": g["median"],
                "gross_win_rate": g["win_rate"],
                "gross_p10": g["p10"],
                "gross_bottom10_mean": g["bottom10_mean"],
                "net20_mean": n20["mean"],
                "net50_mean": n50["mean"],
                "q1_minus_rest_mean":
                    a["q1_vs_rest"]["mean_gap_q1_minus_rest"]
                    if q == "Q1" else np.nan,
                "q1_minus_rest_median":
                    a["q1_vs_rest"]["median_gap_q1_minus_rest"]
                    if q == "Q1" else np.nan,
                "q1_minus_rest_win_rate":
                    a["q1_vs_rest"]["win_rate_gap_q1_minus_rest"]
                    if q == "Q1" else np.nan,
                "bucket_mean_monotonicity":
                    a["bucket_mean_monotonicity"],
            })

    pd.DataFrame(rows).to_csv(
        OUT_DIR / "bucket_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )

    audit = {
        "audit_id": "RET120_DEEP_OVERSOLD_INTEGRATION_V01C",
        "deep_oversold_trade_source": str(trade_path),
        "deep_oversold_source_role": (
            "canonical_csi500_regime"
            if trade_path.name.lower()
                == "deep_oversold_csi500_regime_events_v1.parquet"
            else "explicit_override"
        ),
        "trade_column_mapping": mapping,
        "deep_oversold_population": {
            "trade_identity_rows": int(frozen_trade_identity_rows),
            "symbols": int(frozen_trade_symbols),
            "signal_date_min": frozen_trade_signal_min,
            "signal_date_max": frozen_trade_signal_max,
        },
        "derived_source_crosscheck": derived_crosscheck,
        "ret120_source": str(TREND_PANEL),
        "ret120_bucket_reconciliation": bucket_reconciliation,
        "trade_return_source": "QMT back_ratio, entry OPEN -> exit CLOSE",
        "trade_return_receipt": return_receipt,
        "ret120_tag_coverage_within_overlap": tag_coverage,
        "ret120_key_diagnostics": diagnostics,
        "segments": results,
        "policy": {
            "no_strategy_rule_changed": True,
            "no_veto_applied": True,
            "no_position_modifier_applied": True,
            "purpose": (
                "Attribution only: test whether ret120 bucket carries "
                "incremental information inside frozen Deep Oversold trades."
            ),
        },
    }

    (OUT_DIR / "audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("=" * 110)
    print("RET120 × DEEP OVERSOLD FACTOR INTEGRATION AUDIT V0.1C")
    print("=" * 110)
    print("Deep Oversold trade source:", trade_path)
    print(
        "Source role:",
        (
            "canonical_csi500_regime"
            if trade_path.name.lower()
                == "deep_oversold_csi500_regime_events_v1.parquet"
            else "explicit_override"
        ),
    )
    print("Trades:", len(trades))
    print("Unique trade identities:", frozen_trade_identity_rows)
    print("Trade symbols:", frozen_trade_symbols)
    print(
        "Ret120 tag date range:",
        tag_min_date, "->", tag_max_date,
    )
    print(
        "Trade date range:",
        trades["signal_date"].min(), "->", trades["signal_date"].max(),
    )
    print(
        "Date intersection:",
        f"{len(date_intersection)}/{len(trade_date_set)} trade signal dates",
    )
    print(
        "Symbol intersection:",
        f"{len(symbol_intersection)}/{len(trade_symbol_set)} trade symbols",
    )
    print(
        "Overlap trade rows:",
        len(overlap_trades),
        "| out-of-range rows:",
        len(out_of_range_trades),
    )
    if derived_crosscheck:
        for c in derived_crosscheck:
            print(
                "Derived key cross-check:",
                Path(c["path"]).name,
                "| exact=", c.get("exact_key_match"),
                "| onlyCanonical=", c.get("only_canonical", "NA"),
                "| onlyDerived=", c.get("only_derived", "NA"),
            )
    print(
        "Ret120 tag coverage WITHIN overlap:",
        f"{tag_coverage:.2%}",
    )
    print(
        "Ret120 bucket reconciliation:",
        f"dates={bucket_reconciliation['checked_dates']}",
        f"count_mismatch={bucket_reconciliation['count_mismatch_rows']}",
        f"max_mean_diff={bucket_reconciliation['max_mean_abs_diff']:.3e}",
    )
    print(
        "Trade return coverage:",
        f"{return_receipt['raw_return_coverage']:.2%}",
    )
    print()

    for segment in ("ALL", "F1", "F2", "F3", "ERA_SW2021"):
        a = results[segment]
        qr = a["q1_vs_rest"]

        print(segment)
        print("-" * 110)
        for q in ("Q1", "Q2", "Q3", "Q4", "Q5"):
            g = a["buckets"][q]["gross"]
            print(
                f"{q}: n={g['n']:4d} "
                f"mean={pct(g['mean'])} "
                f"median={pct(g['median'])} "
                f"win={g['win_rate']:.1%} "
                f"p10={pct(g['p10'])} "
                f"bottom10={pct(g['bottom10_mean'])}"
            )

        print(
            "Q1 vs Rest: "
            f"share={qr['q1_share']:.1%} | "
            f"mean gap={pct(qr['mean_gap_q1_minus_rest'])} | "
            f"median gap={pct(qr['median_gap_q1_minus_rest'])} | "
            f"win-rate gap={pct(qr['win_rate_gap_q1_minus_rest'])} | "
            f"Q1 bottom10={pct(qr['q1_bottom10_mean'])} | "
            f"Rest bottom10={pct(qr['rest_bottom10_mean'])}"
        )
        print(
            "Q1→Q5 mean-return monotonicity:",
            f"{a['bucket_mean_monotonicity']:.2f}",
        )
        print()

    print("Output:", OUT_DIR)
    print("No Deep Oversold strategy rule was changed.")
    print("No ret120 veto or position modifier was applied.")
    print("No production database was modified.")


if __name__ == "__main__":
    main()
