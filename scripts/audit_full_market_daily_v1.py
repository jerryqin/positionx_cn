from __future__ import annotations

"""
PositionX — Full Market Daily Data V1 / Phase A Read-Only Audit

Purpose
-------
Audit, without downloading or mutating QMT data:
1) current A-share security universe / security-master feasibility;
2) locally available expired/delisted-contract cache;
3) XtData historical daily-bar API capability;
4) current local 1d cache breadth/freshness;
5) representative local full-history depth.

Safety
------
- Does NOT call download_* APIs.
- Does NOT open PositionX production SQLite.
- Does NOT write PositionX research DuckDB.
- Does NOT use broker / xttrader.
- Writes only CSV/JSON/MD audit artifacts under --output-root.
"""

import argparse
import csv
import inspect
import json
import math
import re
import sys
import traceback
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "data" / "research" / "full_market_daily_v1_audit"

A_SHARE_CODE_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$", re.I)

PREFERRED_UMBRELLA_SECTORS = [
    "沪深京A股",
    "沪深A股",
    "全部A股",
    "A股",
]
PREFERRED_COMPONENT_SECTORS = [
    "上证A股",
    "沪A",
    "深证A股",
    "深A",
    "北证A股",
    "北交所A股",
    "京A股",
    "京A",
]

RECENT_FIELDS = [
    "open", "high", "low", "close",
    "volume", "amount", "preClose", "suspendFlag",
]

CAPABILITY_NAMES = [
    "get_sector_list",
    "get_stock_list_in_sector",
    "get_instrument_detail",
    "get_instrument_type",
    "get_local_data",
    "get_market_data_ex",
    "get_trading_dates",
    "get_divid_factors",
    "download_history_data",
    "download_history_data2",
    "download_history_contracts",
]


def norm_symbol(x: Any) -> str:
    return str(x or "").strip().upper()


def looks_like_a_share_symbol(symbol: str) -> bool:
    return bool(A_SHARE_CODE_RE.match(norm_symbol(symbol)))


def board_of(symbol: str) -> str:
    s = norm_symbol(symbol)
    code, _, market = s.partition(".")
    if market == "BJ":
        return "BSE"
    if market == "SH":
        if code.startswith(("688", "689")):
            return "STAR"
        if code.startswith(("600", "601", "603", "605")):
            return "SSE_MAIN"
        return "SSE_OTHER"
    if market == "SZ":
        if code.startswith(("300", "301")):
            return "CHINEXT"
        if code.startswith(("000", "001", "002", "003")):
            return "SZSE_MAIN"
        return "SZSE_OTHER"
    return "OTHER"


def safe_jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        if isinstance(v, float) and not math.isfinite(v):
            return None
        return v
    if hasattr(v, "item"):
        try:
            return safe_jsonable(v.item())
        except Exception:
            pass
    return str(v)


def normalize_date(v: Any) -> Optional[str]:
    """Best-effort normalize QMT timestamps / yyyymmdd / datetime-like to YYYYMMDD."""
    if v is None:
        return None
    if hasattr(v, "to_pydatetime"):
        try:
            v = v.to_pydatetime()
        except Exception:
            pass
    if isinstance(v, datetime):
        return v.strftime("%Y%m%d")
    s = str(v).strip()
    if not s or s.lower() in {"nan", "none", "nat"}:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 8 and digits[:4].isdigit():
        y = int(digits[:4])
        if 1990 <= y <= 2200:
            return digits[:8]
    try:
        x = float(s)
        if x > 1e12:
            x /= 1000.0
        if x > 1e9:
            return datetime.fromtimestamp(x).strftime("%Y%m%d")
    except Exception:
        pass
    return None


def normalize_contract_date(v: Any) -> Optional[str]:
    if v in (None, "", 0, "0"):
        return None
    d = normalize_date(v)
    return d


def function_signature(obj: Any, name: str) -> Optional[str]:
    f = getattr(obj, name, None)
    if f is None:
        return None
    try:
        return str(inspect.signature(f))
    except Exception:
        return "<available; signature unavailable>"


