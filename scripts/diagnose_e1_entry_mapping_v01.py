from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CFG = PROJECT_ROOT / "config/opportunity/e1_entry_mapping_diagnostic_v0_1.yaml"

KNOWN_CHANGE_TYPES = {
    "IMPROVING",
    "DETERIORATING",
    "TURN_TO_LOSS",
    "TURNAROUND",
    "LOSS_NARROWING",
    "LOSS_WIDENING",
    "FLAT",
}


def _duckdb():
    import duckdb
    return duckdb


def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))


def qtable(full: str) -> str:
    return ".".join('"' + x.replace('"', '""') + '"' for x in full.split("."))


def norm_bool(s: pd.Series) -> pd.Series:
    return s.astype("string").str.strip().str.lower().isin(
        ["true", "1", "yes", "y", "eligible"]
    )


def canonical_symbol(v) -> str | None:
    """Conservative A-share symbol normalizer for diagnostic matching only."""
    if v is None or pd.isna(v):
        return None
    s = str(v).strip().upper()
    if not s:
        return None

    # Standard forms first.
    m = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", s)
    if m:
        return f"{m.group(1)}.{m.group(2)}"

    m = re.fullmatch(r"(SH|SZ|BJ)[\.\-_]?(\d{6})", s)
    if m:
        return f"{m.group(2)}.{m.group(1)}"

    m = re.fullmatch(r"(\d{6})[\.\-_]?(SH|SZ|BJ)", s)
    if m:
        return f"{m.group(1)}.{m.group(2)}"

    # Bare six-digit code: infer exchange only for diagnostics.
    if re.fullmatch(r"\d{6}", s):
        if s.startswith(("60", "68", "90")):
            return f"{s}.SH"
        if s.startswith(("00", "30", "20")):
            return f"{s}.SZ"
        if s.startswith(("43", "83", "87", "88", "92")):
            return f"{s}.BJ"
        return s

    return s


