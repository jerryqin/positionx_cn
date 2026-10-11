from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import pandas as pd


OLD_CODE = "300114.SZ"
NEW_CODE = "302132.SZ"
CODE_CHANGE_DATE = "20250217"


def norm_date(v: Any) -> str:
    s = "".join(ch for ch in str(v) if ch.isdigit())
    if len(s) < 8:
        raise ValueError(f"invalid date {v!r}")
    return s[:8]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_daily(path: Path) -> pd.DataFrame:
    x = pd.read_csv(path, dtype=str)
    required = {"trade_date", "symbol"}
    missing = required - set(x.columns)
    if missing:
        raise ValueError(f"missing daily columns: {sorted(missing)}")
    x = x[["trade_date", "symbol"]].copy()
    x["trade_date"] = x["trade_date"].map(norm_date)
    x["symbol"] = x["symbol"].astype(str)
    if x.duplicated(["trade_date", "symbol"]).any():
        raise ValueError("source has duplicate date/symbol rows")
    return x.sort_values(["trade_date", "symbol"]).reset_index(drop=True)


def daily_sets(df: pd.DataFrame) -> Dict[str, set[str]]:
    return {
        d: set(g["symbol"].astype(str))
        for d, g in df.groupby("trade_date", sort=True)
    }


def canonicalize_code_alias(
    daily: pd.DataFrame,
    expected_size: int = 1000,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    Canonicalize the single listed entity that changed Shenzhen code:
      300114.SZ -> 302132.SZ effective 2025-02-17.

    Before the effective date, the canonical code is 300114.SZ.
    On/after the effective date, the canonical code is 302132.SZ.

    If both aliases coexist on the same date, the historically invalid alias is
    dropped. If only the wrong alias is present, it is replaced.
    """
    x = daily.copy()
    src_sets = daily_sets(x)

    actions: List[Dict[str, Any]] = []
    out_rows = []

    for d, g in x.groupby("trade_date", sort=True):
        syms = set(g["symbol"].astype(str))
        before = set(syms)

        if d < CODE_CHANGE_DATE:
            if NEW_CODE in syms:
                if OLD_CODE in syms:
                    syms.remove(NEW_CODE)
                    action = "DROP_FUTURE_ALIAS_DUPLICATE"
                else:
                    syms.remove(NEW_CODE)
                    syms.add(OLD_CODE)
                    action = "REPLACE_FUTURE_ALIAS_WITH_HISTORICAL_CODE"
                actions.append({
                    "trade_date": d,
                    "action": action,
                    "removed": NEW_CODE,
                    "added": "" if OLD_CODE in before else OLD_CODE,
                    "source_size": len(before),
                    "repaired_size": len(syms),
                })
        else:
            if OLD_CODE in syms:
                if NEW_CODE in syms:
                    syms.remove(OLD_CODE)
                    action = "DROP_OLD_ALIAS_DUPLICATE"
                else:
                    syms.remove(OLD_CODE)
                    syms.add(NEW_CODE)
                    action = "REPLACE_OLD_ALIAS_WITH_CURRENT_CODE"
                actions.append({
                    "trade_date": d,
                    "action": action,
                    "removed": OLD_CODE,
                    "added": "" if NEW_CODE in before else NEW_CODE,
                    "source_size": len(before),
                    "repaired_size": len(syms),
                })

        for s in sorted(syms):
            out_rows.append({"trade_date": d, "symbol": s})

    out = pd.DataFrame(out_rows)
    if out.duplicated(["trade_date", "symbol"]).any():
        raise RuntimeError("repair created duplicate date/symbol rows")

    counts_before = daily.groupby("trade_date")["symbol"].nunique()
    counts_after = out.groupby("trade_date")["symbol"].nunique()
    bad_before = counts_before[counts_before != expected_size]
    bad_after = counts_after[counts_after != expected_size]

    # Fail closed: this repair is only allowed when it fully resolves the size
    # anomaly without introducing new size failures.
    if len(bad_after):
        raise RuntimeError(
            "REPAIR_DID_NOT_RESTORE_EXPECTED_SIZE "
            + json.dumps(
                {str(k): int(v) for k, v in bad_after.head(30).items()},
                ensure_ascii=False,
            )
        )

    # Strong forensic invariant for this delivery:
    # every original 1001-member anomaly must contain BOTH aliases, proving a
    # duplicated logical security rather than an arbitrary extra constituent.
    bad_alias_checks = []
    for d, n in bad_before.items():
        ss = src_sets[str(d)]
        bad_alias_checks.append({
            "trade_date": str(d),
            "source_size": int(n),
            "old_present": OLD_CODE in ss,
            "new_present": NEW_CODE in ss,
        })
    if bad_alias_checks and not all(
        r["source_size"] == expected_size + 1
        and r["old_present"]
        and r["new_present"]
        for r in bad_alias_checks
    ):
        raise RuntimeError(
            "BAD_SIZE_DATES_NOT_EXPLAINED_BY_ALIAS_DUPLICATION "
            + json.dumps(bad_alias_checks, ensure_ascii=False)
        )

    return out.sort_values(["trade_date", "symbol"]).reset_index(drop=True), {
        "old_code": OLD_CODE,
        "new_code": NEW_CODE,
        "effective_date": CODE_CHANGE_DATE,
        "source_rows": int(len(daily)),
        "repaired_rows": int(len(out)),
        "source_bad_size_dates": int(len(bad_before)),
        "repaired_bad_size_dates": int(len(bad_after)),
        "source_size_min": int(counts_before.min()),
        "source_size_max": int(counts_before.max()),
        "repaired_size_min": int(counts_after.min()),
        "repaired_size_max": int(counts_after.max()),
        "alias_actions": actions,
        "bad_alias_checks": bad_alias_checks,
    }


def rebuild_history(daily: pd.DataFrame) -> pd.DataFrame:
    dates = sorted(daily["trade_date"].unique().tolist())
    nextday = {dates[i]: dates[i + 1] for i in range(len(dates) - 1)}
    by_symbol = {
        s: sorted(g["trade_date"].tolist())
        for s, g in daily.groupby("symbol", sort=True)
    }

    rows = []
    for symbol, ds in by_symbol.items():
        start = prev = ds[0]
        for d in ds[1:]:
            if nextday.get(prev) == d:
                prev = d
            else:
                rows.append({
                    "symbol": symbol,
                    "valid_from": start,
                    "valid_to_exclusive": nextday.get(prev, ""),
                })
                start = prev = d
        rows.append({
            "symbol": symbol,
            "valid_from": start,
            "valid_to_exclusive": nextday.get(prev, ""),
        })

    return pd.DataFrame(rows).sort_values(
        ["symbol", "valid_from"]
    ).reset_index(drop=True)


def build_accepted_csi1000(
    source_dir: Path,
    destination: Path,
    dataset_id: str,
) -> Dict[str, Any]:
    source_daily = source_dir / "csi1000_membership_daily.csv"
    source_history = source_dir / "csi1000_membership_history.csv"
    if not source_daily.exists() or not source_history.exists():
        raise FileNotFoundError("missing CSI1000 source assets")

    daily = load_daily(source_daily)
    repaired, audit = canonicalize_code_alias(daily, expected_size=1000)
    history = rebuild_history(repaired)

    destination.mkdir(parents=True, exist_ok=True)
    repaired.to_csv(
        destination / "csi1000_membership_daily.csv",
        index=False,
        encoding="utf-8",
    )
    history.to_csv(
        destination / "csi1000_membership_history.csv",
        index=False,
        encoding="utf-8",
    )
    pd.DataFrame(audit["alias_actions"]).to_csv(
        destination / "csi1000_alias_repair_log.csv",
        index=False,
        encoding="utf-8",
    )

    manifest = {
        "dataset_id": dataset_id,
        "status": "READY_FOR_DEEP_OVERSOLD_REPLICATION",
        "source_kind": "EXTERNAL_EVA_DELIVERY_REPAIRED_ON_ACCEPTANCE",
        "source_dir_read_only": str(source_dir),
        "repair_scope": "SECURITY_CODE_ALIAS_ONLY",
        "repair_rule": {
            "historical_code": OLD_CODE,
            "new_code": NEW_CODE,
            "new_code_effective_date": CODE_CHANGE_DATE,
            "pre_effective_semantics": OLD_CODE,
            "post_effective_semantics": NEW_CODE,
        },
        "official_evidence_note": (
            "SZSE implementation announcement: 300114 changed to 302132 "
            "effective 2025-02-17. Historical 302132 rows before that date are "
            "therefore identifier alias contamination, not a distinct security."
        ),
        "source_sha256": {
            "daily": sha256_file(source_daily),
            "history": sha256_file(source_history),
        },
        "accepted_sha256": {
            "daily": sha256_file(destination / "csi1000_membership_daily.csv"),
            "history": sha256_file(destination / "csi1000_membership_history.csv"),
            "repair_log": sha256_file(destination / "csi1000_alias_repair_log.csv"),
        },
        "audit": audit,
        "accepted_history_rows": int(len(history)),
        "accepted_symbols_ever": int(repaired["symbol"].nunique()),
        "accepted_trading_days": int(repaired["trade_date"].nunique()),
    }
    (destination / "acceptance_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest
