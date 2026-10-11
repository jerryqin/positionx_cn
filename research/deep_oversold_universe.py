from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import pandas as pd


@dataclass(frozen=True)
class UniverseSpec:
    tag: str
    expected_size: int
    expected_index: str


CSI500 = UniverseSpec("csi500", 500, "000905.SH")
CSI1000 = UniverseSpec("csi1000", 1000, "000852.SH")


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


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def audit_universe_assets(assets_dir: Path, spec: UniverseSpec) -> Dict[str, Any]:
    daily_path = assets_dir / f"{spec.tag}_membership_daily.csv"
    hist_path = assets_dir / f"{spec.tag}_membership_history.csv"
    meta_path = assets_dir / "px_assets_meta_346.json"
    verify_path = assets_dir / "px_assets_verification.json"

    missing = [str(p) for p in (daily_path, hist_path, meta_path, verify_path) if not p.exists()]
    if missing:
        raise FileNotFoundError("missing universe assets: " + ", ".join(missing))

    daily = pd.read_csv(daily_path, dtype=str)
    hist = pd.read_csv(hist_path, dtype=str)
    need_daily = {"trade_date", "symbol"}
    need_hist = {"symbol", "valid_from", "valid_to_exclusive"}
    if need_daily - set(daily.columns):
        raise ValueError(f"daily missing columns {sorted(need_daily - set(daily.columns))}")
    if need_hist - set(hist.columns):
        raise ValueError(f"history missing columns {sorted(need_hist - set(hist.columns))}")

    daily["trade_date"] = daily["trade_date"].map(norm_date)
    daily["symbol"] = daily["symbol"].astype(str)
    hist["valid_from"] = hist["valid_from"].map(norm_date)

    dup = int(daily.duplicated(["trade_date", "symbol"]).sum())
    counts = daily.groupby("trade_date")["symbol"].nunique().sort_index()
    bad = counts[counts != spec.expected_size]

    inversions = 0
    for r in hist.itertuples(index=False):
        end = str(r.valid_to_exclusive).strip() if pd.notna(r.valid_to_exclusive) else ""
        if end and norm_date(r.valid_from) >= norm_date(end):
            inversions += 1

    meta = _load_json(meta_path)
    verification = _load_json(verify_path)
    m = meta.get("四", {}).get(spec.tag, {})
    v = verification.get("四", {}).get(spec.tag, {})

    checks = {
        "daily_no_duplicates": dup == 0,
        "daily_all_days_expected_size": len(bad) == 0,
        "history_no_interval_inversions": inversions == 0,
        "verification_all_days_expected_size": bool(v.get("all_days_expected_size", False)),
        "meta_snapshot_crosscheck_exact": (
            int(m.get("snapshot_crosscheck", {}).get("exact", -1))
            == int(m.get("snapshot_crosscheck", {}).get("checked", -2))
            and int(m.get("snapshot_crosscheck", {}).get("checked", 0)) > 0
        ),
    }
    accepted = all(checks.values())

    return {
        "tag": spec.tag,
        "expected_size": spec.expected_size,
        "index": spec.expected_index,
        "accepted": accepted,
        "checks": checks,
        "daily_rows": int(len(daily)),
        "trading_days": int(len(counts)),
        "size_min": int(counts.min()),
        "size_max": int(counts.max()),
        "symbols_ever_daily": int(daily["symbol"].nunique()),
        "history_rows": int(len(hist)),
        "history_symbols": int(hist["symbol"].nunique()),
        "duplicates": dup,
        "interval_inversions": inversions,
        "bad_size_dates_count": int(len(bad)),
        "bad_size_dates_sample": [
            {"trade_date": str(k), "size": int(val)}
            for k, val in bad.head(20).items()
        ],
        "source_meta": m,
        "source_verification": v,
        "sha256": {
            "daily": sha256_file(daily_path),
            "history": sha256_file(hist_path),
            "meta": sha256_file(meta_path),
            "verification": sha256_file(verify_path),
        },
    }


def accept_universe_assets(
    assets_dir: Path,
    destination: Path,
    spec: UniverseSpec,
    dataset_id: str,
) -> Dict[str, Any]:
    audit = audit_universe_assets(assets_dir, spec)
    if not audit["accepted"]:
        raise RuntimeError(
            "UNIVERSE_NOT_ACCEPTED " + json.dumps(audit, ensure_ascii=False)
        )

    destination.mkdir(parents=True, exist_ok=True)
    files = [
        f"{spec.tag}_membership_daily.csv",
        f"{spec.tag}_membership_history.csv",
    ]
    for name in files:
        shutil.copy2(assets_dir / name, destination / name)

    manifest = {
        "dataset_id": dataset_id,
        "status": "READY_FOR_DEEP_OVERSOLD_REPLICATION",
        "source_kind": "EXTERNAL_EVA_DELIVERY_ACCEPTED_BY_POSITIONX",
        "read_only_source_dir": str(assets_dir),
        "accepted_files": files,
        "audit": audit,
    }
    (destination / "acceptance_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


def load_accepted_daily(destination: Path, spec: UniverseSpec) -> pd.DataFrame:
    manifest_path = destination / "acceptance_manifest.json"
    daily_path = destination / f"{spec.tag}_membership_daily.csv"
    if not manifest_path.exists() or not daily_path.exists():
        raise FileNotFoundError(
            f"accepted {spec.tag} universe missing; run acceptance first"
        )
    manifest = _load_json(manifest_path)
    if manifest.get("status") != "READY_FOR_DEEP_OVERSOLD_REPLICATION":
        raise RuntimeError(f"bad accepted universe status: {manifest.get('status')}")
    daily = pd.read_csv(daily_path, dtype=str)
    daily["trade_date"] = daily["trade_date"].map(norm_date)
    daily["symbol"] = daily["symbol"].astype(str)
    return daily