def event_setup(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    x["event_time"] = pd.to_datetime(x["event_time"], errors="coerce")
    x["event_date"] = x["event_time"].dt.normalize()
    x["event_year"] = x["event_time"].dt.year.astype("Int64")

    eligible = norm_bool(x["strategy_eligible_semantics"])
    kind = x["event_kind"].astype("string").str.upper().str.strip()
    change = x["change_type"].astype("string").str.upper().str.strip()

    x["e1_setup"] = (
        eligible
        & kind.eq("ORIGINAL_FORECAST")
        & change.isin(KNOWN_CHANGE_TYPES)
        & x["event_time"].notna()
    )
    x["symbol_exact"] = x["symbol"].astype("string").str.strip().str.upper()
    x["symbol_canonical"] = x["symbol_exact"].map(canonical_symbol)
    return x[x["e1_setup"]].copy()


def market_symbol_profile(con, cfg):
    table = qtable(cfg["market_table"])
    df = con.execute(f"""
        select
            cast(symbol as varchar) as symbol,
            min(trade_date) as min_trade_date,
            max(trade_date) as max_trade_date,
            count(*) as rows
        from {table}
        group by symbol
    """).fetchdf()

    df["symbol_exact"] = df["symbol"].astype("string").str.strip().str.upper()
    df["symbol_canonical"] = df["symbol_exact"].map(canonical_symbol)
    df["min_trade_date"] = pd.to_datetime(df["min_trade_date"], errors="coerce")
    df["max_trade_date"] = pd.to_datetime(df["max_trade_date"], errors="coerce")
    return df


def classify_mappings(setup: pd.DataFrame, market: pd.DataFrame) -> pd.DataFrame:
    exact = market[
        ["symbol_exact", "min_trade_date", "max_trade_date", "rows"]
    ].rename(columns={
        "min_trade_date": "exact_min_trade_date",
        "max_trade_date": "exact_max_trade_date",
        "rows": "exact_market_rows",
    })

    canon = (
        market.sort_values(["symbol_canonical", "rows"], ascending=[True, False])
        .drop_duplicates("symbol_canonical")
        [["symbol_canonical", "symbol_exact", "min_trade_date", "max_trade_date", "rows"]]
        .rename(columns={
            "symbol_exact": "canonical_market_symbol",
            "min_trade_date": "canonical_min_trade_date",
            "max_trade_date": "canonical_max_trade_date",
            "rows": "canonical_market_rows",
        })
    )

    z = setup.merge(exact, on="symbol_exact", how="left", validate="many_to_one")
    z = z.merge(canon, on="symbol_canonical", how="left", validate="many_to_one")

    z["exact_symbol_present"] = z["exact_market_rows"].notna()
    z["canonical_symbol_present"] = z["canonical_market_rows"].notna()

    # A next tradable bar can exist only if event date is strictly before max market date.
    z["exact_next_bar_possible"] = (
        z["exact_symbol_present"]
        & z["event_date"].notna()
        & z["exact_max_trade_date"].notna()
        & (z["event_date"] < z["exact_max_trade_date"])
    )
    z["canonical_next_bar_possible"] = (
        z["canonical_symbol_present"]
        & z["event_date"].notna()
        & z["canonical_max_trade_date"].notna()
        & (z["event_date"] < z["canonical_max_trade_date"])
    )

    def reason(row):
        if pd.isna(row["event_date"]):
            return "INVALID_EVENT_DATE"
        if row["exact_next_bar_possible"]:
            return "EXACT_MAPPING_POSSIBLE"
        if row["canonical_next_bar_possible"] and not row["exact_symbol_present"]:
            return "NORMALIZED_SYMBOL_ONLY"
        if not row["canonical_symbol_present"]:
            return "NO_MARKET_SYMBOL"
        if pd.notna(row["canonical_min_trade_date"]) and row["event_date"] < row["canonical_min_trade_date"]:
            return "EVENT_BEFORE_FIRST_MARKET_BAR"
        if pd.notna(row["canonical_max_trade_date"]) and row["event_date"] >= row["canonical_max_trade_date"]:
            return "EVENT_AT_OR_AFTER_LAST_MARKET_BAR"
        return "OTHER_MAPPING_GAP"

    z["mapping_reason"] = z.apply(reason, axis=1)
    return z


def candidate_price_tables(con):
    """Metadata-only discovery of tables that look like historical OHLC sources."""
    tabs = con.execute("""
        select table_schema, table_name
        from information_schema.tables
        where table_schema not in ('information_schema', 'pg_catalog')
        order by table_schema, table_name
    """).fetchdf()

    cols = con.execute("""
        select table_schema, table_name, lower(column_name) as column_name
        from information_schema.columns
        where table_schema not in ('information_schema', 'pg_catalog')
    """).fetchdf()

    required = {"symbol", "trade_date", "open", "high", "low", "close"}
    out = []
    for row in tabs.itertuples(index=False):
        sub = cols[
            (cols["table_schema"] == row.table_schema)
            & (cols["table_name"] == row.table_name)
        ]
        names = set(sub["column_name"].tolist())
        if required.issubset(names):
            out.append({
                "table": f"{row.table_schema}.{row.table_name}",
                "has_pre_close": "pre_close" in names,
                "columns_count": int(len(names)),
            })
    return out


def summarize(z: pd.DataFrame, market: pd.DataFrame, cfg: dict):
    n = len(z)
    exact_cov = float(z["exact_next_bar_possible"].mean()) if n else 0.0
    canon_cov = float(z["canonical_next_bar_possible"].mean()) if n else 0.0
    target = float(cfg["target_entry_mapping_coverage"])

    by_year = {}
    for y, g in z[z["event_year"].notna()].groupby("event_year"):
        by_year[str(int(y))] = {
            "setup_rows": int(len(g)),
            "exact_next_bar_possible_rate": float(g["exact_next_bar_possible"].mean()),
            "canonical_next_bar_possible_rate": float(g["canonical_next_bar_possible"].mean()),
            "reason_counts": {
                str(k): int(v)
                for k, v in g["mapping_reason"].value_counts(dropna=False).items()
            },
        }

    reason_counts = {
        str(k): int(v)
        for k, v in z["mapping_reason"].value_counts(dropna=False).items()
    }

    missing_symbol = z[z["mapping_reason"] == "NO_MARKET_SYMBOL"]
    top_missing = (
        missing_symbol.groupby(["symbol_exact", "symbol_canonical"], dropna=False)
        .size()
        .sort_values(ascending=False)
        .head(100)
    )
    top_missing_rows = [
        {
            "symbol_exact": idx[0],
            "symbol_canonical": idx[1],
            "setup_events": int(v),
        }
        for idx, v in top_missing.items()
    ]

    if exact_cov >= target:
        diagnosis = "EXACT_MAPPING_SHOULD_PASS_GATE"
        next_action = (
            "Runner mapping implementation likely differs from this necessary-condition diagnostic; "
            "inspect actual next-session join logic."
        )
    elif canon_cov >= target and exact_cov < target:
        diagnosis = "SYMBOL_FORMAT_ADAPTER_LIKELY"
        next_action = (
            "Implement a deterministic symbol normalization adapter in E1; do not alter the research gate."
        )
    elif float(z["canonical_symbol_present"].mean()) < target:
        diagnosis = "MARKET_PRICE_ASSET_SYMBOL_COVERAGE_GAP"
        next_action = (
            "Do not relax E1. Identify or acquire historical price coverage for missing symbols, "
            "especially delisted/historical securities."
        )
    else:
        tail_rate = float(
            (z["mapping_reason"] == "EVENT_AT_OR_AFTER_LAST_MARKET_BAR").mean()
        )
        before_rate = float(
            (z["mapping_reason"] == "EVENT_BEFORE_FIRST_MARKET_BAR").mean()
        )
        if tail_rate > 0.02:
            diagnosis = "MARKET_HISTORY_TAIL_GAP"
            next_action = "Extend market.daily_bars_v1 through the required forward/entry period."
        elif before_rate > 0.02:
            diagnosis = "MARKET_HISTORY_HEAD_GAP"
            next_action = "Extend historical market bars backward for affected symbols."
        else:
            diagnosis = "MIXED_MAPPING_GAP_REQUIRES_ROW_LEVEL_REVIEW"
            next_action = "Inspect the emitted mapping-gap CSV before changing E1."

    return {
        "setup_rows": int(n),
        "setup_distinct_symbols": int(z["symbol_exact"].nunique(dropna=True)),
        "market_distinct_symbols": int(market["symbol_exact"].nunique(dropna=True)),
        "exact_symbol_presence_rate": float(z["exact_symbol_present"].mean()) if n else 0.0,
        "canonical_symbol_presence_rate": float(z["canonical_symbol_present"].mean()) if n else 0.0,
        "exact_next_bar_possible_rate": exact_cov,
        "canonical_next_bar_possible_rate": canon_cov,
        "target_entry_mapping_coverage": target,
        "reason_counts": reason_counts,
        "by_year": by_year,
        "top_missing_symbols": top_missing_rows,
        "diagnosis": diagnosis,
        "next_action": next_action,
    }


def cmd_check(cfg):
    p = Path(cfg["catalog"])
    result = {
        "status": (
            "PASS_E1_ENTRY_MAPPING_DIAGNOSTIC_V0_1_CHECK"
            if p.exists()
            else "BLOCKED_E1_ENTRY_MAPPING_DIAGNOSTIC_V0_1_CHECK"
        ),
        "catalog": str(p),
        "catalog_exists": p.exists(),
        "event_table": cfg["event_table"],
        "market_table": cfg["market_table"],
        "target_entry_mapping_coverage": cfg["target_entry_mapping_coverage"],
        "read_only": True,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if p.exists() else 3


def cmd_run(cfg):
    try:
        p = Path(cfg["catalog"])
        if not p.exists():
            return cmd_check(cfg)

        duckdb = _duckdb()
        con = duckdb.connect(str(p), read_only=True)
        try:
            events = con.execute(
                f"select * from {qtable(cfg['event_table'])}"
            ).fetchdf()
            market = market_symbol_profile(con, cfg)
            price_tables = candidate_price_tables(con)
        finally:
            con.close()

        setup = event_setup(events)
        mapped = classify_mappings(setup, market)
        summary = summarize(mapped, market, cfg)

        out = Path(cfg["output_dir"])
        out.mkdir(parents=True, exist_ok=True)

        gaps = mapped[~mapped["exact_next_bar_possible"]].copy()
        gap_cols = [
            "announcementId", "symbol", "symbol_exact", "symbol_canonical",
            "event_time", "event_date", "event_year", "change_type",
            "mapping_reason",
            "exact_min_trade_date", "exact_max_trade_date",
            "canonical_market_symbol",
            "canonical_min_trade_date", "canonical_max_trade_date",
        ]
        gap_cols = [c for c in gap_cols if c in gaps.columns]
        gaps_path = out / "e1_entry_mapping_gaps_v0_1.csv"
        gaps[gap_cols].sort_values(
            ["event_year", "symbol_exact", "event_date"]
        ).to_csv(gaps_path, index=False, encoding="utf-8-sig")

        result = {
            "status": "PASS_E1_ENTRY_MAPPING_DIAGNOSTIC_V0_1_RUN",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "research_id": cfg["research_id"],
            "summary": summary,
            "candidate_price_tables": price_tables,
            "artifacts": {
                "mapping_gaps_csv": str(gaps_path),
            },
            "safety": {
                "read_only": True,
                "research_logic_changed": False,
                "gate_changed": False,
            },
        }

        summary_path = out / "e1_entry_mapping_diagnostic_v0_1_summary.json"
        summary_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        print(json.dumps(result, ensure_ascii=False, indent=2))
        print(f"OUTPUT: {out}")
        return 0

    except Exception as e:
        print(json.dumps({
            "status": "FAIL_CLOSED_E1_ENTRY_MAPPING_DIAGNOSTIC_V0_1",
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
