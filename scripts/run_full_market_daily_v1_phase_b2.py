from __future__ import annotations

"""
PositionX — Full Market Daily Data V1 / Phase B2
Representative full-history 1d download pilot.

Frozen scope for this pilot:
- Research/trading current universe: SH + SZ verified stocks only.
- BSE / .BJ: excluded.
- Current ST/*ST/S*ST/SST/NST names: excluded from research/trading universe.
- Expired SH/SZ stocks may be sampled only as ARCHIVE/ANTI-SURVIVORSHIP data-quality
  probes. They are never marked currently trade-eligible.

Important ST rule:
ST status is time-varying. We therefore do NOT permanently erase a symbol's historical
raw bars merely because its current name is ST. Final historical research eligibility
must use PIT ST status by date. This pilot freezes current eligibility only.
"""

import argparse
import csv
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PHASE_A_ROOT = PROJECT_ROOT/"data"/"research"/"full_market_daily_v1_audit"
PHASE_B1_ROOT = PROJECT_ROOT/"data"/"research"/"full_market_daily_v1_phase_b1"
OUTPUT_ROOT = PROJECT_ROOT/"data"/"research"/"full_market_daily_v1_phase_b2"

ST_RE = re.compile(r"^(?:\*?ST|S\*?ST|SST|NST)", re.I)


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


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=safe), encoding="utf-8")


def write_csv(path: Path, rows: Sequence[Dict[str, Any]], fields: Optional[Sequence[str]]=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        keys, seen = [], set()
        for r in rows:
            for k in r:
                if k not in seen:
                    seen.add(k); keys.append(k)
        fields = keys or ["empty"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(fields), extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: safe(r.get(k)) for k in fields})


def read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def latest_dir(root: Path, required: Sequence[str]) -> Path:
    if not root.exists():
        raise RuntimeError(f"Missing root: {root}")
    for d in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p:p.name, reverse=True):
        if all((d/r).exists() for r in required):
            return d
    raise RuntimeError(f"No complete output found under {root}")


def b(v: Any) -> bool:
    return str(v).strip().lower() in {"1","true","yes","y"}


def clean_name(v: Any) -> str:
    return str(v or "").replace(" ", "").replace("　", "").replace("＊", "*").strip()


def is_current_st_name(name: Any) -> bool:
    return bool(ST_RE.match(clean_name(name).upper()))


def current_eligibility(row: Dict[str, str]) -> Tuple[bool, str]:
    if row.get("board") == "BSE" or str(row.get("symbol","")).upper().endswith(".BJ"):
        return False, "EXCLUDE_BSE"
    if not b(row.get("instrument_type_stock")):
        return False, "EXCLUDE_NOT_VERIFIED_STOCK"
    if is_current_st_name(row.get("name")):
        return False, "EXCLUDE_CURRENT_ST"
    if not str(row.get("symbol","")).upper().endswith((".SH",".SZ")):
        return False, "EXCLUDE_OTHER_MARKET"
    return True, "ELIGIBLE_CURRENT_NON_ST_SH_SZ"


