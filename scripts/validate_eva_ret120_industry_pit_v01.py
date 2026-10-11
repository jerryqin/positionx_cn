from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
EDGE_DIR = ROOT / "data" / "research" / "edge_discovery"
TEMPORAL_DIR = EDGE_DIR / "ret120_temporal_v01"
OUT_DIR = EDGE_DIR / "ret120_eva_industry_validation_v01"

REQUIRED = [
    "symbol",
    "effective_from",
    "effective_to_exclusive",
    "industry_code",
    "industry_name",
    "standard",
    "level",
    "source_id",
    "source_url",
    "provenance_note",
]

UNIVERSES = ("csi300", "csi500")
CUTOFF = "20260924"


def normalize_date(v, allow_blank: bool = False):
    if pd.isna(v) or str(v).strip() == "":
        return None if allow_blank else "INVALID"

    s = "".join(ch for ch in str(v) if ch.isdigit())[:8]
    if len(s) != 8:
        return "INVALID"

    try:
        datetime.strptime(s, "%Y%m%d")
    except ValueError:
        return "INVALID"

    return s


def load_folds():
    p = TEMPORAL_DIR / "audit.json"
    payload = json.loads(p.read_text(encoding="utf-8"))
    raw = payload["folds"]
    return {
        k: (str(raw[k]["start"]), str(raw[k]["end"]))
        for k in ("F1", "F2", "F3")
    }


def assign_fold(d: str, folds):
    for name in ("F1", "F2", "F3"):
        a, b = folds[name]
        if a <= d <= b:
            return name
    return None


def validate_intervals(df: pd.DataFrame):
    issues = []

    for symbol, x in df.groupby("symbol", sort=False):
        x = x.sort_values("effective_from")

        prev_end = None
        for _, r in x.iterrows():
            start = r["effective_from"]
            end = r["effective_to_exclusive"]

            if start == "INVALID":
                issues.append((symbol, "INVALID_START_DATE", str(r.to_dict())))
                continue

            if end == "INVALID":
                issues.append((symbol, "INVALID_END_DATE", str(r.to_dict())))
                continue

            if end is not None and end <= start:
                issues.append((symbol, "NONPOSITIVE_INTERVAL", str(r.to_dict())))

            if prev_end is not None and start < prev_end:
                issues.append((symbol, "OVERLAPPING_INTERVAL", str(r.to_dict())))

            if end is not None:
                prev_end = end
            else:
                prev_end = "99999999"

    return issues


def load_panel(con):
    parts = []

    for universe in UNIVERSES:
        p = EDGE_DIR / f"trend_persistence_scan_{universe}_panel_v0221.parquet"
        q = p.as_posix().replace("'", "''")
        x = con.execute(
            f"""
            SELECT signal_date, symbol
            FROM read_parquet('{q}')
            WHERE ret120 IS NOT NULL
              AND excess_120 IS NOT NULL
              AND COALESCE(label_quality_reject_120, FALSE) = FALSE
              AND signal_date <= '{CUTOFF}'
            """
        ).fetchdf()
        x.insert(0, "universe", universe)
        parts.append(x)

    panel = pd.concat(parts, ignore_index=True)
    panel["signal_date"] = panel["signal_date"].astype(str)
    panel["symbol"] = panel["symbol"].astype(str)
    return panel


