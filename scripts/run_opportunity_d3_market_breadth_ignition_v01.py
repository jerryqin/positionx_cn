
from __future__ import annotations
import argparse, hashlib, json, sys
from datetime import datetime
from pathlib import Path
import duckdb, pandas as pd, yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from opportunity.d3_market_breadth_ignition import (
    add_breadth_and_signal, add_forward_labels_from_t1, evaluate_d3, synthetic_adjust_close
)
CFG = PROJECT_ROOT / "config/opportunity/d3_market_breadth_ignition_v0_1.yaml"

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024*1024), b""):
            h.update(chunk)
    return h.hexdigest()

def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))

def load_market(cfg, pit):
    catalog = Path(cfg["market_catalog"])
    symbols = pd.DataFrame({"symbol": sorted(pit["symbol"].astype(str).unique())})
    d0 = pd.to_datetime(pit["trade_date"].min(), format="%Y%m%d").strftime("%Y-%m-%d")
    d1 = pd.to_datetime(pit["trade_date"].max(), format="%Y%m%d").strftime("%Y-%m-%d")
    con = duckdb.connect(str(catalog), read_only=True)
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
    pit = pit_raw[["trade_date","symbol"]].drop_duplicates().copy()
    pit["trade_date"] = pit["trade_date"].astype(str)
    pit["symbol"] = pit["symbol"].astype(str)
    market = load_market(cfg, pit)
    parts = []
    for _, g in market.groupby("symbol", sort=False):
        z = synthetic_adjust_close(g)
        if not z.empty:
            parts.append(add_forward_labels_from_t1(z))
    if not parts:
        raise RuntimeError("no D3 rows built")
    full = pd.concat(parts, ignore_index=True)
    keep = [
        "trade_date","symbol","ret20","above_ma20",
        "D3_TREND25_60_label","D3_TREND25_60_mfe","D3_TREND25_60_mae_before_hit","D3_TREND25_60_time_to_target",
        "D3_TREND40_120_label","D3_TREND40_120_mfe","D3_TREND40_120_mae_before_hit","D3_TREND40_120_time_to_target",
    ]
    daily = full[keep].drop_duplicates(["trade_date","symbol"], keep="last")
    panel = pit.merge(daily, on=["trade_date","symbol"], how="left", validate="one_to_one")
    coverage = {
        "ret20": float(panel["ret20"].notna().mean()),
        "above_ma20": float(panel["above_ma20"].notna().mean()),
        "D3_TREND25_60_label": float(panel["D3_TREND25_60_label"].notna().mean()),
    }
    if coverage["ret20"] < 0.90 or coverage["above_ma20"] < 0.90:
        raise RuntimeError(f"D3 feature coverage too low: {coverage}")
    panel = add_breadth_and_signal(
        panel,
        leader_rank_min=float(cfg["mechanism"]["leader_rank_min"]),
        breadth_delta_sessions=int(cfg["mechanism"]["breadth_delta_sessions"]),
        breadth_history_sessions=int(cfg["mechanism"]["breadth_history_sessions"]),
        breadth_ignition_quantile=float(cfg["mechanism"]["breadth_ignition_quantile"]),
    )
    return panel, coverage

def cmd_check(cfg):
    catalog, pit = Path(cfg["market_catalog"]), Path(cfg["pit_panel"])
    result = {
        "status": "PASS_D3_MARKET_BREADTH_IGNITION_V0_1_CHECK" if catalog.exists() and pit.exists() else "BLOCKED_D3_MARKET_BREADTH_IGNITION_V0_1_CHECK",
        "research_id": cfg["research_id"],
        "market_catalog": str(catalog), "market_catalog_exists": catalog.exists(), "market_table": cfg["market_table"],
        "pit_panel": str(pit), "pit_panel_exists": pit.exists(),
        "pit_panel_sha256": sha256_file(pit) if pit.exists() else None,
        "mechanism": cfg["mechanism"], "labels": cfg["labels"], "gate": cfg["gate"], "safety": cfg["safety"],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if catalog.exists() and pit.exists() else 3

def cmd_run(cfg):
    try:
        panel, coverage = build_panel(cfg)
        primary = evaluate_d3(
            panel, "D3_TREND25_60_label",
            int(cfg["gate"]["selected_min"]),
            float(cfg["gate"]["primary_lift_vs_leader_setup_min"]),
            float(cfg["gate"]["primary_precision_minus_leader_setup_min"]),
            int(cfg["gate"]["positive_years_min"]),
        )
        secondary = evaluate_d3(
            panel, "D3_TREND40_120_label",
            int(cfg["gate"]["selected_min"]),
            float(cfg["gate"]["primary_lift_vs_leader_setup_min"]),
            float(cfg["gate"]["primary_precision_minus_leader_setup_min"]),
            int(cfg["gate"]["positive_years_min"]),
        )
        out = Path(cfg["output_dir"]); out.mkdir(parents=True, exist_ok=True)
        panel_path = out/"d3_market_breadth_ignition_v0_1_panel.parquet"
        panel.to_parquet(panel_path, index=False)
        daily = panel[["trade_date","breadth20","breadth_eligible","breadth_delta5","breadth_delta5_prior_q75","breadth_ignition"]].drop_duplicates("trade_date").sort_values("trade_date")
        breadth_path = out/"d3_market_breadth_ignition_v0_1_daily_breadth.parquet"
        daily.to_parquet(breadth_path, index=False)
        result = {
            "status": "PASS_D3_MARKET_BREADTH_IGNITION_V0_1_RUN",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "research_id": cfg["research_id"],
            "source": {"market_catalog":cfg["market_catalog"],"market_table":cfg["market_table"],"pit_panel":cfg["pit_panel"],"pit_panel_sha256":sha256_file(Path(cfg["pit_panel"]))},
            "mechanism": cfg["mechanism"],
            "feature_coverage": coverage,
            "breadth_diagnostics": {
                "breadth_days": int(daily["breadth20"].notna().sum()),
                "ignition_days": int(daily["breadth_ignition"].fillna(False).sum()),
                "first_ignition_date": str(daily.loc[daily["breadth_ignition"].fillna(False),"trade_date"].min()) if daily["breadth_ignition"].fillna(False).any() else None,
            },
            "primary": primary, "secondary": secondary, "overall_verdict": primary["verdict"],
            "interpretation": "Tests whether market-wide breadth acceleration adds predictive value beyond being an already-strong top-quartile RET20 stock. PASS advances only to robustness.",
            "artifacts": {"panel":str(panel_path),"panel_sha256":sha256_file(panel_path),"daily_breadth":str(breadth_path),"daily_breadth_sha256":sha256_file(breadth_path)},
            "safety": cfg["safety"],
        }
        (out/"d3_market_breadth_ignition_v0_1_summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print(f"OUTPUT: {out}")
        return 0
    except Exception as e:
        print(json.dumps({"status":"FAIL_CLOSED_D3_MARKET_BREADTH_IGNITION_V0_1","error":f"{type(e).__name__}: {e}"}, ensure_ascii=False, indent=2))
        return 3

def main():
    p = argparse.ArgumentParser(); p.add_argument("command", choices=["check","run"]); args=p.parse_args()
    cfg=load_cfg()
    return cmd_check(cfg) if args.command=="check" else cmd_run(cfg)
if __name__=="__main__":
    raise SystemExit(main())
