from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from opportunity.bottom_reversal_trigger_audit import (
    add_trigger_features,
    evaluate_bottom_reversal_trigger_audit,
)

PANEL = Path(r"D:\positionx_data\research\opportunity_engine\trend_ignition_v0_1\trend_ignition_v0_1_panel.parquet")
CATALOG = Path(r"D:\positionx_data\catalog\market_data.duckdb")
TABLE = "market.daily_bars_v1"
OUT = Path(r"D:\positionx_data\research\opportunity_engine\bottom_reversal_trigger_audit_v0_1")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_market_for_panel(panel: pd.DataFrame) -> pd.DataFrame:
    symbols = pd.DataFrame({"symbol": sorted(panel["symbol"].astype(str).unique())})
    date_min = pd.to_datetime(panel["trade_date"].min(), format="%Y%m%d").strftime("%Y-%m-%d")
    date_max = pd.to_datetime(panel["trade_date"].max(), format="%Y%m%d").strftime("%Y-%m-%d")

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
        return con.execute(q, [date_min, date_max]).fetchdf()
    finally:
        con.close()


def build_trigger_rows(panel: pd.DataFrame, market: pd.DataFrame) -> pd.DataFrame:
    parts = []
    for _, g in market.groupby("symbol", sort=False):
        z = add_trigger_features(g)
        if not z.empty:
            parts.append(z)
    if not parts:
        raise RuntimeError("no trigger feature rows built")
    f = pd.concat(parts, ignore_index=True)

    keep = [
        "trade_date", "symbol",
        "ret1", "prior5_return", "intraday_return",
        "close_location", "lower_wick_share", "upper_wick_share",
        "range_vs_prior20", "volume_vs_prior20", "recovery_from_low",
        "distance_to_prior20_low",
        "failed_breakdown_5d", "failed_breakdown_20d",
        "bullish_reversal_day", "lower_wick_dominant",
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
        "status": "PASS_BOTTOM_REVERSAL_TRIGGER_AUDIT_V0_1_CHECK"
                  if not missing else "BLOCKED_BOTTOM_REVERSAL_TRIGGER_AUDIT_V0_1_CHECK",
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
            "Audit T-known price/volume reversal triggers at favorable-path episode boundaries. "
            "No detector threshold tuning and no strategy qualification."
        ),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not missing else 3


def cmd_run():
    if not PANEL.exists() or not CATALOG.exists():
        return cmd_check()

    panel_sha = sha256_file(PANEL)
    panel = pd.read_parquet(PANEL)
    market = load_market_for_panel(panel)
    trigger = build_trigger_rows(panel, market)

    coverage = {
        c: float(trigger[c].notna().mean())
        for c in [
            "ret1", "prior5_return", "close_location",
            "range_vs_prior20", "volume_vs_prior20"
        ]
    }
    if min(coverage.values()) < 0.90:
        raise RuntimeError(f"trigger feature coverage too low: {coverage}")

    study, events = evaluate_bottom_reversal_trigger_audit(panel, trigger)

    OUT.mkdir(parents=True, exist_ok=True)
    events_path = OUT / "bottom_reversal_trigger_audit_v0_1_events.parquet"
    events.to_parquet(events_path, index=False)

    result = {
        "status": "PASS_BOTTOM_REVERSAL_TRIGGER_AUDIT_V0_1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "research_id": "OPPORTUNITY_BOTTOM_REVERSAL_TRIGGER_AUDIT_V0_1",
        "source": {
            "panel": str(PANEL),
            "panel_sha256": panel_sha,
            "market_catalog": str(CATALOG),
            "market_table": TABLE,
            "frozen_parent_verdict": "FAST_REJECT_TREND_IGNITION_MECHANISM_V0_1",
            "anchor_audit_conclusion": (
                "boundary and max-MFE anchors are close; favorable episodes cluster around "
                "short-term weakening followed by recovery"
            ),
        },
        "feature_coverage": coverage,
        **study,
        "artifacts": {
            "event_rows": str(events_path),
            "event_rows_sha256": sha256_file(events_path),
        },
        "safety": {
            "research_only": True,
            "production_db": False,
            "broker": False,
            "qmt": False,
        },
    }

    summary = OUT / "bottom_reversal_trigger_audit_v0_1_summary.json"
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
