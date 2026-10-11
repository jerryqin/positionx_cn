from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from _forecast_semantic_v021_common import (
    sha256_file, profile, evaluate_research_gate
)

CFG = PROJECT_ROOT / "config/opportunity/forecast_semantic_promotion_validation_v0_2_1.yaml"


def _duckdb():
    import duckdb
    return duckdb


def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))


def cmd_check(cfg):
    durable = Path(cfg["durable"]["directory"]) / cfg["durable"]["parquet_name"]
    manifest = Path(cfg["durable"]["directory"]) / cfg["durable"]["manifest_name"]
    catalog = Path(cfg["catalog"]["path"])

    result = {
        "status": (
            "PASS_FORECAST_SEMANTIC_FORMAL_VALIDATION_V0_2_CHECK"
            if durable.exists() and manifest.exists() and catalog.exists()
            else "BLOCKED_FORECAST_SEMANTIC_FORMAL_VALIDATION_V0_2_CHECK"
        ),
        "durable_path": str(durable),
        "durable_exists": durable.exists(),
        "manifest_path": str(manifest),
        "manifest_exists": manifest.exists(),
        "catalog": str(catalog),
        "catalog_exists": catalog.exists(),
        "catalog_table": cfg["catalog"]["table"],
        "formal_history_years_min": cfg["research_gate"]["formal_history_years_min"],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"].startswith("PASS") else 3


def cmd_run(cfg):
    try:
        durable = Path(cfg["durable"]["directory"]) / cfg["durable"]["parquet_name"]
        manifest_path = Path(cfg["durable"]["directory"]) / cfg["durable"]["manifest_name"]
        catalog = Path(cfg["catalog"]["path"])

        if not durable.exists() or not manifest_path.exists() or not catalog.exists():
            return cmd_check(cfg)

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        durable_sha = sha256_file(durable)
        if durable_sha != cfg["source"]["sha256"]:
            raise RuntimeError(
                f"durable SHA mismatch expected={cfg['source']['sha256']} actual={durable_sha}"
            )
        if manifest.get("durable_sha256") != durable_sha:
            raise RuntimeError("manifest durable SHA mismatch")

        df = pd.read_parquet(durable)
        p = profile(df)
        g = evaluate_research_gate(
            p, cfg["research_gate"], cfg["source"]["parser_version"]
        )

        duckdb = _duckdb()
        con = duckdb.connect(str(catalog), read_only=True)
        try:
            table_rows = int(
                con.execute(
                    f"select count(*) from {cfg['catalog']['table']}"
                ).fetchone()[0]
            )
            reg = con.execute(
                f"""
                select asset_id, asset_version, table_name, source_sha256, row_count, metadata_json
                from {cfg['catalog']['registry_table']}
                where asset_id=?
                """,
                [cfg["asset"]["asset_id"]],
            ).fetchone()
        finally:
            con.close()

        catalog_checks = {
            "table_rows_exact": table_rows == cfg["source"]["expected_rows"],
            "registry_present": reg is not None,
            "registry_asset_version_exact": (
                reg is not None and reg[1] == cfg["asset"]["asset_version"]
            ),
            "registry_table_exact": (
                reg is not None and reg[2] == cfg["catalog"]["table"]
            ),
            "registry_sha_exact": (
                reg is not None and reg[3] == durable_sha
            ),
            "registry_rows_exact": (
                reg is not None and int(reg[4]) == cfg["source"]["expected_rows"]
            ),
        }

        formal_ready = g["accepted"] and all(catalog_checks.values())
        verdict = (
            "ACCEPT_FORECAST_SEMANTIC_CORPUS_FOR_FORMAL_OPPORTUNITY_RESEARCH"
            if formal_ready
            else "HOLD_FORECAST_SEMANTIC_CORPUS_FORMAL_RESEARCH"
        )

        out_dir = PROJECT_ROOT / "data" / "reports"
        # PositionX project-local report is convenient but not required for lineage.
        # The canonical result also goes to the research output directory below.
        research_out = Path(
            r"D:\positionx_data\research\opportunity_engine\forecast_semantic_corpus_validation_v0_2"
        )
        research_out.mkdir(parents=True, exist_ok=True)

        profile_path = research_out / "forecast_semantic_corpus_v0_2_profile.json"
        profile_path.write_text(
            json.dumps(p, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        result = {
            "status": "PASS_FORECAST_SEMANTIC_FORMAL_VALIDATION_V0_2_RUN",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "asset": {
                "asset_id": cfg["asset"]["asset_id"],
                "asset_version": cfg["asset"]["asset_version"],
                "durable_path": str(durable),
                "durable_sha256": durable_sha,
                "manifest_path": str(manifest_path),
                "manifest_sha256": sha256_file(manifest_path),
                "catalog": str(catalog),
                "catalog_table": cfg["catalog"]["table"],
            },
            "profile": p,
            "research_gate": g,
            "catalog_checks": catalog_checks,
            "formal_research_ready": formal_ready,
            "verdict": verdict,
            "next_action": (
                "Design the first frozen non-price Opportunity mechanism from the observed semantic distributions; do not tune on outcomes."
                if formal_ready
                else "Repair failed formal-validation/catalog checks before any Opportunity event study."
            ),
        }

        summary = research_out / "forecast_semantic_corpus_validation_v0_2_summary.json"
        summary.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        print(json.dumps(result, ensure_ascii=False, indent=2))
        print(f"OUTPUT: {research_out}")
        return 0
    except Exception as e:
        print(json.dumps({
            "status":"FAIL_CLOSED_FORECAST_SEMANTIC_FORMAL_VALIDATION_V0_2",
            "error":f"{type(e).__name__}: {e}",
        }, ensure_ascii=False, indent=2))
        return 3


def main():
    p=argparse.ArgumentParser()
    p.add_argument("command",choices=["check","run"])
    args=p.parse_args()
    cfg=load_cfg()
    return cmd_check(cfg) if args.command=="check" else cmd_run(cfg)


if __name__=="__main__":
    raise SystemExit(main())
