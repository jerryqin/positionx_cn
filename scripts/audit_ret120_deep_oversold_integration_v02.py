from __future__ import annotations

import importlib.util
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = (
    ROOT / "data" / "research" / "edge_discovery"
    / "ret120_deep_oversold_integration_v02"
)

ORIGIN_CANDIDATES = [
    ROOT / "scripts" / "run_deep_oversold_csi500_universe.py",
    ROOT / "payload" / "scripts" / "run_deep_oversold_csi500_universe.py",
]

RET120_PANEL = (
    ROOT / "data" / "research" / "edge_discovery"
    / "trend_persistence_scan_csi500_panel_v0221.parquet"
)
RET120_BUCKETS = (
    ROOT / "data" / "research" / "edge_discovery"
    / "trend_persistence_scan_csi500_daily_buckets_v0221.parquet"
)

START_DATE = "20200803"
END_DATE = "20260924"


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
        "positionx_deep_oversold_csi500_origin",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not import original script: {path}")

    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, path


def rebuild_frozen_dedup(origin) -> tuple[pd.DataFrame, dict[str, Any]]:
    """
    Rebuild ONLY through the original script's frozen event pipeline.

    No strategy logic is reimplemented here. We call the original module's:
      load_accepted_daily
      event_membership
      generate_events
      load_status_for_events
      apply_standard_fillability
      dedup_while_held

    This intentionally stops BEFORE any ret120 filtering.
    """
    daily = origin.load_accepted_daily(
        origin.ACCEPTED_DIR,
        origin.CSI500,
    )
    membership = origin.event_membership(
        daily,
        START_DATE,
        END_DATE,
    )
    symbols = sorted(membership["symbol"].unique().tolist())

    adapter = origin.XtDataWatchlistAdapter(
        origin.xtdata,
        origin.AdapterConfig(
            sector="",
            bars=2000,
            dividend_type="front",
        ),
    )
    loaded = adapter.history(
        symbols + [origin.CSI500.expected_index]
    )
    loaded = {
        s: origin.normalize_history(df)
        for s, df in loaded.items()
        if df is not None and not df.empty
    }

    missing = [
        s
        for s in symbols + [origin.CSI500.expected_index]
        if s not in loaded
    ]
    if missing:
        raise RuntimeError(
            f"BLOCKED_INCOMPLETE_HISTORY missing={len(missing)} "
            f"sample={missing[:30]}"
        )

    benchmark = loaded.pop(origin.CSI500.expected_index)
    market_dates = [
        d
        for d in benchmark.index
        if START_DATE <= d <= END_DATE
    ]

    cfg = origin.OversoldReplicationConfig()

    events = origin.generate_events(
        loaded,
        benchmark,
        membership,
        market_dates,
        cfg,
    )
    if events.empty:
        raise RuntimeError("No CSI500 Deep Oversold events.")

    complete = events[
        events["complete_outcome"] == True  # noqa: E712
    ].copy()

    status = origin.load_status_for_events(
        origin.STATUS_PARQUET,
        complete,
    )
    screened = origin.apply_standard_fillability(
        complete,
        status,
    )

    if int(screened["status_found"].sum()) == 0:
        raise RuntimeError("BLOCKED_STATUS_JOIN_ZERO_MATCH")

    fillable = screened[
        screened["standard_fillable"] == True  # noqa: E712
    ].copy()

    if fillable.empty:
        raise RuntimeError("No fillable CSI500 Deep Oversold events.")

    dedup = origin.dedup_while_held(fillable).copy()

    required = [
        "symbol",
        "signal_date",
        "entry_date",
        "exit_date",
        "open_gross_return",
        "open_excess_gross",
        "open_net_20bps",
        "open_net_50bps",
    ]
    missing_cols = [c for c in required if c not in dedup.columns]
    if missing_cols:
        raise RuntimeError(
            f"Original dedup frame missing expected columns: {missing_cols}"
        )

    for c in ("signal_date", "entry_date", "exit_date"):
        dedup[c] = dedup[c].map(normalize_date)
    dedup["symbol"] = dedup["symbol"].map(normalize_symbol)

    if dedup[["signal_date", "entry_date", "exit_date"]].isna().any().any():
        raise RuntimeError("Invalid dates in original dedup frame.")

    receipt = {
        "raw_events": int(len(events)),
        "complete_events": int(len(complete)),
        "fillable_events": int(len(fillable)),
        "first_signal_while_flat_events": int(len(dedup)),
        "symbols": int(dedup["symbol"].nunique()),
        "signal_dates": int(dedup["signal_date"].nunique()),
        "signal_min": str(dedup["signal_date"].min()),
        "signal_max": str(dedup["signal_date"].max()),
    }

    return dedup, receipt


