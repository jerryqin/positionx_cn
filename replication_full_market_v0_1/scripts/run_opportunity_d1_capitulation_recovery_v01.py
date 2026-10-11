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

from opportunity.d1_capitulation_recovery import (
    add_forward_labels_from_t4,
    add_same_day_shock_score,
    add_t_features,
    evaluate_d1,
    mark_setup_and_signal,
)

CFG = PROJECT_ROOT / "config/opportunity/d1_capitulation_recovery_v0_1.yaml"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))


def load_market(cfg, pit: pd.DataFrame) -> pd.DataFrame:
    catalog = Path(cfg["market_catalog"])
    symbols = pd.DataFrame({"symbol": sorted(pit["symbol"].astype(str).unique())})
    d0 = pd.to_datetime(pit["trade_date"].min(), format="%Y%m%d").strftime("%Y-%m-%d")
    d1 = pd.to_datetime(pit["trade_date"].max(), format="%Y%m%d").strftime("%Y-%m-%d")

    con = duckdb.connect(str(catalog), read_only=True)
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
        from {cfg["market_table"]} m
        inner join wanted_symbols w on cast(m.symbol as varchar)=w.symbol
        where m.trade_date between (cast(? as date) - interval 120 day)
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
        z = add_t_features(g)
        if z.empty:
            continue
        z = add_forward_labels_from_t4(z)
        parts.append(z)
    if not parts:
        raise RuntimeError("no D1 feature rows built")

    full = pd.concat(parts, ignore_index=True)
    keep = [
        "trade_date", "symbol",
        "ret1", "intraday_return", "close_location", "range_vs_prior20",
        "recovery_3d", "open_t4",
        "D1_TREND25_60_label", "D1_TREND25_60_mfe",
        "D1_TREND25_60_mae_before_hit", "D1_TREND25_60_time_to_target",
        "D1_TREND40_120_label", "D1_TREND40_120_mfe",
        "D1_TREND40_120_mae_before_hit", "D1_TREND40_120_time_to_target",
    ]
    daily = full[keep].drop_duplicates(["trade_date", "symbol"], keep="last")
    panel = pit.merge(daily, on=["trade_date", "symbol"], how="left", validate="one_to_one")

    coverage_cols = ["ret1", "intraday_return", "close_location", "range_vs_prior20", "recovery_3d"]
    coverage = {c: float(panel[c].notna().mean()) for c in coverage_cols}
    if min(coverage.values()) < 0.90:
        raise RuntimeError(f"D1 feature coverage too low: {coverage}")

    panel = add_same_day_shock_score(panel)
    panel = mark_setup_and_signal(
        panel,
        setup_score_min=float(cfg["mechanism"]["setup_score_min"]),
    )
    return panel, coverage


def cmd_check(cfg):
    catalog = Path(cfg["market_catalog"])
    pit = Path(cfg["pit_panel"])
    result = {
        "status": "PASS_D1_CAPITULATION_RECOVERY_V0_1_CHECK"
                  if catalog.exists() and pit.exists()
                  else "BLOCKED_D1_CAPITULATION_RECOVERY_V0_1_CHECK",
        "research_id": cfg["research_id"],
        "market_catalog": str(catalog),
        "market_catalog_exists": catalog.exists(),
        "market_table": cfg["market_table"],
        "pit_panel": str(pit),
        "pit_panel_exists": pit.exists(),
        "pit_panel_sha256": sha256_file(pit) if pit.exists() else None,
        "mechanism": cfg["mechanism"],
        "labels": cfg["labels"],
        "gate": cfg["gate"],
        "safety": cfg["safety"],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if catalog.exists() and pit.exists() else 3


def cmd_run(cfg):
    try:
        panel, coverage = build_panel(cfg)

        primary = evaluate_d1(
            panel,
            "D1_TREND25_60_label",
            selected_min=int(cfg["gate"]["selected_min"]),
            lift_min=float(cfg["gate"]["primary_lift_vs_setup_min"]),
            precision_delta_min=float(cfg["gate"]["primary_precision_minus_setup_min"]),
            positive_years_min=int(cfg["gate"]["positive_years_min"]),
        )
        secondary = evaluate_d1(
            panel,
            "D1_TREND40_120_label",
            selected_min=int(cfg["gate"]["selected_min"]),
            lift_min=float(cfg["gate"]["primary_lift_vs_setup_min"]),
            precision_delta_min=float(cfg["gate"]["primary_precision_minus_setup_min"]),
            positive_years_min=int(cfg["gate"]["positive_years_min"]),
        )

        out = Path(cfg["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        panel_path = out / "d1_capitulation_recovery_v0_1_panel.parquet"
        panel.to_parquet(panel_path, index=False)

        result = {
            "status": "PASS_D1_CAPITULATION_RECOVERY_V0_1_RUN",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "research_id": cfg["research_id"],
            "source": {
                "market_catalog": cfg["market_catalog"],
                "market_table": cfg["market_table"],
                "pit_panel": cfg["pit_panel"],
                "pit_panel_sha256": sha256_file(Path(cfg["pit_panel"])),
            },
            "mechanism": cfg["mechanism"],
            "feature_coverage": coverage,
            "primary": primary,
            "secondary": secondary,
            "overall_verdict": primary["verdict"],
            "interpretation": (
                "Primary gate compares confirmed recovery signals against all frozen "
                "capitulation setups under the identical T+4 open entry timing. "
                "PASS only advances the mechanism to robustness; it is not Strategy qualification."
            ),
            "artifacts": {
                "panel": str(panel_path),
                "panel_sha256": sha256_file(panel_path),
            },
            "safety": cfg["safety"],
        }
        summary = out / "d1_capitulation_recovery_v0_1_summary.json"
        summary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print(f"OUTPUT: {out}")
        return 0
    except Exception as e:
        print(json.dumps({
            "status": "FAIL_CLOSED_D1_CAPITULATION_RECOVERY_V0_1",
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
