from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
EDGE_DIR = ROOT / "data" / "research" / "edge_discovery"
ROBUST_DIR = EDGE_DIR / "ret120_robustness_v01"
OUTPUT_DIR = EDGE_DIR / "ret120_temporal_v01"

HORIZONS = (20, 60, 120)
UNIVERSES = ("csi300", "csi500")

DATE_RE = re.compile(r"^(20\d{2})[-/]?(\d{2})[-/]?(\d{2})$")
DATE_TOKEN = r"(20\d{2}[-/]?\d{2}[-/]?\d{2})"


def normalize_date(value: Any) -> str | None:
    """
    Accept only a real calendar date.

    Previous versions only checked the YYYYMMDD shape, so unrelated numeric
    text such as 20235691 could be mistaken for a fold boundary.
    """
    if value is None:
        return None

    s = str(value).strip()
    m = DATE_RE.match(s)
    if not m:
        return None

    y, mo, d = m.groups()
    compact = f"{y}{mo}{d}"

    try:
        datetime.strptime(compact, "%Y%m%d")
    except ValueError:
        return None

    return compact


def _extract_start_end(d: dict[str, Any]) -> tuple[str, str] | None:
    start_keys = (
        "start", "start_date", "from", "begin", "begin_date",
        "signal_start", "date_start", "fold_start",
        "first_date", "first_signal_date", "period_start",
        "sample_start", "window_start",
    )
    end_keys = (
        "end", "end_date", "to", "stop", "stop_date",
        "signal_end", "date_end", "fold_end",
        "last_date", "last_signal_date", "period_end",
        "sample_end", "window_end",
    )

    start = None
    end = None

    for k in start_keys:
        if k in d:
            start = normalize_date(d[k])
            if start:
                break

    for k in end_keys:
        if k in d:
            end = normalize_date(d[k])
            if end:
                break

    if start and end and start <= end:
        return start, end
    return None


def _resolve_fold_candidates(
    found: dict[str, list[tuple[str, str]]]
) -> dict[str, tuple[str, str]]:
    resolved: dict[str, tuple[str, str]] = {}

    for name, pairs in found.items():
        unique = sorted(set(pairs))
        if len(unique) == 1:
            resolved[name] = unique[0]
        elif len(unique) > 1:
            raise RuntimeError(
                f"Ambiguous frozen fold definition for {name}: {unique}"
            )

    if set(resolved) != {"F1", "F2", "F3"}:
        return {}

    ordered = [resolved["F1"], resolved["F2"], resolved["F3"]]
    if not (
        ordered[0][0] <= ordered[0][1] <
        ordered[1][0] <= ordered[1][1] <
        ordered[2][0] <= ordered[2][1]
    ):
        raise RuntimeError(f"Frozen folds overlap or are out of order: {resolved}")

    return resolved


def extract_named_folds(obj: Any) -> dict[str, tuple[str, str]]:
    found: dict[str, list[tuple[str, str]]] = {"F1": [], "F2": [], "F3": []}

    def add(name: str, pair: tuple[str, str] | None):
        if name in found and pair is not None and pair not in found[name]:
            found[name].append(pair)

    def walk(x: Any):
        if isinstance(x, dict):
            for name in ("F1", "F2", "F3"):
                if name in x and isinstance(x[name], dict):
                    add(name, _extract_start_end(x[name]))

            fold_name = None
            for key in (
                "fold", "fold_name", "name", "id", "label",
                "period", "slice",
            ):
                v = x.get(key)
                if isinstance(v, str) and v.upper() in found:
                    fold_name = v.upper()
                    break

            if fold_name:
                add(fold_name, _extract_start_end(x))

            for v in x.values():
                walk(v)

        elif isinstance(x, list):
            for item in x:
                walk(item)

    walk(obj)
    return _resolve_fold_candidates(found)


