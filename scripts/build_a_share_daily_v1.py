from __future__ import annotations

"""
PositionX — A_SHARE_SHSZ_NON_ST_DAILY_V1
Formal resumable full-history daily-bar build.

Frozen scope (current universe):
- SH/SZ only
- SSE_MAIN / SZSE_MAIN / CHINEXT / STAR only
- verified stock
- current ST/*ST/S*ST/SST/NST excluded
- pre-listing / zero-daily-history current codes excluded
- BSE excluded

Historical archive:
- supported SH/SZ expired stock contracts from the accepted Phase-B1 metadata
- current ST-like archive names excluded
- archive rows are raw anti-survivorship data, never current trade candidates

Historical ST caveat:
- raw daily bars can be built now
- strict all-market historical strategy eligibility remains blocked until PIT ST status
  is complete enough for the requested date range
"""

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PHASE_A_ROOT = PROJECT_ROOT / "data" / "research" / "full_market_daily_v1_audit"
PHASE_B1_ROOT = PROJECT_ROOT / "data" / "research" / "full_market_daily_v1_phase_b1"
PHASE_B21_ROOT = PROJECT_ROOT / "data" / "research" / "full_market_daily_v1_phase_b2_1"

DATA_ROOT = Path(r"D:\positionx_data\market\a_share_daily_v1")
CATALOG_PATH = Path(r"D:\positionx_data\catalog\market_data.duckdb")
CATALOG_BACKUP_ROOT = Path(r"D:\positionx_data\backup\market_data_catalog")

ALLOWED_BOARDS = {"SSE_MAIN", "SZSE_MAIN", "CHINEXT", "STAR"}
ST_RE = re.compile(r"^(?:\*?ST|S\*?ST|SST|NST)", re.I)

DAILY_FIELDS = ["open", "high", "low", "close", "volume", "amount", "preClose", "suspendFlag"]
START_ALL = "19900101"
SOURCE = "QMT_XTDATA"
DATASET_PREFIX = "A_SHARE_SHSZ_NON_ST_DAILY_V1"

# Exact raw-source anomalies accepted only after explicit diagnosis.
# Raw QMT values remain unchanged.
KNOWN_OHLC_ANOMALIES = {
    ("600690.SH", "19940404"): {
        "reason": "QMT_EARLY_EXRIGHT_OHLC_INVARIANT",
        "event": "1994-04-04 ex-right date, 10送3",
    },
    ("600807.SH", "19940404"): {
        "reason": "QMT_EARLY_EXRIGHT_OHLC_INVARIANT",
        "event": "1994-04-04 ex-right date, 10送5派1.5",
    },
}


def safe(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        if isinstance(v, float) and not math.isfinite(v):
            return None
        return v
    if hasattr(v, "item"):
        try:
            return safe(v.item())
        except Exception:
            pass
    return str(v)


def boolish(v: Any) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "y"}


def clean_name(v: Any) -> str:
    return str(v or "").replace(" ", "").replace("　", "").replace("＊", "*").strip()


def is_st_name(v: Any) -> bool:
    return bool(ST_RE.match(clean_name(v).upper()))


def norm_date(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.strftime("%Y%m%d")
    s = str(v).strip()
    if not s or s.lower() in {"nan", "nat", "none"}:
        return None
    d = re.sub(r"\D", "", s)
    if len(d) >= 8 and d[:4].isdigit():
        y = int(d[:4])
        if 1990 <= y <= 2200:
            return d[:8]
    try:
        x = float(s)
        if x > 1e12:
            x /= 1000.0
        if x > 1e9:
            return datetime.fromtimestamp(x).strftime("%Y%m%d")
    except Exception:
        pass
    return None


def json_write(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=safe), encoding="utf-8")
    os.replace(tmp, path)


def csv_write(path: Path, rows: Sequence[Dict[str, Any]], fields: Optional[Sequence[str]] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        keys, seen = [], set()
        for r in rows:
            for k in r:
                if k not in seen:
                    seen.add(k)
                    keys.append(k)
        fields = keys or ["empty"]
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(fields), extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: safe(r.get(k)) for k in fields})
    os.replace(tmp, path)


def read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def latest_dir(root: Path, required: Sequence[str]) -> Path:
    if not root.exists():
        raise RuntimeError(f"Missing root: {root}")
    for d in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True):
        if all((d / x).exists() for x in required):
            return d
    raise RuntimeError(f"No complete artifact found under {root}")


def sha256_text_lines(values: Sequence[str]) -> str:
    h = hashlib.sha256()
    for x in values:
        h.update((str(x) + "\n").encode("utf-8"))
    return h.hexdigest()


