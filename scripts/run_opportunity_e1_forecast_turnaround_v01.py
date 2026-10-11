from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from opportunity.e1_forecast_turnaround import (
    prepare_event_universe,
    synthetic_adjust_ohlc,
    attach_next_tradable_entry,
    add_forward_labels,
    evaluate_e1,
)

CFG = PROJECT_ROOT / "config/opportunity/e1_forecast_turnaround_v0_1.yaml"


def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))


def qtable(full: str) -> str:
    return ".".join('"' + x.replace('"', '""') + '"' for x in full.split("."))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_events(con, cfg):
    return con.execute(
        f"select * from {qtable(cfg['event_table'])}"
    ).fetchdf()


def load_market(con, cfg):
    table = qtable(cfg["market_table"])
    # Robust to DATE / VARCHAR / integer-like trade_date representations.
    return con.execute(f"""
        select
            cast(symbol as varchar) as symbol,
            regexp_replace(cast(trade_date as varchar), '[^0-9]', '', 'g') as trade_date_key,
            open, high, low, close, pre_close
        from {table}
        where regexp_replace(cast(trade_date as varchar), '[^0-9]', '', 'g')
              between '20190101' and '20271231'
        order by symbol, trade_date_key
    """).fetchdf()


def check_asset_registry(con, cfg):
    row = con.execute("""
        select asset_version, source_sha256, row_count, table_name
        from research.asset_registry_v1
        where asset_id=?
    """, [cfg["asset"]["asset_id"]]).fetchone()
    if row is None:
        raise RuntimeError("forecast semantic asset registry row missing")
    actual = {
        "asset_version": row[0],
        "source_sha256": row[1],
        "row_count": int(row[2]),
        "table_name": row[3],
    }
    if actual["asset_version"] != cfg["asset"]["asset_version"]:
        raise RuntimeError(f"asset version mismatch {actual}")
    if actual["source_sha256"] != cfg["asset"]["source_sha256"]:
        raise RuntimeError(f"asset SHA mismatch {actual}")
    if actual["table_name"] != cfg["event_table"]:
        raise RuntimeError(f"asset table mismatch {actual}")
    return actual


def cmd_check(cfg):
    catalog = Path(cfg["catalog"])
    result = {
        "status": "PASS_E1_FORECAST_TURNAROUND_V0_1_CHECK",
        "research_id": cfg["research_id"],
        "catalog": str(catalog),
        "catalog_exists": catalog.exists(),
        "event_table": cfg["event_table"],
        "market_table": cfg["market_table"],
        "asset": cfg["asset"],
        "mechanism": cfg["mechanism"],
        "baseline": cfg["baseline"],
        "labels": cfg["labels"],
        "data_quality_gate": cfg["data_quality_gate"],
        "fast_reject": cfg["fast_reject"],
        "safety": cfg["safety"],
    }
    if not catalog.exists():
        result["status"] = "BLOCKED_E1_FORECAST_TURNAROUND_V0_1_CHECK"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"].startswith("PASS") else 3


