from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from xtquant import xtdata


ROOT = Path(__file__).resolve().parents[1]
EDGE_DIR = ROOT / "data" / "research" / "edge_discovery"
OUT_DIR = EDGE_DIR / "ret120_size_pit_source_v01"

CUTOFF = "20260924"
DOWNLOAD_START = "20180101"
UNIVERSES = ("csi300", "csi500")


def parse_date(value: Any) -> str | None:
    if value is None:
        return None

    if isinstance(value, float) and not np.isfinite(value):
        return None

    # Numeric timestamps or YYYYMMDD.
    if isinstance(value, (int, float, np.integer, np.floating)):
        v = int(value)
        s = str(abs(v))

        if len(s) == 8 and s.startswith(("19", "20")):
            try:
                return datetime.strptime(s, "%Y%m%d").strftime("%Y%m%d")
            except ValueError:
                return None

        # QMT fields may appear as seconds or milliseconds.
        try:
            if abs(v) >= 10**12:
                dt = datetime.fromtimestamp(v / 1000.0, tz=timezone.utc)
            elif abs(v) >= 10**9:
                dt = datetime.fromtimestamp(v, tz=timezone.utc)
            else:
                return None
            return dt.strftime("%Y%m%d")
        except (ValueError, OSError, OverflowError):
            return None

    s = str(value).strip()
    if not s:
        return None

    # Strip common timestamp suffixes and separators.
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) >= 8 and digits[:4].startswith(("19", "20")):
        candidate = digits[:8]
        try:
            return datetime.strptime(candidate, "%Y%m%d").strftime("%Y%m%d")
        except ValueError:
            pass

    # Last chance: pandas parser.
    try:
        ts = pd.to_datetime(s, errors="raise")
        return ts.strftime("%Y%m%d")
    except Exception:
        return None


def safe_float(value: Any) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return math.nan
    return x if np.isfinite(x) else math.nan


def panel_path(universe: str) -> Path:
    return EDGE_DIR / f"trend_persistence_scan_{universe}_panel_v0221.parquet"


def load_union_symbols(con: duckdb.DuckDBPyConnection) -> list[str]:
    symbols = set()
    for universe in UNIVERSES:
        p = panel_path(universe)
        if not p.exists():
            raise FileNotFoundError(p)
        q = p.as_posix().replace("'", "''")
        rows = con.execute(
            f"SELECT DISTINCT symbol FROM read_parquet('{q}') WHERE symbol IS NOT NULL"
        ).fetchall()
        symbols.update(str(r[0]) for r in rows)
    return sorted(symbols)


def download_capital(symbols: list[str]) -> None:
    print(
        f"Downloading QMT Capital data for {len(symbols)} symbols "
        f"({DOWNLOAD_START} -> {CUTOFF}) ..."
    )

    if hasattr(xtdata, "download_financial_data2"):
        xtdata.download_financial_data2(
            symbols,
            table_list=["Capital"],
            start_time=DOWNLOAD_START,
            end_time=CUTOFF,
        )
    else:
        # Older compatibility path.
        xtdata.download_financial_data(
            symbols,
            table_list=["Capital"],
        )


