from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from opportunity.d4_sector_ignition import (
    add_forward_labels_from_t1,
    add_sector_ignition_features,
    attach_pit_industry,
    evaluate_d4,
    synthetic_adjust_close,
)

CFG = PROJECT_ROOT / "config/opportunity/d4_sector_ignition_v0_1.yaml"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))


def load_membership(cfg):
    con = duckdb.connect(str(cfg["market_catalog"]), read_only=True)
    try:
        tables = con.execute("""
            select table_schema || '.' || table_name as n
            from information_schema.tables
        """).fetchdf()["n"].astype(str).tolist()
        if cfg["industry_table"] not in tables:
            raise RuntimeError(
                f"industry table missing: {cfg['industry_table']}; run promotion first"
            )
        return con.execute(
            f"select * from {cfg['industry_table']} order by symbol, effective_from"
        ).fetchdf()
    finally:
        con.close()


def load_market(cfg, pit):
    symbols = pd.DataFrame({"symbol": sorted(pit["symbol"].astype(str).unique())})
    d0 = pd.to_datetime(pit["trade_date"].min(), format="%Y%m%d").strftime("%Y-%m-%d")
    d1 = pd.to_datetime(pit["trade_date"].max(), format="%Y%m%d").strftime("%Y-%m-%d")

    con = duckdb.connect(str(cfg["market_catalog"]), read_only=True)
    try:
        con.register("wanted_symbols", symbols)
        q = f"""
        select
            cast(m.symbol as varchar) as "symbol",
            strftime(m.trade_date, '%Y%m%d') as "trade_date",
            cast(m.open as double) as "open",
            cast(m.high as double) as "high",
            cast(m.low as double) as "low",
            cast(m.close as double) as "close",
            cast(m.pre_close as double) as "pre_close"
        from {cfg["market_table"]} m
        inner join wanted_symbols w on cast(m.symbol as varchar)=w.symbol
        where m.trade_date between (cast(? as date) - interval 500 day)
                               and (cast(? as date) + interval 220 day)
        order by m.symbol, m.trade_date
        """
        return con.execute(q, [d0, d1]).fetchdf()
    finally:
        con.close()


def build_panel(cfg):
    pit_path = Path(cfg["pit_panel"])
    pit_raw = pd.read_parquet(pit_path)
    pit = pit_raw[["trade_date", "symbol"]].drop_duplicates().copy()
    pit["trade_date"] = pit["trade_date"].astype(str)
    pit["symbol"] = pit["symbol"].astype(str)

    market = load_market(cfg, pit)
    parts = []
    for _, g in market.groupby("symbol", sort=False):
        z = synthetic_adjust_close(g)
        if not z.empty:
            parts.append(add_forward_labels_from_t1(z))
    if not parts:
        raise RuntimeError("no D4 market rows built")

    full = pd.concat(parts, ignore_index=True)
    keep = [
        "trade_date", "symbol", "ret20", "above_ma20",
        "D4_TREND25_60_label", "D4_TREND25_60_mfe",
        "D4_TREND25_60_mae_before_hit", "D4_TREND25_60_time_to_target",
        "D4_TREND40_120_label", "D4_TREND40_120_mfe",
        "D4_TREND40_120_mae_before_hit", "D4_TREND40_120_time_to_target",
    ]
    daily = full[keep].drop_duplicates(["trade_date", "symbol"], keep="last")
    panel = pit.merge(daily, on=["trade_date", "symbol"], how="left", validate="one_to_one")

    membership = load_membership(cfg)
    panel = attach_pit_industry(panel, membership)

    coverage = {
        "ret20": float(panel["ret20"].notna().mean()),
        "above_ma20": float(panel["above_ma20"].notna().mean()),
        "industry_membership": float(panel["sector_key"].notna().mean()),
        "D4_TREND25_60_label": float(panel["D4_TREND25_60_label"].notna().mean()),
    }
    if coverage["industry_membership"] < 0.95:
        raise RuntimeError(f"D4 industry membership coverage too low: {coverage}")

    m = cfg["mechanism"]
    panel = add_sector_ignition_features(
        panel,
        sector_min_members=int(m["sector_min_members"]),
        setup_rank_min=float(m["stock_setup"]["sector_ret20_rank_min"]),
        setup_rank_max=float(m["stock_setup"]["sector_ret20_rank_max_exclusive"]),
        breadth_delta_sessions=int(m["breadth"]["delta_sessions"]),
        history_sessions=int(m["breadth"]["history_sessions"]),
        ignition_quantile=float(m["breadth"]["ignition_quantile"]),
    )
    return panel, coverage


