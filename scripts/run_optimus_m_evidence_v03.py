from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

CST = timezone(timedelta(hours=8))
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "config/research/optimus_m_evidence_v0_3.json"


def now_cst() -> datetime:
    return datetime.now(CST)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _iso_date(x: str) -> str:
    s = str(x).strip().replace("-", "")
    if len(s) != 8 or not s.isdigit():
        raise ValueError(f"invalid as-of date: {x}")
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"


def comparable_index(df: pd.DataFrame) -> pd.Series:
    """Create a split/dividend-comparable price index from close/pre_close daily returns.

    The absolute starting level is arbitrary. The index is used only for rolling returns,
    moving averages, highs and drawdowns. Rows with invalid pre_close/close are excluded.
    """
    x = df.sort_values("trade_date").copy()
    r = pd.to_numeric(x["close"], errors="coerce") / pd.to_numeric(x["pre_close"], errors="coerce") - 1.0
    r = r.where((pd.to_numeric(x["close"], errors="coerce") > 0) & (pd.to_numeric(x["pre_close"], errors="coerce") > 0))
    r = r.replace([np.inf, -np.inf], np.nan)
    valid = r.notna()
    out = pd.Series(np.nan, index=x.index, dtype="float64")
    if valid.any():
        out.loc[valid] = (1.0 + r.loc[valid]).cumprod()
    return out


def trailing_return(idx: pd.Series, n: int) -> float | None:
    s = idx.dropna()
    if len(s) < n + 1:
        return None
    a = float(s.iloc[-n-1])
    b = float(s.iloc[-1])
    if not math.isfinite(a) or not math.isfinite(b) or a <= 0:
        return None
    return b / a - 1.0


def rolling_mean_last(s: pd.Series, n: int) -> float | None:
    x = pd.to_numeric(s, errors="coerce").dropna()
    if len(x) < n:
        return None
    return float(x.iloc[-n:].mean())


def rolling_std_last(s: pd.Series, n: int) -> float | None:
    x = pd.to_numeric(s, errors="coerce").dropna()
    if len(x) < n:
        return None
    return float(x.iloc[-n:].std(ddof=1))