def extract_capital_events(raw: dict, symbols: list[str]) -> tuple[pd.DataFrame, list[dict]]:
    rows = []
    audit = []

    for symbol in symbols:
        payload = raw.get(symbol)
        table = None

        if isinstance(payload, dict):
            table = payload.get("Capital")
            if table is None:
                table = payload.get("capital")

        if table is None or not hasattr(table, "iterrows"):
            audit.append({
                "symbol": symbol,
                "status": "NO_CAPITAL_TABLE",
                "rows_raw": 0,
                "rows_valid": 0,
            })
            continue

        cols = list(table.columns)
        required = {"total_capital", "circulating_capital", "m_anntime", "m_timetag"}
        missing = sorted(required - set(cols))
        if missing:
            audit.append({
                "symbol": symbol,
                "status": "MISSING_COLUMNS",
                "missing_columns": "|".join(missing),
                "columns": "|".join(map(str, cols)),
                "rows_raw": int(len(table)),
                "rows_valid": 0,
            })
            continue

        valid = 0
        for _, r in table.iterrows():
            announce_date = parse_date(r.get("m_anntime"))
            report_date = parse_date(r.get("m_timetag"))
            total_capital = safe_float(r.get("total_capital"))
            circulating_capital = safe_float(r.get("circulating_capital"))

            if not announce_date or announce_date > CUTOFF:
                continue
            if not np.isfinite(total_capital) or total_capital <= 0:
                continue
            if not np.isfinite(circulating_capital) or circulating_capital <= 0:
                continue
            if circulating_capital > total_capital * 1.01:
                continue

            rows.append({
                "symbol": symbol,
                "announce_date": announce_date,
                "report_date": report_date,
                "total_capital": total_capital,
                "circulating_capital": circulating_capital,
            })
            valid += 1

        audit.append({
            "symbol": symbol,
            "status": "OK" if valid else "NO_VALID_ROWS",
            "rows_raw": int(len(table)),
            "rows_valid": int(valid),
            "columns": "|".join(map(str, cols)),
        })

    if not rows:
        return pd.DataFrame(
            columns=[
                "symbol", "announce_date", "report_date",
                "total_capital", "circulating_capital",
            ]
        ), audit

    events = pd.DataFrame(rows)
    events = events.sort_values(
        ["symbol", "announce_date", "report_date"],
        na_position="first",
    )

    # For identical symbol + announce_date, later report_date wins; this is
    # deterministic but remains only an "announced-share proxy".
    events = events.drop_duplicates(
        ["symbol", "announce_date"],
        keep="last",
    ).reset_index(drop=True)

    return events, audit


def latest_asof_event(events: pd.DataFrame, symbol: str, date: str):
    x = events[events["symbol"] == symbol]
    if x.empty:
        return None
    x = x[x["announce_date"] <= date]
    if x.empty:
        return None
    return x.iloc[-1]


def compare_current_instrument_units(events: pd.DataFrame, symbols: list[str]) -> list[dict]:
    # Deterministic evenly-spaced sample, max 24 symbols.
    if not symbols:
        return []

    n = min(24, len(symbols))
    idx = np.linspace(0, len(symbols) - 1, n).round().astype(int)
    sample = [symbols[i] for i in sorted(set(idx.tolist()))]

    rows = []
    for symbol in sample:
        try:
            detail = xtdata.get_instrument_detail(symbol, False) or {}
        except Exception as e:
            rows.append({
                "symbol": symbol,
                "status": "DETAIL_ERROR",
                "error": repr(e),
            })
            continue

        ev = latest_asof_event(events, symbol, CUTOFF)
        if ev is None:
            rows.append({
                "symbol": symbol,
                "status": "NO_CAPITAL_ASOF",
                "detail_total_volume": safe_float(detail.get("TotalVolume")),
                "detail_float_volume": safe_float(detail.get("FloatVolume")),
            })
            continue

        detail_total = safe_float(detail.get("TotalVolume"))
        detail_float = safe_float(detail.get("FloatVolume"))
        cap_total = float(ev["total_capital"])
        cap_float = float(ev["circulating_capital"])

        total_ratio = (
            detail_total / cap_total
            if np.isfinite(detail_total) and detail_total > 0 and cap_total > 0
            else math.nan
        )
        float_ratio = (
            detail_float / cap_float
            if np.isfinite(detail_float) and detail_float > 0 and cap_float > 0
            else math.nan
        )

        rows.append({
            "symbol": symbol,
            "status": "OK",
            "announce_date": ev["announce_date"],
            "report_date": ev["report_date"],
            "capital_total": cap_total,
            "detail_total_volume": detail_total,
            "total_unit_ratio_detail_over_capital": total_ratio,
            "capital_float": cap_float,
            "detail_float_volume": detail_float,
            "float_unit_ratio_detail_over_capital": float_ratio,
        })

    return rows