def load_ret120_tags() -> tuple[pd.DataFrame, dict[str, Any]]:
    if not RET120_PANEL.exists():
        raise FileNotFoundError(RET120_PANEL)

    qp = RET120_PANEL.as_posix().replace("'", "''")
    con = duckdb.connect()
    try:
        panel = con.execute(
            f"""
            SELECT
                signal_date,
                symbol,
                ret120,
                excess_120
            FROM read_parquet('{qp}')
            WHERE ret120 IS NOT NULL
            ORDER BY signal_date, symbol
            """
        ).fetchdf()
    finally:
        con.close()

    panel["signal_date"] = panel["signal_date"].map(normalize_date)
    panel["symbol"] = panel["symbol"].map(normalize_symbol)

    if panel["signal_date"].isna().any():
        raise RuntimeError("Invalid signal_date in ret120 panel.")

    parts = []
    for _, g in panel.groupby("signal_date", sort=True):
        x = g.copy()
        x["ret120_bucket"] = assign_quintiles(x["ret120"])
        parts.append(x)

    tagged = pd.concat(parts, ignore_index=True)

    rec = {
        "panel_rows": int(len(tagged)),
        "tag_dates": int(tagged["signal_date"].nunique()),
        "tag_symbols": int(tagged["symbol"].nunique()),
        "tag_min": str(tagged["signal_date"].min()),
        "tag_max": str(tagged["signal_date"].max()),
        "bucket_reconciliation": None,
    }

    # Reconcile against the already-frozen daily bucket artifact on dates where
    # future S120 excess labels exist. This validates the bucket algorithm but
    # does not require excess_120 for recent ret120 tagging.
    if RET120_BUCKETS.exists():
        qb = RET120_BUCKETS.as_posix().replace("'", "''")
        con = duckdb.connect()
        try:
            frozen = con.execute(
                f"""
                SELECT signal_date, bucket, n, mean_excess
                FROM read_parquet('{qb}')
                WHERE feature = 'ret120'
                  AND horizon = 120
                ORDER BY signal_date, bucket
                """
            ).fetchdf()
        finally:
            con.close()

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
        chk["mean_diff"] = (
            chk["mean_excess_calc"] - chk["mean_excess"]
        )

        rec["bucket_reconciliation"] = {
            "rows": int(len(chk)),
            "dates": int(chk["signal_date"].nunique()),
            "count_mismatch_rows": int((chk["n_diff"] != 0).sum()),
            "max_mean_abs_diff": (
                float(chk["mean_diff"].abs().max())
                if len(chk)
                else math.nan
            ),
        }

        r = rec["bucket_reconciliation"]
        if (
            r["count_mismatch_rows"] != 0
            or not finite(r["max_mean_abs_diff"])
            or r["max_mean_abs_diff"] > 1e-12
        ):
            raise RuntimeError(
                "ret120 bucket reconstruction diverges from frozen "
                f"daily buckets: {r}"
            )

    return (
        tagged[
            [
                "signal_date",
                "symbol",
                "ret120",
                "ret120_bucket",
            ]
        ].copy(),
        rec,
    )


def summarize(values: pd.Series) -> dict[str, Any]:
    x = pd.to_numeric(values, errors="coerce")
    x = x[np.isfinite(x)]

    if len(x) == 0:
        return {
            "n": 0,
            "mean": None,
            "median": None,
            "win_rate": None,
            "p10": None,
            "bottom10_mean": None,
        }

    arr = x.to_numpy(dtype=float)
    k = max(1, int(math.ceil(len(arr) * 0.10)))
    bottom = np.sort(arr)[:k]

    return {
        "n": int(len(arr)),
        "mean": float(np.mean(arr)),
        "median": float(np.median(arr)),
        "win_rate": float(np.mean(arr > 0)),
        "p10": float(np.quantile(arr, 0.10)),
        "bottom10_mean": float(np.mean(bottom)),
    }


def bucket_summary(x: pd.DataFrame) -> dict[str, Any]:
    out = {}

    for q in ("Q1", "Q2", "Q3", "Q4", "Q5"):
        y = x[x["ret120_bucket"] == q]
        out[q] = {
            "trades": int(len(y)),
            "signal_dates": int(y["signal_date"].nunique()),
            "gross": summarize(y["open_gross_return"]),
            "excess": summarize(y["open_excess_gross"]),
            "net20": summarize(y["open_net_20bps"]),
            "net50": summarize(y["open_net_50bps"]),
        }

    return out