def cmd_check(cfg):
    catalog = Path(cfg["market_catalog"])
    pit = Path(cfg["pit_panel"])

    table_exists = False
    registry = None
    if catalog.exists():
        con = duckdb.connect(str(catalog), read_only=True)
        try:
            tables = con.execute("""
                select table_schema || '.' || table_name as n
                from information_schema.tables
            """).fetchdf()["n"].astype(str).tolist()
            table_exists = cfg["industry_table"] in tables

            if "research.asset_registry_v1" in tables:
                row = con.execute("""
                    select asset_id, asset_version, source_sha256, row_count
                    from research.asset_registry_v1
                    where asset_id=? and asset_version=?
                """, [cfg["industry_asset_id"], cfg["industry_asset_version"]]).fetchdf()
                if len(row):
                    registry = row.iloc[0].to_dict()
        finally:
            con.close()

    result = {
        "status": "PASS_D4_SECTOR_IGNITION_V0_1_CHECK"
                  if catalog.exists() and pit.exists() and table_exists
                  else "BLOCKED_D4_SECTOR_IGNITION_V0_1_CHECK",
        "research_id": cfg["research_id"],
        "market_catalog": str(catalog),
        "market_catalog_exists": catalog.exists(),
        "pit_panel": str(pit),
        "pit_panel_exists": pit.exists(),
        "pit_panel_sha256": sha256_file(pit) if pit.exists() else None,
        "industry_table": cfg["industry_table"],
        "industry_table_exists": table_exists,
        "industry_registry": registry,
        "mechanism": cfg["mechanism"],
        "labels": cfg["labels"],
        "gate": cfg["gate"],
        "operational_diagnostic": cfg["operational_diagnostic"],
        "safety": cfg["safety"],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result["status"].startswith("PASS") else 3


def cmd_run(cfg):
    try:
        panel, coverage = build_panel(cfg)
        g = cfg["gate"]

        primary = evaluate_d4(
            panel, "D4_TREND25_60_label",
            selected_min=int(g["selected_min"]),
            lift_min=float(g["primary_lift_vs_setup_min"]),
            precision_delta_min=float(g["primary_precision_minus_setup_min"]),
            positive_years_min=int(g["positive_years_min"]),
        )
        secondary = evaluate_d4(
            panel, "D4_TREND40_120_label",
            selected_min=int(g["selected_min"]),
            lift_min=float(g["primary_lift_vs_setup_min"]),
            precision_delta_min=float(g["primary_precision_minus_setup_min"]),
            positive_years_min=int(g["positive_years_min"]),
        )

        out = Path(cfg["output_dir"])
        out.mkdir(parents=True, exist_ok=True)

        panel_path = out / "d4_sector_ignition_v0_1_panel.parquet"
        panel.to_parquet(panel_path, index=False)

        sector_daily = (
            panel[
                [
                    "trade_date", "sector_key", "industry_code", "industry_name",
                    "standard", "level", "sector_breadth20", "sector_members",
                    "sector_breadth_delta5", "market_breadth20", "market_breadth_delta5",
                    "sector_excess_breadth_delta5", "sector_excess_prior_q75",
                    "sector_ignition",
                ]
            ]
            .drop_duplicates(["trade_date", "sector_key"])
            .sort_values(["trade_date", "sector_key"])
        )
        sector_path = out / "d4_sector_ignition_v0_1_sector_daily.parquet"
        sector_daily.to_parquet(sector_path, index=False)

        result = {
            "status": "PASS_D4_SECTOR_IGNITION_V0_1_RUN",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "research_id": cfg["research_id"],
            "source": {
                "market_catalog": cfg["market_catalog"],
                "market_table": cfg["market_table"],
                "pit_panel": cfg["pit_panel"],
                "pit_panel_sha256": sha256_file(Path(cfg["pit_panel"])),
                "industry_table": cfg["industry_table"],
                "industry_asset_id": cfg["industry_asset_id"],
                "industry_asset_version": cfg["industry_asset_version"],
                "industry_source_sha256": cfg["industry_source_sha256"],
            },
            "mechanism": cfg["mechanism"],
            "feature_coverage": coverage,
            "sector_diagnostics": {
                "sector_daily_rows": int(len(sector_daily)),
                "sector_keys": int(sector_daily["sector_key"].nunique()),
                "ignition_sector_days": int(sector_daily["sector_ignition"].fillna(False).sum()),
                "ignition_dates": int(
                    sector_daily.loc[
                        sector_daily["sector_ignition"].fillna(False), "trade_date"
                    ].nunique()
                ),
            },
            "primary": primary,
            "secondary": secondary,
            "overall_verdict": primary["verdict"],
            "artifacts": {
                "panel": str(panel_path),
                "panel_sha256": sha256_file(panel_path),
                "sector_daily": str(sector_path),
                "sector_daily_sha256": sha256_file(sector_path),
            },
            "safety": cfg["safety"],
        }

        summary = out / "d4_sector_ignition_v0_1_summary.json"
        summary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

        print(json.dumps(result, ensure_ascii=False, indent=2))
        print(f"OUTPUT: {out}")
        return 0
    except Exception as e:
        print(json.dumps({
            "status": "FAIL_CLOSED_D4_SECTOR_IGNITION_V0_1",
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