def csv_write(path: Path, rows: Sequence[Dict[str, Any]], fieldnames: Optional[Sequence[str]] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        keys: List[str] = []
        seen = set()
        for row in rows:
            for k in row:
                if k not in seen:
                    seen.add(k)
                    keys.append(k)
        fieldnames = keys or ["empty"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(fieldnames), extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: safe_jsonable(row.get(k)) for k in fieldnames})


def json_write(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=safe_jsonable), encoding="utf-8")


def get_sector_members(xtdata: Any, name: str) -> Tuple[List[str], Optional[str]]:
    try:
        vals = xtdata.get_stock_list_in_sector(name)
        vals = [norm_symbol(x) for x in (vals or [])]
        vals = sorted({x for x in vals if x})
        return vals, None
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


def is_a_share_sector_name(name: str) -> bool:
    n = str(name)
    if n in PREFERRED_UMBRELLA_SECTORS or n in PREFERRED_COMPONENT_SECTORS:
        return True
    if "A股" not in n:
        return False
    bad = ("港", "美", "H股", "B股", "ADR")
    if any(x in n for x in bad):
        return False
    # Keep this broad only as a fallback discovery signal.
    return True


def choose_current_universe(xtdata: Any, sectors: List[str]) -> Tuple[List[str], List[Dict[str, Any]], str]:
    """
    Prefer a known umbrella sector. If absent, union known A-share component sectors.
    If still too small, union all locally available sector names that look A-share-like.
    """
    sector_rows: List[Dict[str, Any]] = []
    by_name = set(sectors)

    # 1) Strong umbrella candidate.
    for name in PREFERRED_UMBRELLA_SECTORS:
        if name not in by_name:
            continue
        members, err = get_sector_members(xtdata, name)
        filtered = [x for x in members if looks_like_a_share_symbol(x)]
        sector_rows.append({
            "sector": name, "role": "umbrella_candidate",
            "raw_members": len(members), "a_share_like_members": len(filtered),
            "error": err,
        })
        if len(filtered) >= 4500:
            # If umbrella excludes BJ, add a local BJ A-share sector if one is available.
            if name == "沪深A股":
                bj_union = set()
                for s in sectors:
                    if ("北" in s or "京" in s) and "A股" in s:
                        m, e = get_sector_members(xtdata, s)
                        fm = [x for x in m if x.endswith(".BJ") and looks_like_a_share_symbol(x)]
                        sector_rows.append({
                            "sector": s, "role": "bj_supplement",
                            "raw_members": len(m), "a_share_like_members": len(fm),
                            "error": e,
                        })
                        bj_union.update(fm)
                filtered = sorted(set(filtered) | bj_union)
            return sorted(set(filtered)), sector_rows, f"UMBRELLA:{name}"

    # 2) Known component sectors.
    union: set[str] = set()
    used: List[str] = []
    for name in PREFERRED_COMPONENT_SECTORS:
        if name not in by_name:
            continue
        members, err = get_sector_members(xtdata, name)
        filtered = [x for x in members if looks_like_a_share_symbol(x)]
        sector_rows.append({
            "sector": name, "role": "component_candidate",
            "raw_members": len(members), "a_share_like_members": len(filtered),
            "error": err,
        })
        if filtered:
            used.append(name)
            union.update(filtered)
    if len(union) >= 4500:
        return sorted(union), sector_rows, "COMPONENT_UNION:" + ",".join(used)

    # 3) Broad discovery fallback; exact universe is NOT silently asserted.
    broad_used: List[str] = []
    for name in sectors:
        if not is_a_share_sector_name(name):
            continue
        if any(r["sector"] == name for r in sector_rows):
            continue
        members, err = get_sector_members(xtdata, name)
        filtered = [x for x in members if looks_like_a_share_symbol(x)]
        sector_rows.append({
            "sector": name, "role": "broad_fallback_candidate",
            "raw_members": len(members), "a_share_like_members": len(filtered),
            "error": err,
        })
        if filtered:
            broad_used.append(name)
            union.update(filtered)
    return sorted(union), sector_rows, "BROAD_FALLBACK:" + ",".join(broad_used)