def q1_vs_rest(x: pd.DataFrame) -> dict[str, Any]:
    q1 = x[x["ret120_bucket"] == "Q1"]
    rest = x[x["ret120_bucket"] != "Q1"]

    fields = [
        "open_gross_return",
        "open_excess_gross",
        "open_net_20bps",
        "open_net_50bps",
    ]

    result = {
        "q1_n": int(len(q1)),
        "rest_n": int(len(rest)),
        "q1_share": float(len(q1) / len(x)) if len(x) else None,
    }

    for f in fields:
        a = summarize(q1[f])
        b = summarize(rest[f])
        result[f] = {
            "q1": a,
            "rest": b,
            "mean_gap_q1_minus_rest": (
                a["mean"] - b["mean"]
                if a["mean"] is not None and b["mean"] is not None
                else None
            ),
            "median_gap_q1_minus_rest": (
                a["median"] - b["median"]
                if a["median"] is not None and b["median"] is not None
                else None
            ),
            "win_rate_gap_q1_minus_rest": (
                a["win_rate"] - b["win_rate"]
                if a["win_rate"] is not None and b["win_rate"] is not None
                else None
            ),
            "bottom10_gap_q1_minus_rest": (
                a["bottom10_mean"] - b["bottom10_mean"]
                if (
                    a["bottom10_mean"] is not None
                    and b["bottom10_mean"] is not None
                )
                else None
            ),
        }

    return result


def paired_date_q1_vs_rest(
    x: pd.DataFrame,
    value_col: str,
) -> dict[str, Any]:
    rows = []

    for d, g in x.groupby("signal_date", sort=True):
        q1 = g[g["ret120_bucket"] == "Q1"]
        rest = g[g["ret120_bucket"] != "Q1"]

        if q1.empty or rest.empty:
            continue

        q1_mean = float(q1[value_col].mean())
        rest_mean = float(rest[value_col].mean())

        rows.append({
            "signal_date": d,
            "q1_mean": q1_mean,
            "rest_mean": rest_mean,
            "gap_q1_minus_rest": q1_mean - rest_mean,
        })

    if not rows:
        return {
            "paired_dates": 0,
            "mean_gap": None,
            "median_gap": None,
            "q1_better_date_ratio": None,
        }

    df = pd.DataFrame(rows)
    gap = df["gap_q1_minus_rest"]

    return {
        "paired_dates": int(len(df)),
        "mean_gap": float(gap.mean()),
        "median_gap": float(gap.median()),
        "q1_better_date_ratio": float((gap > 0).mean()),
    }


def folds_from_origin(origin) -> list[tuple[str, str, str]]:
    raw = getattr(origin, "FOLDS", None)
    if raw is None:
        raise RuntimeError("Original module does not expose FOLDS.")

    out = []
    for item in raw:
        if len(item) != 3:
            raise RuntimeError(f"Unexpected FOLDS item: {item!r}")
        name, start, end = item
        out.append((str(name), str(start), str(end)))
    return out