def safe_ratio(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or not math.isfinite(a) or not math.isfinite(b) or b == 0:
        return None
    return a / b


def m_state(row: dict[str, Any], amount_expansion_threshold: float) -> str:
    """Transparent descriptive M-state, not a trading score.

    STRONG: positive RS20 and RS60, above MA20/60/120, and either amount expansion
    or within 3% of the 60d high.
    IMPROVING: positive RS20 + above MA20, plus at least one of positive RS60,
    above MA60, or 20d amount >= 60d amount average.
    Otherwise WEAK. Missing inputs fail closed toward WEAK/INSUFFICIENT.
    """
    if row.get("data_status") != "OK":
        return "INSUFFICIENT_DATA"
    pos20 = bool(row.get("rs20_vs_broad", -999) > 0)
    pos60 = bool(row.get("rs60_vs_broad", -999) > 0)
    a20 = bool(row.get("above_ma20", False))
    a60 = bool(row.get("above_ma60", False))
    a120 = bool(row.get("above_ma120", False))
    amt = row.get("amount20_vs_60")
    near60 = bool(row.get("within_3pct_60d_high", False))
    amt_strong = amt is not None and math.isfinite(float(amt)) and float(amt) >= amount_expansion_threshold
    if pos20 and pos60 and a20 and a60 and a120 and (amt_strong or near60):
        return "M_STRONG"
    amt_ok = amt is not None and math.isfinite(float(amt)) and float(amt) >= 1.0
    if pos20 and a20 and (pos60 or a60 or amt_ok):
        return "M_IMPROVING"
    return "M_WEAK"


def connect_readonly(path: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(path), read_only=True)


def table_columns(con: duckdb.DuckDBPyConnection, view: str) -> list[tuple[str, str]]:
    q = f"DESCRIBE SELECT * FROM {view}"
    return [(str(r[0]), str(r[1])) for r in con.execute(q).fetchall()]


def latest_market_date(con: duckdb.DuckDBPyConnection, view: str) -> str | None:
    x = con.execute(f"SELECT max(trade_date) FROM {view}").fetchone()[0]
    return None if x is None else pd.Timestamp(x).strftime("%Y-%m-%d")


def load_stock(con: duckdb.DuckDBPyConnection, view: str, symbol: str, as_of: str, limit: int = 180) -> pd.DataFrame:
    return con.execute(
        f"""
        SELECT symbol, trade_date, open, high, low, close, volume, amount, pre_close, suspend_flag
        FROM {view}
        WHERE symbol = ? AND trade_date <= CAST(? AS DATE)
        ORDER BY trade_date DESC
        LIMIT ?
        """,
        [symbol, as_of, limit],
    ).fetchdf().sort_values("trade_date").reset_index(drop=True)


def load_market_daily_returns(con: duckdb.DuckDBPyConnection, view: str, as_of: str, sessions: int = 181) -> pd.DataFrame:
    # Resolve the last N market dates first, then compute equal-weight stock daily returns.
    dates = con.execute(
        f"SELECT DISTINCT trade_date FROM {view} WHERE trade_date <= CAST(? AS DATE) ORDER BY trade_date DESC LIMIT ?",
        [as_of, sessions],
    ).fetchdf()
    if dates.empty:
        return pd.DataFrame(columns=["trade_date", "broad_ret", "n"])
    min_date = pd.Timestamp(dates["trade_date"].min()).strftime("%Y-%m-%d")
    return con.execute(
        f"""
        SELECT trade_date,
               avg(CASE WHEN close > 0 AND pre_close > 0 THEN close / pre_close - 1 END) AS broad_ret,
               count(CASE WHEN close > 0 AND pre_close > 0 THEN 1 END) AS n
        FROM {view}
        WHERE trade_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
        GROUP BY trade_date
        ORDER BY trade_date
        """,
        [min_date, as_of],
    ).fetchdf()


def broad_index(mkt: pd.DataFrame, min_daily_symbols: int) -> pd.Series:
    x = mkt.copy()
    r = pd.to_numeric(x["broad_ret"], errors="coerce")
    n = pd.to_numeric(x["n"], errors="coerce")
    r = r.where(n >= min_daily_symbols)
    return (1.0 + r).cumprod()


def stock_metrics(df: pd.DataFrame, broad20: float | None, broad60: float | None, rules: dict[str, Any]) -> dict[str, Any]:
    min_hist = int(rules["min_history_sessions"])
    if len(df) < min_hist:
        return {"data_status": "INSUFFICIENT_HISTORY", "history_sessions": int(len(df))}
    idx = comparable_index(df)
    sidx = idx.dropna()
    if len(sidx) < min_hist:
        return {"data_status": "INSUFFICIENT_COMPARABLE_HISTORY", "history_sessions": int(len(sidx))}

    ret20 = trailing_return(sidx, 20)
    ret60 = trailing_return(sidx, 60)
    rs20 = None if ret20 is None or broad20 is None else ret20 - broad20
    rs60 = None if ret60 is None or broad60 is None else ret60 - broad60

    cur = float(sidx.iloc[-1])
    ma20 = rolling_mean_last(sidx, 20)
    ma60 = rolling_mean_last(sidx, 60)
    ma120 = rolling_mean_last(sidx, 120)
    hi20 = float(sidx.iloc[-20:].max())
    hi60 = float(sidx.iloc[-60:].max())
    dd60 = cur / hi60 - 1.0 if hi60 > 0 else None
    near = float(rules["near_high_pct"])

    amount = pd.to_numeric(df["amount"], errors="coerce")
    a20 = rolling_mean_last(amount, 20)
    a60 = rolling_mean_last(amount, 60)
    amount20_vs_60 = safe_ratio(a20, a60)
    prev20 = None
    xamt = amount.dropna()
    if len(xamt) >= 40:
        prev20 = float(xamt.iloc[-40:-20].mean())
    amount20_vs_prev20 = safe_ratio(a20, prev20)

    daily_ret = pd.to_numeric(df["close"], errors="coerce") / pd.to_numeric(df["pre_close"], errors="coerce") - 1.0
    vol20 = rolling_std_last(daily_ret.replace([np.inf, -np.inf], np.nan), 20)
    vol60 = rolling_std_last(daily_ret.replace([np.inf, -np.inf], np.nan), 60)

    return {
        "data_status": "OK",
        "history_sessions": int(len(sidx)),
        "stock_ret20": ret20,
        "stock_ret60": ret60,
        "broad_ret20": broad20,
        "broad_ret60": broad60,
        "rs20_vs_broad": rs20,
        "rs60_vs_broad": rs60,
        "above_ma20": bool(ma20 is not None and cur >= ma20),
        "above_ma60": bool(ma60 is not None and cur >= ma60),
        "above_ma120": bool(ma120 is not None and cur >= ma120),
        "at_20d_high": bool(cur >= hi20 - 1e-12),
        "at_60d_high": bool(cur >= hi60 - 1e-12),
        "distance_to_20d_high": cur / hi20 - 1.0,
        "distance_to_60d_high": dd60,
        "within_3pct_60d_high": bool(dd60 is not None and dd60 >= -near),
        "amount20_mean": a20,
        "amount60_mean": a60,
        "amount20_vs_60": amount20_vs_60,
        "amount20_vs_prev20": amount20_vs_prev20,
        "vol20": vol20,
        "vol60": vol60,
        "vol20_vs_60": safe_ratio(vol20, vol60),
        "drawdown_from_60d_high": dd60,
    }


def build_breadth(df: pd.DataFrame) -> dict[str, Any]:
    ok = df[df["data_status"] == "OK"].copy()
    n = len(ok)
    if n == 0:
        return {"valid_candidates": 0}
    def frac_bool(c: str) -> float:
        return float(ok[c].fillna(False).mean())
    return {
        "valid_candidates": int(n),
        "above_ma20": frac_bool("above_ma20"),
        "above_ma60": frac_bool("above_ma60"),
        "above_ma120": frac_bool("above_ma120"),
        "at_20d_high": frac_bool("at_20d_high"),
        "at_60d_high": frac_bool("at_60d_high"),
        "positive_rs20": float((pd.to_numeric(ok["rs20_vs_broad"], errors="coerce") > 0).mean()),
        "positive_rs60": float((pd.to_numeric(ok["rs60_vs_broad"], errors="coerce") > 0).mean()),
        "amount_expansion_20_vs_60": float((pd.to_numeric(ok["amount20_vs_60"], errors="coerce") >= 1.10).mean()),
    }


def candidate_basket_return(df: pd.DataFrame, col: str) -> float | None:
    x = pd.to_numeric(df.loc[df["data_status"] == "OK", col], errors="coerce").dropna()
    return None if x.empty else float(x.mean())


def write_markdown(path: Path, payload: dict[str, Any], rows: pd.DataFrame) -> None:
    b = payload["breadth"]
    lines = [
        "# Optimus M-Evidence Snapshot V0.3",
        "",
        f"- As-of: `{payload['as_of']}`",
        f"- Market catalog: `{payload['market']['catalog']}`",
        f"- Market max date: `{payload['market']['max_date']}`",
        f"- Candidate registry DB: `{payload['candidate_registry']['db']}`",
        f"- Candidate registry SHA256: `{payload['candidate_registry']['sha256']}`",
        f"- Candidate count: `{payload['candidate_registry']['count']}`",
        f"- Candidate basket 20D return: `{payload['basket'].get('ret20')}`",
        f"- Candidate basket 60D return: `{payload['basket'].get('ret60')}`",
        f"- Broad-market EW 20D return: `{payload['broad_market'].get('ret20')}`",
        f"- Broad-market EW 60D return: `{payload['broad_market'].get('ret60')}`",
        "",
        "## Breadth",
        "",
    ]
    for k, v in b.items():
        lines.append(f"- {k}: `{v}`")
    lines += ["", "## Candidate Market Evidence", ""]
    show = [
        "symbol", "company", "registry_basis", "lifecycle_state", "f_status", "needs_review", "freshness_state", "component_domain", "m_state", "rs20_vs_broad", "rs60_vs_broad",
        "above_ma20", "above_ma60", "above_ma120", "distance_to_60d_high",
        "amount20_vs_60", "vol20_vs_60", "drawdown_from_60d_high"
    ]
    headers = show
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "|".join(["---"] * len(headers)) + "|")
    for _, rr in rows[headers].iterrows():
        vals = []
        for h in headers:
            v = rr[h]
            if pd.isna(v):
                vals.append("")
            elif isinstance(v, float):
                vals.append(f"{v:.6f}")
            else:
                vals.append(str(v))
        lines.append("| " + " | ".join(vals) + " |")
    lines += [
        "",
        "## Interpretation Boundary",
        "",
        "This snapshot measures **M-Evidence only** on the V0.3 registry-driven active research universe. It does not upgrade F-Evidence, does not confirm Tesla/Optimus supplier identity, and does not create a trading signal.",
        "The broad benchmark is an equal-weight A-share daily-return index constructed from `close/pre_close`. Industry/auto-parts/industrial-automation benchmarks remain `PENDING_INDUSTRY_BENCHMARK` in V0.3 rather than being silently approximated.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def candidate_registry_digest(df: pd.DataFrame) -> str:
    cols = ["symbol", "company", "component_domain", "lifecycle_state", "f_status", "canonical", "state_basis"]
    rows = []
    for _, r in df[cols].sort_values("symbol").iterrows():
        rows.append({k: (None if pd.isna(r[k]) else r[k]) for k in cols})
    raw = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def load_candidate_universe(cfg: dict[str, Any]) -> pd.DataFrame:
    db = Path(cfg["registry_db"])
    if not db.exists():
        raise RuntimeError(f"candidate registry db missing: {db}")
    allowed = list(cfg["rules"].get("active_lifecycle_states", ["ACTIVE_SEED", "ACTIVE_CANONICAL"]))
    marks = ",".join(["?"] * len(allowed))
    con = duckdb.connect(str(db), read_only=True)
    try:
        df = con.execute(
            f"""
            SELECT symbol, company, component_domain, lifecycle_state, f_status,
                   canonical, needs_review, freshness_state, origin, state_basis,
                   max_evidence_level, max_tesla_link_level
            FROM optimus_candidate_registry_v03
            WHERE lifecycle_state IN ({marks}) AND f_status <> 'REMOVED'
            ORDER BY symbol
            """, allowed
        ).fetchdf()
    finally:
        con.close()
    if df.empty:
        raise RuntimeError("candidate registry has no active research candidates; bootstrap/reconcile first")
    df["registry_basis"] = np.where(df["canonical"].fillna(False), "CANONICAL", "SEED")
    return df


def registry_basis_breadth(df: pd.DataFrame) -> dict[str, Any]:
    out = {"ALL": build_breadth(df)}
    for basis in sorted(df["registry_basis"].dropna().unique().tolist()):
        out[str(basis)] = build_breadth(df[df["registry_basis"] == basis])
    return out

def run_check(cfg: dict[str, Any]) -> int:
    catalog = Path(cfg["market_catalog"])
    registry_db = Path(cfg["registry_db"])
    if not catalog.exists():
        print(json.dumps({"status": "BLOCKED_MARKET_CATALOG_MISSING", "catalog": str(catalog)}, ensure_ascii=False, indent=2))
        return 2
    if not registry_db.exists():
        print(json.dumps({"status": "BLOCKED_REGISTRY_DB_MISSING", "registry_db": str(registry_db)}, ensure_ascii=False, indent=2))
        return 2
    con = connect_readonly(catalog)
    try:
        cols = table_columns(con, cfg["market_view"])
        names = {x[0] for x in cols}
        required = {"symbol", "trade_date", "open", "high", "low", "close", "volume", "amount", "pre_close"}
        missing = sorted(required - names)
        candidates = load_candidate_universe(cfg)
        out = {
            "status": "PASS_OPTIMUS_M_EVIDENCE_V0_3_CHECK" if not missing else "BLOCKED_SCHEMA_MISMATCH",
            "catalog": str(catalog), "view": cfg["market_view"], "max_date": latest_market_date(con, cfg["market_view"]),
            "columns": cols, "missing_required": missing,
            "registry_db": str(registry_db), "candidate_registry_sha256": candidate_registry_digest(candidates),
            "candidate_count": int(len(candidates)),
            "registry_basis_counts": candidates["registry_basis"].value_counts().to_dict(),
            "lifecycle_counts": candidates["lifecycle_state"].value_counts().to_dict(),
            "f_status_counts": candidates["f_status"].value_counts().to_dict(),
            "research_only": True, "broker": False, "production_db": False,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 0 if not missing else 2
    finally:
        con.close()

def run_capture(cfg: dict[str, Any], as_of_raw: str) -> int:
    as_of = _iso_date(as_of_raw)
    catalog = Path(cfg["market_catalog"])
    if not catalog.exists():
        raise SystemExit(f"BLOCKED_MARKET_CATALOG_MISSING {catalog}")
    con = connect_readonly(catalog)
    try:
        max_date = latest_market_date(con, cfg["market_view"])
        if max_date is None or max_date < as_of:
            raise SystemExit(f"BLOCKED_MARKET_NOT_READY as_of={as_of} market_max={max_date}")

        market = load_market_daily_returns(con, cfg["market_view"], as_of, sessions=181)
        bidx = broad_index(market, int(cfg["rules"]["market_min_daily_symbols"]))
        broad20 = trailing_return(bidx, 20)
        broad60 = trailing_return(bidx, 60)
        if broad20 is None or broad60 is None:
            raise SystemExit("BLOCKED_BROAD_MARKET_HISTORY_INSUFFICIENT")

        candidates = load_candidate_universe(cfg)
        rows: list[dict[str, Any]] = []
        for c in candidates.to_dict(orient="records"):
            hist = load_stock(con, cfg["market_view"], c["symbol"], as_of, limit=180)
            m = stock_metrics(hist, broad20, broad60, cfg["rules"])
            row = {**c, **m}
            row["m_state"] = m_state(row, float(cfg["rules"]["amount_expansion_threshold"]))
            rows.append(row)
        outdf = pd.DataFrame(rows)
        breadth = build_breadth(outdf)
        breadth_by_registry_basis = registry_basis_breadth(outdf)
        basket = {
            "ret20": candidate_basket_return(outdf, "stock_ret20"),
            "ret60": candidate_basket_return(outdf, "stock_ret60"),
            "rs20_vs_broad": candidate_basket_return(outdf, "rs20_vs_broad"),
            "rs60_vs_broad": candidate_basket_return(outdf, "rs60_vs_broad"),
        }

        stamp = now_cst().strftime("%Y%m%dT%H%M%S+0800")
        root = Path(cfg["output_root"]) / as_of.replace("-", "") / stamp
        if root.exists():
            raise SystemExit(f"BLOCKED_IMMUTABLE_OUTPUT_EXISTS {root}")
        root.mkdir(parents=True, exist_ok=False)
        csv_path = root / "optimus_m_evidence_v03_candidates.csv"
        json_path = root / "optimus_m_evidence_v03_snapshot.json"
        md_path = root / "OPTIMUS_M_EVIDENCE_V03_SNAPSHOT.md"
        manifest_path = root / "MANIFEST.json"
        outdf.to_csv(csv_path, index=False, encoding="utf-8-sig")

        payload = {
            "schema_version": "OPTIMUS_M_EVIDENCE_V0_3",
            "research_id": cfg["research_id"],
            "capture_id": stamp,
            "as_of": as_of,
            "observed_at": now_cst().isoformat(),
            "status": "CAPTURED_RESEARCH_ONLY",
            "market": {
                "catalog": str(catalog),
                "view": cfg["market_view"],
                "max_date": max_date,
                "price_state": "SYNTHETIC_COMPARABLE_INDEX_FROM_CLOSE_OVER_PRE_CLOSE",
            },
            "broad_market": {"method": "A_SHARE_EQUAL_WEIGHT_DAILY_RETURN", "ret20": broad20, "ret60": broad60},
            "basket": basket,
            "breadth": breadth,
            "breadth_by_registry_basis": breadth_by_registry_basis,
            "m_state_counts": outdf["m_state"].value_counts(dropna=False).to_dict(),
            "m_state_counts_by_registry_basis": {
                str(t): g["m_state"].value_counts(dropna=False).to_dict()
                for t, g in outdf.groupby("registry_basis", sort=True)
            },
            "candidate_registry": {
                "db": str(Path(cfg["registry_db"])),
                "sha256": candidate_registry_digest(candidates),
                "count": int(len(candidates)),
                "registry_basis_counts": candidates["registry_basis"].value_counts().to_dict(),
                "lifecycle_counts": candidates["lifecycle_state"].value_counts().to_dict(),
                "f_status_counts": candidates["f_status"].value_counts().to_dict(),
            },
            "industry_benchmarks": "PENDING_INDUSTRY_BENCHMARK",
            "boundaries": {
                "m_evidence_only": True,
                "no_f_evidence_upgrade": True,
                "no_supplier_confirmation_inference": True,
                "no_trading_signal": True,
                "no_broker": True,
                "production_db": False,
                "no_lookahead": f"trade_date <= {as_of}",
            },
        }
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        write_markdown(md_path, payload, outdf)
        manifest = {
            "schema_version": "OPTIMUS_M_EVIDENCE_MANIFEST_V0_3",
            "capture_id": stamp,
            "as_of": as_of,
            "files": {
                csv_path.name: sha256_file(csv_path),
                json_path.name: sha256_file(json_path),
                md_path.name: sha256_file(md_path),
            },
            "config_sha256": sha256_file(Path(args.config)),
            "candidate_registry_sha256": candidate_registry_digest(candidates),
            "immutable": True,
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({
            "status": "PASS_OPTIMUS_M_EVIDENCE_V0_3_CAPTURE",
            "as_of": as_of,
            "output": str(root),
            "market_max_date": max_date,
            "candidate_count": int(len(outdf)),
            "m_state_counts": payload["m_state_counts"],
            "breadth": breadth,
            "breadth_by_registry_basis": breadth_by_registry_basis,
            "basket": basket,
            "manifest_sha256": sha256_file(manifest_path),
            "research_only": True,
        }, ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        con.close()


def main() -> int:
    global args
    ap = argparse.ArgumentParser(description="PositionX Optimus registry-driven M-Evidence snapshot V0.3")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    sp = ap.add_subparsers(dest="command", required=True)
    sp.add_parser("check")
    cap = sp.add_parser("capture")
    cap.add_argument("--as-of", required=True, help="YYYYMMDD or YYYY-MM-DD")
    args = ap.parse_args()
    cfg = load_config(Path(args.config))
    if args.command == "check":
        return run_check(cfg)
    if args.command == "capture":
        return run_capture(cfg, args.as_of)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
