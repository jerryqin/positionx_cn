from __future__ import annotations

"""
PositionX — Full Market Daily Data V1 / Phase B1
Load expired/delisted contract metadata, then audit security-master coverage.

This script intentionally does NOT download daily bars.
It mutates only QMT's local static/history-contract metadata cache through
xtdata.download_history_contracts(). PositionX SQLite / DuckDB are not opened.
"""

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUDIT_ROOT = PROJECT_ROOT / "data" / "research" / "full_market_daily_v1_audit"
OUTPUT_ROOT = PROJECT_ROOT / "data" / "research" / "full_market_daily_v1_phase_b1"
A_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$", re.I)


def norm(x: Any) -> str:
    return str(x or "").strip().upper()


def is_a_symbol(x: str) -> bool:
    return bool(A_RE.match(norm(x)))


def board(symbol: str) -> str:
    s = norm(symbol)
    code, _, m = s.partition(".")
    if m == "BJ":
        return "BSE"
    if m == "SH":
        if code.startswith(("688", "689")):
            return "STAR"
        if code.startswith(("600", "601", "603", "605")):
            return "SSE_MAIN"
        return "SSE_OTHER"
    if m == "SZ":
        if code.startswith(("300", "301")):
            return "CHINEXT"
        if code.startswith(("000", "001", "002", "003")):
            return "SZSE_MAIN"
        return "SZSE_OTHER"
    return "OTHER"


def nd(v: Any) -> Optional[str]:
    if v in (None, "", 0, "0"):
        return None
    s = str(v).strip()
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


def safe(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if hasattr(v, "item"):
        try:
            return safe(v.item())
        except Exception:
            pass
    return str(v)


def write_csv(path: Path, rows: Sequence[Dict[str, Any]], fields: Optional[Sequence[str]] = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        seen, fields2 = set(), []
        for r in rows:
            for k in r:
                if k not in seen:
                    seen.add(k); fields2.append(k)
        fields = fields2 or ["empty"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(fields), extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: safe(r.get(k)) for k in fields})


def write_json(path: Path, obj: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=safe), encoding="utf-8")


def latest_phase_a_dir() -> Path:
    if not AUDIT_ROOT.exists():
        raise RuntimeError(f"Phase-A audit root not found: {AUDIT_ROOT}")
    dirs = sorted([p for p in AUDIT_ROOT.iterdir() if p.is_dir()], key=lambda p: p.name, reverse=True)
    for d in dirs:
        if (d/"audit_summary.json").exists() and (d/"security_master_current.csv").exists() and (d/"local_cache_recent_coverage.csv").exists():
            return d
    raise RuntimeError("No complete Phase-A audit directory found.")


def read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def boolish(v: Any) -> bool:
    return str(v).strip().lower() in {"1","true","yes","y"}


