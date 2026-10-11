from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = Path(
    r"D:\positionx_data\incoming\eva\20260927_pead_phase0_audit"
)
OUT_DIR = (
    ROOT / "data" / "research" / "edge_discovery"
    / "pead_phase0_acceptance_v01"
)

REQUIRED_FORECAST_COLUMNS = {
    "stock_code",
    "ann_date",
    "first_ann_date",
    "end_date",
    "type",
    "p_change_min",
    "p_change_max",
    "last_parent_net",
    "update_flag",
}

KNOWN_TYPES = {
    "预增",
    "略增",
    "扭亏",
    "续盈",
    "预减",
    "略减",
    "首亏",
    "续亏",
    "不确定",
}

POSITIVE_TYPES = {"预增", "略增", "扭亏", "续盈"}
NEGATIVE_TYPES = {"预减", "略减", "首亏", "续亏"}


def finite_ratio(n: int, d: int) -> float | None:
    return None if d <= 0 else n / d


def decide(metrics: dict[str, Any]) -> dict[str, Any]:
    """
    Acceptance, not strategy evaluation.

    Missing/invalid groups may later be excluded, but the delivered asset must
    contain the actual FIRST forecast announcement row with high coverage.
    """
    checks = {
        "required_columns_present":
            bool(metrics.get("required_columns_present")),
        "group_count_ge_1000":
            int(metrics.get("groups", 0)) >= 1000,
        "first_ann_nonnull_ge_99pct":
            (metrics.get("first_ann_nonnull_ratio") or 0.0) >= 0.99,
        "no_ann_date_before_first_ann_date":
            int(metrics.get("rows_ann_before_first", 1)) == 0,
        "first_ann_consistent_within_group":
            int(metrics.get("groups_inconsistent_first_ann", 1)) == 0,
        "actual_first_row_available_ge_99pct":
            (metrics.get("first_row_available_ratio") or 0.0) >= 0.99,
        "no_conflicting_payload_on_first_date":
            int(metrics.get("groups_conflicting_first_payload", 1)) == 0,
        "magnitude_min_coverage_ge_95pct":
            (metrics.get("first_rows_p_change_min_nonnull_ratio") or 0.0)
            >= 0.95,
        "magnitude_max_coverage_ge_95pct":
            (metrics.get("first_rows_p_change_max_nonnull_ratio") or 0.0)
            >= 0.95,
        "last_parent_net_coverage_ge_95pct":
            (metrics.get("first_rows_last_parent_net_nonnull_ratio") or 0.0)
            >= 0.95,
    }

    p1_ready = all(checks.values())

    return {
        "checks": checks,
        "p1_earnings_forecast_drift": (
            "READY_FOR_FAST_REJECT"
            if p1_ready
            else "DATA_NOT_ACCEPTED"
        ),
        # Phase-0 package did not deliver fina_indicator.parquet. We refuse to
        # accept a claimed SUE source that cannot be independently re-opened.
        "p2_sue_pead": "NOT_ACCEPTED_FROM_THIS_DELIVERY",
    }


def q1(con, sql: str):
    return con.execute(sql).fetchone()[0]


