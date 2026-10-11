from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import duckdb
import numpy as np
import pandas as pd
from xtquant import xtdata

from research.deep_oversold_replication import normalize_history
from research.deep_oversold_universe import (
    CSI500,
    CSI1000,
    load_accepted_daily,
)
from research.research_store import ResearchStore, sha256_file
from research.trend_edge_scan import (
    FEATURES,
    HORIZONS,
    FOLDS,
    add_stock_features,
    attach_forward_excess,
    assign_quintiles,
    summarize_daily_buckets,
    empty_summary,
    fast_reject,
    PRICE_ADJUSTMENT_MODE,
    quarantine_histories_with_audit,
)
from research.watchlist_adapter import (
    AdapterConfig,
    XtDataWatchlistAdapter,
    save_watchlist_snapshot,
)


@dataclass(frozen=True)
class ScanUniverse:
    tag: str
    display: str
    expected_size: int
    benchmark: str
    source_kind: str
    source_path: Path


UNIVERSES = {
    "csi300": ScanUniverse(
        "csi300", "CSI300", 300, "000300.SH", "SQLITE_CANONICAL",
        PROJECT_ROOT/"data/research/positionx_reference.db",
    ),
    "csi500": ScanUniverse(
        "csi500", "CSI500", 500, "000905.SH", "ACCEPTED_CSV",
        PROJECT_ROOT/"data/research/universe_assets/csi500",
    ),
    "csi1000": ScanUniverse(
        "csi1000", "CSI1000", 1000, "000852.SH", "ACCEPTED_CSV",
        PROJECT_ROOT/"data/research/universe_assets/csi1000",
    ),
}

OUT_DIR = PROJECT_ROOT/"data/research/edge_discovery"
DB = PROJECT_ROOT/"data/research/positionx_research.duckdb"