def coverage(panel, intervals, folds):
    by_symbol = {
        s: g.sort_values("effective_from")
        for s, g in intervals.groupby("symbol", sort=False)
    }

    covered = np.zeros(len(panel), dtype=bool)
    ambiguous = np.zeros(len(panel), dtype=bool)

    for symbol, idxs in panel.groupby("symbol").groups.items():
        iv = by_symbol.get(symbol)
        if iv is None or iv.empty:
            continue

        starts = iv["effective_from"].to_numpy(dtype=str)
        ends = iv["effective_to_exclusive"].tolist()
        dates = panel.loc[idxs, "signal_date"].to_numpy(dtype=str)

        for j, (row_idx, d) in enumerate(zip(idxs, dates)):
            matches = []
            for k, start in enumerate(starts):
                end = ends[k]
                if start <= d and (end is None or d < end):
                    matches.append(k)
            if len(matches) == 1:
                covered[row_idx] = True
            elif len(matches) > 1:
                ambiguous[row_idx] = True

    x = panel.copy()
    x["covered"] = covered
    x["ambiguous"] = ambiguous
    x["fold"] = x["signal_date"].map(lambda d: assign_fold(d, folds))

    rows = []
    for universe in UNIVERSES:
        for seg in ("ALL", "F1", "F2", "F3"):
            if seg == "ALL":
                y = x[x["universe"] == universe]
            else:
                y = x[(x["universe"] == universe) & (x["fold"] == seg)]

            rows.append({
                "universe": universe,
                "segment": seg,
                "rows": int(len(y)),
                "covered_rows": int(y["covered"].sum()),
                "ambiguous_rows": int(y["ambiguous"].sum()),
                "coverage": float(y["covered"].mean()) if len(y) else np.nan,
            })

    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path")
    args = ap.parse_args()

    path = Path(args.csv_path)
    if not path.exists():
        raise FileNotFoundError(path)

    df = pd.read_csv(path, dtype=str, keep_default_na=False)

    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise RuntimeError(f"Missing required columns: {missing}")

    df = df[REQUIRED].copy()
    df["symbol"] = df["symbol"].str.strip()
    df["effective_from"] = df["effective_from"].map(
        lambda x: normalize_date(x, allow_blank=False)
    )
    df["effective_to_exclusive"] = df["effective_to_exclusive"].map(
        lambda x: normalize_date(x, allow_blank=True)
    )

    standards = sorted(
        set(
            (df["standard"].str.strip() + "|" + df["level"].str.strip())
            .replace("|", np.nan)
            .dropna()
            .tolist()
        )
    )

    issues = validate_intervals(df)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    pd.DataFrame(
        issues,
        columns=["symbol", "issue", "row"],
    ).to_csv(
        OUT_DIR / "interval_issues.csv",
        index=False,
        encoding="utf-8-sig",
    )

    con = duckdb.connect()
    try:
        panel = load_panel(con)
    finally:
        con.close()

    folds = load_folds()

    valid = df[
        (df["effective_from"] != "INVALID")
        & (df["effective_to_exclusive"] != "INVALID")
    ].copy()

    cov = coverage(panel, valid, folds)
    cov.to_csv(
        OUT_DIR / "coverage_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )

    source_blank = (
        (df["source_id"].str.strip() == "")
        & (df["source_url"].str.strip() == "")
    )

    summary = {
        "validation_id": "RET120_EVA_INDUSTRY_PIT_VALIDATION_V01",
        "input": str(path),
        "rows": int(len(df)),
        "symbols": int(df["symbol"].nunique()),
        "standard_level_pairs": standards,
        "interval_issue_count": int(len(issues)),
        "rows_without_source_id_or_url": int(source_blank.sum()),
        "coverage": cov.to_dict(orient="records"),
        "usable_for_industry_audit": (
            len(issues) == 0
            and len(standards) == 1
            and int(source_blank.sum()) == 0
            and bool((cov["ambiguous_rows"] == 0).all())
            and bool((cov["coverage"] >= 0.95).all())
        ),
    }

    (OUT_DIR / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("=" * 100)
    print("RET120 EVA INDUSTRY PIT VALIDATION V0.1")
    print("=" * 100)
    print("Rows:", len(df))
    print("Symbols:", df["symbol"].nunique())
    print("Standard/level:", standards)
    print("Interval issues:", len(issues))
    print("Rows missing source_id AND source_url:", int(source_blank.sum()))
    print()
    for _, r in cov.iterrows():
        print(
            f"{r['universe'].upper()} {r['segment']}: "
            f"coverage={r['coverage']:.2%} "
            f"ambiguous={int(r['ambiguous_rows'])}"
        )
    print()
    print("Usable for industry audit:", summary["usable_for_industry_audit"])
    print("Output:", OUT_DIR)


if __name__ == "__main__":
    main()