def extract_named_folds_from_text(text: str) -> dict[str, tuple[str, str]]:
    found: dict[str, list[tuple[str, str]]] = {"F1": [], "F2": [], "F3": []}

    line_patterns = [
        re.compile(
            rf"""(?ix)
            ['"]?(F[123])['"]?
            \s*(?:[:=,]\s*)+
            [\[\(\{{]?\s*['"]?{DATE_TOKEN}['"]?
            \s*(?:,|->|=>|to|\.\.|~|-)\s*
            ['"]?{DATE_TOKEN}['"]?
            """
        ),
        re.compile(
            rf"""(?ix)
            [\(\[]\s*['"]?(F[123])['"]?\s*,\s*
            ['"]?{DATE_TOKEN}['"]?\s*,\s*
            ['"]?{DATE_TOKEN}['"]?\s*[\)\]]
            """
        ),
        re.compile(
            rf"""(?ix)
            ['"]?(F[123])['"]?
            .*?
            (?:start|from|begin|first)[^0-9]{{0,30}}['"]?{DATE_TOKEN}['"]?
            .*?
            (?:end|to|stop|last)[^0-9]{{0,30}}['"]?{DATE_TOKEN}['"]?
            """
        ),
    ]

    for line in text.splitlines():
        if not any(k in line for k in ("F1", "F2", "F3")):
            continue
        for pat in line_patterns:
            for m in pat.finditer(line):
                name = m.group(1).upper()
                d1 = normalize_date(m.group(2))
                d2 = normalize_date(m.group(3))
                if d1 and d2 and d1 <= d2:
                    pair = (d1, d2)
                    if pair not in found[name]:
                        found[name].append(pair)

    compact = re.sub(r"\s+", " ", text)
    block_pat = re.compile(
        rf"""(?ix)
        ['"]?(F[123])['"]?
        .{{0,120}}?
        ['"]?{DATE_TOKEN}['"]?
        .{{0,120}}?
        ['"]?{DATE_TOKEN}['"]?
        """
    )
    for m in block_pat.finditer(compact):
        name = m.group(1).upper()
        d1 = normalize_date(m.group(2))
        d2 = normalize_date(m.group(3))
        if d1 and d2 and d1 <= d2:
            pair = (d1, d2)
            if pair not in found[name]:
                found[name].append(pair)

    return _resolve_fold_candidates(found)


def _is_trend_provenance_file(path: Path) -> bool:
    s = str(path).lower().replace("\\", "/")
    name = path.name.lower()

    banned = (
        "deep_oversold",
        "shared_equity",
        "breakout",
        "capacity",
        "ret120_temporal",
        "ret120_robustness",
    )
    if any(x in s for x in banned):
        return False

    trend_markers = (
        "trend_persistence",
        "trend_edge_scan",
        "edge_discovery_trend",
    )
    return any(x in s or x in name for x in trend_markers)


def candidate_provenance_files() -> list[Path]:
    candidates: set[Path] = set()

    search_roots = (
        ROOT / "scripts",
        ROOT / "positionx",
        ROOT / "config",
        EDGE_DIR,
    )
    patterns = ("*.py", "*.json", "*.yaml", "*.yml", "*.toml", "*.txt", "*.md")

    for base in search_roots:
        if not base.exists():
            continue
        for pat in patterns:
            for p in base.rglob(pat):
                if _is_trend_provenance_file(p):
                    try:
                        if p.is_file() and p.stat().st_size <= 5_000_000:
                            candidates.add(p)
                    except OSError:
                        pass

    return sorted(candidates)