def get_stock_type(xtdata: Any, symbol: str) -> Tuple[Optional[bool], Optional[str], Dict[str, Any]]:
    try:
        t = xtdata.get_instrument_type(symbol)
        if not isinstance(t, dict):
            return None, None, {}
        stock = t.get("stock")
        true_types = sorted(str(k) for k, v in t.items() if bool(v))
        return (bool(stock) if stock is not None else None), ";".join(true_types), t
    except Exception as e:
        return None, f"ERROR:{type(e).__name__}:{e}", {}


def build_current_master(xtdata: Any, symbols: List[str], progress_every: int = 250) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    n = len(symbols)
    for i, symbol in enumerate(symbols, 1):
        detail = None
        detail_err = None
        try:
            detail = xtdata.get_instrument_detail(symbol, False)
        except TypeError:
            try:
                detail = xtdata.get_instrument_detail(symbol)
            except Exception as e:
                detail_err = f"{type(e).__name__}: {e}"
        except Exception as e:
            detail_err = f"{type(e).__name__}: {e}"
        detail = detail if isinstance(detail, dict) else {}

        stock_flag, true_types, _ = get_stock_type(xtdata, symbol)
        rows.append({
            "symbol": symbol,
            "board": board_of(symbol),
            "instrument_type_stock": stock_flag,
            "instrument_true_types": true_types,
            "name": detail.get("InstrumentName"),
            "exchange_id": detail.get("ExchangeID"),
            "exchange_code": detail.get("ExchangeCode"),
            "instrument_id": detail.get("InstrumentID"),
            "unified_code": detail.get("UniCode"),
            "open_date": normalize_contract_date(detail.get("OpenDate")),
            "expire_date": normalize_contract_date(detail.get("ExpireDate")),
            "is_trading": detail.get("IsTrading"),
            "instrument_status": detail.get("InstrumentStatus"),
            "float_volume": detail.get("FloatVolume"),
            "total_volume": detail.get("TotalVolume"),
            "pre_close": detail.get("PreClose"),
            "up_stop_price": detail.get("UpStopPrice"),
            "down_stop_price": detail.get("DownStopPrice"),
            "detail_found": bool(detail),
            "detail_error": detail_err,
        })
        if progress_every and (i == 1 or i % progress_every == 0 or i == n):
            print(f"[SECURITY MASTER] {i}/{n} {symbol}")
    return rows