def norm_date(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in {"nan","nat","none"}:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 8 and 1990 <= int(digits[:4]) <= 2200:
        return digits[:8]
    try:
        x = float(s)
        if x > 1e12: x /= 1000.0
        if x > 1e9:
            return datetime.fromtimestamp(x).strftime("%Y%m%d")
    except Exception:
        pass
    return None


def local_data_call(xtdata: Any, fields: List[str], symbols: List[str], count: int=-1):
    try:
        return xtdata.get_local_data(
            field_list=fields, stock_list=symbols, period="1d",
            start_time="", end_time="", count=count,
            dividend_type="none", fill_data=False,
        )
    except TypeError:
        return xtdata.get_local_data(fields, symbols, "1d", "", "", count, "none", False)


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
        return {d: safe(v) for k,v in s.items() if (d:=norm_date(k))}
    except Exception:
        pass
    if isinstance(series, dict):
        return {d:safe(v) for k,v in series.items() if v is not None and (d:=norm_date(k))}
    return {}


def pick_current_sample(rows: List[Dict[str, str]], max_current: int=24) -> List[Dict[str, Any]]:
    eligible = []
    for r in rows:
        ok, reason = current_eligibility(r)
        if ok:
            rr = dict(r)
            rr["sample_role"] = "CURRENT_ELIGIBLE"
            rr["eligibility_reason"] = reason
            eligible.append(rr)

    by = defaultdict(list)
    for r in eligible:
        by[r.get("board") or "OTHER"].append(r)

    chosen: List[Dict[str, Any]] = []
    anchors = ["000001.SZ","000333.SZ","002594.SZ","300750.SZ","600000.SH","600519.SH","601318.SH","688981.SH"]
    idx = {r["symbol"]:r for r in eligible}
    for s in anchors:
        if s in idx and idx[s] not in chosen:
            chosen.append(idx[s])

    for board in ("SSE_MAIN","SZSE_MAIN","CHINEXT","STAR"):
        vals = by.get(board, [])
        vals = sorted(vals, key=lambda r: ((r.get("open_date") or "99999999"), r["symbol"]))
        candidates = vals[:3] + list(reversed(vals[-3:]))
        for r in candidates:
            if r not in chosen:
                chosen.append(r)

    return chosen[:max_current]


def pick_expired_sample(rows: List[Dict[str, str]], max_expired: int=6) -> List[Dict[str, Any]]:
    vals = []
    for r in rows:
        sym = str(r.get("symbol","")).upper()
        if not sym.endswith((".SH",".SZ")):
            continue
        if r.get("board") == "BSE":
            continue
        if not b(r.get("instrument_type_stock")):
            continue
        # Current ST label is excluded even from this small pilot when alternatives exist.
        if is_current_st_name(r.get("name")):
            continue
        rr = dict(r)
        rr["sample_role"] = "EXPIRED_ARCHIVE_PROBE"
        rr["eligibility_reason"] = "ARCHIVE_ONLY_NOT_CURRENTLY_TRADE_ELIGIBLE"
        vals.append(rr)
    vals.sort(key=lambda r: ((r.get("expire_date") or "99999999"), (r.get("open_date") or "99999999"), r["symbol"]))
    if len(vals) <= max_expired:
        return vals
    # spread across the list
    picks = []
    idxs = sorted({round(i*(len(vals)-1)/(max_expired-1)) for i in range(max_expired)}) if max_expired > 1 else [0]
    for i in idxs:
        picks.append(vals[i])
    return picks[:max_expired]


def download_one(xtdata: Any, symbol: str, start: str) -> Tuple[bool, Optional[str]]:
    try:
        try:
            xtdata.download_history_data(symbol, "1d", start, "", False)
        except TypeError:
            xtdata.download_history_data(symbol, "1d", start, "")
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def get_dividend_probe(xtdata: Any, symbol: str) -> Dict[str, Any]:
    try:
        v = xtdata.get_divid_factors(symbol, "", "")
        if v is None:
            return {"ok":True, "type":"NoneType", "len":0, "error":None}
        try:
            n = len(v)
        except Exception:
            n = None
        return {"ok":True, "type":type(v).__name__, "len":n, "error":None}
    except Exception as e:
        return {"ok":False, "type":None, "len":None, "error":f"{type(e).__name__}: {e}"}


def collect_symbol_rows(xtdata: Any, symbol: str, fields: List[str]) -> List[Dict[str, Any]]:
    raw = local_data_call(xtdata, fields, [symbol], -1)
    maps = {f:item_map(series_for(raw, f, symbol)) for f in fields}
    dates = sorted(set().union(*(set(m) for m in maps.values())))
    rows = []
    for d in dates:
        r = {"symbol":symbol, "trade_date":d}
        for f in fields:
            r[f] = maps[f].get(d)
        rows.append(r)
    return rows


def validate_rows(symbol: str, rows: List[Dict[str, Any]], meta: Dict[str, Any]) -> Dict[str, Any]:
    dates = [r["trade_date"] for r in rows]
    close_nonnull = sum(r.get("close") is not None for r in rows)
    amount_nonnull = sum(r.get("amount") is not None for r in rows)
    pre_nonnull = sum(r.get("preClose") is not None for r in rows)
    susp_nonnull = sum(r.get("suspendFlag") is not None for r in rows)
    bad_ohlc = 0
    zero_or_neg_close = 0
    for r in rows:
        try:
            o,h,l,c = map(float, [r.get("open"),r.get("high"),r.get("low"),r.get("close")])
            if c <= 0: zero_or_neg_close += 1
            if h < max(o,c,l) or l > min(o,c,h):
                bad_ohlc += 1
        except Exception:
            pass
    dup = len(dates) - len(set(dates))
    return {
        "symbol": symbol,
        "sample_role": meta.get("sample_role"),
        "board": meta.get("board"),
        "name": meta.get("name"),
        "open_date_master": meta.get("open_date"),
        "expire_date_master": meta.get("expire_date"),
        "bars": len(rows),
        "first_date": min(dates) if dates else None,
        "last_date": max(dates) if dates else None,
        "duplicate_dates": dup,
        "close_nonnull": close_nonnull,
        "amount_nonnull": amount_nonnull,
        "preclose_nonnull": pre_nonnull,
        "suspendflag_nonnull": susp_nonnull,
        "amount_coverage": amount_nonnull/len(rows) if rows else 0.0,
        "preclose_coverage": pre_nonnull/len(rows) if rows else 0.0,
        "suspendflag_coverage": susp_nonnull/len(rows) if rows else 0.0,
        "bad_ohlc_rows": bad_ohlc,
        "zero_or_negative_close_rows": zero_or_neg_close,
    }


def write_parquet_with_duckdb(df: pd.DataFrame, path: Path) -> Dict[str, Any]:
    try:
        import duckdb
        con = duckdb.connect(database=":memory:")
        try:
            con.register("pilot_df", df)
            escaped = str(path).replace("'", "''")
            con.execute(f"COPY pilot_df TO '{escaped}' (FORMAT PARQUET, COMPRESSION ZSTD)")
            n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{escaped}')").fetchone()[0]
            schema = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{escaped}')").fetchall()
            return {"ok":True, "rows":int(n), "schema":[list(x) for x in schema], "error":None}
        finally:
            con.close()
    except Exception as e:
        return {"ok":False, "rows":None, "schema":None, "error":f"{type(e).__name__}: {e}"}


def main() -> int:
    p = argparse.ArgumentParser(description="PositionX Full Market Daily V1 Phase B2")
    p.add_argument("--current-sample", type=int, default=24)
    p.add_argument("--expired-sample", type=int, default=6)
    p.add_argument("--output-root", default=str(OUTPUT_ROOT))
    args = p.parse_args()

    phase_a = latest_dir(PHASE_A_ROOT, ["security_master_current.csv","audit_summary.json"])
    phase_b1 = latest_dir(PHASE_B1_ROOT, ["phase_b1_summary.json","expired_contract_master_after.csv"])
    current = read_csv(phase_a/"security_master_current.csv")
    expired = read_csv(phase_b1/"expired_contract_master_after.csv")

    elig_rows, excl_rows = [], []
    for r in current:
        ok, reason = current_eligibility(r)
        rr = dict(r)
        rr["eligible_research_trade"] = ok
        rr["eligibility_reason"] = reason
        (elig_rows if ok else excl_rows).append(rr)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(args.output_root)/stamp
    out.mkdir(parents=True, exist_ok=False)
    write_csv(out/"eligible_current_universe.csv", elig_rows)
    write_csv(out/"excluded_current_universe.csv", excl_rows)

    excluded_counts = Counter(r["eligibility_reason"] for r in excl_rows)
    st_excluded = [r for r in excl_rows if r["eligibility_reason"]=="EXCLUDE_CURRENT_ST"]
    bse_excluded = [r for r in excl_rows if r["eligibility_reason"]=="EXCLUDE_BSE"]

    current_sample = pick_current_sample(current, max_current=max(1,args.current_sample))
    expired_sample = pick_expired_sample(expired, max_expired=max(0,args.expired_sample))
    sample = current_sample + expired_sample
    write_csv(out/"pilot_symbols.csv", sample)

    print("="*100)
    print("POSITIONX FULL MARKET DAILY V1 — PHASE B2 FULL-HISTORY PILOT")
    print("="*100)
    print(f"PHASE A: {phase_a}")
    print(f"PHASE B1: {phase_b1}")
    print(f"OUTPUT: {out}")
    print()
    print(f"CURRENT RAW UNIVERSE : {len(current)}")
    print(f"ELIGIBLE SH/SZ NON-ST: {len(elig_rows)}")
    print(f"EXCLUDED BSE         : {len(bse_excluded)}")
    print(f"EXCLUDED CURRENT ST  : {len(st_excluded)}")
    print(f"PILOT CURRENT        : {len(current_sample)}")
    print(f"PILOT EXPIRED ARCHIVE: {len(expired_sample)}")
    print()

    from xtquant import xtdata
    try:
        xtdata.enable_hello = False
    except Exception:
        pass

    fields = ["open","high","low","close","volume","amount","preClose","suspendFlag"]
    all_rows: List[Dict[str, Any]] = []
    audits: List[Dict[str, Any]] = []
    dividend_probes: List[Dict[str, Any]] = []

    for i, meta in enumerate(sample,1):
        symbol = meta["symbol"]
        start = meta.get("open_date") or "19900101"
        print(f"[PILOT DOWNLOAD] {i}/{len(sample)} {symbol} start={start} role={meta.get('sample_role')}")
        ok, err = download_one(xtdata, symbol, start)
        rows = []
        read_err = None
        if ok:
            try:
                rows = collect_symbol_rows(xtdata, symbol, fields)
            except Exception as e:
                read_err = f"{type(e).__name__}: {e}"
        a = validate_rows(symbol, rows, meta)
        a["download_ok"] = ok
        a["download_error"] = err
        a["read_error"] = read_err
        audits.append(a)
        for r in rows:
            r["sample_role"] = meta.get("sample_role")
            r["board"] = meta.get("board")
            all_rows.append(r)
        dividend_probes.append({"symbol":symbol, **get_dividend_probe(xtdata, symbol)})

    write_csv(out/"pilot_symbol_audit.csv", audits)
    write_csv(out/"dividend_factor_probe.csv", dividend_probes)

    df = pd.DataFrame(all_rows)
    if not df.empty:
        df = df.sort_values(["symbol","trade_date"]).reset_index(drop=True)
    csv_path = out/"daily_bars_pilot.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    parquet_result = write_parquet_with_duckdb(df, out/"daily_bars_pilot.parquet")

    good = [
        a for a in audits
        if a["download_ok"] and not a["read_error"] and a["bars"] > 0
        and a["duplicate_dates"] == 0 and a["bad_ohlc_rows"] == 0
        and a["zero_or_negative_close_rows"] == 0
    ]
    current_good = [a for a in good if a["sample_role"]=="CURRENT_ELIGIBLE"]
    expired_good = [a for a in good if a["sample_role"]=="EXPIRED_ARCHIVE_PROBE"]

    if len(current_good) == len(current_sample) and (len(expired_sample)==0 or len(expired_good)==len(expired_sample)) and parquet_result["ok"]:
        verdict = "PASS_FULL_HISTORY_PILOT"
        rc = 0
    else:
        verdict = "REVIEW_FULL_HISTORY_PILOT"
        rc = 3

    summary = {
        "generated_at":datetime.now().isoformat(timespec="seconds"),
        "scope_policy":{
            "eligible_markets":["SH","SZ"],
            "exclude_bse":True,
            "exclude_current_st":True,
            "st_name_regex":ST_RE.pattern,
            "historical_st_policy":"PIT_ST_FILTER_REQUIRED_BEFORE_STRATEGY_RESEARCH",
            "raw_history_note":"Do not erase a symbol's valid pre-ST history solely because its current name is ST.",
        },
        "phase_a_dir":str(phase_a),
        "phase_b1_dir":str(phase_b1),
        "current_universe":{
            "raw_count":len(current),
            "eligible_non_bse_non_st_verified":len(elig_rows),
            "excluded_counts":dict(excluded_counts),
            "eligible_board_counts":dict(Counter(r.get("board") for r in elig_rows)),
        },
        "pilot":{
            "current_sample":len(current_sample),
            "expired_archive_sample":len(expired_sample),
            "total_symbols":len(sample),
            "good_current":len(current_good),
            "good_expired":len(expired_good),
            "total_rows":len(all_rows),
            "parquet":parquet_result,
        },
        "dividend_factor_probe":{
            "ok_count":sum(1 for r in dividend_probes if r["ok"]),
            "nonempty_count":sum(1 for r in dividend_probes if r["ok"] and (r.get("len") or 0)>0),
        },
        "safety":{
            "broker_used":False,
            "production_sqlite_opened":False,
            "research_duckdb_opened":False,
            "qmt_daily_history_download_symbols":len(sample),
            "bulk_full_market_download_started":False,
        },
        "verdict":verdict,
        "output_dir":str(out),
    }
    write_json(out/"phase_b2_summary.json", summary)

    report = [
        "# PositionX · Full Market Daily Data V1 — Phase B2 Pilot",
        "",
        f"- Verdict: **{verdict}**",
        f"- Current raw universe: **{len(current)}**",
        f"- Eligible SH/SZ verified non-ST universe: **{len(elig_rows)}**",
        f"- BSE excluded: **{len(bse_excluded)}**",
        f"- Current ST excluded: **{len(st_excluded)}**",
        "",
        "## Pilot",
        f"- Current eligible symbols: {len(current_sample)}; good: {len(current_good)}",
        f"- Expired archive probes: {len(expired_sample)}; good: {len(expired_good)}",
        f"- Total daily-bar rows: {len(all_rows)}",
        f"- Parquet write/read: {parquet_result['ok']}",
        "",
        "## Frozen eligibility rule",
        "- Current research/trading universe = verified stock AND SH/SZ AND not current ST.",
        "- BSE is outside scope.",
        "- ST is time-varying. Final historical strategy eligibility requires PIT ST status by date.",
        "- Expired stocks sampled here are archive/anti-survivorship probes, not current trade candidates.",
        "",
        "## Safety",
        "- No broker APIs used.",
        "- No PositionX production SQLite opened.",
        "- No PositionX research DuckDB opened.",
        "- Only the small pilot symbol set was downloaded; full-market download was not started.",
        "",
    ]
    (out/"phase_b2_report.md").write_text("\n".join(report), encoding="utf-8")

    print()
    print(json.dumps({
        "verdict":verdict,
        "eligible_current":len(elig_rows),
        "excluded_counts":dict(excluded_counts),
        "pilot_total":len(sample),
        "pilot_good":len(good),
        "parquet_ok":parquet_result["ok"],
    }, ensure_ascii=False, indent=2))
    print(f"SUMMARY: {out/'phase_b2_summary.json'}")
    print(f"REPORT : {out/'phase_b2_report.md'}")
    print("NEXT: share phase_b2_summary.json + phase_b2_report.md + pilot_symbol_audit.csv.")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