def build_board_cache_breakdown(master: List[Dict[str, str]], cache: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    cm = {r["symbol"]: r for r in cache}
    groups = defaultdict(list)
    for r in master:
        groups[r.get("board") or "OTHER"].append(r)
    rows = []
    for b in sorted(groups):
        vals = groups[b]
        total = len(vals)
        stock_verified = sum(1 for r in vals if boolish(r.get("instrument_type_stock")))
        with_recent = 0
        fresh = 0
        for r in vals:
            c = cm.get(r["symbol"], {})
            try:
                bars = int(float(c.get("recent_close_bars") or 0))
            except Exception:
                bars = 0
            if bars > 0:
                with_recent += 1
            if boolish(c.get("latest_is_expected")):
                fresh += 1
        rows.append({
            "board": b,
            "universe_count": total,
            "stock_verified": stock_verified,
            "with_recent_close": with_recent,
            "missing_recent_close": total - with_recent,
            "fresh_latest": fresh,
            "not_fresh": total - fresh,
            "recent_coverage_ratio": with_recent / total if total else None,
            "fresh_ratio": fresh / total if total else None,
        })
    return rows


def get_members(xtdata: Any, sector: str) -> Tuple[List[str], Optional[str]]:
    try:
        vals = xtdata.get_stock_list_in_sector(sector) or []
        return sorted({norm(x) for x in vals if norm(x)}), None
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


def stock_type(xtdata: Any, symbol: str) -> Tuple[Optional[bool], str]:
    try:
        t = xtdata.get_instrument_type(symbol)
        if not isinstance(t, dict):
            return None, ""
        true_types = ";".join(sorted(str(k) for k,v in t.items() if bool(v)))
        return (bool(t.get("stock")) if "stock" in t else None), true_types
    except Exception as e:
        return None, f"ERROR:{type(e).__name__}:{e}"


def detail(xtdata: Any, symbol: str) -> Tuple[Dict[str, Any], Optional[str]]:
    try:
        try:
            d = xtdata.get_instrument_detail(symbol, False)
        except TypeError:
            d = xtdata.get_instrument_detail(symbol)
        return (d if isinstance(d, dict) else {}), None
    except Exception as e:
        return {}, f"{type(e).__name__}: {e}"


def expired_snapshot(xtdata: Any, label: str, out_dir: Path) -> Dict[str, Any]:
    sectors = list(xtdata.get_sector_list() or [])
    expired_names = [s for s in sectors if ("过期" in s or "退市" in s)]
    sector_rows = []
    provenance = defaultdict(set)

    for name in expired_names:
        members, err = get_members(xtdata, name)
        a = [x for x in members if is_a_symbol(x)]
        sector_rows.append({
            "sector": name, "raw_members": len(members),
            "a_share_like_members": len(a), "error": err
        })
        for s in a:
            provenance[s].add(name)

    write_csv(out_dir/f"expired_sector_probe_{label}.csv", sector_rows)

    master_rows = []
    syms = sorted(provenance)
    for i, s in enumerate(syms, 1):
        d, derr = detail(xtdata, s)
        sf, types = stock_type(xtdata, s)
        master_rows.append({
            "symbol": s,
            "board": board(s),
            "instrument_type_stock": sf,
            "instrument_true_types": types,
            "name": d.get("InstrumentName"),
            "exchange_id": d.get("ExchangeID"),
            "exchange_code": d.get("ExchangeCode"),
            "instrument_id": d.get("InstrumentID"),
            "unified_code": d.get("UniCode"),
            "open_date": nd(d.get("OpenDate")),
            "expire_date": nd(d.get("ExpireDate")),
            "is_trading": d.get("IsTrading"),
            "instrument_status": d.get("InstrumentStatus"),
            "provenance_sectors": ";".join(sorted(provenance[s])),
            "detail_found": bool(d),
            "detail_error": derr,
        })
        if i == 1 or i % 250 == 0 or i == len(syms):
            print(f"[EXPIRED MASTER {label}] {i}/{len(syms)} {s}")

    write_csv(out_dir/f"expired_contract_master_{label}.csv", master_rows)
    return {
        "sector_count": len(expired_names),
        "a_share_like_member_count": len(syms),
        "stock_verified": sum(1 for r in master_rows if r["instrument_type_stock"] is True),
        "detail_found": sum(1 for r in master_rows if r["detail_found"]),
        "board_counts": dict(Counter(r["board"] for r in master_rows)),
    }


def markdown(summary: Dict[str, Any]) -> str:
    b = summary["phase_a_board_cache_breakdown"]
    lines = [
        "# PositionX · Full Market Daily Data V1 — Phase B1",
        "",
        f"- Generated: `{summary['generated_at']}`",
        f"- Phase-A source: `{summary['phase_a_dir']}`",
        "",
        "## 1. Phase-A board cache breakdown",
        "",
        "| Board | Universe | Stock verified | Recent close | Missing recent | Fresh | Fresh ratio |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in b:
        lines.append(
            f"| {r['board']} | {r['universe_count']} | {r['stock_verified']} | "
            f"{r['with_recent_close']} | {r['missing_recent_close']} | "
            f"{r['fresh_latest']} | {r['fresh_ratio']:.2%} |"
        )
    lines += [
        "",
        "## 2. Expired/delisted contract metadata",
        "",
        f"- Before: `{json.dumps(summary['expired_before'], ensure_ascii=False)}`",
        f"- download_history_contracts call: `{summary['download_history_contracts']}`",
        f"- After: `{json.dumps(summary['expired_after'], ensure_ascii=False)}`",
        "",
        "## 3. Verdict",
        "",
        f"- `{summary['verdict']}`",
        "",
        "## 4. Safety",
        "",
        "- QMT static/history-contract metadata cache was updated.",
        "- Daily bar history was **not** downloaded.",
        "- No broker / xttrader API was used.",
        "- No PositionX production SQLite was opened.",
        "- No PositionX research DuckDB was opened or modified.",
        "",
        "## 5. Next gate",
        "",
        "Only after reviewing this report should Phase B2 download a small representative set of full-history 1d bars.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="PositionX Full Market Daily V1 Phase B1")
    p.add_argument("--output-root", default=str(OUTPUT_ROOT))
    args = p.parse_args()

    phase_a = latest_phase_a_dir()
    phase_a_summary = json.loads((phase_a/"audit_summary.json").read_text(encoding="utf-8"))
    master = read_csv(phase_a/"security_master_current.csv")
    cache = read_csv(phase_a/"local_cache_recent_coverage.csv")
    breakdown = build_board_cache_breakdown(master, cache)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.output_root)/stamp
    out_dir.mkdir(parents=True, exist_ok=False)
    write_csv(out_dir/"phase_a_board_cache_breakdown.csv", breakdown)

    print("="*100)
    print("POSITIONX FULL MARKET DAILY V1 — PHASE B1")
    print("="*100)
    print(f"PHASE A: {phase_a}")
    print(f"OUTPUT : {out_dir}")
    print("ACTION : download expired/delisted contract metadata ONLY")
    print("NO DAILY BAR DOWNLOAD")
    print()

    from xtquant import xtdata
    try:
        xtdata.enable_hello = False
    except Exception:
        pass

    before = expired_snapshot(xtdata, "before", out_dir)

    print()
    print("Calling xtdata.download_history_contracts() ...")
    try:
        result = xtdata.download_history_contracts()
        call_status = {
            "ok": True,
            "return_value": safe(result),
            "error": None,
        }
    except Exception as e:
        call_status = {
            "ok": False,
            "return_value": None,
            "error": f"{type(e).__name__}: {e}",
        }
        print("DOWNLOAD_HISTORY_CONTRACTS_FAILED:", call_status["error"])

    after = expired_snapshot(xtdata, "after", out_dir)

    verified_current = int(phase_a_summary["security_master"]["stock_verified"])
    expired_verified = int(after["stock_verified"])
    if call_status["ok"] and expired_verified > 0:
        verdict = "PASS_EXPIRED_METADATA_READY"
        rc = 0
    elif call_status["ok"]:
        verdict = "REVIEW_NO_EXPIRED_A_SHARE_METADATA_AFTER_DOWNLOAD"
        rc = 3
    else:
        verdict = "FAIL_HISTORY_CONTRACT_DOWNLOAD"
        rc = 2

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "phase_a_dir": str(phase_a),
        "phase_a_current_universe_count": phase_a_summary["current_universe"]["count"],
        "phase_a_current_stock_verified": verified_current,
        "phase_a_board_cache_breakdown": breakdown,
        "expired_before": before,
        "download_history_contracts": call_status,
        "expired_after": after,
        "provisional_security_master": {
            "current_verified_stocks": verified_current,
            "expired_verified_stocks": expired_verified,
            "union_requires_dedup": True,
            "final_master_not_frozen_yet": True,
        },
        "safety": {
            "daily_bar_download_called": False,
            "broker_used": False,
            "production_sqlite_opened": False,
            "research_duckdb_opened": False,
            "qmt_history_contract_metadata_mutated": bool(call_status["ok"]),
        },
        "verdict": verdict,
        "output_dir": str(out_dir),
    }
    write_json(out_dir/"phase_b1_summary.json", summary)
    (out_dir/"phase_b1_report.md").write_text(markdown(summary), encoding="utf-8")

    print()
    print(json.dumps({
        "verdict": verdict,
        "expired_before": before,
        "expired_after": after,
        "current_verified_stocks": verified_current,
        "expired_verified_stocks": expired_verified,
    }, ensure_ascii=False, indent=2))
    print(f"REPORT : {out_dir/'phase_b1_report.md'}")
    print(f"SUMMARY: {out_dir/'phase_b1_summary.json'}")
    print()
    print("NEXT: share phase_b1_summary.json + phase_b1_report.md before any daily-bar bulk download.")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