def expired_sector_audit(xtdata: Any, sectors: List[str]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    names = [s for s in sectors if ("过期" in s or "退市" in s)]
    sector_rows: List[Dict[str, Any]] = []
    members_rows: List[Dict[str, Any]] = []
    seen = set()
    for name in names:
        members, err = get_sector_members(xtdata, name)
        a = [x for x in members if looks_like_a_share_symbol(x)]
        sector_rows.append({
            "sector": name,
            "raw_members": len(members),
            "a_share_like_members": len(a),
            "error": err,
        })
        for s in a:
            key = (name, s)
            if key not in seen:
                seen.add(key)
                members_rows.append({"sector": name, "symbol": s, "board": board_of(s)})
    return sector_rows, members_rows


def local_data_call(xtdata: Any, fields: List[str], symbols: List[str], count: int) -> Any:
    try:
        return xtdata.get_local_data(
            field_list=fields,
            stock_list=symbols,
            period="1d",
            start_time="",
            end_time="",
            count=count,
            dividend_type="none",
            fill_data=False,
        )
    except TypeError:
        # Compatibility with older positional-only builds.
        return xtdata.get_local_data(fields, symbols, "1d", "", "", count, "none", False)


def series_for(raw: Any, field: str, symbol: str):
    """
    Supports documented get_local_data shape {field: DataFrame(stock x time)}
    and a defensive alternate {symbol: DataFrame(time x fields)}.
    """
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


def nonnull_items(series: Any) -> List[Tuple[Any, Any]]:
    if series is None:
        return []
    try:
        s = series.dropna()
        return list(s.items())
    except Exception:
        pass
    if isinstance(series, dict):
        return [(k, v) for k, v in series.items() if v is not None]
    if isinstance(series, (list, tuple)):
        return list(enumerate([v for v in series if v is not None]))
    return []


def recent_cache_audit(
    xtdata: Any,
    symbols: List[str],
    expected_by_market: Dict[str, Optional[str]],
    chunk_size: int = 200,
    count: int = 5,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    rows: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    n = len(symbols)
    for start in range(0, n, chunk_size):
        batch = symbols[start:start + chunk_size]
        try:
            raw = local_data_call(xtdata, RECENT_FIELDS, batch, count)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            errors.append({"batch_start": start, "batch_size": len(batch), "error": err})
            for symbol in batch:
                rows.append({
                    "symbol": symbol,
                    "board": board_of(symbol),
                    "latest_date": None,
                    "recent_close_bars": 0,
                    "expected_latest": expected_by_market.get(symbol.split(".")[-1]),
                    "latest_is_expected": False,
                    "read_error": err,
                })
            continue

        for symbol in batch:
            close_items = nonnull_items(series_for(raw, "close", symbol))
            latest_date = normalize_date(close_items[-1][0]) if close_items else None
            market = symbol.split(".")[-1]
            expected = expected_by_market.get(market)
            row = {
                "symbol": symbol,
                "board": board_of(symbol),
                "latest_date": latest_date,
                "recent_close_bars": len(close_items),
                "expected_latest": expected,
                "latest_is_expected": bool(latest_date and expected and latest_date == expected),
                "read_error": None,
            }
            for f in RECENT_FIELDS:
                items = nonnull_items(series_for(raw, f, symbol))
                row[f"has_{f}"] = bool(items)
                row[f"{f}_nonnull_recent"] = len(items)
            rows.append(row)
        done = min(start + len(batch), n)
        print(f"[LOCAL CACHE RECENT] {done}/{n}")
    return rows, errors


def get_calendar_latest(xtdata: Any, market: str) -> Optional[str]:
    today = datetime.now().strftime("%Y%m%d")
    try:
        vals = xtdata.get_trading_dates(market, start_time="", end_time=today, count=1)
    except TypeError:
        try:
            vals = xtdata.get_trading_dates(market, "", today, 1)
        except Exception:
            return None
    except Exception:
        return None
    if not vals:
        return None
    return normalize_date(vals[-1])


def select_deep_sample(master_rows: List[Dict[str, Any]], max_n: int = 64) -> List[str]:
    by_symbol = {r["symbol"]: r for r in master_rows}
    chosen: List[str] = []

    anchors = [
        "000001.SZ", "000002.SZ", "000333.SZ", "002594.SZ",
        "300750.SZ", "600000.SH", "600519.SH", "601318.SH",
        "688981.SH",
    ]
    for s in anchors:
        if s in by_symbol and s not in chosen:
            chosen.append(s)

    dated = [r for r in master_rows if r.get("open_date")]
    dated.sort(key=lambda r: (r["open_date"], r["symbol"]))
    for r in dated[:12]:
        if r["symbol"] not in chosen:
            chosen.append(r["symbol"])
    for r in reversed(dated[-12:]):
        if r["symbol"] not in chosen:
            chosen.append(r["symbol"])

    groups: Dict[str, List[str]] = defaultdict(list)
    for r in master_rows:
        groups[str(r.get("board") or "OTHER")].append(r["symbol"])
    for board in sorted(groups):
        vals = sorted(groups[board])
        picks = vals[:4] + vals[-4:]
        for s in picks:
            if s not in chosen:
                chosen.append(s)

    return chosen[:max_n]


def deep_history_audit(xtdata: Any, symbols: List[str]) -> List[Dict[str, Any]]:
    rows = []
    for i, symbol in enumerate(symbols, 1):
        err = None
        try:
            raw = local_data_call(xtdata, ["close", "amount", "volume"], [symbol], -1)
            close_items = nonnull_items(series_for(raw, "close", symbol))
            amount_items = nonnull_items(series_for(raw, "amount", symbol))
            volume_items = nonnull_items(series_for(raw, "volume", symbol))
            dates = [normalize_date(k) for k, _ in close_items]
            dates = [d for d in dates if d]
            rows.append({
                "symbol": symbol,
                "board": board_of(symbol),
                "bars_close": len(close_items),
                "bars_amount": len(amount_items),
                "bars_volume": len(volume_items),
                "first_local_date": min(dates) if dates else None,
                "last_local_date": max(dates) if dates else None,
                "read_error": None,
            })
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            rows.append({
                "symbol": symbol,
                "board": board_of(symbol),
                "bars_close": 0,
                "bars_amount": 0,
                "bars_volume": 0,
                "first_local_date": None,
                "last_local_date": None,
                "read_error": err,
            })
        print(f"[LOCAL CACHE DEEP SAMPLE] {i}/{len(symbols)} {symbol}")
    return rows


def pct(n: int, d: int) -> Optional[float]:
    return (n / d) if d else None


def markdown_report(summary: Dict[str, Any]) -> str:
    s = summary
    cap = s["api_capabilities"]
    lines = [
        "# PositionX · Full Market Daily Data V1 — Phase A Read-Only Audit",
        "",
        f"- Generated: `{s['generated_at']}`",
        f"- Safety mode: **READ_ONLY_QMT_AUDIT**",
        f"- Current universe source: `{s['current_universe']['source']}`",
        f"- Current A-share-like symbols: **{s['current_universe']['count']}**",
        f"- Instrument type verified as stock: **{s['security_master']['stock_verified']} / {s['current_universe']['count']}**",
        "",
        "## 1. Verdict",
        "",
        f"- Current-universe discovery: **{s['verdict']['current_universe']}**",
        f"- Local recent daily cache: **{s['verdict']['recent_cache']}**",
        f"- Expired/delisted-contract cache: **{s['verdict']['expired_contract_cache']}**",
        f"- Historical daily API surface: **{s['verdict']['historical_api']}**",
        f"- Overall: **{s['verdict']['overall']}**",
        "",
        "## 2. Current local cache",
        "",
        f"- Symbols with any recent close: {s['local_cache']['symbols_with_recent_close']}",
        f"- Symbols at market calendar latest: {s['local_cache']['symbols_at_expected_latest']}",
        f"- Fresh ratio: {s['local_cache']['fresh_ratio']}",
        f"- Latest-date distribution: `{json.dumps(s['local_cache']['latest_date_distribution'], ensure_ascii=False)}`",
        "",
        "## 3. Historical / delisted master",
        "",
        f"- Local sector names containing `过期`/`退市`: {s['expired_contracts']['sector_count']}",
        f"- A-share-like expired-sector members found: {s['expired_contracts']['member_count']}",
        "- This audit intentionally did **not** call `download_history_contracts()`.",
        "",
        "## 4. XtData capability surface",
        "",
    ]
    for name, v in cap.items():
        lines.append(f"- `{name}`: {'YES' if v['available'] else 'NO'} `{v.get('signature') or ''}`")
    lines += [
        "",
        "## 5. Safety / mutation statement",
        "",
        "- No `download_*` function was called.",
        "- No broker / xttrader API was imported.",
        "- No production SQLite was opened.",
        "- No research DuckDB was opened or modified.",
        "- Only this audit output directory was written.",
        "",
        "## 6. Next gate",
        "",
        "Do **not** start the 5,200+ symbol bulk download solely from this report.",
        "Use this report to decide whether Phase B should first:",
        "1. load expired/delisted contract metadata;",
        "2. run a controlled small-symbol historical download pilot;",
        "3. then freeze the full-market Parquet schema and batch-download plan.",
        "",
    ]
    return "\n".join(lines)


def run_self_test() -> int:
    assert normalize_date("20260928") == "20260928"
    assert normalize_date("2026-09-28") == "20260928"
    assert looks_like_a_share_symbol("600000.SH")
    assert looks_like_a_share_symbol("920000.BJ")
    assert not looks_like_a_share_symbol("000300.SHX")
    assert board_of("688981.SH") == "STAR"
    assert board_of("300750.SZ") == "CHINEXT"
    assert board_of("600000.SH") == "SSE_MAIN"
    assert board_of("000001.SZ") == "SZSE_MAIN"
    assert board_of("920000.BJ") == "BSE"
    print("SELF_TEST_OK")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="PositionX Full Market Daily V1 Phase-A read-only audit")
    p.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    p.add_argument("--chunk-size", type=int, default=200)
    p.add_argument("--recent-count", type=int, default=5)
    p.add_argument("--deep-sample", type=int, default=64)
    p.add_argument("--max-symbols", type=int, default=0,
                   help="0=all; positive value is for smoke test only")
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()

    if args.self_test:
        return run_self_test()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.output_root) / stamp
    out_dir.mkdir(parents=True, exist_ok=False)

    print("=" * 100)
    print("POSITIONX FULL MARKET DAILY V1 — PHASE A READ-ONLY AUDIT")
    print("=" * 100)
    print(f"OUTPUT: {out_dir}")
    print("SAFETY: no download_*, no broker, no production SQLite, no research DuckDB")
    print()

    try:
        from xtquant import xtdata
    except Exception as e:
        summary = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "fatal": f"xtquant import failed: {type(e).__name__}: {e}",
        }
        json_write(out_dir / "audit_summary.json", summary)
        print(summary["fatal"])
        return 2

    try:
        xtdata.enable_hello = False
    except Exception:
        pass

    # Capability inventory.
    api_capabilities = {}
    for name in CAPABILITY_NAMES:
        api_capabilities[name] = {
            "available": hasattr(xtdata, name),
            "signature": function_signature(xtdata, name),
        }

    # Sector inventory: READ ONLY; do not refresh/download sector data here.
    try:
        sectors = list(xtdata.get_sector_list() or [])
    except Exception as e:
        sectors = []
        sector_error = f"{type(e).__name__}: {e}"
    else:
        sector_error = None

    sector_inventory = [{
        "sector": s,
        "looks_a_share_related": is_a_share_sector_name(s),
        "looks_expired_or_delisted": ("过期" in s or "退市" in s),
    } for s in sectors]
    csv_write(out_dir / "sector_inventory.csv", sector_inventory,
              ["sector", "looks_a_share_related", "looks_expired_or_delisted"])

    symbols, universe_sector_rows, universe_source = choose_current_universe(xtdata, sectors)
    if args.max_symbols > 0:
        symbols = symbols[:args.max_symbols]
        universe_source += f"|SMOKE_LIMIT={args.max_symbols}"
    csv_write(out_dir / "current_universe_sector_probe.csv", universe_sector_rows)

    print(f"CURRENT UNIVERSE: {len(symbols)} symbols via {universe_source}")

    master_rows = build_current_master(xtdata, symbols)
    csv_write(out_dir / "security_master_current.csv", master_rows)

    expired_sector_rows, expired_members_rows = expired_sector_audit(xtdata, sectors)
    csv_write(out_dir / "expired_sector_probe.csv", expired_sector_rows)
    csv_write(out_dir / "expired_sector_members.csv", expired_members_rows,
              ["sector", "symbol", "board"])

    expected_by_market = {
        "SH": get_calendar_latest(xtdata, "SH"),
        "SZ": get_calendar_latest(xtdata, "SZ"),
        "BJ": get_calendar_latest(xtdata, "BJ"),
    }
    if expected_by_market["BJ"] is None:
        # Most A-share trading dates align; keep explicit fallback visible in summary.
        expected_by_market["BJ"] = expected_by_market["SH"]

    cache_rows, cache_errors = recent_cache_audit(
        xtdata, symbols, expected_by_market,
        chunk_size=max(1, args.chunk_size),
        count=max(1, args.recent_count),
    )
    csv_write(out_dir / "local_cache_recent_coverage.csv", cache_rows)
    csv_write(out_dir / "local_cache_batch_errors.csv", cache_errors)

    deep_symbols = select_deep_sample(master_rows, max_n=max(1, args.deep_sample))
    deep_rows = deep_history_audit(xtdata, deep_symbols)
    csv_write(out_dir / "local_cache_deep_history_sample.csv", deep_rows)

    n = len(symbols)
    stock_verified = sum(1 for r in master_rows if r.get("instrument_type_stock") is True)
    detail_found = sum(1 for r in master_rows if r.get("detail_found"))
    with_recent = sum(1 for r in cache_rows if r.get("recent_close_bars", 0) > 0)
    fresh = sum(1 for r in cache_rows if r.get("latest_is_expected"))
    date_dist = Counter(str(r.get("latest_date")) for r in cache_rows)
    deep_nonempty = sum(1 for r in deep_rows if r.get("bars_close", 0) > 0)

    current_universe_verdict = (
        "PASS" if (args.max_symbols == 0 and 4500 <= n <= 7000 and stock_verified >= max(1, int(n * 0.98)))
        else ("SMOKE_ONLY" if args.max_symbols > 0 else "REVIEW")
    )
    fresh_ratio = pct(fresh, n)
    recent_cache_verdict = (
        "PASS" if fresh_ratio is not None and fresh_ratio >= 0.95
        else ("PARTIAL" if with_recent > 0 else "NOT_READY")
    )
    expired_member_count = len({r["symbol"] for r in expired_members_rows})
    expired_verdict = "PRESENT" if expired_member_count > 0 else "NOT_PRESENT_READ_ONLY_AUDIT"
    hist_api_ok = all(api_capabilities[x]["available"] for x in
                      ["get_local_data", "download_history_data", "get_trading_dates"])
    historical_api_verdict = "AVAILABLE_NOT_DOWNLOAD_TESTED" if hist_api_ok else "MISSING_REQUIRED_API"

    overall = "AUDIT_COMPLETE"
    if not sectors or n == 0:
        overall = "AUDIT_INCOMPLETE"
    elif current_universe_verdict == "REVIEW":
        overall = "AUDIT_COMPLETE_REVIEW_UNIVERSE"

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "READ_ONLY_QMT_AUDIT",
        "output_dir": str(out_dir),
        "project_root": str(PROJECT_ROOT),
        "sector_error": sector_error,
        "sector_count": len(sectors),
        "current_universe": {
            "source": universe_source,
            "count": n,
            "board_counts": dict(Counter(r["board"] for r in master_rows)),
        },
        "security_master": {
            "detail_found": detail_found,
            "stock_verified": stock_verified,
            "stock_verified_ratio": pct(stock_verified, n),
        },
        "calendar_latest_by_market": expected_by_market,
        "local_cache": {
            "symbols_with_recent_close": with_recent,
            "symbols_at_expected_latest": fresh,
            "fresh_ratio": fresh_ratio,
            "latest_date_distribution": dict(date_dist.most_common()),
            "batch_errors": len(cache_errors),
            "deep_sample_size": len(deep_rows),
            "deep_sample_nonempty": deep_nonempty,
        },
        "expired_contracts": {
            "sector_count": len(expired_sector_rows),
            "member_rows": len(expired_members_rows),
            "member_count": expired_member_count,
            "download_history_contracts_called": False,
        },
        "api_capabilities": api_capabilities,
        "safety": {
            "download_functions_called": [],
            "broker_imported": False,
            "production_sqlite_opened": False,
            "research_duckdb_opened": False,
            "writes_only_output_dir": True,
        },
        "verdict": {
            "current_universe": current_universe_verdict,
            "recent_cache": recent_cache_verdict,
            "expired_contract_cache": expired_verdict,
            "historical_api": historical_api_verdict,
            "overall": overall,
        },
    }
    json_write(out_dir / "audit_summary.json", summary)
    (out_dir / "audit_report.md").write_text(markdown_report(summary), encoding="utf-8")

    print()
    print("=" * 100)
    print("AUDIT SUMMARY")
    print("=" * 100)
    print(json.dumps(summary["verdict"], ensure_ascii=False, indent=2))
    print(f"current_universe={n}")
    print(f"stock_verified={stock_verified}/{n}")
    print(f"recent_cache={with_recent}/{n}; fresh={fresh}/{n}")
    print(f"expired_a_share_candidates={expired_member_count}")
    print(f"deep_history_sample_nonempty={deep_nonempty}/{len(deep_rows)}")
    print(f"REPORT: {out_dir / 'audit_report.md'}")
    print(f"SUMMARY: {out_dir / 'audit_summary.json'}")
    print()
    print("NEXT: share audit_summary.json + audit_report.md before any full-market download.")
    return 0 if overall != "AUDIT_INCOMPLETE" else 3


if __name__ == "__main__":
    raise SystemExit(main())