def coverage_for_panel(
    con: duckdb.DuckDBPyConnection,
    universe: str,
    events_by_symbol: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]],
    folds: dict[str, tuple[str, str]],
) -> list[dict]:
    p = panel_path(universe)
    q = p.as_posix().replace("'", "''")

    panel = con.execute(
        f"""
        SELECT signal_date, symbol
        FROM read_parquet('{q}')
        WHERE ret120 IS NOT NULL
          AND signal_date <= '{CUTOFF}'
        ORDER BY symbol, signal_date
        """
    ).fetchdf()

    panel["covered"] = False
    panel["capital_age_days"] = np.nan

    for symbol, idxs in panel.groupby("symbol").groups.items():
        pack = events_by_symbol.get(symbol)
        if pack is None:
            continue

        announce_dates, announce_ordinals, _ = pack
        dates = panel.loc[idxs, "signal_date"].astype(str).to_numpy()

        # YYYYMMDD lexical ordering equals date ordering.
        pos = np.searchsorted(announce_dates, dates, side="right") - 1
        ok = pos >= 0

        if np.any(ok):
            selected_ann = announce_ordinals[pos[ok]]
            signal_ord = np.array(
                [
                    datetime.strptime(d, "%Y%m%d").date().toordinal()
                    for d in dates[ok]
                ],
                dtype=float,
            )
            panel.loc[np.array(list(idxs))[ok], "covered"] = True
            panel.loc[np.array(list(idxs))[ok], "capital_age_days"] = (
                signal_ord - selected_ann
            )

    rows = []

    def add_row(label: str, x: pd.DataFrame):
        total = len(x)
        covered = int(x["covered"].sum())
        age = x.loc[x["covered"], "capital_age_days"]
        rows.append({
            "universe": universe,
            "segment": label,
            "rows": total,
            "covered_rows": covered,
            "coverage": covered / total if total else math.nan,
            "median_capital_age_days": float(age.median()) if len(age) else math.nan,
            "p90_capital_age_days": float(age.quantile(0.90)) if len(age) else math.nan,
        })

    add_row("ALL", panel)

    for name in ("F1", "F2", "F3"):
        start, end = folds[name]
        add_row(
            name,
            panel[
                (panel["signal_date"] >= start)
                & (panel["signal_date"] <= end)
            ],
        )

    return rows


