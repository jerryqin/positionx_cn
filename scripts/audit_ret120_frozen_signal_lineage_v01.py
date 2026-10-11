from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


AUDIT_ID = "RET120_FROZEN_SIGNAL_LINEAGE_V0_1"
PASS_STATUS = "PASS_RET120_FROZEN_SIGNAL_LINEAGE_V0_1"
FAIL_STATUS = "FAIL_CLOSED_RET120_FROZEN_SIGNAL_LINEAGE_V0_1"

EXPECTED_PANEL_NAME = "trend_persistence_scan_csi500_panel_v0221.parquet"
EXPECTED_BUCKET_NAME = "trend_persistence_scan_csi500_daily_buckets_v0221.parquet"
EXPECTED_JSON_NAME = "trend_persistence_scan_csi500_v022_latest.json"
EXPECTED_REPLICATION_JSON_NAME = "trend_persistence_replication_v022_latest.json"

EXPECTED_FOLDS = (
    ("F1", "20200803", "20221017"),
    ("F2", "20221018", "20240919"),
    ("F3", "20240920", "20260924"),
)
EXPECTED_HORIZONS = (20, 60, 120)
EXPECTED_REPORTED = {
    20: {"spread_pct_2dp": 0.18, "mono_2dp": 0.80, "positive_folds": 2},
    60: {"spread_pct_2dp": 1.53, "mono_2dp": 0.90, "positive_folds": 3},
    120: {"spread_pct_2dp": 3.50, "mono_2dp": 1.00, "positive_folds": 3},
}

# Historical archived v0.2.2 patch reference hashes.
# Informational for the current working tree; the frozen data-artifact reconciliation
# below is the decisive lineage proof.
EXPECTED_CANONICAL_SOURCE_SHA256 = {
    "research/trend_edge_scan.py":
        "801fcda9cc7db7287eb8ea766c3e054c7804d0cdd64ba0650196701787f0fe16",
    "scripts/run_edge_discovery_trend_replication_v022.py":
        "96dc4bcb8fdbbc3097338aa0f9b2b1c48fb2dcc0a235e0db29cccfd7c3f8e4d1",
}

FLOAT_TOL = 1e-12


