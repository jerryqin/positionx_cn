from __future__ import annotations

import argparse
import hashlib
import json
import shutil
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


def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))


def qident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def qtable(full: str) -> str:
    return ".".join(qident(x) for x in full.split("."))


def sql_string(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def tree_hash(directory: Path):
    if not directory.exists():
        return None, []
    candidates = []
    for p in sorted(directory.rglob("*")):
        if p.is_file() and p.suffix.lower() in {".py", ".json", ".md", ".yaml", ".yml"}:
            if p.name == "semantic_events_2020_2026_v0_2_1.parquet":
                continue
            candidates.append(p)
    if not candidates:
        return None, []
    h = hashlib.sha256()
    rels = []
    for p in candidates:
        rel = str(p.relative_to(directory)).replace("\\", "/")
        rels.append({"path": rel, "sha256": sha256_file(p)})
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(bytes.fromhex(sha256_file(p)))
    return h.hexdigest(), rels


def validate_source(cfg):
    src = Path(cfg["source"]["path"])
    if not src.exists():
        raise FileNotFoundError(src)
    actual = sha256_file(src)
    if actual != cfg["source"]["sha256"]:
        raise RuntimeError(f"source SHA mismatch expected={cfg['source']['sha256']} actual={actual}")

    acceptance = Path(cfg["acceptance"]["summary_path"])
    if not acceptance.exists():
        raise FileNotFoundError(acceptance)
    a = json.loads(acceptance.read_text(encoding="utf-8"))
    verdict = a.get("validation", {}).get("verdict")
    accepted = a.get("validation", {}).get("accepted")
    candidate_sha = a.get("validation", {}).get("candidate_sha256")
    if accepted is not True or verdict != cfg["acceptance"]["required_verdict"]:
        raise RuntimeError(f"acceptance summary not accepted: {verdict}")
    if candidate_sha != actual:
        raise RuntimeError(f"acceptance candidate SHA mismatch acceptance={candidate_sha} source={actual}")

    df = pd.read_parquet(src)
    p = profile(df)
    g = evaluate_research_gate(
        p, cfg["research_gate"], cfg["source"]["parser_version"]
    )
    if not g["accepted"]:
        failed = [k for k,v in g["checks"].items() if not v]
        raise RuntimeError(f"source research gates failed: {failed}")

    return src, actual, acceptance, sha256_file(acceptance), p, g


def _duckdb():
    import duckdb
    return duckdb


def registry_columns(con, table_name: str):
    schema, table = table_name.split(".", 1)
    rows = con.execute("""
        select column_name
        from information_schema.columns
        where table_schema=? and table_name=?
        order by ordinal_position
    """, [schema, table]).fetchall()
    return [r[0] for r in rows]


def ensure_registry(con, table_name: str):
    schema, _ = table_name.split(".", 1)
    con.execute(f"create schema if not exists {qident(schema)}")
    if not registry_columns(con, table_name):
        con.execute(f"""
            create table {qtable(table_name)} (
                asset_id varchar,
                asset_version varchar,
                table_name varchar,
                source_path varchar,
                source_sha256 varchar,
                row_count bigint,
                promoted_at timestamp,
                metadata_json varchar
            )
        """)
    cols = set(registry_columns(con, table_name))
    expected = {
        "asset_id","asset_version","table_name","source_path",
        "source_sha256","row_count","promoted_at","metadata_json"
    }
    if not expected.issubset(cols):
        raise RuntimeError(
            f"registry schema incompatible missing={sorted(expected-cols)} existing={sorted(cols)}"
        )


def cmd_check(cfg):
    try:
        src, actual, acceptance, acceptance_sha, p, g = validate_source(cfg)
        durable = Path(cfg["durable"]["directory"]) / cfg["durable"]["parquet_name"]
        catalog = Path(cfg["catalog"]["path"])
        result = {
            "status": "PASS_FORECAST_SEMANTIC_PROMOTION_V0_2_1_CHECK",
            "asset_id": cfg["asset"]["asset_id"],
            "asset_version": cfg["asset"]["asset_version"],
            "source": str(src),
            "source_sha256": actual,
            "acceptance_summary": str(acceptance),
            "acceptance_summary_sha256": acceptance_sha,
            "profile": p,
            "gate": g,
            "durable_target": str(durable),
            "catalog": str(catalog),
            "catalog_exists": catalog.exists(),
            "catalog_table": cfg["catalog"]["table"],
            "registry_table": cfg["catalog"]["registry_table"],
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if catalog.exists() else 3
    except Exception as e:
        print(json.dumps({
            "status":"BLOCKED_FORECAST_SEMANTIC_PROMOTION_V0_2_1_CHECK",
            "error":f"{type(e).__name__}: {e}",
        }, ensure_ascii=False, indent=2))
        return 3


def cmd_promote(cfg):
    try:
        src, actual, acceptance, acceptance_sha, p, g = validate_source(cfg)

        durable_dir = Path(cfg["durable"]["directory"])
        durable_dir.mkdir(parents=True, exist_ok=True)
        durable = durable_dir / cfg["durable"]["parquet_name"]
        tmp = durable.with_suffix(durable.suffix + ".tmp")

        shutil.copy2(src, tmp)
        copied_sha = sha256_file(tmp)
        if copied_sha != actual:
            tmp.unlink(missing_ok=True)
            raise RuntimeError("durable copy SHA mismatch")
        tmp.replace(durable)

        parser_tree_sha, parser_files = tree_hash(src.parent)

        manifest = {
            "asset_id": cfg["asset"]["asset_id"],
            "asset_version": cfg["asset"]["asset_version"],
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "source_path": str(src),
            "source_sha256": actual,
            "durable_path": str(durable),
            "durable_sha256": copied_sha,
            "row_count": p["rows"],
            "parser_version": cfg["source"]["parser_version"],
            "parser_tree_sha256": parser_tree_sha,
            "parser_lineage_files": parser_files,
            "acceptance_summary_path": str(acceptance),
            "acceptance_summary_sha256": acceptance_sha,
            "acceptance_verdict": cfg["acceptance"]["required_verdict"],
            "profile": p,
            "research_gate": g,
        }
        manifest_path = durable_dir / cfg["durable"]["manifest_name"]
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        manifest_sha = sha256_file(manifest_path)

        catalog = Path(cfg["catalog"]["path"])
        if not catalog.exists():
            raise FileNotFoundError(catalog)

        table = cfg["catalog"]["table"]
        registry = cfg["catalog"]["registry_table"]

        duckdb = _duckdb()
        con = duckdb.connect(str(catalog))
        try:
            ensure_registry(con, registry)
            schema, _ = table.split(".", 1)
            con.execute(f"create schema if not exists {qident(schema)}")

            parquet_sql = str(durable).replace("\\", "/")
            con.execute("begin transaction")
            try:
                con.execute(
                    f"create or replace table {qtable(table)} as "
                    f"select * from read_parquet({sql_string(parquet_sql)})"
                )

                table_rows = int(
                    con.execute(f"select count(*) from {qtable(table)}").fetchone()[0]
                )
                if table_rows != p["rows"]:
                    raise RuntimeError(
                        f"catalog table row mismatch expected={p['rows']} actual={table_rows}"
                    )

                con.execute(
                    f"delete from {qtable(registry)} where asset_id=?",
                    [cfg["asset"]["asset_id"]],
                )
                metadata = {
                    "durable_path": str(durable),
                    "durable_sha256": copied_sha,
                    "manifest_path": str(manifest_path),
                    "manifest_sha256": manifest_sha,
                    "parser_version": cfg["source"]["parser_version"],
                    "parser_tree_sha256": parser_tree_sha,
                    "acceptance_summary_sha256": acceptance_sha,
                    "event_years": p["event_years"],
                    "eligible_rows": p["eligible_rows"],
                    "eligible_strict_payload_rate": p["eligible_strict_payload_rate"],
                    "eligible_current_profit_min_coverage": p["eligible_current_profit_min_coverage"],
                    "eligible_current_profit_max_coverage": p["eligible_current_profit_max_coverage"],
                }
                con.execute(
                    f"""
                    insert into {qtable(registry)}
                    (asset_id, asset_version, table_name, source_path,
                     source_sha256, row_count, promoted_at, metadata_json)
                    values (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        cfg["asset"]["asset_id"],
                        cfg["asset"]["asset_version"],
                        table,
                        str(durable),
                        copied_sha,
                        p["rows"],
                        datetime.now(),
                        json.dumps(metadata, ensure_ascii=False),
                    ],
                )
                con.execute("commit")
            except Exception:
                con.execute("rollback")
                raise
        finally:
            con.close()

        result = {
            "status": "PASS_FORECAST_SEMANTIC_PROMOTION_V0_2_1",
            "asset_id": cfg["asset"]["asset_id"],
            "asset_version": cfg["asset"]["asset_version"],
            "durable_path": str(durable),
            "durable_sha256": copied_sha,
            "manifest_path": str(manifest_path),
            "manifest_sha256": manifest_sha,
            "parser_tree_sha256": parser_tree_sha,
            "catalog": str(catalog),
            "catalog_table": table,
            "registry_table": registry,
            "rows": p["rows"],
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as e:
        print(json.dumps({
            "status":"FAIL_CLOSED_FORECAST_SEMANTIC_PROMOTION_V0_2_1",
            "error":f"{type(e).__name__}: {e}",
        }, ensure_ascii=False, indent=2))
        return 3


def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["check","promote"])
    args = p.parse_args()
    cfg = load_cfg()
    return cmd_check(cfg) if args.command == "check" else cmd_promote(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
