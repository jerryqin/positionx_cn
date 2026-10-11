from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from opportunity.capitulation_recovery_audit import (
    add_sequence_features,
    evaluate_capitulation_recovery,
)

PANEL = Path(r"D:\positionx_data\research\opportunity_engine\trend_ignition_v0_1\trend_ignition_v0_1_panel.parquet")
CATALOG = Path(r"D:\positionx_data\catalog\market_data.duckdb")
TABLE = "market.daily_bars_v1"
OUT = Path(r"D:\positionx_data\research\opportunity_engine\capitulation_recovery_audit_v0_1")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_market(panel: pd.DataFrame) -> pd.DataFrame:
    symbols = pd.DataFrame({"symbol": sorted(panel["symbol"].astype(str).unique())})
    d0 = pd.to_datetime(panel["trade_date"].min(), format="%Y%m%d").strftime("%Y-%m-%d")
    d1 = pd.to_datetime(panel["trade_date"].max(), format="%Y%m%d").strftime("%Y-%m-%d")

    con = duckdb.connect(str(CATALOG), read_only=True)
    try:
        con.register("wanted_symbols", symbols)
        q = f"""
        select
            cast(m.symbol as varchar) as symbol,
            strftime(m.trade_date, '%Y%m%d') as trade_date,
            cast(m.open as double) as open,
            cast(m.high as double) as high,
            cast(m.low as double) as low,
            cast(m.close as double) as close,
            cast(m.pre_close as double) as pre_close,
            cast(m.volume as double) as volume
        from {TABLE} m
        inner join wanted_symbols w on cast(m.symbol as varchar)=w.symbol
        where m.trade_date between (cast(? as date) - interval 120 day)
                               and (cast(? as date) + interval 30 day)
        order by m.symbol, m.trade_date
        """
        return con.execute(q, [d0, d1]).fetchdf()
    finally:
        con.close()


def sequence_for_panel(panel: pd.DataFrame, market: pd.DataFrame) -> pd.DataFrame:
    parts = []
    for _, g in market.groupby("symbol", sort=False):
        z = add_sequence_features(g)
        if not z.empty:
            parts.append(z)
    if not parts:
        raise RuntimeError("no sequence rows built")
    f = pd.concat(parts, ignore_index=True)

    keep = [
        "trade_date", "symbol",
        "ret1", "intraday_return", "close_location", "range_vs_prior20",
        "gap_1", "fwd_close_ret_1", "fwd_close_ret_2",
        "fwd_close_ret_3", "fwd_close_ret_5",
        "next1_intraday_return", "next1_close_location",
        "close_above_t_close_1", "close_above_t_high_1", "close_above_t_high_3",
    ]
    f = f[keep].drop_duplicates(["trade_date", "symbol"], keep="last")
    keys = panel[["trade_date", "symbol"]].drop_duplicates()
    return keys.merge(f, on=["trade_date", "symbol"], how="left", validate="one_to_one")


def cmd_check():
    missing = []
    if not PANEL.exists():
        missing.append(str(PANEL))
    if not CATALOG.exists():
        missing.append(str(CATALOG))
    result = {
        "status": "PASS_CAPITULATION_RECOVERY_AUDIT_V0_1_CHECK"
                  if not missing else "BLOCKED_CAPITULATION_RECOVERY_AUDIT_V0_1_CHECK",
        "source_panel": str(PANEL),
        "source_panel_exists": PANEL.exists(),
        "source_panel_sha256": sha256_file(PANEL) if PANEL.exists() else None,
        "market_catalog": str(CATALOG),
        "market_catalog_exists": CATALOG.exists(),
        "market_table": TABLE,
        "missing": missing,
        "research_only": True,
        "production_db": False,
        "broker": False,
        "qmt": False,
        "purpose": (
            "Separate capitulation shock intensity from subsequent T+1..T+5 recovery "
            "using same-day nearest-neighbor controls."
        ),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not missing else 3


def cmd_run():
    if not PANEL.exists() or not CATALOG.exists():
        return cmd_check()

    panel_sha = sha256_file(PANEL)
    panel = pd.read_parquet(PANEL)
    market = load_market(panel)
    seq = sequence_for_panel(panel, market)

    coverage_cols = [
        "ret1", "intraday_return", "close_location", "range_vs_prior20",
        "fwd_close_ret_1", "fwd_close_ret_3",
    ]
    coverage = {c: float(seq[c].notna().mean()) for c in coverage_cols}
    if min(coverage.values()) < 0.90:
        raise RuntimeError(f"sequence feature coverage too low: {coverage}")

    study, matches = evaluate_capitulation_recovery(panel, seq)

    OUT.mkdir(parents=True, exist_ok=True)
    matches_path = OUT / "capitulation_recovery_audit_v0_1_matches.parquet"
    matches.to_parquet(matches_path, index=False)

    result = {
        "status": "PASS_CAPITULATION_RECOVERY_AUDIT_V0_1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "research_id": "OPPORTUNITY_CAPITULATION_RECOVERY_AUDIT_V0_1",
        "source": {
            "panel": str(PANEL),
            "panel_sha256": panel_sha,
            "market_catalog": str(CATALOG),
            "market_table": TABLE,
            "frozen_parent_verdict": "FAST_REJECT_TREND_IGNITION_MECHANISM_V0_1",
            "bottom_reversal_trigger_audit": (
                "boundary day is capitulation-like; classic same-day reversal patterns rejected"
            ),
        },
        "feature_coverage": coverage,
        **study,
        "artifacts": {
            "matches": str(matches_path),
            "matches_sha256": sha256_file(matches_path),
        },
        "safety": {
            "research_only": True,
            "production_db": False,
            "broker": False,
            "qmt": False,
        },
    }

    summary = OUT / "capitulation_recovery_audit_v0_1_summary.json"
    summary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"OUTPUT: {OUT}")
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["check", "run"])
    args = p.parse_args()
    return cmd_check() if args.command == "check" else cmd_run()


if __name__ == "__main__":
    raise SystemExit(main())