def cmd_run(cfg):
    try:
        catalog = Path(cfg["catalog"])
        if not catalog.exists():
            return cmd_check(cfg)

        con = duckdb.connect(str(catalog), read_only=True)
        try:
            registry = check_asset_registry(con, cfg)
            events = load_events(con, cfg)
            market = load_market(con, cfg)
        finally:
            con.close()

        market["trade_date"] = pd.to_datetime(
            market["trade_date_key"], format="%Y%m%d", errors="coerce"
        )
        market = market.drop(columns=["trade_date_key"])
        market["symbol"] = market["symbol"].astype("string").str.strip()

        events["symbol"] = events["symbol"].astype("string").str.strip()
        ev = prepare_event_universe(events)
        bars = synthetic_adjust_ohlc(market)

        setup_rows = int(ev["e1_setup"].sum())
        signal_rows = int(ev["e1_signal"].sum())

        factor_valid_rate = float(bars["_factor_valid"].mean()) if len(bars) else 0.0

        ev = attach_next_tradable_entry(ev, bars)
        mapped = ev.loc[ev["e1_setup"], "entry_date"].notna()
        entry_mapping_coverage = float(mapped.mean()) if setup_rows else 0.0

        dq = cfg["data_quality_gate"]
        dq_checks = {
            "entry_mapping_coverage_min": (
                entry_mapping_coverage >= float(dq["entry_mapping_coverage_min"])
            ),
            "market_factor_valid_rate_min": (
                factor_valid_rate >= float(dq["market_factor_valid_rate_min"])
            ),
        }
        if not all(dq_checks.values()):
            raise RuntimeError(f"data quality gate failed {dq_checks}")

        p = cfg["labels"]["primary"]
        s = cfg["labels"]["secondary"]
        panel = add_forward_labels(
            ev,
            bars,
            primary_target=float(p["target"]),
            primary_horizon=int(p["horizon_sessions"]),
            primary_mae_floor=float(p["mae_floor"]),
            secondary_target=float(s["target"]),
            secondary_horizon=int(s["horizon_sessions"]),
            secondary_mae_floor=float(s["mae_floor"]),
        )

        g = cfg["fast_reject"]
        min_stratum = int(cfg["baseline"]["min_setup_per_stratum"])

        primary = evaluate_e1(
            panel,
            "E1_TREND25_60_label",
            selected_min=int(g["selected_min"]),
            lift_min=float(g["primary_lift_vs_matched_setup_min"]),
            precision_delta_min=float(g["primary_precision_minus_matched_setup_min"]),
            positive_years_min=int(g["positive_years_min"]),
            min_setup_per_stratum=min_stratum,
        )
        secondary = evaluate_e1(
            panel,
            "E1_TREND40_120_label",
            selected_min=int(g["selected_min"]),
            lift_min=float(g["primary_lift_vs_matched_setup_min"]),
            precision_delta_min=float(g["primary_precision_minus_matched_setup_min"]),
            positive_years_min=int(g["positive_years_min"]),
            min_setup_per_stratum=min_stratum,
        )

        # Cluster diagnostic: multiple event identities leading to same symbol/entry day.
        setup_mapped = panel[panel["e1_setup"] & panel["entry_date"].notna()].copy()
        cluster_counts = (
            setup_mapped.groupby(["symbol", "entry_date"])
            .size()
        )
        clustered_rows = int(cluster_counts[cluster_counts > 1].sum()) if len(cluster_counts) else 0
        cluster_rate = (
            float(clustered_rows / len(setup_mapped))
            if len(setup_mapped) else 0.0
        )

        out = Path(cfg["output_dir"])
        out.mkdir(parents=True, exist_ok=True)

        panel_path = out / "e1_forecast_turnaround_v0_1_panel.parquet"
        panel.to_parquet(panel_path, index=False)

        result = {
            "status": "PASS_E1_FORECAST_TURNAROUND_V0_1_RUN",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "research_id": cfg["research_id"],
            "asset_registry": registry,
            "mechanism": cfg["mechanism"],
            "baseline": cfg["baseline"],
            "labels": cfg["labels"],
            "coverage": {
                "event_rows": int(len(events)),
                "setup_rows": setup_rows,
                "signal_rows": signal_rows,
                "entry_mapping_coverage": entry_mapping_coverage,
                "market_rows": int(len(bars)),
                "market_factor_valid_rate": factor_valid_rate,
                "primary_label_coverage_of_setup": float(
                    panel.loc[panel["e1_setup"], "E1_TREND25_60_label"].notna().mean()
                ) if setup_rows else 0.0,
                "secondary_label_coverage_of_setup": float(
                    panel.loc[panel["e1_setup"], "E1_TREND40_120_label"].notna().mean()
                ) if setup_rows else 0.0,
            },
            "event_cluster_diagnostic": {
                "mapped_setup_rows": int(len(setup_mapped)),
                "symbol_entry_clusters_gt1_rows": clustered_rows,
                "clustered_row_rate": cluster_rate,
            },
            "data_quality_checks": dq_checks,
            "primary": primary,
            "secondary": secondary,
            "overall_verdict": primary["verdict"],
            "artifacts": {
                "panel": str(panel_path),
                "panel_sha256": sha256_file(panel_path),
            },
            "safety": cfg["safety"],
        }

        summary = out / "e1_forecast_turnaround_v0_1_summary.json"
        summary.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        print(json.dumps(result, ensure_ascii=False, indent=2))
        print(f"OUTPUT: {out}")
        return 0

    except Exception as e:
        print(json.dumps({
            "status": "FAIL_CLOSED_E1_FORECAST_TURNAROUND_V0_1",
            "error": f"{type(e).__name__}: {e}",
        }, ensure_ascii=False, indent=2))
        return 3


def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["check", "run"])
    args = p.parse_args()
    cfg = load_cfg()
    return cmd_check(cfg) if args.command == "check" else cmd_run(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