def dataset_paths(as_of: str) -> Dict[str, Path]:
    dataset_id = f"{DATASET_PREFIX}_{as_of}"
    root = DATA_ROOT / dataset_id
    return {
        "dataset_id": Path(dataset_id),
        "root": root,
        "meta": root / "meta",
        "daily": root / "daily_bars",
        "state": root / "state",
        "logs": root / "logs",
        "manifest": root / "manifest.json",
        "master_csv": root / "meta" / "security_master.csv",
        "master_parquet": root / "meta" / "security_master.parquet",
        "current_csv": root / "meta" / "current_universe.csv",
        "archive_csv": root / "meta" / "expired_archive_universe.csv",
        "build_state": root / "state" / "build_state.json",
        "failed_csv": root / "state" / "failed_symbols.csv",
        "verification": root / "verification.json",
    }


def duckdb_write_parquet(df: pd.DataFrame, path: Path, select_sql: str) -> int:
    import duckdb
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    con = duckdb.connect(database=":memory:")
    try:
        con.register("src_df", df)
        p = str(tmp).replace("\\", "/").replace("'", "''")
        con.execute(f"COPY ({select_sql}) TO '{p}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        n = con.execute(f"SELECT count(*) FROM read_parquet('{p}')").fetchone()[0]
    finally:
        con.close()
    os.replace(tmp, path)
    return int(n)


def prepare(as_of: Optional[str] = None) -> Dict[str, Any]:
    pa = latest_dir(PHASE_A_ROOT, ["audit_summary.json", "security_master_current.csv"])
    pb1 = latest_dir(PHASE_B1_ROOT, ["phase_b1_summary.json", "expired_contract_master_after.csv"])
    pb21 = latest_dir(PHASE_B21_ROOT, ["phase_b2_1_summary.json", "eligible_current_universe.csv"])

    pa_summary = json.loads((pa / "audit_summary.json").read_text(encoding="utf-8"))
    pb21_summary = json.loads((pb21 / "phase_b2_1_summary.json").read_text(encoding="utf-8"))

    resolved_as_of = as_of or pa_summary["calendar_latest_by_market"]["SH"]
    if resolved_as_of != pa_summary["calendar_latest_by_market"]["SZ"]:
        raise RuntimeError("SH/SZ calendar latest mismatch; refuse to freeze dataset.")
    if pb21_summary["verdict"] != "PASS_FULL_HISTORY_PILOT_REFINED":
        raise RuntimeError(f"Phase B2.1 is not PASS: {pb21_summary['verdict']}")

    p = dataset_paths(resolved_as_of)
    root = p["root"]
    if p["manifest"].exists():
        existing = json.loads(p["manifest"].read_text(encoding="utf-8"))
        print(f"PREPARE_ALREADY_DONE dataset_id={existing['dataset_id']}")
        return existing

    current = read_csv(pb21 / "eligible_current_universe.csv")
    if len(current) != int(pb21_summary["current_universe"]["eligible"]):
        raise RuntimeError(
            f"Current universe count mismatch: csv={len(current)} summary={pb21_summary['current_universe']['eligible']}"
        )

    expired_raw = read_csv(pb1 / "expired_contract_master_after.csv")
    current_symbols = {r["symbol"] for r in current}

    archive = []
    for r in expired_raw:
        sym = str(r.get("symbol", "")).upper()
        if r.get("board") not in ALLOWED_BOARDS:
            continue
        if not sym.endswith((".SH", ".SZ")):
            continue
        if not boolish(r.get("instrument_type_stock")):
            continue
        if is_st_name(r.get("name")):
            continue
        if sym in current_symbols:
            continue
        rr = dict(r)
        rr["scope_role"] = "EXPIRED_ARCHIVE"
        rr["research_trade_eligible_current"] = False
        archive.append(rr)

    current_rows = []
    for r in current:
        rr = dict(r)
        rr["scope_role"] = "CURRENT_ELIGIBLE"
        rr["research_trade_eligible_current"] = True
        current_rows.append(rr)

    master = []
    for r in current_rows + archive:
        master.append({
            "symbol": r.get("symbol"),
            "name": r.get("name"),
            "board": r.get("board"),
            "exchange_id": r.get("exchange_id"),
            "exchange_code": r.get("exchange_code"),
            "instrument_id": r.get("instrument_id"),
            "unified_code": r.get("unified_code"),
            "open_date": norm_date(r.get("open_date")),
            "expire_date": norm_date(r.get("expire_date")),
            "scope_role": r.get("scope_role"),
            "research_trade_eligible_current": boolish(r.get("research_trade_eligible_current")),
            "instrument_type_stock": boolish(r.get("instrument_type_stock")),
            "source": "QMT_STATIC_ACCEPTED_PHASES",
            "as_of": resolved_as_of,
        })

    # Hard safety invariants.
    syms = [r["symbol"] for r in master]
    if len(syms) != len(set(syms)):
        dup = [x for x, n in Counter(syms).items() if n > 1][:20]
        raise RuntimeError(f"Duplicate symbols in frozen master: {dup}")
    if any(str(x).endswith(".BJ") for x in syms):
        raise RuntimeError("BSE symbol leaked into frozen master.")
    if any(r["board"] not in ALLOWED_BOARDS for r in master):
        raise RuntimeError("Unsupported board leaked into frozen master.")
    if any(is_st_name(r.get("name")) and r["scope_role"] == "CURRENT_ELIGIBLE" for r in master):
        raise RuntimeError("Current ST leaked into current eligible universe.")

    root.mkdir(parents=True, exist_ok=True)
    p["meta"].mkdir(parents=True, exist_ok=True)
    p["daily"].mkdir(parents=True, exist_ok=True)
    p["state"].mkdir(parents=True, exist_ok=True)
    p["logs"].mkdir(parents=True, exist_ok=True)

    csv_write(p["current_csv"], current_rows)
    csv_write(p["archive_csv"], archive)
    csv_write(p["master_csv"], master)

    dfm = pd.DataFrame(master)
    master_n = duckdb_write_parquet(
        dfm, p["master_parquet"],
        """
        SELECT
          CAST(symbol AS VARCHAR) AS symbol,
          CAST("name" AS VARCHAR) AS "name",
          CAST(board AS VARCHAR) AS board,
          CAST(exchange_id AS VARCHAR) AS exchange_id,
          CAST(exchange_code AS VARCHAR) AS exchange_code,
          CAST(instrument_id AS VARCHAR) AS instrument_id,
          CAST(unified_code AS VARCHAR) AS unified_code,
          CASE WHEN open_date IS NULL OR open_date='' THEN NULL ELSE strptime(open_date, '%Y%m%d')::DATE END AS open_date,
          CASE WHEN expire_date IS NULL OR expire_date='' THEN NULL ELSE strptime(expire_date, '%Y%m%d')::DATE END AS expire_date,
          CAST(scope_role AS VARCHAR) AS scope_role,
          CAST(research_trade_eligible_current AS BOOLEAN) AS research_trade_eligible_current,
          CAST(instrument_type_stock AS BOOLEAN) AS instrument_type_stock,
          CAST(source AS VARCHAR) AS source,
          strptime(as_of, '%Y%m%d')::DATE AS as_of
        FROM src_df
        """
    )

    manifest = {
        "dataset_id": str(p["dataset_id"]),
        "version": "1.0",
        "as_of": resolved_as_of,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source": SOURCE,
        "raw_price_adjustment": "NONE",
        "fields": [
            "symbol", "trade_date", "open", "high", "low", "close",
            "volume", "amount", "pre_close", "suspend_flag", "source", "ingested_at"
        ],
        "scope_policy": {
            "allowed_boards": sorted(ALLOWED_BOARDS),
            "exclude_bse": True,
            "exclude_current_st": True,
            "exclude_prelisting_current": True,
            "current_universe_count": len(current_rows),
            "archive_expired_count": len(archive),
            "total_download_symbols": len(master),
            "historical_st_policy": "PIT_ST_FILTER_REQUIRED_BEFORE_STRATEGY_RESEARCH",
            "historical_research_ready": False,
        },
        "phase_inputs": {
            "phase_a": str(pa),
            "phase_b1": str(pb1),
            "phase_b2_1": str(pb21),
        },
        "symbol_sha256": sha256_text_lines(sorted(syms)),
        "security_master_rows": master_n,
        "storage": {
            "daily_parquet": str(p["daily"]),
            "security_master_parquet": str(p["master_parquet"]),
            "catalog": str(CATALOG_PATH),
        },
    }
    json_write(p["manifest"], manifest)

    print(json.dumps({
        "status": "PREPARED",
        "dataset_id": manifest["dataset_id"],
        "as_of": resolved_as_of,
        "current": len(current_rows),
        "expired_archive": len(archive),
        "total": len(master),
        "root": str(root),
    }, ensure_ascii=False, indent=2))
    return manifest


def local_data_call(xtdata: Any, fields: List[str], symbols: List[str], as_of: str):
    try:
        return xtdata.get_local_data(
            field_list=fields, stock_list=symbols, period="1d",
            start_time=START_ALL, end_time=as_of, count=-1,
            dividend_type="none", fill_data=False,
        )
    except TypeError:
        return xtdata.get_local_data(fields, symbols, "1d", START_ALL, as_of, -1, "none", False)


def series_for(raw: Any, field: str, symbol: str):
    if not isinstance(raw, dict):
        return None
    obj = raw.get(field)
    if obj is not None:
        try:
            if hasattr(obj, "index") and symbol in obj.index:
                return obj.loc[symbol]
            if hasattr(obj, "columns") and symbol in obj.columns:
                return obj[symbol]
        except Exception:
            pass
    obj2 = raw.get(symbol)
    if obj2 is not None:
        try:
            if hasattr(obj2, "columns") and field in obj2.columns:
                return obj2[field]
            if isinstance(obj2, dict) and field in obj2:
                return obj2[field]
        except Exception:
            pass
    return None


def item_map(series: Any) -> Dict[str, Any]:
    if series is None:
        return {}
    try:
        s = series.dropna()
        return {d: safe(v) for k, v in s.items() if (d := norm_date(k))}
    except Exception:
        pass
    if isinstance(series, dict):
        return {d: safe(v) for k, v in series.items() if v is not None and (d := norm_date(k))}
    return {}


def extract_batch(raw: Any, symbols: Sequence[str], ingested_at: str) -> Tuple[pd.DataFrame, List[str]]:
    rows: List[Dict[str, Any]] = []
    missing: List[str] = []
    for symbol in symbols:
        maps = {f: item_map(series_for(raw, f, symbol)) for f in DAILY_FIELDS}
        dates = sorted(set().union(*(set(m) for m in maps.values())))
        if not dates:
            missing.append(symbol)
            continue
        for d in dates:
            rows.append({
                "symbol": symbol,
                "trade_date": d,
                "open": maps["open"].get(d),
                "high": maps["high"].get(d),
                "low": maps["low"].get(d),
                "close": maps["close"].get(d),
                "volume": maps["volume"].get(d),
                "amount": maps["amount"].get(d),
                "pre_close": maps["preClose"].get(d),
                "suspend_flag": maps["suspendFlag"].get(d),
                "source": SOURCE,
                "ingested_at": ingested_at,
            })
    return pd.DataFrame(rows), missing


def download_batch(xtdata: Any, symbols: Sequence[str], as_of: str, progress_log: List[Dict[str, Any]]) -> None:
    def cb(data):
        try:
            item = dict(data)
        except Exception:
            item = {"raw": str(data)}
        item["observed_at"] = datetime.now().isoformat(timespec="seconds")
        progress_log.append(item)
        total = item.get("total")
        finished = item.get("finished")
        stock = item.get("stockcode")
        msg = item.get("message")
        if total is not None or finished is not None:
            print(f"    [QMT] {finished}/{total} {stock or ''} {msg or ''}".rstrip())

    try:
        xtdata.download_history_data2(
            list(symbols), "1d", START_ALL, as_of,
            callback=cb, incrementally=False
        )
    except TypeError:
        xtdata.download_history_data2(list(symbols), "1d", START_ALL, as_of, cb, False)


def validate_batch_df(df: pd.DataFrame, symbols: Sequence[str], as_of: str) -> Dict[str, Any]:
    if df.empty:
        return {
            "ok": False, "rows": 0, "symbols": 0, "duplicate_rows": 0,
            "bad_ohlc_rows": 0, "known_bad_ohlc_rows": [],
            "unexpected_bad_ohlc_rows": [], "future_rows": 0, "reason": "EMPTY_BATCH"
        }

    tmp = df.copy()
    dup = int(tmp.duplicated(["symbol", "trade_date"]).sum())

    numeric = ["open", "high", "low", "close"]
    for c in numeric:
        tmp[c] = pd.to_numeric(tmp[c], errors="coerce")

    valid4 = tmp[numeric].notna().all(axis=1)
    bad_mask = (
        valid4 &
        (
            (tmp["high"] < tmp[["open", "low", "close"]].max(axis=1)) |
            (tmp["low"] > tmp[["open", "high", "close"]].min(axis=1))
        )
    )
    bad = tmp.loc[bad_mask, ["symbol", "trade_date", "open", "high", "low", "close"]].copy()

    known_bad = []
    unexpected_bad = []
    for _, r in bad.iterrows():
        key = (str(r["symbol"]), str(r["trade_date"]))
        item = {
            "symbol": key[0], "trade_date": key[1],
            "open": safe(r["open"]), "high": safe(r["high"]),
            "low": safe(r["low"]), "close": safe(r["close"]),
        }
        if key in KNOWN_OHLC_ANOMALIES:
            item.update(KNOWN_OHLC_ANOMALIES[key])
            known_bad.append(item)
        else:
            unexpected_bad.append(item)

    future_rows = int((tmp["trade_date"] > as_of).sum())
    actual_symbols = set(tmp["symbol"].unique())
    expected = set(symbols)
    unexpected = sorted(actual_symbols - expected)

    return {
        "ok": dup == 0 and not unexpected_bad and future_rows == 0 and not unexpected,
        "rows": int(len(tmp)),
        "symbols": int(tmp["symbol"].nunique()),
        "duplicate_rows": dup,
        "bad_ohlc_rows": int(len(bad)),
        "known_bad_ohlc_rows": known_bad,
        "unexpected_bad_ohlc_rows": unexpected_bad,
        "future_rows": future_rows,
        "unexpected_symbols": unexpected,
    }


def write_batch_parts(df: pd.DataFrame, daily_root: Path, batch_id: int) -> List[Dict[str, Any]]:
    if df.empty:
        return []
    d = df.copy()
    d["trade_date_dt"] = pd.to_datetime(d["trade_date"], format="%Y%m%d", errors="raise")
    d["year"] = d["trade_date_dt"].dt.year.astype(int)
    d["ingested_at_dt"] = pd.to_datetime(d["ingested_at"], errors="raise")

    outputs = []
    for year, g in d.groupby("year", sort=True):
        out = daily_root / f"year={int(year)}" / f"part-{batch_id:05d}.parquet"
        select_sql = """
        SELECT
          CAST(symbol AS VARCHAR) AS symbol,
          CAST(trade_date_dt AS DATE) AS trade_date,
          CAST("open" AS DOUBLE) AS "open",
          CAST("high" AS DOUBLE) AS "high",
          CAST("low" AS DOUBLE) AS "low",
          CAST("close" AS DOUBLE) AS "close",
          CAST(volume AS BIGINT) AS volume,
          CAST(amount AS DOUBLE) AS amount,
          CAST(pre_close AS DOUBLE) AS pre_close,
          CAST(suspend_flag AS INTEGER) AS suspend_flag,
          CAST(source AS VARCHAR) AS source,
          CAST(ingested_at_dt AS TIMESTAMP) AS ingested_at
        FROM src_df
        ORDER BY symbol, trade_date_dt
        """
        n = duckdb_write_parquet(g, out, select_sql)
        outputs.append({"year": int(year), "path": str(out), "rows": n})
    return outputs


def remove_batch_parts(daily_root: Path, batch_id: int) -> None:
    if not daily_root.exists():
        return
    pat = f"part-{batch_id:05d}.parquet"
    for p in daily_root.glob(f"year=*/" + pat):
        p.unlink(missing_ok=True)


def load_or_init_build_state(p: Dict[str, Path], manifest: Dict[str, Any], batch_size: int) -> Dict[str, Any]:
    if p["build_state"].exists():
        st = json.loads(p["build_state"].read_text(encoding="utf-8"))
        if int(st["batch_size"]) != int(batch_size):
            raise RuntimeError(
                f"Build already started with batch_size={st['batch_size']}; "
                f"requested {batch_size}. Resume with the original batch size."
            )
        if st["symbol_sha256"] != manifest["symbol_sha256"]:
            raise RuntimeError("Frozen symbol universe changed; refuse to resume.")
        return st

    master = read_csv(p["master_csv"])
    symbols = sorted(r["symbol"] for r in master)
    batches = []
    for i in range(0, len(symbols), batch_size):
        batch_id = i // batch_size + 1
        vals = symbols[i:i + batch_size]
        batches.append({
            "batch_id": batch_id,
            "symbols": vals,
            "symbol_sha256": sha256_text_lines(vals),
            "status": "PENDING",
            "attempts": 0,
            "rows": 0,
            "missing_symbols": [],
            "parts": [],
            "last_error": None,
        })

    st = {
        "dataset_id": manifest["dataset_id"],
        "as_of": manifest["as_of"],
        "batch_size": batch_size,
        "symbol_sha256": manifest["symbol_sha256"],
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "batches": batches,
    }
    json_write(p["build_state"], st)
    return st


def build(batch_size: int = 200, max_batches: int = 0, continue_on_missing: bool = True) -> int:
    manifest = prepare()
    p = dataset_paths(manifest["as_of"])
    st = load_or_init_build_state(p, manifest, batch_size)

    from xtquant import xtdata
    try:
        xtdata.enable_hello = False
    except Exception:
        pass

    processed_this_run = 0
    for b in st["batches"]:
        if b["status"] == "DONE":
            continue
        if max_batches and processed_this_run >= max_batches:
            break

        batch_id = int(b["batch_id"])
        symbols = list(b["symbols"])
        processed_this_run += 1

        print("=" * 100)
        print(f"BATCH {batch_id}/{len(st['batches'])} symbols={len(symbols)}")
        print(f"{symbols[0]} ... {symbols[-1]}")
        print("=" * 100)

        remove_batch_parts(p["daily"], batch_id)
        b["attempts"] = int(b.get("attempts", 0)) + 1
        b["status"] = "RUNNING"
        b["last_error"] = None
        st["updated_at"] = datetime.now().isoformat(timespec="seconds")
        json_write(p["build_state"], st)

        progress: List[Dict[str, Any]] = []
        try:
            t0 = time.time()
            download_batch(xtdata, symbols, manifest["as_of"], progress)
            ingested_at = datetime.now().isoformat(timespec="seconds")
            raw = local_data_call(xtdata, DAILY_FIELDS, symbols, manifest["as_of"])
            df, missing = extract_batch(raw, symbols, ingested_at)
            audit = validate_batch_df(df, symbols, manifest["as_of"])
            if not audit["ok"]:
                raise RuntimeError(f"Batch data validation failed: {audit}")

            parts = write_batch_parts(df, p["daily"], batch_id)
            if sum(x["rows"] for x in parts) != len(df):
                raise RuntimeError("Parquet row-count mismatch inside batch.")

            b["status"] = "DONE" if not missing else "DONE_WITH_MISSING"
            b["rows"] = int(len(df))
            b["missing_symbols"] = missing
            b["parts"] = parts
            b["duration_seconds"] = round(time.time() - t0, 3)
            b["audit"] = audit
            b["qmt_progress_tail"] = progress[-10:]
            print(json.dumps({
                "batch_id": batch_id,
                "status": b["status"],
                "rows": b["rows"],
                "symbols_with_rows": audit["symbols"],
                "missing_symbols": missing,
                "parts": len(parts),
                "duration_seconds": b["duration_seconds"],
            }, ensure_ascii=False, indent=2))
        except Exception as e:
            b["status"] = "FAILED"
            b["last_error"] = f"{type(e).__name__}: {e}"
            b["qmt_progress_tail"] = progress[-10:]
            print("BATCH_FAILED:", b["last_error"])
        finally:
            st["updated_at"] = datetime.now().isoformat(timespec="seconds")
            json_write(p["build_state"], st)

    missing_rows = []
    failed_batches = []
    for b in st["batches"]:
        if b["status"] == "FAILED":
            failed_batches.append(b["batch_id"])
        for sym in b.get("missing_symbols", []):
            missing_rows.append({
                "batch_id": b["batch_id"],
                "symbol": sym,
                "batch_status": b["status"],
            })
    csv_write(p["failed_csv"], missing_rows, ["batch_id", "symbol", "batch_status"])

    counts = Counter(b["status"] for b in st["batches"])
    print()
    print("BUILD STATUS:", dict(counts))
    if failed_batches:
        print("FAILED BATCHES:", failed_batches)
        return 2
    if missing_rows and not continue_on_missing:
        return 3
    if counts.get("PENDING", 0) or counts.get("RUNNING", 0):
        return 4
    return 0 if not missing_rows else 3


def backup_catalog_if_exists() -> Optional[Path]:
    if not CATALOG_PATH.exists():
        return None
    CATALOG_BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = CATALOG_BACKUP_ROOT / f"market_data_{stamp}.duckdb"
    shutil.copy2(CATALOG_PATH, dst)
    return dst


def create_catalog(p: Dict[str, Path], manifest: Dict[str, Any]) -> Dict[str, Any]:
    import duckdb
    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    backup = backup_catalog_if_exists()

    daily_glob = str((p["daily"] / "year=*" / "part-*.parquet")).replace("\\", "/").replace("'", "''")
    master_path = str(p["master_parquet"]).replace("\\", "/").replace("'", "''")

    con = duckdb.connect(str(CATALOG_PATH))
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS market")
        con.execute(
            f"""
            CREATE OR REPLACE VIEW market.daily_bars_v1 AS
            SELECT * FROM read_parquet('{daily_glob}', union_by_name=true, hive_partitioning=0)
            """
        )
        con.execute(
            f"""
            CREATE OR REPLACE VIEW market.security_master_v1 AS
            SELECT * FROM read_parquet('{master_path}')
            """
        )
        con.execute(
            """
            CREATE OR REPLACE VIEW market.current_universe_v1 AS
            SELECT * FROM market.security_master_v1
            WHERE scope_role='CURRENT_ELIGIBLE' AND research_trade_eligible_current
            """
        )
        con.execute(
            """
            CREATE OR REPLACE VIEW market.expired_archive_universe_v1 AS
            SELECT * FROM market.security_master_v1
            WHERE scope_role='EXPIRED_ARCHIVE'
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS market.dataset_registry (
              dataset_id VARCHAR PRIMARY KEY,
              as_of DATE,
              version VARCHAR,
              root VARCHAR,
              created_at TIMESTAMP,
              historical_research_ready BOOLEAN
            )
            """
        )
        con.execute("DELETE FROM market.dataset_registry WHERE dataset_id=?", [manifest["dataset_id"]])
        con.execute(
            """
            INSERT INTO market.dataset_registry
            VALUES (?, strptime(?, '%Y%m%d')::DATE, ?, ?, ?, ?)
            """,
            [
                manifest["dataset_id"],
                manifest["as_of"],
                manifest["version"],
                str(p["root"]),
                manifest["created_at"],
                bool(manifest["scope_policy"]["historical_research_ready"]),
            ],
        )
        counts = {
            "daily_rows": int(con.execute("SELECT count(*) FROM market.daily_bars_v1").fetchone()[0]),
            "daily_symbols": int(con.execute("SELECT count(DISTINCT symbol) FROM market.daily_bars_v1").fetchone()[0]),
            "current_universe": int(con.execute("SELECT count(*) FROM market.current_universe_v1").fetchone()[0]),
            "expired_archive": int(con.execute("SELECT count(*) FROM market.expired_archive_universe_v1").fetchone()[0]),
        }
    finally:
        con.close()
    return {"catalog": str(CATALOG_PATH), "backup": str(backup) if backup else None, **counts}


def verify(create_catalog_on_pass: bool = True) -> int:
    manifest = prepare()
    p = dataset_paths(manifest["as_of"])
    if not p["build_state"].exists():
        print("VERIFY_FAIL: build_state missing")
        return 2

    st = json.loads(p["build_state"].read_text(encoding="utf-8"))
    statuses = Counter(b["status"] for b in st["batches"])
    not_done = [b["batch_id"] for b in st["batches"] if b["status"] not in {"DONE", "DONE_WITH_MISSING"}]
    missing = sorted({s for b in st["batches"] for s in b.get("missing_symbols", [])})

    part_rows = sum(int(part.get("rows", 0)) for b in st["batches"] for part in b.get("parts", []))
    expected_symbols = int(manifest["scope_policy"]["total_download_symbols"])

    result: Dict[str, Any] = {
        "dataset_id": manifest["dataset_id"],
        "as_of": manifest["as_of"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "batch_status_counts": dict(statuses),
        "not_done_batches": not_done,
        "missing_symbols": missing,
        "manifest_symbol_count": expected_symbols,
        "manifest_part_rows": part_rows,
        "historical_research_ready": False,
    }

    if not_done:
        result["ok"] = False
        result["reason"] = "BUILD_NOT_COMPLETE"
        json_write(p["verification"], result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2

    import duckdb
    glob = str((p["daily"] / "year=*" / "part-*.parquet")).replace("\\", "/").replace("'", "''")
    con = duckdb.connect(database=":memory:")
    try:
        q = f"read_parquet('{glob}', union_by_name=true, hive_partitioning=0)"
        stats = con.execute(
            f"""
            SELECT
              count(*) AS rows,
              count(DISTINCT symbol) AS symbols,
              min(trade_date) AS first_date,
              max(trade_date) AS last_date,
              sum(CASE WHEN "close" IS NULL THEN 1 ELSE 0 END) AS close_nulls,
              sum(CASE WHEN amount IS NULL THEN 1 ELSE 0 END) AS amount_nulls,
              sum(CASE WHEN pre_close IS NULL THEN 1 ELSE 0 END) AS preclose_nulls,
              sum(CASE WHEN suspend_flag IS NULL THEN 1 ELSE 0 END) AS suspend_nulls,
              sum(CASE WHEN "close" <= 0 THEN 1 ELSE 0 END) AS nonpositive_close,
              sum(CASE WHEN
                    "open" IS NOT NULL AND "high" IS NOT NULL AND "low" IS NOT NULL AND "close" IS NOT NULL
                    AND ("high" < greatest("open", "low", "close") OR "low" > least("open", "high", "close"))
                  THEN 1 ELSE 0 END) AS bad_ohlc
            FROM {q}
            """
        ).fetchone()
        cols = ["rows", "symbols", "first_date", "last_date", "close_nulls", "amount_nulls",
                "preclose_nulls", "suspend_nulls", "nonpositive_close", "bad_ohlc"]
        result["parquet_stats"] = {k: safe(v) for k, v in zip(cols, stats)}

        master_symbols = {
            r[0] for r in con.execute(
                f"SELECT symbol FROM read_parquet('{str(p['master_parquet']).replace(chr(92), '/')}')"
            ).fetchall()
        }
        data_symbols = {r[0] for r in con.execute(f"SELECT DISTINCT symbol FROM {q}").fetchall()}
        result["symbols_missing_from_daily"] = sorted(master_symbols - data_symbols)
        result["unexpected_daily_symbols"] = sorted(data_symbols - master_symbols)

        bad_rows = con.execute(
            f"""
            SELECT symbol, strftime(trade_date, '%Y%m%d') AS trade_date,
                   "open", "high", "low", "close"
            FROM {q}
            WHERE "open" IS NOT NULL AND "high" IS NOT NULL
              AND "low" IS NOT NULL AND "close" IS NOT NULL
              AND ("high" < greatest("open", "low", "close")
                   OR "low" > least("open", "high", "close"))
            ORDER BY symbol, trade_date
            """
        ).fetchall()
        known_bad = []
        unexpected_bad = []
        for symbol, trade_date, opn, high, low, close in bad_rows:
            key = (str(symbol), str(trade_date))
            item = {
                "symbol": key[0], "trade_date": key[1],
                "open": safe(opn), "high": safe(high),
                "low": safe(low), "close": safe(close),
            }
            if key in KNOWN_OHLC_ANOMALIES:
                item.update(KNOWN_OHLC_ANOMALIES[key])
                known_bad.append(item)
            else:
                unexpected_bad.append(item)
        result["known_bad_ohlc_rows"] = known_bad
        result["unexpected_bad_ohlc_rows"] = unexpected_bad
        result["quality_status"] = (
            "PASS_WITH_DOCUMENTED_SOURCE_ANOMALIES"
            if known_bad and not unexpected_bad
            else ("PASS_CLEAN" if not bad_rows else "REVIEW_UNEXPECTED_OHLC_ANOMALY")
        )
    finally:
        con.close()

    hard_ok = (
        not result["symbols_missing_from_daily"]
        and not result["unexpected_daily_symbols"]
        and int(result["parquet_stats"]["rows"]) == int(part_rows)
        and int(result["parquet_stats"]["symbols"]) == expected_symbols
        and not result["unexpected_bad_ohlc_rows"]
        and int(result["parquet_stats"]["nonpositive_close"]) == 0
        and not missing
    )
    result["ok"] = bool(hard_ok)
    result["reason"] = "PASS_RAW_DAILY_DATASET" if hard_ok else "REVIEW_RAW_DAILY_DATASET"

    if hard_ok and create_catalog_on_pass:
        result["catalog"] = create_catalog(p, manifest)

    json_write(p["verification"], result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if hard_ok else 3


def status() -> int:
    try:
        manifest = prepare()
    except Exception as e:
        print(f"STATUS: NOT_PREPARED ({type(e).__name__}: {e})")
        return 1
    p = dataset_paths(manifest["as_of"])
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    if p["build_state"].exists():
        st = json.loads(p["build_state"].read_text(encoding="utf-8"))
        print("BATCH_STATUS:", dict(Counter(b["status"] for b in st["batches"])))
        print("MISSING_SYMBOLS:", sum(len(b.get("missing_symbols", [])) for b in st["batches"]))
    if p["verification"].exists():
        print("VERIFICATION:")
        print(p["verification"].read_text(encoding="utf-8"))
    return 0


def self_test() -> int:
    assert is_st_name("ST测试")
    assert is_st_name("*ST测试")
    assert not is_st_name("贵州茅台")
    assert norm_date("2026-09-28") == "20260928"
    assert norm_date("20260928") == "20260928"
    assert ALLOWED_BOARDS == {"SSE_MAIN", "SZSE_MAIN", "CHINEXT", "STAR"}
    print("SELF_TEST_OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="PositionX formal SH/SZ non-ST daily-data V1 build")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("self-test")

    pp = sub.add_parser("prepare")
    pp.add_argument("--as-of")

    pb = sub.add_parser("build")
    pb.add_argument("--batch-size", type=int, default=200)
    pb.add_argument("--max-batches", type=int, default=0,
                    help="0=all pending batches; positive value limits work this run")

    pv = sub.add_parser("verify")
    pv.add_argument("--no-catalog", action="store_true")

    pa = sub.add_parser("all")
    pa.add_argument("--batch-size", type=int, default=200)
    pa.add_argument("--max-batches", type=int, default=0)

    sub.add_parser("status")

    a = ap.parse_args()

    if a.cmd == "self-test":
        return self_test()
    if a.cmd == "prepare":
        prepare(a.as_of)
        return 0
    if a.cmd == "build":
        return build(a.batch_size, a.max_batches)
    if a.cmd == "verify":
        return verify(create_catalog_on_pass=not a.no_catalog)
    if a.cmd == "all":
        prepare()
        rc = build(a.batch_size, a.max_batches)
        if rc not in (0, 3, 4):
            return rc
        # Only verify when all batches have completed.
        manifest = prepare()
        p = dataset_paths(manifest["as_of"])
        st = json.loads(p["build_state"].read_text(encoding="utf-8"))
        if any(b["status"] not in {"DONE", "DONE_WITH_MISSING"} for b in st["batches"]):
            print("ALL: partial build completed; rerun the same command to resume.")
            return 4
        return verify(create_catalog_on_pass=True)
    if a.cmd == "status":
        return status()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
