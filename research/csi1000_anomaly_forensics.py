from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

import pandas as pd


def norm_date(v: Any) -> str:
    s = "".join(ch for ch in str(v) if ch.isdigit())
    if len(s) < 8:
        raise ValueError(f"invalid date {v!r}")
    return s[:8]


def fingerprint(symbols: Iterable[str]) -> str:
    payload = "\n".join(sorted(map(str, symbols))).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def load_daily(path: Path) -> pd.DataFrame:
    x = pd.read_csv(path, dtype=str)
    need = {"trade_date", "symbol"}
    missing = need - set(x.columns)
    if missing:
        raise ValueError(f"daily membership missing columns: {sorted(missing)}")
    x["trade_date"] = x["trade_date"].map(norm_date)
    x["symbol"] = x["symbol"].astype(str)
    if x.duplicated(["trade_date", "symbol"]).any():
        raise ValueError("duplicate trade_date/symbol rows")
    return x.sort_values(["trade_date", "symbol"]).reset_index(drop=True)


def group_sets(daily: pd.DataFrame) -> Tuple[List[str], Dict[str, set[str]]]:
    dates = sorted(daily["trade_date"].unique().tolist())
    sets = {
        d: set(g["symbol"].astype(str))
        for d, g in daily.groupby("trade_date", sort=True)
    }
    return dates, sets


def contiguous_bad_blocks(
    dates: Sequence[str],
    sets: Mapping[str, set[str]],
    expected_size: int,
) -> List[List[str]]:
    bad_idx = [i for i, d in enumerate(dates) if len(sets[d]) != expected_size]
    if not bad_idx:
        return []
    blocks: List[List[str]] = []
    cur = [bad_idx[0]]
    for i in bad_idx[1:]:
        if i == cur[-1] + 1:
            cur.append(i)
        else:
            blocks.append([dates[j] for j in cur])
            cur = [i]
    blocks.append([dates[j] for j in cur])
    return blocks


def _diff(a: set[str], b: set[str]) -> List[str]:
    return sorted(a - b)


def analyze_block(
    dates: Sequence[str],
    sets: Mapping[str, set[str]],
    block: Sequence[str],
    expected_size: int,
) -> Dict[str, Any]:
    pos = {d: i for i, d in enumerate(dates)}
    first, last = block[0], block[-1]
    i0, i1 = pos[first], pos[last]
    prev_d = dates[i0 - 1] if i0 > 0 else None
    next_d = dates[i1 + 1] if i1 + 1 < len(dates) else None

    block_fps = {d: fingerprint(sets[d]) for d in block}
    unique_fps = sorted(set(block_fps.values()))
    first_set = sets[first]
    last_set = sets[last]
    prev_set = sets[prev_d] if prev_d else set()
    next_set = sets[next_d] if next_d else set()

    out = {
        "first_bad_date": first,
        "last_bad_date": last,
        "bad_trading_days": len(block),
        "bad_sizes": {d: len(sets[d]) for d in block},
        "unique_bad_set_fingerprints": unique_fps,
        "bad_set_constant_across_block": len(unique_fps) == 1,
        "prev_clean_date": prev_d,
        "prev_clean_size": len(prev_set) if prev_d else None,
        "next_clean_date": next_d,
        "next_clean_size": len(next_set) if next_d else None,
        "first_bad_vs_prev": {
            "added": _diff(first_set, prev_set),
            "removed": _diff(prev_set, first_set),
        },
        "next_vs_last_bad": {
            "added": _diff(next_set, last_set),
            "removed": _diff(last_set, next_set),
        },
        "first_bad_vs_next": {
            "only_in_bad": _diff(first_set, next_set),
            "only_in_next": _diff(next_set, first_set),
        },
    }

    # A narrow, evidence-based classification. It does NOT repair.
    if (
        prev_d and next_d
        and len(prev_set) == expected_size
        and len(next_set) == expected_size
        and all(len(sets[d]) == expected_size + 1 for d in block)
        and len(unique_fps) == 1
    ):
        out["shape_classification"] = "STABLE_PLUS_ONE_BLOCK_BETWEEN_CLEAN_1000_SETS"
    else:
        out["shape_classification"] = "COMPLEX_OR_MIXED_BLOCK"

    return out


def load_adjustments(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=["effective_date", "symbol", "action"])
    x = pd.read_csv(path, dtype=str)
    if "effective_date" in x.columns:
        x["effective_date"] = x["effective_date"].map(norm_date)
    return x


