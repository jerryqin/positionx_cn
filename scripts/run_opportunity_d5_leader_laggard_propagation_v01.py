
from __future__ import annotations
import argparse, hashlib, json, sys
from datetime import datetime
from pathlib import Path
import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from opportunity.d5_leader_laggard_propagation import add_leader_propagation_features, evaluate_d5

CFG = PROJECT_ROOT / "config/opportunity/d5_leader_laggard_propagation_v0_1.yaml"

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024*1024), b""):
            h.update(chunk)
    return h.hexdigest()

def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))

def validate_source(cfg):
    p = Path(cfg["source_panel"])
    if not p.exists():
        raise FileNotFoundError(p)
    actual = sha256_file(p)
    if actual != cfg["source_panel_sha256"]:
        raise RuntimeError(f"source SHA mismatch expected={cfg['source_panel_sha256']} actual={actual}")
    return p, actual

def build_panel(cfg):
    p, actual = validate_source(cfg)
    cols = [
        "trade_date","symbol","ret20",
        "industry_code","industry_name","standard","level","sector_key",
        "D4_TREND25_60_label","D4_TREND40_120_label",
    ]
    panel = pd.read_parquet(p, columns=cols)
    panel["trade_date"] = panel["trade_date"].astype(str)

    m = cfg["mechanism"]
    panel = add_leader_propagation_features(
        panel,
        sector_min_members=int(m["sector_min_members"]),
        leader_rank_min=float(m["leader"]["sector_ret20_rank_min"]),
        follower_rank_min=float(m["follower_setup"]["sector_ret20_rank_min"]),
        follower_rank_max=float(m["follower_setup"]["sector_ret20_rank_max_exclusive"]),
        impulse_delta_sessions=int(m["impulse"]["delta_sessions"]),
        history_sessions=int(m["impulse"]["history_sessions"]),
        ignition_quantile=float(m["impulse"]["ignition_quantile"]),
    )
    coverage = {
        "ret20": float(panel["ret20"].notna().mean()),
        "industry_membership": float(panel["sector_key"].notna().mean()),
        "primary_label": float(panel["D4_TREND25_60_label"].notna().mean()),
        "secondary_label": float(panel["D4_TREND40_120_label"].notna().mean()),
    }
    return panel, coverage, actual

def cmd_check(cfg):
    p = Path(cfg["source_panel"])
    actual = sha256_file(p) if p.exists() else None
    result = {
        "status": "PASS_D5_LEADER_LAGGARD_PROPAGATION_V0_1_CHECK"
                  if p.exists() and actual == cfg["source_panel_sha256"]
                  else "BLOCKED_D5_LEADER_LAGGARD_PROPAGATION_V0_1_CHECK",
        "research_id": cfg["research_id"],
        "source_panel": str(p),
        "source_panel_exists": p.exists(),
        "source_panel_sha256": actual,
        "expected_source_panel_sha256": cfg["source_panel_sha256"],
        "mechanism": cfg["mechanism"],
        "labels": cfg["labels"],
        "gate": cfg["gate"],
        "operational_diagnostic": cfg["operational_diagnostic"],
        "safety": cfg["safety"],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"].startswith("PASS") else 3

def cmd_run(cfg):
    try:
        panel, coverage, actual = build_panel(cfg)
        g = cfg["gate"]
        primary = evaluate_d5(
            panel, cfg["labels"]["primary"]["source_column"],
            int(g["selected_min"]),
            float(g["primary_lift_vs_setup_min"]),
            float(g["primary_precision_minus_setup_min"]),
            int(g["positive_years_min"]),
        )
        secondary = evaluate_d5(
            panel, cfg["labels"]["secondary"]["source_column"],
            int(g["selected_min"]),
            float(g["primary_lift_vs_setup_min"]),
            float(g["primary_precision_minus_setup_min"]),
            int(g["positive_years_min"]),
        )

        out = Path(cfg["output_dir"]); out.mkdir(parents=True, exist_ok=True)
        panel_path = out/"d5_leader_laggard_propagation_v0_1_panel.parquet"
        panel.to_parquet(panel_path, index=False)

        daily = panel[
            [
                "trade_date","sector_key","industry_code","industry_name","standard","level",
                "sector_members_d5","sector_ret20_median","leader_count","leader_ret20_mean",
                "leader_spread20","leader_impulse5","leader_impulse_prior_q75","leader_ignition",
            ]
        ].drop_duplicates(["trade_date","sector_key"]).sort_values(["trade_date","sector_key"])
        daily_path = out/"d5_leader_laggard_propagation_v0_1_sector_daily.parquet"
        daily.to_parquet(daily_path, index=False)

        result = {
            "status": "PASS_D5_LEADER_LAGGARD_PROPAGATION_V0_1_RUN",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "research_id": cfg["research_id"],
            "source": {
                "source_panel": cfg["source_panel"],
                "source_panel_sha256": actual,
                "label_reuse_reason": "same T-close/T+1-open timing and same forward targets as D4",
            },
            "mechanism": cfg["mechanism"],
            "feature_coverage": coverage,
            "sector_diagnostics": {
                "sector_daily_rows": int(len(daily)),
                "sector_keys": int(daily["sector_key"].nunique()),
                "leader_ignition_sector_days": int(daily["leader_ignition"].fillna(False).sum()),
                "leader_ignition_dates": int(daily.loc[daily["leader_ignition"].fillna(False),"trade_date"].nunique()),
            },
            "primary": primary,
            "secondary": secondary,
            "overall_verdict": primary["verdict"],
            "artifacts": {
                "panel": str(panel_path),
                "panel_sha256": sha256_file(panel_path),
                "sector_daily": str(daily_path),
                "sector_daily_sha256": sha256_file(daily_path),
            },
            "safety": cfg["safety"],
        }
        (out/"d5_leader_laggard_propagation_v0_1_summary.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print(f"OUTPUT: {out}")
        return 0
    except Exception as e:
        print(json.dumps({
            "status":"FAIL_CLOSED_D5_LEADER_LAGGARD_PROPAGATION_V0_1",
            "error":f"{type(e).__name__}: {e}",
        }, ensure_ascii=False, indent=2))
        return 3

def main():
    p=argparse.ArgumentParser()
    p.add_argument("command", choices=["check","run"])
    args=p.parse_args()
    cfg=load_cfg()
    return cmd_check(cfg) if args.command=="check" else cmd_run(cfg)

if __name__=="__main__":
    raise SystemExit(main())