class FailClosed(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize_signal_date(s: pd.Series) -> pd.Series:
    def one(v: Any) -> str:
        if pd.isna(v):
            return ""
        if isinstance(v, (np.integer, int)):
            return f"{int(v):08d}"
        if isinstance(v, (np.floating, float)) and float(v).is_integer():
            return f"{int(v):08d}"
        x = str(v).strip()
        digits = "".join(ch for ch in x if ch.isdigit())
        return digits[:8] if len(digits) >= 8 else x
    return s.map(one)


def assign_quintiles(values: pd.Series) -> pd.Series:
    """Exact frozen v0.2.2 algorithm."""
    v = pd.to_numeric(values, errors="coerce")
    out = pd.Series(index=v.index, dtype="object")
    good = v.dropna()
    if len(good) < 50:
        return out
    r = good.rank(method="first")
    q = pd.qcut(r, 5, labels=["Q1", "Q2", "Q3", "Q4", "Q5"])
    out.loc[q.index] = q.astype(str)
    return out


def monotonicity_score(qmeans: dict[str, float]) -> float | None:
    ys = []
    for q in ("Q1", "Q2", "Q3", "Q4", "Q5"):
        v = qmeans.get(q)
        if v is None or not math.isfinite(float(v)):
            return None
        ys.append(float(v))
    ranks = pd.Series(ys).rank(method="average").to_numpy(dtype=float)
    return float(np.corrcoef(np.arange(1, 6, dtype=float), ranks)[0, 1])


def summarize_daily_buckets(daily: pd.DataFrame) -> dict[str, Any]:
    qmeans = {
        q: float(daily.loc[daily["bucket"] == q, "mean_excess"].mean())
        for q in ["Q1", "Q2", "Q3", "Q4", "Q5"]
    }
    spread = qmeans["Q5"] - qmeans["Q1"]
    return {
        "days": int(daily["signal_date"].nunique()),
        "qmeans": qmeans,
        "q5_minus_q1": float(spread),
        "monotonicity": monotonicity_score(qmeans),
    }


def reconstruct_ret120(panel: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Replays only the exact frozen v0.2.2 ret120 branch.

    Important: bucket assignment is made from the full signal-date panel BEFORE
    horizon-label completeness is applied. This matches build_daily_buckets()
    in the frozen scanner and avoids the later-audit shortcut that first filtered
    to complete S120 labels.
    """
    membership_parts: list[pd.DataFrame] = []
    bucket_rows: list[dict[str, Any]] = []

    for d, g in panel.groupby("signal_date", sort=True):
        z = g.copy()
        z["bucket"] = assign_quintiles(z["ret120"])

        min_feature = max(50, int(round(len(g) * 0.80)))
        if z["bucket"].notna().sum() < min_feature:
            continue

        membership_parts.append(
            z[["signal_date", "symbol", "ret120", "bucket"]].copy()
        )

        for h in EXPECTED_HORIZONS:
            col = f"excess_{h}"
            valid = z.dropna(subset=["bucket", col])
            min_label = max(50, int(round(len(g) * 0.60)))
            if len(valid) < min_label:
                continue

            for q, qg in valid.groupby("bucket", observed=True):
                if len(qg) < 10:
                    continue
                bucket_rows.append({
                    "signal_date": d,
                    "feature": "ret120",
                    "horizon": int(h),
                    "bucket": str(q),
                    "n": int(len(qg)),
                    "mean_excess": float(qg[col].mean()),
                    "median_excess": float(qg[col].median()),
                })

    membership = (
        pd.concat(membership_parts, ignore_index=True)
        if membership_parts
        else pd.DataFrame(columns=["signal_date", "symbol", "ret120", "bucket"])
    )
    buckets = pd.DataFrame(bucket_rows)
    return membership, buckets


def _asset_hash_from_json(payload: dict[str, Any], key: str) -> str | None:
    assets = payload.get("assets") or {}
    v = assets.get(key)
    return str(v).lower() if v else None


def _asset_path_from_json(payload: dict[str, Any], key: str) -> str | None:
    assets = payload.get("assets") or {}
    v = assets.get(key)
    return str(v) if v else None


def get_ret120_result(payload: dict[str, Any]) -> dict[str, Any]:
    for row in payload.get("results", []):
        if row.get("feature") == "ret120":
            return row
    raise FailClosed("ret120 result is missing from frozen scanner JSON.")


def normalize_folds(raw: Any) -> tuple[tuple[str, str, str], ...]:
    if not isinstance(raw, (list, tuple)):
        raise FailClosed("Frozen fold definition is missing or not a list.")
    out = []
    for row in raw:
        if not isinstance(row, (list, tuple)) or len(row) != 3:
            raise FailClosed(f"Invalid frozen fold row: {row!r}")
        out.append((str(row[0]), str(row[1]), str(row[2])))
    return tuple(out)


def compare_rebuilt_buckets(
    rebuilt: pd.DataFrame,
    frozen: pd.DataFrame,
) -> dict[str, Any]:
    keys = ["signal_date", "horizon", "bucket"]
    r = rebuilt.copy()
    f = frozen.copy()

    r["signal_date"] = normalize_signal_date(r["signal_date"])
    f["signal_date"] = normalize_signal_date(f["signal_date"])
    r["horizon"] = pd.to_numeric(r["horizon"], errors="raise").astype(int)
    f["horizon"] = pd.to_numeric(f["horizon"], errors="raise").astype(int)
    r["bucket"] = r["bucket"].astype(str)
    f["bucket"] = f["bucket"].astype(str)

    for x in (r, f):
        if x.duplicated(keys).any():
            dup = x.loc[x.duplicated(keys, keep=False), keys].head(10)
            raise FailClosed(f"Duplicate daily bucket keys detected:\n{dup}")

    m = f.merge(
        r,
        on=keys,
        how="outer",
        suffixes=("_frozen", "_rebuilt"),
        indicator=True,
        validate="one_to_one",
    )
    missing_keys = int((m["_merge"] != "both").sum())

    both = m[m["_merge"] == "both"].copy()
    n_mismatch = int(
        (
            pd.to_numeric(both["n_frozen"], errors="coerce")
            != pd.to_numeric(both["n_rebuilt"], errors="coerce")
        ).sum()
    )
    mean_diff = (
        pd.to_numeric(both["mean_excess_frozen"], errors="coerce")
        - pd.to_numeric(both["mean_excess_rebuilt"], errors="coerce")
    ).abs()
    median_diff = (
        pd.to_numeric(both["median_excess_frozen"], errors="coerce")
        - pd.to_numeric(both["median_excess_rebuilt"], errors="coerce")
    ).abs()

    max_mean_diff = float(mean_diff.max()) if len(mean_diff) else math.nan
    max_median_diff = float(median_diff.max()) if len(median_diff) else math.nan

    if missing_keys:
        sample = m.loc[m["_merge"] != "both", keys + ["_merge"]].head(20)
        raise FailClosed(
            f"Frozen/rebuilt bucket key mismatch rows={missing_keys}. Sample:\n{sample}"
        )
    if n_mismatch:
        raise FailClosed(f"Frozen/rebuilt bucket count mismatch rows={n_mismatch}.")
    if not math.isfinite(max_mean_diff) or max_mean_diff > FLOAT_TOL:
        raise FailClosed(
            f"Frozen/rebuilt mean_excess mismatch max_abs_diff={max_mean_diff}."
        )
    if not math.isfinite(max_median_diff) or max_median_diff > FLOAT_TOL:
        raise FailClosed(
            f"Frozen/rebuilt median_excess mismatch max_abs_diff={max_median_diff}."
        )

    return {
        "frozen_rows": int(len(f)),
        "rebuilt_rows": int(len(r)),
        "missing_or_extra_key_rows": missing_keys,
        "n_mismatch_rows": n_mismatch,
        "max_mean_abs_diff": max_mean_diff,
        "max_median_abs_diff": max_median_diff,
    }


def validate_metrics(
    rebuilt_buckets: pd.DataFrame,
    frozen_result: dict[str, Any],
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for h in EXPECTED_HORIZONS:
        x = rebuilt_buckets[rebuilt_buckets["horizon"] == h]
        if x.empty:
            raise FailClosed(f"No rebuilt bucket rows for horizon={h}.")
        s = summarize_daily_buckets(x)

        j = (frozen_result.get("full_sample") or {}).get(str(h))
        if not isinstance(j, dict):
            raise FailClosed(f"Frozen JSON full_sample[{h}] missing.")

        for field in ("q5_minus_q1", "monotonicity"):
            a = float(s[field])
            b = float(j[field])
            if abs(a - b) > FLOAT_TOL:
                raise FailClosed(
                    f"Frozen JSON mismatch horizon={h} field={field}: "
                    f"rebuilt={a} frozen={b}"
                )

        fold_spreads: dict[str, float] = {}
        positive = 0
        for name, start, end in EXPECTED_FOLDS:
            y = x[
                (x["signal_date"] >= start)
                & (x["signal_date"] <= end)
            ]
            if y.empty:
                raise FailClosed(f"No rebuilt rows for {name} horizon={h}.")
            fs = summarize_daily_buckets(y)
            frozen_fold = (
                (frozen_result.get("folds") or {}).get(name, {}).get(str(h))
            )
            if not isinstance(frozen_fold, dict):
                raise FailClosed(f"Frozen JSON fold missing: {name}/{h}")
            a = float(fs["q5_minus_q1"])
            b = float(frozen_fold["q5_minus_q1"])
            if abs(a - b) > FLOAT_TOL:
                raise FailClosed(
                    f"Frozen fold mismatch {name}/{h}: rebuilt={a} frozen={b}"
                )
            fold_spreads[name] = a
            if a > 0:
                positive += 1

        exp = EXPECTED_REPORTED[h]
        if round(s["q5_minus_q1"] * 100, 2) != exp["spread_pct_2dp"]:
            raise FailClosed(
                f"Reported spread checkpoint failed for {h}: "
                f"{s['q5_minus_q1'] * 100:.8f}%"
            )
        if round(float(s["monotonicity"]), 2) != exp["mono_2dp"]:
            raise FailClosed(
                f"Reported monotonicity checkpoint failed for {h}: "
                f"{s['monotonicity']}"
            )
        if positive != exp["positive_folds"]:
            raise FailClosed(
                f"Reported positive-fold checkpoint failed for {h}: {positive}"
            )

        out[str(h)] = {
            "q5_minus_q1": float(s["q5_minus_q1"]),
            "monotonicity": float(s["monotonicity"]),
            "positive_folds": positive,
            "fold_q5_minus_q1": fold_spreads,
        }
    return out


def inspect_local_source(root: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for rel, expected in EXPECTED_CANONICAL_SOURCE_SHA256.items():
        p = root / rel
        if not p.exists():
            out[rel] = {
                "exists": False,
                "expected_historical_sha256": expected,
                "status": "MISSING_INFORMATIONAL",
            }
            continue
        actual = sha256_file(p)
        out[rel] = {
            "exists": True,
            "sha256": actual,
            "expected_historical_sha256": expected,
            "status": (
                "MATCH_HISTORICAL_CANONICAL"
                if actual == expected
                else "LOCAL_SOURCE_DRIFT_INFORMATIONAL"
            ),
        }
    return out


def validate_json_semantics(
    scanner: dict[str, Any],
    replication: dict[str, Any] | None,
) -> None:
    fs = scanner.get("frozen_scanner") or {}
    features = tuple(fs.get("features") or ())
    horizons = tuple(int(x) for x in (fs.get("horizons") or ()))
    folds = normalize_folds(fs.get("folds"))

    if "ret120" not in features:
        raise FailClosed("ret120 missing from frozen_scanner.features.")
    if horizons != EXPECTED_HORIZONS:
        raise FailClosed(f"Unexpected frozen horizons: {horizons}")
    if folds != EXPECTED_FOLDS:
        raise FailClosed(f"Unexpected frozen folds: {folds}")
    if str(fs.get("entry_label")) != "T+1 OPEN":
        raise FailClosed(f"Unexpected entry label: {fs.get('entry_label')!r}")
    exits = tuple(fs.get("future_exit_labels") or ())
    if exits != ("S20 CLOSE", "S60 CLOSE", "S120 CLOSE"):
        raise FailClosed(f"Unexpected exit labels: {exits}")

    if replication is not None:
        rfs = replication.get("frozen_scanner") or {}
        if rfs.get("folds") is not None:
            if normalize_folds(rfs.get("folds")) != EXPECTED_FOLDS:
                raise FailClosed("Replication JSON fold definitions disagree.")


def build_audit(root: Path) -> dict[str, Any]:
    edge = root / "data" / "research" / "edge_discovery"
    panel_path = edge / EXPECTED_PANEL_NAME
    bucket_path = edge / EXPECTED_BUCKET_NAME
    json_path = edge / EXPECTED_JSON_NAME
    replication_path = edge / EXPECTED_REPLICATION_JSON_NAME

    for p in (panel_path, bucket_path, json_path):
        if not p.exists():
            raise FailClosed(f"Required frozen artifact is missing: {p}")

    scanner = json.loads(json_path.read_text(encoding="utf-8"))
    replication = (
        json.loads(replication_path.read_text(encoding="utf-8"))
        if replication_path.exists()
        else None
    )
    validate_json_semantics(scanner, replication)

    actual_panel_sha = sha256_file(panel_path)
    actual_bucket_sha = sha256_file(bucket_path)
    expected_panel_sha = _asset_hash_from_json(scanner, "panel_sha256")
    expected_bucket_sha = _asset_hash_from_json(scanner, "daily_buckets_sha256")

    if not expected_panel_sha or actual_panel_sha != expected_panel_sha:
        raise FailClosed(
            "Panel SHA256 does not match the frozen scanner JSON: "
            f"actual={actual_panel_sha} expected={expected_panel_sha}"
        )
    if not expected_bucket_sha or actual_bucket_sha != expected_bucket_sha:
        raise FailClosed(
            "Daily-buckets SHA256 does not match the frozen scanner JSON: "
            f"actual={actual_bucket_sha} expected={expected_bucket_sha}"
        )

    json_panel_path = _asset_path_from_json(scanner, "panel")
    json_bucket_path = _asset_path_from_json(scanner, "daily_buckets")
    if json_panel_path and Path(json_panel_path).name != EXPECTED_PANEL_NAME:
        raise FailClosed(f"Frozen JSON panel path points to: {json_panel_path}")
    if json_bucket_path and Path(json_bucket_path).name != EXPECTED_BUCKET_NAME:
        raise FailClosed(f"Frozen JSON bucket path points to: {json_bucket_path}")

    required_panel = [
        "signal_date", "symbol", "ret120",
        "excess_20", "excess_60", "excess_120",
        "label_quality_reject_20",
        "label_quality_reject_60",
        "label_quality_reject_120",
    ]
    panel = pd.read_parquet(panel_path)
    missing = [c for c in required_panel if c not in panel.columns]
    if missing:
        raise FailClosed(f"Frozen panel missing columns: {missing}")
    panel = panel[required_panel].copy()
    panel["signal_date"] = normalize_signal_date(panel["signal_date"])
    panel["symbol"] = panel["symbol"].astype(str)

    dup_symbol_date = int(panel.duplicated(["signal_date", "symbol"]).sum())
    if dup_symbol_date:
        raise FailClosed(f"Duplicate panel symbol-date rows: {dup_symbol_date}")

    frozen_buckets = pd.read_parquet(bucket_path)
    required_bucket = [
        "signal_date", "feature", "horizon", "bucket",
        "n", "mean_excess", "median_excess",
    ]
    missing = [c for c in required_bucket if c not in frozen_buckets.columns]
    if missing:
        raise FailClosed(f"Frozen daily buckets missing columns: {missing}")
    frozen_buckets = frozen_buckets[required_bucket].copy()
    frozen_buckets["signal_date"] = normalize_signal_date(
        frozen_buckets["signal_date"]
    )
    frozen_ret120 = frozen_buckets[
        frozen_buckets["feature"].astype(str) == "ret120"
    ].copy()

    membership, rebuilt_buckets = reconstruct_ret120(panel)
    rec = compare_rebuilt_buckets(rebuilt_buckets, frozen_ret120)

    frozen_result = get_ret120_result(scanner)
    metrics = validate_metrics(rebuilt_buckets, frozen_result)

    fast = frozen_result.get("fast_reject") or {}
    if not bool(fast.get("fast_reject_candidate")):
        raise FailClosed("Frozen ret120 fast_reject_candidate is not true.")
    pf = fast.get("positive_folds_by_horizon") or {}
    for h in EXPECTED_HORIZONS:
        if int(pf.get(str(h), -999)) != EXPECTED_REPORTED[h]["positive_folds"]:
            raise FailClosed(
                f"Frozen JSON fast-reject positive folds mismatch for {h}: "
                f"{pf.get(str(h))}"
            )

    q5 = membership[membership["bucket"] == "Q5"].copy()
    q5 = q5.merge(
        panel[
            ["signal_date", "symbol"]
            + [f"excess_{h}" for h in EXPECTED_HORIZONS]
            + [f"label_quality_reject_{h}" for h in EXPECTED_HORIZONS]
        ],
        on=["signal_date", "symbol"],
        how="left",
        validate="one_to_one",
    )
    complete_s120 = (
        q5["excess_120"].notna()
        & ~q5["label_quality_reject_120"].fillna(False).astype(bool)
    )

    local_source = inspect_local_source(root)

    return {
        "audit_id": AUDIT_ID,
        "status": PASS_STATUS,
        "root": str(root),
        "frozen_artifacts": {
            "panel": str(panel_path),
            "panel_sha256": actual_panel_sha,
            "daily_buckets": str(bucket_path),
            "daily_buckets_sha256": actual_bucket_sha,
            "scanner_json": str(json_path),
            "replication_json": (
                str(replication_path) if replication_path.exists() else None
            ),
        },
        "panel": {
            "rows": int(len(panel)),
            "symbols": int(panel["symbol"].nunique()),
            "signal_dates": int(panel["signal_date"].nunique()),
            "duplicate_symbol_date_rows": dup_symbol_date,
        },
        "lineage": {
            "algorithm": "rank(method='first') -> pd.qcut(5)",
            "bucket_assignment_basis":
                "full signal-date panel before horizon-label completeness",
            "q5_symbol_date_rows": int(len(q5)),
            "q5_signal_dates": int(q5["signal_date"].nunique()),
            "q5_complete_s120_rows": int(complete_s120.sum()),
            "q5_incomplete_s120_rows": int((~complete_s120).sum()),
            "bucket_reconciliation": rec,
        },
        "frozen_metric_reproduction": metrics,
        "price_lineage": {
            "feature_price_representation": "QMT back_ratio",
            "signal_time": "T CLOSE",
            "entry_semantics": "S1/T+1 exact OPEN",
            "exit_semantics": "S120 exact CLOSE",
            "entry_exit_dates": (
                "reconstructable from frozen market-session sequence "
                "(signal index i -> entry i+1, exit i+120)"
            ),
            "exact_historical_price_levels_in_panel": False,
            "status": "RECONSTRUCTABLE_SEMANTICS_NOT_FROZEN_LEVELS",
            "note": (
                "The v0.2.2 panel freezes ret120 and future excess labels, "
                "not entry_open/exit_close levels. P1 must therefore freeze "
                "an explicit price-source/economic-equivalence policy rather "
                "than pretending the historical price levels were persisted."
            ),
        },
        "local_source_reference": local_source,
        "decision": (
            "Exact v0.2.2 CSI500 ret120 Q5 symbol-date lineage is recoverable "
            "from the frozen panel using the frozen algorithm and is proven "
            "by exact reconciliation to frozen daily buckets and scanner JSON."
        ),
    }


def print_summary(audit: dict[str, Any]) -> None:
    print("=" * 108)
    print("RET120 FROZEN SIGNAL LINEAGE DIAGNOSTIC V0.1")
    print("=" * 108)
    p = audit["panel"]
    l = audit["lineage"]
    r = l["bucket_reconciliation"]

    print(f"Panel rows/symbols/dates : {p['rows']} / {p['symbols']} / {p['signal_dates']}")
    print(f"Duplicate symbol-date    : {p['duplicate_symbol_date_rows']}")
    print(f"Q5 symbol-date rows       : {l['q5_symbol_date_rows']}")
    print(f"Q5 signal dates           : {l['q5_signal_dates']}")
    print(
        "Q5 S120 complete/incomp : "
        f"{l['q5_complete_s120_rows']} / {l['q5_incomplete_s120_rows']}"
    )
    print(
        "Bucket reconcile        : "
        f"keys={r['missing_or_extra_key_rows']} "
        f"n={r['n_mismatch_rows']} "
        f"mean_max={r['max_mean_abs_diff']:.3e} "
        f"median_max={r['max_median_abs_diff']:.3e}"
    )
    print()

    print("Frozen metric reproduction")
    for h in EXPECTED_HORIZONS:
        m = audit["frozen_metric_reproduction"][str(h)]
        print(
            f"  H{h:<3} Q5-Q1={m['q5_minus_q1']:+.8%} "
            f"mono={m['monotonicity']:+.6f} "
            f"folds+={m['positive_folds']}/3"
        )
    print()

    print("Price lineage")
    pr = audit["price_lineage"]
    print(f"  representation : {pr['feature_price_representation']}")
    print(f"  entry / exit   : {pr['entry_semantics']} / {pr['exit_semantics']}")
    print(f"  price status   : {pr['status']}")
    print()

    print("Current-tree source reference (informational)")
    for rel, row in audit["local_source_reference"].items():
        print(f"  {rel}: {row['status']}")
    print()
    print(f"FINAL_STATUS = {audit['status']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--root",
        default=None,
        help="PositionX root. Default: parent of this script's scripts/ directory.",
    )
    ap.add_argument(
        "--json-out",
        default=None,
        help="Optional audit JSON path. Omit for a strictly read-only diagnostic.",
    )
    args = ap.parse_args()

    root = (
        Path(args.root).resolve()
        if args.root
        else Path(__file__).resolve().parents[1]
    )

    try:
        audit = build_audit(root)
        print_summary(audit)
        if args.json_out:
            out = Path(args.json_out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps(audit, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"AUDIT_JSON = {out}")
        return 0
    except Exception as e:
        print("=" * 108, file=sys.stderr)
        print("RET120 FROZEN SIGNAL LINEAGE DIAGNOSTIC V0.1", file=sys.stderr)
        print("=" * 108, file=sys.stderr)
        print(f"ERROR = {type(e).__name__}: {e}", file=sys.stderr)
        print(f"FINAL_STATUS = {FAIL_STATUS}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