def main():
    ap = argparse.ArgumentParser(
        description="PositionX PEAD Phase-0 acceptance audit v0.1"
    )
    ap.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT,
        help="IwAgent PEAD Phase-0 delivery directory",
    )
    args = ap.parse_args()

    try:
        import duckdb
    except ImportError as e:
        raise RuntimeError(
            "duckdb is required in the PositionX research environment."
        ) from e

    input_dir = args.input_dir
    parsed = (
        input_dir / "data" / "research" / "pead_phase0"
        / "raw" / "parsed"
    )
    forecast = parsed / "forecast.parquet"
    income = parsed / "income.parquet"
    express = parsed / "express.parquet"
    fina = parsed / "fina_indicator.parquet"

    if not forecast.exists():
        raise FileNotFoundError(f"Missing forecast parquet: {forecast}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(database=":memory:")
    fp = str(forecast).replace("'", "''")

    desc = con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{fp}')"
    ).fetchall()
    columns = [str(r[0]) for r in desc]
    required_present = REQUIRED_FORECAST_COLUMNS.issubset(columns)

    missing_cols = sorted(REQUIRED_FORECAST_COLUMNS - set(columns))
    if missing_cols:
        report = {
            "audit_id": "PEAD_PHASE0_ACCEPTANCE_V01",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "input_dir": str(input_dir),
            "decision": {
                "p1_earnings_forecast_drift": "DATA_NOT_ACCEPTED",
                "p2_sue_pead": "NOT_ACCEPTED_FROM_THIS_DELIVERY",
            },
            "fatal": f"forecast.parquet missing columns: {missing_cols}",
            "forecast_columns": columns,
        }
        (OUT_DIR / "audit.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        raise RuntimeError(report["fatal"])

    # DISTINCT removes duplicated rows produced by overlapping fetch scripts,
    # without guessing which revision is authoritative.
    con.execute(
        f"""
        CREATE VIEW fc AS
        SELECT DISTINCT
            CAST(stock_code AS VARCHAR) AS stock_code,
            CAST(ann_date AS DATE) AS ann_date,
            CAST(first_ann_date AS DATE) AS first_ann_date,
            CAST(end_date AS DATE) AS end_date,
            CAST(type AS VARCHAR) AS type,
            TRY_CAST(p_change_min AS DOUBLE) AS p_change_min,
            TRY_CAST(p_change_max AS DOUBLE) AS p_change_max,
            TRY_CAST(last_parent_net AS DOUBLE) AS last_parent_net,
            CAST(update_flag AS VARCHAR) AS update_flag
        FROM read_parquet('{fp}')
        WHERE stock_code IS NOT NULL
          AND end_date IS NOT NULL
          AND ann_date IS NOT NULL
        """
    )

    con.execute(
        """
        CREATE VIEW grp AS
        SELECT
            stock_code,
            end_date,
            COUNT(*) AS n_rows,
            COUNT(DISTINCT ann_date) AS n_ann_dates,
            MIN(ann_date) AS min_ann_date,
            MAX(ann_date) AS max_ann_date,
            MIN(first_ann_date) AS min_first_ann_date,
            MAX(first_ann_date) AS max_first_ann_date,
            BOOL_OR(first_ann_date IS NOT NULL) AS has_first_ann,
            BOOL_OR(
                first_ann_date IS NOT NULL
                AND ann_date = first_ann_date
            ) AS has_exact_first_row
        FROM fc
        GROUP BY stock_code, end_date
        """
    )

    # Multiple rows on the first announcement date are okay only when their
    # payload is identical. Different payloads make the PIT event ambiguous.
    con.execute(
        """
        CREATE VIEW first_payload AS
        SELECT
            stock_code,
            end_date,
            COUNT(*) AS n_first_rows,
            COUNT(DISTINCT COALESCE(type, '<NULL>')) AS n_type,
            COUNT(DISTINCT COALESCE(CAST(p_change_min AS VARCHAR), '<NULL>'))
                AS n_min,
            COUNT(DISTINCT COALESCE(CAST(p_change_max AS VARCHAR), '<NULL>'))
                AS n_max,
            COUNT(DISTINCT COALESCE(CAST(last_parent_net AS VARCHAR), '<NULL>'))
                AS n_last
        FROM fc
        WHERE first_ann_date IS NOT NULL
          AND ann_date = first_ann_date
        GROUP BY stock_code, end_date
        """
    )

    con.execute(
        """
        CREATE VIEW first_canonical AS
        SELECT *
        FROM (
            SELECT
                *,
                ROW_NUMBER() OVER (
                    PARTITION BY stock_code, end_date
                    ORDER BY ann_date
                ) AS rn
            FROM fc
            WHERE first_ann_date IS NOT NULL
              AND ann_date = first_ann_date
        )
        WHERE rn = 1
        """
    )

    rows = int(q1(con, "SELECT COUNT(*) FROM fc"))
    groups = int(q1(con, "SELECT COUNT(*) FROM grp"))
    symbols = int(q1(con, "SELECT COUNT(DISTINCT stock_code) FROM fc"))
    ann_min = q1(con, "SELECT MIN(ann_date) FROM fc")
    ann_max = q1(con, "SELECT MAX(ann_date) FROM fc")

    groups_first_nonnull = int(q1(
        con,
        "SELECT COUNT(*) FROM grp WHERE has_first_ann"
    ))
    groups_exact_first = int(q1(
        con,
        "SELECT COUNT(*) FROM grp WHERE has_exact_first_row"
    ))
    rows_ann_before_first = int(q1(
        con,
        """
        SELECT COUNT(*) FROM fc
        WHERE first_ann_date IS NOT NULL
          AND ann_date < first_ann_date
        """
    ))
    inconsistent = int(q1(
        con,
        """
        SELECT COUNT(*) FROM grp
        WHERE min_first_ann_date IS DISTINCT FROM max_first_ann_date
        """
    ))
    missing_initial_payload = int(q1(
        con,
        """
        SELECT COUNT(*) FROM grp
        WHERE has_first_ann
          AND NOT has_exact_first_row
        """
    ))
    revision_groups = int(q1(
        con,
        "SELECT COUNT(*) FROM grp WHERE n_ann_dates > 1"
    ))
    conflicting = int(q1(
        con,
        """
        SELECT COUNT(*) FROM first_payload
        WHERE n_type > 1 OR n_min > 1 OR n_max > 1 OR n_last > 1
        """
    ))

    first_rows = int(q1(con, "SELECT COUNT(*) FROM first_canonical"))
    min_nonnull = int(q1(
        con,
        "SELECT COUNT(*) FROM first_canonical WHERE p_change_min IS NOT NULL"
    ))
    max_nonnull = int(q1(
        con,
        "SELECT COUNT(*) FROM first_canonical WHERE p_change_max IS NOT NULL"
    ))
    last_nonnull = int(q1(
        con,
        "SELECT COUNT(*) FROM first_canonical WHERE last_parent_net IS NOT NULL"
    ))

    type_rows = con.execute(
        """
        SELECT
            COALESCE(type, '<NULL>') AS type,
            COUNT(*) AS n
        FROM first_canonical
        GROUP BY 1
        ORDER BY n DESC, type
        """
    ).fetchall()
    type_dist = {str(t): int(n) for t, n in type_rows}
    unknown_types = sorted(
        t for t in type_dist
        if t != "<NULL>" and t not in KNOWN_TYPES
    )

    metrics = {
        "required_columns_present": required_present,
        "forecast_columns": columns,
        "rows_distinct": rows,
        "groups": groups,
        "symbols": symbols,
        "ann_date_min": str(ann_min),
        "ann_date_max": str(ann_max),
        "groups_with_first_ann_date": groups_first_nonnull,
        "first_ann_nonnull_ratio":
            finite_ratio(groups_first_nonnull, groups),
        "groups_with_actual_first_row": groups_exact_first,
        "first_row_available_ratio":
            finite_ratio(groups_exact_first, groups_first_nonnull),
        "groups_missing_initial_payload_despite_first_ann_date":
            missing_initial_payload,
        "rows_ann_before_first": rows_ann_before_first,
        "groups_inconsistent_first_ann": inconsistent,
        "revision_groups_distinct_ann_dates_gt_1": revision_groups,
        "revision_group_ratio": finite_ratio(revision_groups, groups),
        "groups_conflicting_first_payload": conflicting,
        "first_canonical_rows": first_rows,
        "first_rows_p_change_min_nonnull_ratio":
            finite_ratio(min_nonnull, first_rows),
        "first_rows_p_change_max_nonnull_ratio":
            finite_ratio(max_nonnull, first_rows),
        "first_rows_last_parent_net_nonnull_ratio":
            finite_ratio(last_nonnull, first_rows),
        "type_distribution_first_rows": type_dist,
        "unknown_types": unknown_types,
        "positive_types_frozen_candidate": sorted(POSITIVE_TYPES),
        "negative_types_context_only": sorted(NEGATIVE_TYPES),
    }

    decision = decide(metrics)

    package_observations = {
        "income_parquet_present": income.exists(),
        "forecast_parquet_present": forecast.exists(),
        "express_parquet_present": express.exists(),
        "fina_indicator_parquet_present": fina.exists(),
        "important_note": (
            "The external verifier contains several assertion-like checks "
            "hard-coded as True, including fina_indicator PIT EPS. "
            "PositionX therefore does not use the external 15/16 PASS count "
            "as an acceptance criterion."
        ),
        "p2_reason": (
            "SUE-PEAD is not accepted from this delivery because "
            "fina_indicator.parquet is absent. income.basic_eps is not "
            "silently substituted because quarterly/cumulative EPS semantics "
            "and revision handling have not been separately audited."
        ),
    }

    report = {
        "audit_id": "PEAD_PHASE0_ACCEPTANCE_V01",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_dir": str(input_dir),
        "scope": "PositionX acceptance only; no strategy backtest",
        "metrics": metrics,
        "decision": decision,
        "package_observations": package_observations,
        "next_if_p1_ready": {
            "strategy_family_name": "Earnings Forecast Drift",
            "not_called": "classic consensus-surprise PEAD",
            "event_source": "iwaos_forecast",
            "event_time": "first announcement only",
            "pit_rule": (
                "Use only groups where an actual row exists with "
                "ann_date == first_ann_date; never apply a later revision "
                "payload retroactively to first_ann_date."
            ),
            "no_backtest_parameter_selected_here": True,
        },
        "safety": {
            "production_db_opened": False,
            "broker_used": False,
            "orders_created": False,
            "external_delivery_modified": False,
        },
    }

    (OUT_DIR / "audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    print("=" * 110)
    print("POSITIONX — PEAD PHASE-0 ACCEPTANCE AUDIT V0.1")
    print("=" * 110)
    print("Input:", input_dir)
    print(
        f"forecast distinct rows={rows} groups={groups} symbols={symbols} "
        f"ann={ann_min}->{ann_max}"
    )
    print(
        "first_ann non-null:",
        f"{metrics['first_ann_nonnull_ratio']:.2%}"
        if metrics["first_ann_nonnull_ratio"] is not None else "NA",
    )
    print(
        "actual first-row available:",
        f"{metrics['first_row_available_ratio']:.2%}"
        if metrics["first_row_available_ratio"] is not None else "NA",
    )
    print(
        f"missing initial payload={missing_initial_payload} "
        f"inconsistent first_ann={inconsistent} "
        f"ann<first_ann rows={rows_ann_before_first}"
    )
    print(
        f"revision groups={revision_groups} "
        f"conflicting first payload groups={conflicting}"
    )
    print(
        "first-row field coverage:",
        f"p_change_min={metrics['first_rows_p_change_min_nonnull_ratio']:.2%} "
        f"p_change_max={metrics['first_rows_p_change_max_nonnull_ratio']:.2%} "
        f"last_parent_net={metrics['first_rows_last_parent_net_nonnull_ratio']:.2%}"
    )
    print("first-row type distribution:", type_dist)
    print("unknown types:", unknown_types or "NONE")
    print()
    print("ACCEPTANCE CHECKS")
    print("-" * 110)
    for k, v in decision["checks"].items():
        print(f"{k}: {'PASS' if v else 'FAIL'}")
    print()
    print(
        "P1 Earnings Forecast Drift:",
        decision["p1_earnings_forecast_drift"],
    )
    print("P2 SUE-PEAD:", decision["p2_sue_pead"])
    print(
        "fina_indicator.parquet present:",
        package_observations["fina_indicator_parquet_present"],
    )
    print()
    print("Output:", OUT_DIR / "audit.json")
    print("No strategy backtest was run.")
    print("No external source file or production database was modified.")


if __name__ == "__main__":
    main()