def discover_frozen_folds() -> tuple[
    dict[str, tuple[str, str]], Path, str, list[str]
]:
    successful: list[tuple[dict[str, tuple[str, str]], Path, str]] = []
    candidates = candidate_provenance_files()

    for path in candidates:
        if path.suffix.lower() == ".json":
            try:
                obj = json.loads(path.read_text(encoding="utf-8"))
                folds = extract_named_folds(obj)
                if folds:
                    successful.append((folds, path, "structured_json"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        folds = extract_named_folds_from_text(text)
        if folds:
            successful.append((folds, path, "explicit_text"))

    receipt = [str(p) for p in candidates]

    if not successful:
        detail = "\n".join(receipt) or "(no Trend Edge Discovery provenance files found)"
        raise RuntimeError(
            "Could not resolve explicit frozen F1/F2/F3 definitions from "
            "Trend Edge Discovery provenance only.\n"
            "Failing closed rather than inventing or re-splitting fold boundaries.\n"
            "Searched:\n"
            f"{detail}"
        )

    unique: dict[
        tuple[tuple[str, str, str], ...],
        list[tuple[Path, str]]
    ] = {}

    for folds, path, method in successful:
        key = tuple((k, *folds[k]) for k in ("F1", "F2", "F3"))
        unique.setdefault(key, []).append((path, method))

    if len(unique) != 1:
        detail = []
        for key, sources in unique.items():
            detail.append(f"{key} <- {[(str(p), m) for p, m in sources]}")
        raise RuntimeError(
            "Conflicting explicit frozen fold definitions found INSIDE "
            "Trend Edge Discovery provenance:\n"
            + "\n".join(detail)
        )

    key, sources = next(iter(unique.items()))
    folds = {name: (start, end) for name, start, end in key}

    sources_sorted = sorted(
        sources,
        key=lambda pm: (
            "v022" not in pm[0].name.lower(),
            "replication" not in pm[0].name.lower(),
            "run_edge_discovery" not in pm[0].name.lower(),
            str(pm[0]),
        ),
    )
    source, method = sources_sorted[0]
    return folds, source, method, receipt


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
    return float(arr[k:n-k].mean())


def mean_without_top(values, proportion: float = 0.05) -> float:
    arr = np.sort(finite_array(values))
    n = len(arr)
    if n == 0:
        return math.nan
    k = int(math.floor(n * proportion))
    if k == 0:
        return float(arr.mean())
    remain = arr[:n-k]
    return float(remain.mean()) if len(remain) else math.nan


def stats(values) -> dict[str, float | int]:
    arr = finite_array(values)
    if len(arr) == 0:
        return {
            "n_dates": 0,
            "mean": math.nan,
            "median": math.nan,
            "trim5_mean": math.nan,
            "mean_ex_top5": math.nan,
            "positive_date_ratio": math.nan,
        }
    return {
        "n_dates": int(len(arr)),
        "mean": float(arr.mean()),
        "median": float(np.median(arr)),
        "trim5_mean": trimmed_mean(arr, 0.05),
        "mean_ex_top5": mean_without_top(arr, 0.05),
        "positive_date_ratio": float((arr > 0).mean()),
    }


def assign_fold(date_str: str, folds: dict[str, tuple[str, str]]) -> str | None:
    d = normalize_date(date_str)
    if d is None:
        return None
    for name in ("F1", "F2", "F3"):
        start, end = folds[name]
        if start <= d <= end:
            return name
    return None


def pct(x: float) -> str:
    if x is None or not np.isfinite(x):
        return "NA"
    return f"{x * 100:+.3f}%"


def main():
    source_csv = ROBUST_DIR / "daily_metrics.csv"
    if not source_csv.exists():
        raise FileNotFoundError(
            f"Missing v0.1 robustness output: {source_csv}\n"
            "Run scripts/audit_ret120_robustness_v01.py first."
        )

    folds, fold_source, fold_method, search_receipt = discover_frozen_folds()

    print("Frozen folds resolved from:", fold_source)
    print("Resolution method:", fold_method)
    for name in ("F1", "F2", "F3"):
        print(f"  {name}: {folds[name][0]} -> {folds[name][1]}")

    df = pd.read_csv(source_csv, dtype={"signal_date": str})
    required = {
        "universe", "horizon", "signal_date",
        "q5_q1_spread", "q5_minus_universe", "q1_minus_universe",
    }
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"Missing required v0.1 columns: {sorted(missing)}")

    df["signal_date"] = (
        df["signal_date"].astype(str).str.replace("-", "", regex=False).str[:8]
    )
    df["fold"] = df["signal_date"].map(lambda x: assign_fold(x, folds))
    outside = df[df["fold"].isna()]

    rows = []
    for universe in UNIVERSES:
        for horizon in HORIZONS:
            for fold in ("F1", "F2", "F3"):
                x = df[
                    (df["universe"] == universe)
                    & (df["horizon"] == horizon)
                    & (df["fold"] == fold)
                ]
                spread = stats(x["q5_q1_spread"])
                q5 = stats(x["q5_minus_universe"])
                q1 = stats(x["q1_minus_universe"])

                rows.append({
                    "universe": universe,
                    "horizon": horizon,
                    "fold": fold,
                    "n_dates": spread["n_dates"],
                    "spread_mean": spread["mean"],
                    "spread_median": spread["median"],
                    "spread_trim5": spread["trim5_mean"],
                    "spread_ex_top5": spread["mean_ex_top5"],
                    "positive_date_ratio": spread["positive_date_ratio"],
                    "q5_minus_universe_mean": q5["mean"],
                    "q1_minus_universe_mean": q1["mean"],
                })

    out = pd.DataFrame(rows)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUTPUT_DIR / "fold_summary.csv", index=False)
    outside.to_csv(OUTPUT_DIR / "outside_frozen_folds.csv", index=False)
    (OUTPUT_DIR / "provenance_search_receipt.txt").write_text(
        "\n".join(search_receipt),
        encoding="utf-8",
    )

    payload = {
        "audit_id": "RET120_TEMPORAL_V01C",
        "fold_source": str(fold_source),
        "fold_resolution_method": fold_method,
        "folds": {k: {"start": v[0], "end": v[1]} for k, v in folds.items()},
        "outside_fold_rows": int(len(outside)),
        "provenance_search_receipt": search_receipt,
        "results": out.to_dict(orient="records"),
        "notes": {
            "re_split_dates": False,
            "source": str(source_csv),
            "strategy_gate": False,
            "cost_applied": False,
            "scope": "Trend Edge Discovery provenance only",
            "calendar_date_validation": True,
        },
    }
    (OUTPUT_DIR / "audit.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print()
    print("=" * 100)
    print("RET120 TEMPORAL PERSISTENCE V0.1C")
    print("=" * 100)

    for universe in UNIVERSES:
        for horizon in HORIZONS:
            print()
            print(f"{universe.upper()} | ret120 | S{horizon}")
            print("-" * 100)
            part = out[
                (out["universe"] == universe)
                & (out["horizon"] == horizon)
            ]
            for _, r in part.iterrows():
                pos = r["positive_date_ratio"]
                pos_s = "NA" if not np.isfinite(pos) else f"{pos:.1%}"
                print(
                    f"{r['fold']}: "
                    f"mean={pct(r['spread_mean'])} "
                    f"median={pct(r['spread_median'])} "
                    f"trim5={pct(r['spread_trim5'])} "
                    f"exTop5={pct(r['spread_ex_top5'])} "
                    f"posDays={pos_s} "
                    f"Q5-U={pct(r['q5_minus_universe_mean'])} "
                    f"Q1-U={pct(r['q1_minus_universe_mean'])} "
                    f"n={int(r['n_dates'])}"
                )

    print()
    print("Outside frozen fold rows:", len(outside))
    print("Output:", OUTPUT_DIR)
    print("Fold provenance restricted to Trend Edge Discovery only.")
    print("Calendar-date validation enabled.")
    print("No fold boundaries were re-created or optimized.")
    print("No source parquet was modified.")
    print("No transaction cost was applied.")


if __name__ == "__main__":
    main()