def load_folds() -> dict[str, tuple[str, str]]:
    path = EDGE_DIR / "ret120_temporal_v01" / "audit.json"
    if not path.exists():
        raise FileNotFoundError(
            f"Missing temporal audit provenance: {path}"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    folds_raw = payload.get("folds") or {}
    folds = {}
    for name in ("F1", "F2", "F3"):
        item = folds_raw.get(name) or {}
        start = str(item.get("start") or "")
        end = str(item.get("end") or "")
        if len(start) != 8 or len(end) != 8:
            raise RuntimeError(f"Invalid frozen fold in {path}: {name}={item}")
        folds[name] = (start, end)
    return folds


def main():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--skip-download",
        action="store_true",
        help="Use only locally cached QMT Capital data.",
    )
    args = p.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()

    try:
        symbols = load_union_symbols(con)
        folds = load_folds()

        print("=" * 100)
        print("RET120 QMT CAPITAL PIT SOURCE V0.1")
        print("=" * 100)
        print("Union symbols:", len(symbols))
        print("Cutoff:", CUTOFF)
        print("Download start:", DOWNLOAD_START)
        print()

        if not args.skip_download:
            download_capital(symbols)
            print()

        raw = xtdata.get_financial_data(
            symbols,
            table_list=["Capital"],
            start_time=DOWNLOAD_START,
            end_time=CUTOFF,
            report_type="announce_time",
        )

        events, per_symbol_audit = extract_capital_events(raw, symbols)

        if events.empty:
            raise RuntimeError(
                "No valid QMT Capital events were returned. "
                "Do not proceed to size exposure audit."
            )

        events.to_csv(
            OUT_DIR / "capital_events.csv",
            index=False,
            encoding="utf-8-sig",
        )
        pd.DataFrame(per_symbol_audit).to_csv(
            OUT_DIR / "per_symbol_audit.csv",
            index=False,
            encoding="utf-8-sig",
        )

        unit_checks = compare_current_instrument_units(events, symbols)
        pd.DataFrame(unit_checks).to_csv(
            OUT_DIR / "unit_checks.csv",
            index=False,
            encoding="utf-8-sig",
        )

        events_by_symbol = {}
        for symbol, x in events.groupby("symbol"):
            x = x.sort_values("announce_date")
            dates = x["announce_date"].astype(str).to_numpy()
            ordinals = np.array(
                [
                    datetime.strptime(d, "%Y%m%d").date().toordinal()
                    for d in dates
                ],
                dtype=float,
            )
            totals = x["total_capital"].to_numpy(dtype=float)
            events_by_symbol[symbol] = (dates, ordinals, totals)

        coverage_rows = []
        for universe in UNIVERSES:
            coverage_rows.extend(
                coverage_for_panel(con, universe, events_by_symbol, folds)
            )

        coverage = pd.DataFrame(coverage_rows)
        coverage.to_csv(
            OUT_DIR / "coverage_summary.csv",
            index=False,
            encoding="utf-8-sig",
        )

        ok_symbols = sum(
            1 for r in per_symbol_audit if r.get("status") == "OK"
        )

        ratios = []
        for r in unit_checks:
            for k in (
                "total_unit_ratio_detail_over_capital",
                "float_unit_ratio_detail_over_capital",
            ):
                v = safe_float(r.get(k))
                if np.isfinite(v) and v > 0:
                    ratios.append(v)

        summary = {
            "audit_id": "RET120_QMT_CAPITAL_PIT_SOURCE_V01",
            "cutoff": CUTOFF,
            "download_start": DOWNLOAD_START,
            "union_symbols": len(symbols),
            "symbols_with_valid_capital": ok_symbols,
            "symbol_coverage": ok_symbols / len(symbols) if symbols else math.nan,
            "capital_event_rows": int(len(events)),
            "unit_ratio_median_detail_over_capital":
                float(np.median(ratios)) if ratios else math.nan,
            "coverage": coverage.to_dict(orient="records"),
            "method": {
                "pit_key": "m_anntime <= signal_date",
                "report_date_field": "m_timetag",
                "market_cap_proxy":
                    "unadjusted close(T) * latest-announced capital as of T",
                "industry_audit_status":
                    "DEFERRED_NO_HISTORICAL_PIT_SOURCE",
            },
            "notes": [
                "Capital is treated as an announced-share proxy, not exact effective shares.",
                "Current instrument detail is used only for unit sanity checks, never historical PIT reconstruction.",
                "No production SQLite or execution code is modified.",
            ],
        }

        (OUT_DIR / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        print("Valid Capital symbols:", f"{ok_symbols}/{len(symbols)}")
        print("Capital event rows:", len(events))
        print()

        print("Coverage:")
        for _, r in coverage.iterrows():
            print(
                f"  {r['universe'].upper()} {r['segment']}: "
                f"{r['coverage']:.2%} "
                f"(median age={r['median_capital_age_days']:.0f}d, "
                f"p90={r['p90_capital_age_days']:.0f}d)"
            )

        print()
        if ratios:
            print(
                "Median current-detail / latest-Capital unit ratio:",
                f"{np.median(ratios):.6g}",
            )
        else:
            print("No usable current-detail unit ratio checks.")

        print()
        print("Output:", OUT_DIR)
        print("Industry PIT audit remains deferred.")
        print("No production database was modified.")

    finally:
        con.close()


if __name__ == "__main__":
    main()
