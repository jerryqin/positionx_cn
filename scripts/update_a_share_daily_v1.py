from __future__ import annotations

r"""
PositionX — A_SHARE_SHSZ_NON_ST_DAILY_V1 Daily Incremental V1.1.3

Architecture
------------
Immutable historical base:
  D:\positionx_data\market\a_share_daily_v1\A_SHARE_SHSZ_NON_ST_DAILY_V1_20260928

Append-only live sessions:
  D:\positionx_data\market\a_share_daily_v1\live_v1\sessions\trade_date=YYYYMMDD\

Catalog:
  D:\positionx_data\catalog\market_data.duckdb

Forward PIT rule
----------------
A live universe/ST snapshot may only be created for TODAY after market close.
If a trading session is missed, the updater fails closed instead of rebuilding
yesterday's universe from today's constituents.

No broker / execution / production SQLite is used.
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
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASE_ROOT = Path(r"D:\positionx_data\market\a_share_daily_v1\A_SHARE_SHSZ_NON_ST_DAILY_V1_20260928")
BASE_MANIFEST = BASE_ROOT / "manifest.json"
BASE_VERIFICATION = BASE_ROOT / "verification.json"
BASE_DAILY = BASE_ROOT / "daily_bars"
BASE_MASTER = BASE_ROOT / "meta" / "security_master.parquet"

BASELINE = Path(r"D:\positionx_data\backup\market_data\BASELINE_20260928")
CATALOG = Path(r"D:\positionx_data\catalog\market_data.duckdb")
CATALOG_BACKUP = Path(r"D:\positionx_data\backup\market_data_catalog")

LIVE_ROOT = Path(r"D:\positionx_data\market\a_share_daily_v1\live_v1")
SESSIONS = LIVE_ROOT / "sessions"
STAGING = LIVE_ROOT / "staging"
FAILED = LIVE_ROOT / "failed"
STATE = LIVE_ROOT / "state.json"

BASE_AS_OF = "20260928"
ALLOWED_BOARDS = {"SSE_MAIN", "SZSE_MAIN", "CHINEXT", "STAR"}
ST_RE = re.compile(r"^(?:\*?ST|S\*?ST|SST|NST)", re.I)
SECTOR = "沪深京A股"
FIELDS = ["open", "high", "low", "close", "volume", "amount", "preClose", "suspendFlag"]
SOURCE = "QMT_XTDATA_FORWARD_V1"

# V1.1: retry only unexpected missing symbols; never weaken fail-closed validation.
# Delays are deliberately short enough for daily ops but long enough to absorb
# transient QMT/source publication lag. If still missing after all retries, fail closed.
MISSING_RETRY_DELAYS_SECONDS = (5, 30, 90)
MISSING_RETRY_CHUNK_SIZE = 50
# V1.1.1: never bulk-redownload the entire current universe.
# Read today's local cache first, then download only active symbols still missing.
INITIAL_MISSING_DOWNLOAD_CHUNK_SIZE = 50
# V1.1.2: use QMT's latest-session K-line interface for today's post-close bar.
# This avoids the reproducible blocking behavior observed in repeated
# synchronous download_history_data2() calls.
DAILY_ACQUISITION_METHOD = "QMT_GET_FULL_TICK_POSTCLOSE_DAILY_V1_1_3"


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


def json_write_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=safe), encoding="utf-8")
    os.replace(tmp, path)


def sha256_file(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def clean_name(v: Any) -> str:
    return str(v or "").replace(" ", "").replace("　", "").replace("＊", "*").strip()


def is_st_name(v: Any) -> bool:
    return bool(ST_RE.match(clean_name(v).upper()))


def norm_symbol(x: Any) -> str:
    return str(x or "").strip().upper()


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


def board_of(symbol: str) -> str:
    s = norm_symbol(symbol)
    code, _, market = s.partition(".")
    if market == "SH":
        if code.startswith(("688", "689")):
            return "STAR"
        if code.startswith(("600", "601", "603", "605")):
            return "SSE_MAIN"
        return "UNSUPPORTED"
    if market == "SZ":
        if code.startswith(("300", "301")):
            return "CHINEXT"
        if code.startswith(("000", "001", "002", "003")):
            return "SZSE_MAIN"
        return "UNSUPPORTED"
    if market == "BJ":
        return "BSE"
    return "UNSUPPORTED"


def bool_stock_type(xtdata: Any, symbol: str) -> Tuple[Optional[bool], str]:
    try:
        t = xtdata.get_instrument_type(symbol)
        if not isinstance(t, dict):
            return None, ""
        true_types = ";".join(sorted(str(k) for k, v in t.items() if bool(v)))
        return (bool(t.get("stock")) if "stock" in t else None), true_types
    except Exception as e:
        return None, f"ERROR:{type(e).__name__}:{e}"


def instrument_detail(xtdata: Any, symbol: str) -> Tuple[Dict[str, Any], Optional[str]]:
    try:
        try:
            d = xtdata.get_instrument_detail(symbol, False)
        except TypeError:
            d = xtdata.get_instrument_detail(symbol)
        return (d if isinstance(d, dict) else {}), None
    except Exception as e:
        return {}, f"{type(e).__name__}: {e}"


def ensure_foundation() -> Dict[str, Any]:
    for p in [BASE_MANIFEST, BASE_VERIFICATION, BASE_MASTER, CATALOG, BASELINE / "baseline_manifest.json"]:
        if not p.exists():
            raise RuntimeError(f"Foundation asset missing: {p}")

    m = json.loads(BASE_MANIFEST.read_text(encoding="utf-8"))
    v = json.loads(BASE_VERIFICATION.read_text(encoding="utf-8"))
    b = json.loads((BASELINE / "baseline_manifest.json").read_text(encoding="utf-8"))

    checks = {
        "dataset_id": m.get("dataset_id") == "A_SHARE_SHSZ_NON_ST_DAILY_V1_20260928",
        "base_as_of": m.get("as_of") == BASE_AS_OF,
        "verify_ok": v.get("ok") is True,
        "verify_reason": v.get("reason") == "PASS_RAW_DAILY_DATASET",
        "quality_status": v.get("quality_status") == "PASS_WITH_DOCUMENTED_SOURCE_ANOMALIES",
        "baseline_id": b.get("baseline_id") == "A_SHARE_SHSZ_NON_ST_DAILY_V1_BASELINE_20260928",
    }
    bad = [k for k, ok in checks.items() if not ok]
    if bad:
        raise RuntimeError(f"Foundation checks failed: {bad}")
    return {"manifest": m, "verification": v, "baseline": b}


def get_latest_trade_date(xtdata: Any, end_date: str) -> Optional[str]:
    try:
        vals = xtdata.get_trading_dates("SH", start_time="", end_time=end_date, count=1)
    except TypeError:
        vals = xtdata.get_trading_dates("SH", "", end_date, 1)
    if not vals:
        return None
    return norm_date(vals[-1])


def get_sessions_between(xtdata: Any, start: str, end: str) -> List[str]:
    try:
        vals = xtdata.get_trading_dates("SH", start_time=start, end_time=end, count=-1)
    except TypeError:
        vals = xtdata.get_trading_dates("SH", start, end, -1)
    return [d for x in (vals or []) if (d := norm_date(x))]


def load_state() -> Dict[str, Any]:
    if not STATE.exists():
        return {
            "version": "1.0",
            "base_as_of": BASE_AS_OF,
            "last_processed_date": BASE_AS_OF,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "sessions": [],
        }
    st = json.loads(STATE.read_text(encoding="utf-8"))
    if st.get("base_as_of") != BASE_AS_OF:
        raise RuntimeError("Live state base_as_of mismatch.")
    return st


def approved_resume_gap(last: str, target: str, sessions: List[str]):
    # Explicit future cutover boundary, never historical PIT reconstruction.
    from core.qmt_runtime import profile
    cfg=profile();resume=cfg.get("a_share_resume",{})
    if cfg.get("enabled") is not True or resume.get("enabled") is not True:return None
    if (resume.get("start_date")!=target or resume.get("last_validated_session")!=last
            or sessions[-1:]!=[target] or resume.get("missing_sessions")!=sessions[:-1]
            or not resume.get("authorization_ref") or resume["authorization_ref"]=="UNAPPROVED"):
        return None
    return {"from_last_validated_session":last,"resume_date":target,
            "missing_sessions":sessions[:-1],"authorization_ref":resume["authorization_ref"],
            "missing_dates_backfilled":False}


def resolve_plan(xtdata: Any, target_date: Optional[str] = None) -> Dict[str, Any]:
    now = datetime.now()
    today = now.strftime("%Y%m%d")
    latest = get_latest_trade_date(xtdata, today)
    if latest is None:
        raise RuntimeError("Could not resolve latest SH trading date.")

    st = load_state()
    last = st["last_processed_date"]

    if target_date:
        target = str(target_date).replace("-", "")
        if target > latest:
            raise RuntimeError(f"Requested target {target} is later than latest trading date {latest}.")
    else:
        target = latest

    if target <= last:
        return {
            "action": "NO_NEW_SESSION",
            "today": today, "latest_trade_date": latest,
            "last_processed_date": last, "target_date": target,
        }

    # Forward universe/ST snapshots can only be captured on the actual day.
    if target != today:
        raise RuntimeError(
            f"MISSED_SESSION_FAIL_CLOSED: target={target}, today={today}. "
            "Do not rebuild historical PIT universe from today's constituents."
        )

    # If today's trading date is already visible before the close, do not capture early.
    if now.hour < 15 or (now.hour == 15 and now.minute < 30):
        raise RuntimeError(
            f"POSTCLOSE_GUARD: target={target} but local time is {now.strftime('%H:%M:%S')}; "
            "run after 15:30."
        )

    sessions = get_sessions_between(xtdata, last, target)
    sessions = [d for d in sessions if d > last]
    resume_gap=approved_resume_gap(last,target,sessions) if sessions != [target] else None
    if sessions != [target] and resume_gap is None:
        raise RuntimeError(
            f"MISSED_SESSION_FAIL_CLOSED: last={last}, target={target}, missing_chain={sessions}"
        )

    return {
        "action": "PROCESS", "resume_gap": resume_gap,
        "today": today, "latest_trade_date": latest,
        "last_processed_date": last, "target_date": target,
    }


def capture_universe(xtdata: Any, target: str, observed_at: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    members = sorted({norm_symbol(x) for x in (xtdata.get_stock_list_in_sector(SECTOR) or []) if x})
    status_rows: List[Dict[str, Any]] = []

    supported = [s for s in members if board_of(s) in ALLOWED_BOARDS]
    n = len(supported)
    for i, symbol in enumerate(supported, 1):
        d, derr = instrument_detail(xtdata, symbol)
        stock_flag, true_types = bool_stock_type(xtdata, symbol)

        name = d.get("InstrumentName")
        open_date = norm_date(d.get("OpenDate"))
        expire_date = norm_date(d.get("ExpireDate"))
        status_raw = d.get("InstrumentStatus")
        try:
            instrument_status = int(status_raw) if status_raw is not None else None
        except Exception:
            instrument_status = None

        is_st = is_st_name(name)
        eligible = True
        reason = "ELIGIBLE_CURRENT_NON_ST_LISTED_SH_SZ"

        if stock_flag is not True:
            eligible, reason = False, "EXCLUDE_NOT_VERIFIED_STOCK"
        elif is_st:
            eligible, reason = False, "EXCLUDE_CURRENT_ST"
        elif not open_date or open_date > target:
            eligible, reason = False, "EXCLUDE_NOT_YET_LISTED"

        status_rows.append({
            "trade_date": target,
            "observed_at": observed_at,
            "symbol": symbol,
            "name": name,
            "board": board_of(symbol),
            "exchange_id": d.get("ExchangeID"),
            "exchange_code": d.get("ExchangeCode"),
            "instrument_id": d.get("InstrumentID"),
            "unified_code": d.get("UniCode"),
            "open_date": open_date,
            "expire_date": expire_date,
            "instrument_type_stock": stock_flag,
            "instrument_true_types": true_types,
            "instrument_status": instrument_status,
            "is_st": is_st,
            "eligible_current": eligible,
            "eligibility_reason": reason,
            "detail_error": derr,
            "source": "QMT_CURRENT_POSTCLOSE",
        })

        if i == 1 or i % 500 == 0 or i == n:
            print(f"[UNIVERSE] {i}/{n} {symbol}")

    current = [r for r in status_rows if r["eligible_current"]]
    return status_rows, current


def download_chunk(xtdata: Any, symbols: Sequence[str], target: str, chunk_no: int, total_chunks: int) -> None:
    symbols = list(symbols)
    if not symbols:
        return

    print(f"[DOWNLOAD] chunk {chunk_no}/{total_chunks} symbols={len(symbols)}")

    def _progress(data: Any) -> None:
        # QMT commonly reports dict fields: total / finished / stockcode / message.
        # Keep the callback diagnostic-only; it never changes trading/data semantics.
        if not isinstance(data, dict):
            return
        total = data.get("total")
        finished = data.get("finished")
        stockcode = data.get("stockcode")
        message = data.get("message")
        should_print = bool(message)
        try:
            if finished is not None and total is not None:
                fi = int(finished)
                to = int(total)
                should_print = should_print or fi in {1, to} or (fi > 0 and fi % 10 == 0)
        except Exception:
            pass
        if should_print:
            print(
                f"[DOWNLOAD PROGRESS] chunk={chunk_no}/{total_chunks} "
                f"finished={finished}/{total} stock={stockcode} message={message}"
            )

    try:
        xtdata.download_history_data2(
            symbols, "1d", target, target,
            callback=_progress, incrementally=False
        )
    except TypeError:
        xtdata.download_history_data2(symbols, "1d", target, target, _progress, False)

    print(f"[DOWNLOAD DONE] chunk {chunk_no}/{total_chunks} symbols={len(symbols)}")


def local_data_call(xtdata: Any, fields: List[str], symbols: List[str], target: str):
    try:
        return xtdata.get_local_data(
            field_list=fields, stock_list=symbols, period="1d",
            start_time=target, end_time=target, count=-1,
            dividend_type="none", fill_data=False,
        )
    except TypeError:
        return xtdata.get_local_data(fields, symbols, "1d", target, target, -1, "none", False)


def _tick_trade_date(v: Any) -> Optional[str]:
    if v is None:
        return None
    # QMT tick time is normally epoch milliseconds.
    try:
        x = float(v)
        if x > 1e12:
            x /= 1000.0
        if x > 1e9:
            return datetime.fromtimestamp(x).strftime("%Y%m%d")
    except Exception:
        pass
    return norm_date(v)


def full_tick_call(xtdata: Any) -> Dict[str, Any]:
    """
    Fetch one full-market Level-1 snapshot for SH/SZ.

    get_full_tick is part of the ordinary Level-1 snapshot API and is supported
    by older MiniQMT clients that may not support get_full_kline.
    """
    if not hasattr(xtdata, "get_full_tick"):
        raise RuntimeError(
            "QMT xtdata.get_full_tick is unavailable; fail closed."
        )
    print(
        f"[FULL_TICK] request markets=SH,SZ method={DAILY_ACQUISITION_METHOD}"
    )
    raw = xtdata.get_full_tick(["SH", "SZ"])
    if not isinstance(raw, dict):
        raise RuntimeError(
            f"get_full_tick returned unexpected type: {type(raw).__name__}"
        )
    print(f"[FULL_TICK DONE] returned_symbols={len(raw)}")
    return raw


def _rows_from_full_tick(
    raw: Dict[str, Any],
    symbols: Sequence[str],
    target: str,
    observed_at: str,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for symbol in symbols:
        d = raw.get(symbol)
        if not isinstance(d, dict):
            continue

        tick_date = _tick_trade_date(d.get("time"))
        if tick_date != target:
            continue

        def fnum(key: str):
            try:
                x = float(d.get(key))
                return x if math.isfinite(x) else None
            except Exception:
                return None

        op = fnum("open")
        hi = fnum("high")
        lo = fnum("low")
        close = fnum("lastPrice")
        pre = fnum("lastClose")
        vol = fnum("volume")
        amt = fnum("amount")

        # A tradable daily bar reconstructed from a post-close tick snapshot
        # must have positive OHLC and pre-close. Suspended/no-trade instruments
        # are intentionally left missing and handled by existing suspension /
        # fail-closed logic.
        if None in (op, hi, lo, close, pre, vol, amt):
            continue
        if min(op, hi, lo, close, pre) <= 0:
            continue
        if vol < 0 or amt < 0:
            continue

        rows.append({
            "symbol": symbol,
            "trade_date": target,
            "open": op,
            "high": hi,
            "low": lo,
            "close": close,
            "volume": vol,
            "amount": amt,
            "pre_close": pre,
            # Any symbol with a valid same-day traded OHLC snapshot is treated
            # as non-suspended for this acquisition path.
            "suspend_flag": 0,
            "source": SOURCE,
            "ingested_at": observed_at,
            "_tick_time": d.get("time"),
            "_tick_stock_status": d.get("stockStatus"),
        })
    return rows



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


def _rows_from_local_raw(
    raw: Any,
    batch: Sequence[str],
    target: str,
    observed_at: str,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for symbol in batch:
        maps = {f: item_map(series_for(raw, f, symbol)) for f in FIELDS}
        dates = sorted(set().union(*(set(m) for m in maps.values())))
        if target not in dates:
            continue
        rows.append({
            "symbol": symbol,
            "trade_date": target,
            "open": maps["open"].get(target),
            "high": maps["high"].get(target),
            "low": maps["low"].get(target),
            "close": maps["close"].get(target),
            "volume": maps["volume"].get(target),
            "amount": maps["amount"].get(target),
            "pre_close": maps["preClose"].get(target),
            "suspend_flag": maps["suspendFlag"].get(target),
            "source": SOURCE,
            "ingested_at": observed_at,
        })
    return rows


def collect_daily(xtdata: Any, current: List[Dict[str, Any]], target: str, observed_at: str,
                  chunk_size: int = 500) -> Tuple[pd.DataFrame, List[str]]:
    """
    V1.1.3 post-close full-tick collection.

    The installed MiniQMT client rejects get_full_kline as unsupported, while
    repeated download_history_data2() calls reproducibly block. Therefore the
    forward daily path reconstructs today's 1d bar from one post-close SH/SZ
    Level-1 full snapshot.

    Safety:
      - caller already enforces target == today == latest trade date and >=15:30;
      - each tick must itself have time on `target`;
      - only positive same-day OHLC/preClose rows are accepted;
      - existing local 1d data is preferred when present;
      - existing missing/suspension retry and final fail-closed validation stay.
    """
    symbols = [r["symbol"] for r in current]

    # Prefer exact local 1d bars already cached by previous attempts.
    local_rows: List[Dict[str, Any]] = []
    n_chunks = (len(symbols) + chunk_size - 1) // chunk_size
    for j in range(n_chunks):
        batch = symbols[j * chunk_size:(j + 1) * chunk_size]
        raw_local = local_data_call(xtdata, FIELDS, batch, target)
        rows = _rows_from_local_raw(raw_local, batch, target, observed_at)
        local_rows.extend(rows)
        if j == 0 or j + 1 == n_chunks:
            print(
                f"[LOCAL CACHE] chunk {j + 1}/{n_chunks} "
                f"requested={len(batch)} rows={len(rows)}"
            )

    local_df = pd.DataFrame(local_rows)
    local_present = (
        set(local_df["symbol"].unique()) if not local_df.empty else set()
    )
    residual = [s for s in symbols if s not in local_present]

    print(
        f"[LOCAL CACHE RESULT] target={target} requested={len(symbols)} "
        f"present={len(local_present)} residual={len(residual)}"
    )

    tick_rows: List[Dict[str, Any]] = []
    if residual:
        tick_raw = full_tick_call(xtdata)
        tick_rows = _rows_from_full_tick(
            tick_raw, residual, target, observed_at
        )
        print(
            f"[FULL_TICK RESULT] target={target} residual={len(residual)} "
            f"rows={len(tick_rows)}"
        )

    frames = []
    if not local_df.empty:
        frames.append(local_df)
    if tick_rows:
        frames.append(pd.DataFrame(tick_rows))

    if frames:
        df = pd.concat(frames, ignore_index=True)
        # Exact local 1d bars are preferred over snapshot reconstruction.
        df = df.drop_duplicates(
            ["symbol", "trade_date"], keep="first"
        ).reset_index(drop=True)
    else:
        df = pd.DataFrame()

    present = set(df["symbol"].unique()) if not df.empty else set()
    missing = sorted(set(symbols) - present)
    print(
        f"[DAILY ACQUISITION] method={DAILY_ACQUISITION_METHOD} "
        f"rows={len(df)} present={len(present)} missing={len(missing)}"
    )
    return df, missing


def classify_missing(current: List[Dict[str, Any]], missing: Sequence[str]) -> Tuple[List[str], List[Dict[str, Any]]]:
    current_map = {r["symbol"]: r for r in current}
    missing_suspended: List[str] = []
    missing_unexpected: List[Dict[str, Any]] = []

    for s in missing:
        row = current_map.get(s, {})
        status = row.get("instrument_status")
        if status is not None and int(status) >= 1:
            missing_suspended.append(s)
        else:
            missing_unexpected.append({
                "symbol": s,
                "instrument_status": status,
                "name": row.get("name"),
            })
    return missing_suspended, missing_unexpected


def merge_daily_rows(base: pd.DataFrame, patch: pd.DataFrame) -> pd.DataFrame:
    if base.empty:
        return patch.copy().reset_index(drop=True)
    if patch.empty:
        return base.copy().reset_index(drop=True)

    merged = pd.concat([base, patch], ignore_index=True)
    merged = merged.drop_duplicates(["symbol", "trade_date"], keep="last")
    return merged.reset_index(drop=True)


def retry_unexpected_missing(
    xtdata: Any,
    current: List[Dict[str, Any]],
    target: str,
    daily: pd.DataFrame,
    missing: List[str],
    first_audit: Dict[str, Any],
) -> Tuple[pd.DataFrame, List[str], Dict[str, Any]]:
    _, first_unexpected = classify_missing(current, missing)
    first_symbols = [x["symbol"] for x in first_unexpected]

    evidence: Dict[str, Any] = {
        "status": "NOT_NEEDED",
        "policy": "UNEXPECTED_MISSING_ONLY_FAIL_CLOSED_V1",
        "first_attempt_status": "FIRST_ATTEMPT_ERROR" if first_symbols else "FIRST_ATTEMPT_OK",
        "first_attempt_validation_ok": bool(first_audit.get("ok")),
        "first_attempt_rows": int(len(daily)),
        "first_attempt_missing_unexpected": first_unexpected,
        "retry_delays_seconds": list(MISSING_RETRY_DELAYS_SECONDS),
        "retry_chunk_size": MISSING_RETRY_CHUNK_SIZE,
        "attempts": [],
        "recovered_symbols": [],
        "final_missing_unexpected": first_unexpected,
    }

    if not first_symbols:
        return daily, missing, evidence

    current_map = {r["symbol"]: r for r in current}
    all_symbols = set(current_map)
    remaining = list(first_symbols)
    recovered_all: List[str] = []

    print(
        f"[MISSING RETRY] first_attempt unexpected={len(remaining)} "
        f"symbols={remaining}"
    )

    for attempt_no, delay_seconds in enumerate(MISSING_RETRY_DELAYS_SECONDS, 1):
        if not remaining:
            break

        print(
            f"[MISSING RETRY] attempt={attempt_no}/{len(MISSING_RETRY_DELAYS_SECONDS)} "
            f"sleep={delay_seconds}s symbols={remaining}"
        )
        time.sleep(delay_seconds)

        requested = list(remaining)
        retry_current = [current_map[s] for s in requested if s in current_map]
        attempt_observed_at = datetime.now().astimezone().isoformat(timespec="seconds")

        retry_df, _ = collect_daily(
            xtdata,
            retry_current,
            target,
            attempt_observed_at,
            chunk_size=MISSING_RETRY_CHUNK_SIZE,
        )
        daily = merge_daily_rows(daily, retry_df)

        present = set(daily["symbol"].unique()) if not daily.empty else set()
        missing = sorted(all_symbols - present)
        _, unexpected_now = classify_missing(current, missing)
        remaining_now = [x["symbol"] for x in unexpected_now]

        recovered_now = sorted(set(requested) - set(remaining_now))
        recovered_all.extend(s for s in recovered_now if s not in recovered_all)

        evidence["attempts"].append({
            "attempt": attempt_no,
            "attempted_at": attempt_observed_at,
            "delay_seconds": delay_seconds,
            "requested_symbols": requested,
            "rows_returned": int(len(retry_df)),
            "recovered_symbols": recovered_now,
            "remaining_missing_unexpected": unexpected_now,
        })

        print(
            f"[MISSING RETRY] attempt={attempt_no} "
            f"recovered={recovered_now} remaining={remaining_now}"
        )
        remaining = remaining_now

    _, final_unexpected = classify_missing(current, missing)
    evidence["recovered_symbols"] = recovered_all
    evidence["final_missing_unexpected"] = final_unexpected
    evidence["status"] = "RETRY_RECOVERED" if not final_unexpected else "FINAL_ERROR"

    return daily, missing, evidence


def validate_daily(df: pd.DataFrame, current: List[Dict[str, Any]], missing: List[str], target: str) -> Dict[str, Any]:
    missing_suspended, missing_unexpected = classify_missing(current, missing)

    if df.empty:
        return {
            "ok": False,
            "reason": "NO_DAILY_ROWS",
            "rows": 0,
            "symbols_with_rows": 0,
            "eligible_universe": len(current),
            "duplicate_rows": 0,
            "wrong_date_rows": 0,
            "bad_ohlc_rows": [],
            "nonpositive_close": [],
            "null_close_symbols": [],
            "null_amount_symbols": [],
            "null_preclose_symbols": [],
            "null_suspend_symbols": [],
            "missing_suspended_symbols": missing_suspended,
            "missing_unexpected": missing_unexpected,
        }

    d = df.copy()
    dup = int(d.duplicated(["symbol", "trade_date"]).sum())
    bad_date = int((d["trade_date"] != target).sum())

    for c in ["open", "high", "low", "close", "amount", "pre_close", "suspend_flag"]:
        d[c] = pd.to_numeric(d[c], errors="coerce")

    valid4 = d[["open", "high", "low", "close"]].notna().all(axis=1)
    bad_ohlc = d.loc[
        valid4 &
        (
            (d["high"] < d[["open", "low", "close"]].max(axis=1)) |
            (d["low"] > d[["open", "high", "close"]].min(axis=1))
        ),
        ["symbol", "trade_date", "open", "high", "low", "close"]
    ].to_dict("records")

    nonpositive = d.loc[d["close"].notna() & (d["close"] <= 0), ["symbol", "close"]].to_dict("records")
    null_close = d.loc[d["close"].isna(), "symbol"].astype(str).tolist()
    null_amount = d.loc[d["amount"].isna(), "symbol"].astype(str).tolist()
    null_preclose = d.loc[d["pre_close"].isna(), "symbol"].astype(str).tolist()
    null_suspend = d.loc[d["suspend_flag"].isna(), "symbol"].astype(str).tolist()

    ok = (
        dup == 0 and bad_date == 0 and not bad_ohlc and not nonpositive
        and not null_close and not null_amount and not null_preclose and not null_suspend
        and not missing_unexpected
    )
    return {
        "ok": ok,
        "rows": int(len(d)),
        "symbols_with_rows": int(d["symbol"].nunique()),
        "eligible_universe": len(current),
        "duplicate_rows": dup,
        "wrong_date_rows": bad_date,
        "bad_ohlc_rows": bad_ohlc,
        "nonpositive_close": nonpositive,
        "null_close_symbols": null_close,
        "null_amount_symbols": null_amount,
        "null_preclose_symbols": null_preclose,
        "null_suspend_symbols": null_suspend,
        "missing_suspended_symbols": missing_suspended,
        "missing_unexpected": missing_unexpected,
    }


def df_to_parquet(df: pd.DataFrame, path: Path, select_sql: str) -> int:
    import duckdb
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    con = duckdb.connect(":memory:")
    try:
        con.register("src_df", df)
        p = str(tmp).replace("\\", "/").replace("'", "''")
        con.execute(f"COPY ({select_sql}) TO '{p}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        n = int(con.execute(f"SELECT count(*) FROM read_parquet('{p}')").fetchone()[0])
    finally:
        con.close()
    os.replace(tmp, path)
    return n


def write_session(stage: Path, target: str, observed_at: str,
                  status_rows: List[Dict[str, Any]], current: List[Dict[str, Any]],
                  daily: pd.DataFrame, audit: Dict[str, Any]) -> Dict[str, Any]:
    stage.mkdir(parents=True, exist_ok=False)

    daily_df = daily.copy()
    daily_df["trade_date_dt"] = pd.to_datetime(daily_df["trade_date"], format="%Y%m%d")
    daily_df["ingested_at_dt"] = pd.to_datetime(daily_df["ingested_at"])

    status_df = pd.DataFrame(status_rows)
    status_df["trade_date_dt"] = pd.to_datetime(status_df["trade_date"], format="%Y%m%d")
    status_df["observed_at_dt"] = pd.to_datetime(status_df["observed_at"])
    status_df["open_date_dt"] = pd.to_datetime(status_df["open_date"], format="%Y%m%d", errors="coerce")
    status_df["expire_date_dt"] = pd.to_datetime(status_df["expire_date"], format="%Y%m%d", errors="coerce")

    current_df = pd.DataFrame(current)
    current_df["trade_date_dt"] = pd.to_datetime(current_df["trade_date"], format="%Y%m%d")
    current_df["observed_at_dt"] = pd.to_datetime(current_df["observed_at"])
    current_df["open_date_dt"] = pd.to_datetime(current_df["open_date"], format="%Y%m%d", errors="coerce")
    current_df["expire_date_dt"] = pd.to_datetime(current_df["expire_date"], format="%Y%m%d", errors="coerce")

    daily_path = stage / "daily_bars.parquet"
    status_path = stage / "universe_status.parquet"
    current_path = stage / "current_universe.parquet"

    daily_n = df_to_parquet(
        daily_df, daily_path,
        """
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
        ORDER BY symbol
        """
    )

    universe_select = """
        SELECT
          CAST(trade_date_dt AS DATE) AS trade_date,
          CAST(observed_at_dt AS TIMESTAMP) AS observed_at,
          CAST(symbol AS VARCHAR) AS symbol,
          CAST("name" AS VARCHAR) AS "name",
          CAST(board AS VARCHAR) AS board,
          CAST(exchange_id AS VARCHAR) AS exchange_id,
          CAST(exchange_code AS VARCHAR) AS exchange_code,
          CAST(instrument_id AS VARCHAR) AS instrument_id,
          CAST(unified_code AS VARCHAR) AS unified_code,
          CAST(open_date_dt AS DATE) AS open_date,
          CAST(expire_date_dt AS DATE) AS expire_date,
          CAST(instrument_type_stock AS BOOLEAN) AS instrument_type_stock,
          CAST(instrument_true_types AS VARCHAR) AS instrument_true_types,
          CAST(instrument_status AS INTEGER) AS instrument_status,
          CAST(is_st AS BOOLEAN) AS is_st,
          CAST(eligible_current AS BOOLEAN) AS eligible_current,
          CAST(eligibility_reason AS VARCHAR) AS eligibility_reason,
          CAST(detail_error AS VARCHAR) AS detail_error,
          CAST(source AS VARCHAR) AS source
        FROM src_df
        ORDER BY symbol
    """
    status_n = df_to_parquet(status_df, status_path, universe_select)
    current_n = df_to_parquet(current_df, current_path, universe_select)

    files = {}
    for p in [daily_path, status_path, current_path]:
        files[p.name] = {
            "size_bytes": p.stat().st_size,
            "sha256": sha256_file(p),
        }

    manifest = {
        "version": "1.0",
        "trade_date": target,
        "observed_at": observed_at,
        "source": SOURCE,
        "daily_acquisition_method": DAILY_ACQUISITION_METHOD,
        "base_as_of": BASE_AS_OF,
        "rows": {
            "daily_bars": daily_n,
            "universe_status": status_n,
            "current_universe": current_n,
        },
        "audit": audit,
        "files": files,
        "forward_pit": True,
        "retro_reconstruction": False,
    }
    json_write_atomic(stage / "manifest.json", manifest)
    return manifest


def verify_session_dir(session: Path) -> Dict[str, Any]:
    mf = session / "manifest.json"
    if not mf.exists():
        raise RuntimeError(f"Session manifest missing: {session}")
    m = json.loads(mf.read_text(encoding="utf-8"))
    for name, info in m["files"].items():
        p = session / name
        if not p.exists():
            raise RuntimeError(f"Session file missing: {p}")
        if p.stat().st_size != int(info["size_bytes"]):
            raise RuntimeError(f"Session size mismatch: {p}")
        if sha256_file(p) != info["sha256"]:
            raise RuntimeError(f"Session hash mismatch: {p}")
    if not m["audit"]["ok"]:
        raise RuntimeError("Committed session audit is not ok.")
    return m


def backup_catalog() -> Optional[Path]:
    if not CATALOG.exists():
        return None
    CATALOG_BACKUP.mkdir(parents=True, exist_ok=True)
    dst = CATALOG_BACKUP / f"market_data_before_increment_{datetime.now().strftime('%Y%m%d_%H%M%S')}.duckdb"
    shutil.copy2(CATALOG, dst)
    return dst


def update_catalog() -> Dict[str, Any]:
    import duckdb

    backup = backup_catalog()
    base_glob = str((BASE_DAILY / "year=*" / "part-*.parquet")).replace("\\", "/").replace("'", "''")
    incr_glob = str((SESSIONS / "trade_date=*" / "daily_bars.parquet")).replace("\\", "/").replace("'", "''")
    status_glob = str((SESSIONS / "trade_date=*" / "universe_status.parquet")).replace("\\", "/").replace("'", "''")
    current_glob = str((SESSIONS / "trade_date=*" / "current_universe.parquet")).replace("\\", "/").replace("'", "''")
    base_master = str(BASE_MASTER).replace("\\", "/").replace("'", "''")

    con = duckdb.connect(str(CATALOG))
    try:
        con.execute("BEGIN TRANSACTION")
        con.execute("CREATE SCHEMA IF NOT EXISTS market")

        con.execute(
            f"""CREATE OR REPLACE VIEW market.daily_bars_base_v1 AS
                SELECT * FROM read_parquet('{base_glob}', union_by_name=true, hive_partitioning=0)"""
        )
        con.execute(
            f"""CREATE OR REPLACE VIEW market.daily_bars_incremental_v1 AS
                SELECT * FROM read_parquet('{incr_glob}', union_by_name=true, hive_partitioning=0)"""
        )
        con.execute(
            """CREATE OR REPLACE VIEW market.daily_bars_v1 AS
               SELECT * FROM market.daily_bars_base_v1
               UNION ALL
               SELECT * FROM market.daily_bars_incremental_v1"""
        )

        con.execute(
            f"""CREATE OR REPLACE VIEW market.universe_status_forward_v1 AS
                SELECT * FROM read_parquet('{status_glob}', union_by_name=true, hive_partitioning=0)"""
        )
        con.execute(
            f"""CREATE OR REPLACE VIEW market.current_universe_history_v1 AS
                SELECT * FROM read_parquet('{current_glob}', union_by_name=true, hive_partitioning=0)"""
        )
        con.execute(
            """CREATE OR REPLACE VIEW market.current_universe_v1 AS
               SELECT *
               FROM market.current_universe_history_v1
               WHERE trade_date = (SELECT max(trade_date) FROM market.current_universe_history_v1)"""
        )

        con.execute(
            f"""CREATE OR REPLACE VIEW market.security_master_base_v1 AS
                SELECT * FROM read_parquet('{base_master}')"""
        )
        con.execute(
            """CREATE OR REPLACE VIEW market.security_master_v1 AS
               WITH latest_current AS (
                 SELECT *
                 FROM (
                   SELECT *,
                          row_number() OVER (PARTITION BY symbol ORDER BY trade_date DESC, observed_at DESC) AS rn
                   FROM market.current_universe_history_v1
                 )
                 WHERE rn=1
               )
               SELECT * FROM market.security_master_base_v1
               UNION ALL
               SELECT
                 l.symbol,
                 l.name,
                 l.board,
                 l.exchange_id,
                 l.exchange_code,
                 l.instrument_id,
                 l.unified_code,
                 l.open_date,
                 l.expire_date,
                 'CURRENT_ELIGIBLE' AS scope_role,
                 TRUE AS research_trade_eligible_current,
                 TRUE AS instrument_type_stock,
                 'QMT_FORWARD_CURRENT' AS source,
                 l.trade_date AS as_of
               FROM latest_current l
               WHERE NOT EXISTS (
                 SELECT 1 FROM market.security_master_base_v1 b WHERE b.symbol=l.symbol
               )"""
        )

        counts = {
            "daily_rows": int(con.execute("SELECT count(*) FROM market.daily_bars_v1").fetchone()[0]),
            "daily_symbols": int(con.execute("SELECT count(DISTINCT symbol) FROM market.daily_bars_v1").fetchone()[0]),
            "latest_date": safe(con.execute("SELECT max(trade_date) FROM market.daily_bars_v1").fetchone()[0]),
            "current_universe": int(con.execute("SELECT count(*) FROM market.current_universe_v1").fetchone()[0]),
            "forward_status_rows": int(con.execute("SELECT count(*) FROM market.universe_status_forward_v1").fetchone()[0]),
        }
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        con.close()
        if backup and backup.exists():
            shutil.copy2(backup, CATALOG)
        raise
    finally:
        try:
            con.close()
        except Exception:
            pass

    return {"backup": str(backup) if backup else None, **counts}


def commit_state(st: Dict[str, Any], target: str, session_manifest: Dict[str, Any], catalog_result: Dict[str, Any]) -> None:
    st["last_processed_date"] = target
    st["updated_at"] = datetime.now().isoformat(timespec="seconds")
    st["sessions"] = [x for x in st.get("sessions", []) if x.get("trade_date") != target]
    st["sessions"].append({
        "trade_date": target,
        "observed_at": session_manifest["observed_at"],
        "daily_rows": session_manifest["rows"]["daily_bars"],
        "current_universe": session_manifest["rows"]["current_universe"],
        "catalog_daily_rows": catalog_result["daily_rows"],
        "catalog_latest_date": catalog_result["latest_date"],
    })
    json_write_atomic(STATE, st)


def process(target: str, xtdata: Any, resume_gap=None) -> int:
    final = SESSIONS / f"trade_date={target}"
    st = load_state()
    if resume_gap is not None:
        if st["last_processed_date"]!=resume_gap["from_last_validated_session"] or target!=resume_gap["resume_date"]:
            raise RuntimeError("RESUME_STATE_CHANGED_NO_WRITE")
        st.setdefault("acknowledged_session_gaps",[]).append(resume_gap)

    if final.exists():
        print(f"SESSION_ALREADY_COMMITTED: {final}")
        session_manifest = verify_session_dir(final)
        catalog_result = update_catalog()
        commit_state(st, target, session_manifest, catalog_result)
        print(json.dumps({
            "status": "RECONCILED_EXISTING_SESSION",
            "trade_date": target,
            "catalog": catalog_result,
        }, ensure_ascii=False, indent=2))
        return 0

    observed_at = datetime.now().astimezone().isoformat(timespec="seconds")
    capture_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    stage = STAGING / f"{target}_{capture_id}"
    failed = FAILED / f"{target}_{capture_id}"

    status_rows, current = capture_universe(xtdata, target, observed_at)
    print(f"UNIVERSE status={len(status_rows)} eligible_non_st={len(current)}")
    print("EXCLUDED:", dict(Counter(r["eligibility_reason"] for r in status_rows if not r["eligible_current"])))

    daily, missing = collect_daily(xtdata, current, target, observed_at)
    first_audit = validate_daily(daily, current, missing, target)

    daily, missing, retry_evidence = retry_unexpected_missing(
        xtdata, current, target, daily, missing, first_audit
    )
    audit = validate_daily(daily, current, missing, target)
    audit["missing_retry"] = retry_evidence

    print(json.dumps({
        "daily_rows": audit.get("rows"),
        "symbols_with_rows": audit.get("symbols_with_rows"),
        "eligible_universe": audit.get("eligible_universe"),
        "missing_suspended": len(audit.get("missing_suspended_symbols", [])),
        "missing_unexpected": audit.get("missing_unexpected"),
        "missing_retry_status": retry_evidence.get("status"),
        "missing_retry_recovered": retry_evidence.get("recovered_symbols"),
        "bad_ohlc_rows": audit.get("bad_ohlc_rows"),
        "ok": audit.get("ok"),
    }, ensure_ascii=False, indent=2))

    if not audit["ok"]:
        failed.parent.mkdir(parents=True, exist_ok=True)
        # Preserve enough evidence even when Parquet commit is rejected.
        failed.mkdir(parents=True, exist_ok=False)
        json_write_atomic(failed / "audit.json", audit)
        pd.DataFrame(status_rows).to_csv(failed / "universe_status.csv", index=False, encoding="utf-8-sig")
        pd.DataFrame(daily).to_csv(failed / "daily_rows.csv", index=False, encoding="utf-8-sig")
        raise RuntimeError(f"DAILY_INCREMENT_VALIDATION_FAILED; evidence={failed}")

    session_manifest = write_session(stage, target, observed_at, status_rows, current, daily, audit)
    verify_session_dir(stage)

    final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(stage, final)
    print(f"SESSION_COMMITTED: {final}")

    # Re-open after atomic rename.
    session_manifest = verify_session_dir(final)
    catalog_result = update_catalog()
    commit_state(st, target, session_manifest, catalog_result)

    print(json.dumps({
        "status": "SUCCESS",
        "trade_date": target,
        "session": str(final),
        "daily_rows": session_manifest["rows"]["daily_bars"],
        "universe_status": session_manifest["rows"]["universe_status"],
        "current_universe": session_manifest["rows"]["current_universe"],
        "missing_suspended": len(session_manifest["audit"]["missing_suspended_symbols"]),
        "catalog": catalog_result,
    }, ensure_ascii=False, indent=2))
    return 0


def check() -> int:
    ensure_foundation()
    from core.qmt_runtime import get_xtdata
    xtdata = get_xtdata()
    try:
        xtdata.enable_hello = False
    except Exception:
        pass
    plan = resolve_plan(xtdata)
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


def status() -> int:
    ensure_foundation()
    st = load_state()
    result = {"state": st, "sessions_dir": str(SESSIONS), "catalog": str(CATALOG)}
    if CATALOG.exists():
        import duckdb
        con = duckdb.connect(str(CATALOG), read_only=True)
        try:
            result["catalog_daily"] = {
                "rows": int(con.execute("SELECT count(*) FROM market.daily_bars_v1").fetchone()[0]),
                "symbols": int(con.execute("SELECT count(DISTINCT symbol) FROM market.daily_bars_v1").fetchone()[0]),
                "max_date": safe(con.execute("SELECT max(trade_date) FROM market.daily_bars_v1").fetchone()[0]),
            }
        finally:
            con.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def self_test() -> int:
    assert board_of("600000.SH") == "SSE_MAIN"
    assert board_of("688981.SH") == "STAR"
    assert board_of("000001.SZ") == "SZSE_MAIN"
    assert board_of("300750.SZ") == "CHINEXT"
    assert board_of("900901.SH") == "UNSUPPORTED"
    assert board_of("920000.BJ") == "BSE"
    assert is_st_name("*ST测试")
    assert not is_st_name("贵州茅台")
    assert norm_date("2026-09-28") == "20260928"
    assert MISSING_RETRY_DELAYS_SECONDS == (5, 30, 90)
    assert INITIAL_MISSING_DOWNLOAD_CHUNK_SIZE == 50
    assert DAILY_ACQUISITION_METHOD == "QMT_GET_FULL_TICK_POSTCLOSE_DAILY_V1_1_3"

    _base = pd.DataFrame([{"symbol": "A.SH", "trade_date": "20260929", "close": 1.0}])
    _patch = pd.DataFrame([
        {"symbol": "A.SH", "trade_date": "20260929", "close": 2.0},
        {"symbol": "B.SH", "trade_date": "20260929", "close": 3.0},
    ])
    _merged = merge_daily_rows(_base, _patch)
    assert len(_merged) == 2
    assert float(_merged.loc[_merged["symbol"] == "A.SH", "close"].iloc[0]) == 2.0

    _current = [
        {"symbol": "A.SH", "instrument_status": 0, "name": "A"},
        {"symbol": "B.SH", "instrument_status": 1, "name": "B"},
    ]
    _suspended, _unexpected = classify_missing(_current, ["A.SH", "B.SH"])
    assert _suspended == ["B.SH"]
    assert [x["symbol"] for x in _unexpected] == ["A.SH"]

    print("SELF_TEST_OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="PositionX A-share Daily V1.1.3 incremental updater")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("self-test")
    sub.add_parser("check")
    sub.add_parser("status")
    pr = sub.add_parser("run")
    pr.add_argument("--target-date", help="YYYYMMDD; forward PIT capture is still today-only")

    a = ap.parse_args()

    if a.cmd == "self-test":
        return self_test()
    if a.cmd == "check":
        return check()
    if a.cmd == "status":
        return status()

    ensure_foundation()
    from core.qmt_runtime import get_xtdata
    xtdata = get_xtdata()
    try:
        xtdata.enable_hello = False
    except Exception:
        pass

    plan = resolve_plan(xtdata, a.target_date)
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    if plan["action"] == "NO_NEW_SESSION":
        print("NO_NEW_SESSION")
        return 0

    return process(plan["target_date"], xtdata, plan.get("resume_gap"))


if __name__ == "__main__":
    raise SystemExit(main())