def load_csi300_daily(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise SystemExit(f"BLOCKED_CSI300_REFERENCE_MISSING path={path}")
    con = sqlite3.connect(str(path))
    try:
        reg = con.execute(
            "SELECT dataset_id,status,coverage_start,coverage_end "
            "FROM dataset_registry "
            "WHERE name='CSI300 Point-in-Time Canonical Membership' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if reg is None or reg[1] != "READY_FOR_RESEARCH":
            raise SystemExit(f"BLOCKED_CSI300_REFERENCE_NOT_READY registry={reg}")
        df = pd.read_sql_query(
            "SELECT trade_date,symbol FROM csi300_membership_daily "
            "WHERE dataset_id=? ORDER BY trade_date,symbol",
            con,
            params=[reg[0]],
            dtype={"trade_date": str, "symbol": str},
        )
    finally:
        con.close()
    df["trade_date"] = df["trade_date"].astype(str)
    df["symbol"] = df["symbol"].astype(str)
    return df


def load_membership(spec: ScanUniverse, start: str, end: str) -> pd.DataFrame:
    if spec.tag == "csi300":
        daily = load_csi300_daily(spec.source_path)
    elif spec.tag == "csi500":
        daily = load_accepted_daily(spec.source_path, CSI500)
    elif spec.tag == "csi1000":
        daily = load_accepted_daily(spec.source_path, CSI1000)
    else:
        raise ValueError(spec.tag)

    membership = daily[
        (daily["trade_date"] >= start) & (daily["trade_date"] <= end)
    ][["trade_date", "symbol"]].rename(columns={"trade_date": "signal_date"})
    if membership.duplicated(["signal_date", "symbol"]).any():
        raise SystemExit(f"BLOCKED_DUPLICATE_PIT_MEMBERSHIP universe={spec.display}")

    counts = membership.groupby("signal_date")["symbol"].nunique()
    bad = counts[counts != spec.expected_size]
    if len(bad):
        raise SystemExit(
            f"BLOCKED_BAD_PIT_COUNTS universe={spec.display} "
            f"bad_dates={len(bad)} sample={bad.head(20).to_dict()}"
        )
    return membership


def load_histories(symbols: list[str], bars: int = 2200) -> dict[str, pd.DataFrame]:
    adapter = XtDataWatchlistAdapter(
        xtdata,
        AdapterConfig(
            sector="",
            bars=bars,
            dividend_type=PRICE_ADJUSTMENT_MODE,
        ),
    )
    raw = adapter.history(symbols)
    return {
        s: normalize_history(df)
        for s, df in raw.items()
        if df is not None and not df.empty
    }


def refresh_missing(symbols: list[str], start: str, end: str) -> None:
    for i, s in enumerate(symbols, 1):
        print(f"[DATA] refresh {i}/{len(symbols)} {s}")
        try:
            xtdata.download_history_data(
                s, period="1d", start_time=start, end_time=end
            )
        except TypeError:
            xtdata.download_history_data(s, "1d", start, end)


def save_parquet(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.register("dfv", df)
        p = str(path).replace("\\", "/").replace("'", "''")
        con.execute(
            f"COPY dfv TO '{p}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
    finally:
        con.close()


def build_panel(
    histories: dict[str, pd.DataFrame],
    benchmark: pd.DataFrame,
    membership: pd.DataFrame,
    calendar: list[str],
) -> pd.DataFrame:
    bclose = pd.to_numeric(
        benchmark["close"], errors="coerce"
    ).reindex(calendar)

    member_by_symbol = {
        s: g["signal_date"].astype(str).tolist()
        for s, g in membership.groupby("symbol")
    }

    parts = []
    total = len(member_by_symbol)
    for i, (symbol, dates) in enumerate(member_by_symbol.items(), 1):
        if i == 1 or i % 100 == 0:
            print(f"[PANEL] {i}/{total} {symbol}")
        df = histories.get(symbol)
        if df is None:
            continue

        feat = add_stock_features(df, bclose)
        lab = attach_forward_excess(feat, benchmark, calendar, HORIZONS)
        use = lab.reindex(dates)

        out = pd.DataFrame({
            "signal_date": dates,
            "symbol": symbol,
        })
        for c in FEATURES:
            out[c] = use[c].to_numpy()
        for h in HORIZONS:
            out[f"excess_{h}"] = use[f"excess_{h}"].to_numpy()
            out[f"label_quality_reject_{h}"] = (
                use[f"label_quality_reject_{h}"]
                .fillna(False)
                .astype(bool)
                .to_numpy()
            )
        parts.append(out)

    return (
        pd.concat(parts, ignore_index=True)
        if parts else pd.DataFrame()
    )


def build_daily_buckets(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for feature in FEATURES:
        print(f"[SCAN] {feature}")
        for d, g in panel.groupby("signal_date", sort=True):
            buckets = assign_quintiles(g[feature])
            # Preserve the v0.1 CSI500 requirement proportionally:
            # at least 80% of the day's valid universe must have a feature.
            min_feature = max(50, int(round(len(g) * 0.80)))
            if buckets.notna().sum() < min_feature:
                continue

            z = g.copy()
            z["bucket"] = buckets

            for h in HORIZONS:
                col = f"excess_{h}"
                valid = z.dropna(subset=["bucket", col])
                min_label = max(50, int(round(len(g) * 0.60)))
                if len(valid) < min_label:
                    continue

                for q, qg in valid.groupby("bucket"):
                    if len(qg) < 10:
                        continue
                    rows.append({
                        "signal_date": d,
                        "feature": feature,
                        "horizon": h,
                        "bucket": str(q),
                        "n": int(len(qg)),
                        "mean_excess": float(qg[col].mean()),
                        "median_excess": float(qg[col].median()),
                    })
    return pd.DataFrame(rows)


def result_for_feature(
    buckets: pd.DataFrame,
    feature: str,
) -> dict[str, Any]:
    fb = buckets[buckets["feature"] == feature]
    full = {}
    folds = {name: {} for name, _, _ in FOLDS}

    for h in HORIZONS:
        x = fb[fb["horizon"] == h]
        full[str(h)] = (
            summarize_daily_buckets(x) if len(x) else empty_summary()
        )
        for name, start, end in FOLDS:
            y = x[
                (x["signal_date"] >= start)
                & (x["signal_date"] <= end)
            ]
            folds[name][str(h)] = (
                summarize_daily_buckets(y) if len(y) else empty_summary()
            )

    r = {
        "feature": feature,
        "full_sample": full,
        "folds": folds,
    }
    r["fast_reject"] = fast_reject(r)
    return r


def scan_one(
    spec: ScanUniverse,
    *,
    start: str,
    end: str,
    refresh_missing_history: bool,
    db_path: str,
) -> dict[str, Any]:
    print(f"\n{'='*100}")
    print(f"UNIVERSE {spec.display}")
    print(f"{'='*100}")

    membership = load_membership(spec, start, end)
    symbols = sorted(membership["symbol"].unique().tolist())
    names = symbols + [spec.benchmark]

    histories = load_histories(names, 2200)
    missing = sorted(set(names) - set(histories))
    if missing and refresh_missing_history:
        dl_start = (
            datetime.strptime(start, "%Y%m%d") - timedelta(days=400)
        ).strftime("%Y%m%d")
        refresh_missing(missing, dl_start, end)
        histories = load_histories(names, 2200)
        missing = sorted(set(names) - set(histories))

    if missing:
        raise SystemExit(
            f"BLOCKED_INCOMPLETE_HISTORY universe={spec.display} "
            f"missing={len(missing)} sample={missing[:30]}\n"
            "Rerun with --refresh-missing-history"
        )

    benchmark = histories.pop(spec.benchmark)
    integrity_start = (
        datetime.strptime(start, "%Y%m%d") - timedelta(days=400)
    ).strftime("%Y%m%d")

    good_bench, bad_bench, bench_sanitation = quarantine_histories_with_audit(
        {spec.benchmark: benchmark},
        integrity_start=integrity_start,
    )
    if bad_bench:
        raise SystemExit(
            f"BLOCKED_BENCHMARK_PRICE_INTEGRITY "
            f"universe={spec.display} detail={bad_bench}"
        )
    benchmark = good_bench[spec.benchmark]

    histories, quarantined, sanitized_sparse = quarantine_histories_with_audit(
        histories,
        integrity_start=integrity_start,
    )
    quarantine_fraction = len(quarantined) / max(1, len(symbols))
    if quarantine_fraction > 0.01:
        raise SystemExit(
            f"BLOCKED_TOO_MANY_QUARANTINED_HISTORIES "
            f"universe={spec.display} quarantined={len(quarantined)} "
            f"union={len(symbols)} fraction={quarantine_fraction:.4%} "
            f"sample={quarantined[:20]}"
        )

    calendar = [
        d for d in benchmark.index
        if start <= d <= end
    ]
    if len(calendar) < 500:
        raise SystemExit(
            f"BLOCKED_SHORT_CALENDAR universe={spec.display} "
            f"sessions={len(calendar)}"
        )

    panel = build_panel(histories, benchmark, membership, calendar)
    min_rows = int(len(membership) * 0.95)
    if len(panel) < min_rows:
        raise SystemExit(
            f"BLOCKED_PANEL_TOO_SMALL universe={spec.display} "
            f"rows={len(panel)} expected_at_least={min_rows}"
        )

    quality_reject = {
        str(h): int(panel[f"label_quality_reject_{h}"].sum())
        for h in HORIZONS
    }
    max_abs_excess = {
        str(h): (
            float(pd.to_numeric(
                panel[f"excess_{h}"], errors="coerce"
            ).abs().max())
            if panel[f"excess_{h}"].notna().any()
            else None
        )
        for h in HORIZONS
    }
    # Last-resort fail-closed check: do not allow another 1e11-style label
    # to reach the factor means.
    for h, val in max_abs_excess.items():
        if val is not None and val > 100.0:
            raise SystemExit(
                f"BLOCKED_EXTREME_LABEL_AFTER_QUALITY_GATE "
                f"universe={spec.display} horizon={h} max_abs_excess={val}"
            )

    buckets = build_daily_buckets(panel)
    results = [
        result_for_feature(buckets, f)
        for f in FEATURES
    ]
    candidates = [
        r["feature"]
        for r in results
        if r["fast_reject"]["fast_reject_candidate"]
    ]

    panel_path = OUT_DIR/f"trend_persistence_scan_{spec.tag}_panel_v0221.parquet"
    buckets_path = OUT_DIR/f"trend_persistence_scan_{spec.tag}_daily_buckets_v0221.parquet"
    json_path = OUT_DIR/f"trend_persistence_scan_{spec.tag}_v022_latest.json"

    save_parquet(panel, panel_path)
    save_parquet(buckets, buckets_path)

    with ResearchStore(db_path) as store:
        store.register_external_view(
            asset_id=f"EDGE2_TREND_{spec.display}_PANEL_V022",
            view_name=f"edge2_trend_{spec.tag}_panel_v022",
            path=panel_path,
            source_system="POSITIONX_DERIVED_RESEARCH",
            file_kind="parquet",
            asof_date=end,
            row_count=len(panel),
            metadata={
                "stage": "discovery",
                "live_strategy": False,
                "version": "0.2.1",
                "price_integrity_quarantine": True,
            },
        )
        store.register_external_view(
            asset_id=f"EDGE2_TREND_{spec.display}_BUCKETS_V022",
            view_name=f"edge2_trend_{spec.tag}_daily_buckets_v022",
            path=buckets_path,
            source_system="POSITIONX_DERIVED_RESEARCH",
            file_kind="parquet",
            asof_date=end,
            row_count=len(buckets),
            metadata={
                "stage": "discovery",
                "live_strategy": False,
                "version": "0.2.1",
            },
        )

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "EDGE_DISCOVERY_2_TREND_PERSISTENCE_SCAN_V022",
        "status": "OK",
        "safety": {
            "uses_broker": False,
            "changes_forward_protocol": False,
            "creates_strategy": False,
            "changes_execution": False,
        },
        "frozen_scanner": {
            "features": list(FEATURES),
            "horizons": list(HORIZONS),
            "folds": [list(x) for x in FOLDS],
            "entry_label": "T+1 OPEN",
            "future_exit_labels": [
                "S20 CLOSE", "S60 CLOSE", "S120 CLOSE"
            ],
            "fast_reject_gate": (
                "unchanged from v0.1: all 3 full-sample spreads positive; "
                ">=2 horizons monotonicity >=0.5; each horizon >=2/3 positive folds"
            ),
        },
        "universe": {
            "name": spec.display,
            "expected_members": spec.expected_size,
            "benchmark": spec.benchmark,
            "source_kind": spec.source_kind,
            "source_path": str(spec.source_path),
            "union_symbols": len(symbols),
            "start_date": start,
            "end_date": end,
        },
        "data_quality_fix": {
            "price_adjustment_mode": PRICE_ADJUSTMENT_MODE,
            "v01_result_invalid": True,
            "reason": (
                "v0.1 lacked catastrophic continuity checks; v0.2/v0.2.1 also "
                "misinterpreted ordinary front-adjusted negative levels as invalid. "
                "v0.2.2 uses ratio-style back adjustment plus the same fail-closed "
                "continuity checks."
            ),
            "quarantined_symbol_count": len(quarantined),
            "sanitized_sparse_symbol_count": len(sanitized_sparse),
            "sanitized_sparse_sample": sanitized_sparse[:50],
            "quarantined_fraction_of_union": quarantine_fraction,
            "quarantined_sample": quarantined[:50],
            "label_quality_reject_count": quality_reject,
            "max_abs_excess_after_gate": max_abs_excess,
            "winsorization": False,
            "return_clipping": False,
        },
        "counts": {
            "membership_rows": len(membership),
            "panel_rows": len(panel),
            "symbols_in_panel": int(panel["symbol"].nunique()),
            "signal_dates": int(panel["signal_date"].nunique()),
            "bucket_rows": len(buckets),
        },
        "results": results,
        "fast_reject_candidates": candidates,
        "assets": {
            "panel": str(panel_path),
            "panel_sha256": sha256_file(panel_path),
            "daily_buckets": str(buckets_path),
            "daily_buckets_sha256": sha256_file(buckets_path),
        },
    }
    save_watchlist_snapshot(json_path, out)
    out["json_path"] = str(json_path)

    print("\nDATA QUALITY")
    print(f"  price_adjustment={PRICE_ADJUSTMENT_MODE}")
    print(
        f"  union={len(symbols)} quarantined={len(quarantined)} "
        f"({quarantine_fraction:.3%}) sanitized_sparse={len(sanitized_sparse)}"
    )
    print(f"  label_reject={quality_reject}")
    print(f"  max_abs_excess={max_abs_excess}")
    if quarantined:
        print("  quarantine sample:")
        for q in quarantined[:10]:
            print(
                f"    {q['symbol']} reason={q['reason']} "
                f"bad_close={q.get('bad_close_dates', [])[:3]} "
                f"bad_open={q.get('bad_open_dates', [])[:3]}"
            )

    print("\nFEATURE SUMMARY — date-equal Q5 minus Q1 future excess")
    print(
        " feature          20d spread/mono     60d spread/mono    "
        "120d spread/mono   folds+(20/60/120) verdict"
    )
    print("-"*118)
    for r in results:
        vals = []
        for h in HORIZONS:
            x = r["full_sample"][str(h)]
            vals.append(
                f"{x['q5_minus_q1']:+.2%}/{x['monotonicity']:+.2f}"
            )
        fp = r["fast_reject"]["positive_folds_by_horizon"]
        verdict = (
            "CANDIDATE"
            if r["fast_reject"]["fast_reject_candidate"]
            else "REJECT"
        )
        print(
            f" {r['feature']:15s} "
            f"{vals[0]:>16s} {vals[1]:>18s} {vals[2]:>18s}    "
            f"{fp['20']}/{fp['60']}/{fp['120']}       {verdict}"
        )

    print("\nFAST-REJECT CANDIDATES:", candidates if candidates else "NONE")
    print(f"JSON: {json_path}")
    print(f"PANEL: {panel_path}")
    print(f"BUCKETS: {buckets_path}")
    return out


def main() -> int:
    p = argparse.ArgumentParser(
        description=(
            "PositionX Edge Discovery #2 — Trend Persistence "
            "CSI300/CSI500/CSI1000 replication v0.2.2"
        )
    )
    p.add_argument("--start-date", default="20200803")
    p.add_argument("--end-date", default="20260924")
    p.add_argument(
        "--universe",
        choices=["all", "csi300", "csi500", "csi1000"],
        default="all",
    )
    p.add_argument("--refresh-missing-history", action="store_true")
    p.add_argument("--db", default=str(DB))
    a = p.parse_args()

    tags = (
        ["csi300", "csi500", "csi1000"]
        if a.universe == "all"
        else [a.universe]
    )

    outputs = {}
    for tag in tags:
        spec = UNIVERSES[tag]
        outputs[tag] = scan_one(
            spec,
            start=a.start_date,
            end=a.end_date,
            refresh_missing_history=a.refresh_missing_history,
            db_path=a.db,
        )

    replication = []
    if len(outputs) > 1:
        print(f"\n{'='*100}")
        print("CROSS-UNIVERSE REPLICATION")
        print(f"{'='*100}")
        print(
            " feature          CSI300      CSI500      CSI1000     "
            "candidate universes"
        )
        print("-"*82)
        for feature in FEATURES:
            flags = {}
            for tag in ("csi300", "csi500", "csi1000"):
                if tag not in outputs:
                    continue
                rr = next(
                    x for x in outputs[tag]["results"]
                    if x["feature"] == feature
                )
                flags[tag] = bool(
                    rr["fast_reject"]["fast_reject_candidate"]
                )
            names = [
                UNIVERSES[tag].display
                for tag, v in flags.items() if v
            ]
            replication.append({
                "feature": feature,
                "candidate_by_universe": flags,
                "candidate_universe_count": sum(flags.values()),
                "candidate_universes": names,
            })
            print(
                f" {feature:15s} "
                f"{('CANDIDATE' if flags.get('csi300') else 'REJECT'):>10s} "
                f"{('CANDIDATE' if flags.get('csi500') else 'REJECT'):>11s} "
                f"{('CANDIDATE' if flags.get('csi1000') else 'REJECT'):>12s}    "
                f"{', '.join(names) if names else '-'}"
            )

        summary_path = OUT_DIR/"trend_persistence_replication_v022_latest.json"
        summary = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "mode": "EDGE_DISCOVERY_2_TREND_MULTIUNIVERSE_REPLICATION_V022",
            "status": "OK",
            "frozen_scanner": {
                "features": list(FEATURES),
                "horizons": list(HORIZONS),
                "folds": [list(x) for x in FOLDS],
                "fast_reject_unchanged_from_v01": True,
            },
            "universe_results": {
                tag: {
                    "candidates": outputs[tag]["fast_reject_candidates"],
                    "json_path": outputs[tag]["json_path"],
                    "quality": outputs[tag]["data_quality_fix"],
                }
                for tag in outputs
            },
            "cross_universe_replication": replication,
            "note": (
                "Candidate counts are descriptive replication evidence only. "
                "No cross-universe promotion threshold or strategy was created."
            ),
        }
        save_watchlist_snapshot(summary_path, summary)
        print("\nNo strategy, threshold, target weight or Forward protocol was created.")
        print(f"SUMMARY JSON: {summary_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