def pct(v: Any) -> str:
    return "NA" if not finite(v) else f"{float(v):+.2%}"


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    origin, origin_path = load_origin_module()
    dedup, dedup_receipt = rebuild_frozen_dedup(origin)
    tags, tag_receipt = load_ret120_tags()

    merged = dedup.merge(
        tags,
        on=["signal_date", "symbol"],
        how="left",
        validate="many_to_one",
    )

    coverage = float(merged["ret120_bucket"].notna().mean())

    if coverage < 0.995:
        merged[
            merged["ret120_bucket"].isna()
        ].to_csv(
            OUT_DIR / "missing_ret120_tags.csv",
            index=False,
            encoding="utf-8-sig",
        )
        raise RuntimeError(
            f"ret120 coverage on ORIGINAL dedup events is "
            f"{coverage:.2%} < 99.5%."
        )

    work = merged[
        merged["ret120_bucket"].notna()
    ].copy()

    work.to_csv(
        OUT_DIR / "deep_oversold_dedup_with_ret120.csv",
        index=False,
        encoding="utf-8-sig",
    )

    folds = folds_from_origin(origin)

    segments = {
        "ALL": work,
    }

    for name, start, end in folds:
        segments[name] = work[
            (work["signal_date"] >= start)
            & (work["signal_date"] <= end)
        ].copy()

    results = {}

    summary_rows = []

    for name, x in segments.items():
        b = bucket_summary(x)
        qr = q1_vs_rest(x)
        paired_excess = paired_date_q1_vs_rest(
            x,
            "open_excess_gross",
        )
        paired_gross = paired_date_q1_vs_rest(
            x,
            "open_gross_return",
        )

        results[name] = {
            "rows": int(len(x)),
            "signal_dates": int(x["signal_date"].nunique()),
            "buckets": b,
            "q1_vs_rest": qr,
            "paired_date_excess": paired_excess,
            "paired_date_gross": paired_gross,
        }

        for q in ("Q1", "Q2", "Q3", "Q4", "Q5"):
            summary_rows.append({
                "segment": name,
                "bucket": q,
                "trades": b[q]["trades"],
                "signal_dates": b[q]["signal_dates"],
                "gross_mean": b[q]["gross"]["mean"],
                "gross_median": b[q]["gross"]["median"],
                "gross_win_rate": b[q]["gross"]["win_rate"],
                "excess_mean": b[q]["excess"]["mean"],
                "excess_median": b[q]["excess"]["median"],
                "excess_win_rate": b[q]["excess"]["win_rate"],
                "excess_p10": b[q]["excess"]["p10"],
                "excess_bottom10_mean":
                    b[q]["excess"]["bottom10_mean"],
                "net20_mean": b[q]["net20"]["mean"],
                "net50_mean": b[q]["net50"]["mean"],
            })

    pd.DataFrame(summary_rows).to_csv(
        OUT_DIR / "bucket_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )

    audit = {
        "audit_id": "RET120_X_DEEP_OVERSOLD_V02",
        "origin_script": str(origin_path),
        "origin_pipeline": (
            "events -> complete -> standard_fillable -> fillable "
            "-> dedup_while_held -> ret120 attribution"
        ),
        "strategy_logic_changed": False,
        "veto_applied": False,
        "position_modifier_applied": False,
        "dedup_receipt": dedup_receipt,
        "ret120_receipt": tag_receipt,
        "ret120_coverage_on_dedup": coverage,
        "folds": folds,
        "results": results,
    }

    (OUT_DIR / "audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("=" * 110)
    print("RET120 × ORIGINAL DEEP OVERSOLD CSI500 ATTRIBUTION V0.2")
    print("=" * 110)
    print("Original script:", origin_path)
    print(
        "Original event counts:",
        f"raw={dedup_receipt['raw_events']} "
        f"complete={dedup_receipt['complete_events']} "
        f"fillable={dedup_receipt['fillable_events']} "
        f"dedup={dedup_receipt['first_signal_while_flat_events']}"
    )
    print(
        "Ret120 coverage on original dedup:",
        f"{coverage:.2%}",
    )
    if tag_receipt["bucket_reconciliation"] is not None:
        r = tag_receipt["bucket_reconciliation"]
        print(
            "Ret120 frozen-bucket reconciliation:",
            f"dates={r['dates']} "
            f"count_mismatch={r['count_mismatch_rows']} "
            f"max_mean_diff={r['max_mean_abs_diff']:.3e}"
        )
    print()

    for name in ["ALL"] + [x[0] for x in folds]:
        a = results[name]
        qr = a["q1_vs_rest"]["open_excess_gross"]
        pg = a["paired_date_excess"]

        print(name)
        print("-" * 110)

        for q in ("Q1", "Q2", "Q3", "Q4", "Q5"):
            b = a["buckets"][q]
            ex = b["excess"]
            gr = b["gross"]
            print(
                f"{q}: n={b['trades']:4d} "
                f"gross={pct(gr['mean'])} "
                f"excess={pct(ex['mean'])} "
                f"exMed={pct(ex['median'])} "
                f"exWin={ex['win_rate']:.1%} "
                f"exP10={pct(ex['p10'])} "
                f"exBottom10={pct(ex['bottom10_mean'])}"
            )

        print(
            "Q1 vs Rest — trade-equal EXCESS: "
            f"meanGap={pct(qr['mean_gap_q1_minus_rest'])} "
            f"medianGap={pct(qr['median_gap_q1_minus_rest'])} "
            f"winGap={pct(qr['win_rate_gap_q1_minus_rest'])} "
            f"bottom10Gap={pct(qr['bottom10_gap_q1_minus_rest'])}"
        )
        print(
            "Q1 vs Rest — paired signal dates EXCESS: "
            f"dates={pg['paired_dates']} "
            f"meanGap={pct(pg['mean_gap'])} "
            f"medianGap={pct(pg['median_gap'])} "
            "Q1Better="
            + (
                "NA"
                if pg["q1_better_date_ratio"] is None
                else f"{pg['q1_better_date_ratio']:.1%}"
            )
        )
        print()

    print("Output:", OUT_DIR)
    print("No veto was applied.")
    print("No shared-equity portfolio was rerun.")
    print("No strategy parameter or production database was modified.")


if __name__ == "__main__":
    main()