def adjustment_context(
    adjustments: pd.DataFrame,
    start_date: str,
    next_clean_date: str | None,
    window_days: int = 20,
) -> List[Dict[str, Any]]:
    if adjustments.empty or next_clean_date is None:
        return []
    # We intentionally avoid calendar arithmetic here; just include the next
    # effective date(s) at or shortly after the anomaly by lexical date range.
    lo = start_date
    hi = (
        pd.Timestamp(next_clean_date) + pd.Timedelta(days=window_days)
    ).strftime("%Y%m%d")
    x = adjustments[
        (adjustments["effective_date"] >= lo)
        & (adjustments["effective_date"] <= hi)
    ].copy()
    return x.sort_values(["effective_date", "action", "symbol"]).to_dict("records")


def raw_snapshot_context(
    raw_dir: Path,
    years: Iterable[int],
    prev_date: str | None,
    last_bad_date: str,
    next_clean_date: str | None,
) -> List[Dict[str, Any]]:
    frames = []
    for y in sorted(set(years)):
        p = raw_dir / f"iw_csi1000_{y}.parquet"
        if p.exists():
            # Avoid pandas optional parquet engines (pyarrow/fastparquet).
            # DuckDB is already part of the PositionX research environment.
            import duckdb
            pp = str(p).replace("\\", "/").replace("'", "''")
            con = duckdb.connect()
            try:
                z = con.execute(
                    f"SELECT trade_date, concept_code FROM read_parquet('{pp}')"
                ).fetchdf()
            finally:
                con.close()
            if {"trade_date", "concept_code"} <= set(z.columns):
                z = z[["trade_date", "concept_code"]].copy()
                z["trade_date"] = pd.to_datetime(z["trade_date"]).dt.strftime("%Y%m%d")
                z["concept_code"] = z["concept_code"].astype(str)
                frames.append(z)
    if not frames:
        return []

    x = pd.concat(frames, ignore_index=True)
    lo = prev_date or x["trade_date"].min()
    hi = next_clean_date or last_bad_date
    x = x[(x["trade_date"] >= lo) & (x["trade_date"] <= hi)]
    out = []
    for d, g in x.groupby("trade_date", sort=True):
        ss = set(g["concept_code"])
        out.append({
            "snapshot_date": d,
            "snapshot_size": len(ss),
            "snapshot_fingerprint": fingerprint(ss),
        })
    return out


def analyze_assets(assets_dir: Path, expected_size: int = 1000) -> Dict[str, Any]:
    daily_path = assets_dir / "csi1000_membership_daily.csv"
    adj_path = assets_dir / "csi1000_adjustments.csv"
    raw_dir = assets_dir / "raw"

    if not daily_path.exists():
        raise FileNotFoundError(daily_path)

    daily = load_daily(daily_path)
    dates, sets = group_sets(daily)
    blocks = contiguous_bad_blocks(dates, sets, expected_size)
    adjustments = load_adjustments(adj_path)

    reports = []
    for block in blocks:
        r = analyze_block(dates, sets, block, expected_size)
        years = {int(d[:4]) for d in block}
        r["adjustment_context"] = adjustment_context(
            adjustments, r["first_bad_date"], r["next_clean_date"]
        )
        r["raw_snapshot_context"] = raw_snapshot_context(
            raw_dir,
            years,
            r["prev_clean_date"],
            r["last_bad_date"],
            r["next_clean_date"],
        )

        # Link membership diffs to scheduled actions, without asserting cause.
        scheduled_in = {
            a["symbol"] for a in r["adjustment_context"]
            if str(a.get("action", "")).upper() == "IN"
        }
        scheduled_out = {
            a["symbol"] for a in r["adjustment_context"]
            if str(a.get("action", "")).upper() == "OUT"
        }
        added = set(r["first_bad_vs_prev"]["added"])
        removed = set(r["first_bad_vs_prev"]["removed"])
        only_bad = set(r["first_bad_vs_next"]["only_in_bad"])
        only_next = set(r["first_bad_vs_next"]["only_in_next"])
        r["scheduled_overlap"] = {
            "first_bad_added_and_scheduled_in": sorted(added & scheduled_in),
            "first_bad_removed_and_scheduled_out": sorted(removed & scheduled_out),
            "only_in_bad_and_scheduled_out": sorted(only_bad & scheduled_out),
            "only_in_next_and_scheduled_in": sorted(only_next & scheduled_in),
        }

        reports.append(r)

    return {
        "status": "OK",
        "purpose": "CSI1000 anomaly forensics only; no repair or mutation.",
        "daily_rows": int(len(daily)),
        "trading_days": int(len(dates)),
        "expected_size": expected_size,
        "bad_blocks": len(reports),
        "bad_dates": sum(r["bad_trading_days"] for r in reports),
        "blocks": reports,
        "repair_authorized": False,
    }
