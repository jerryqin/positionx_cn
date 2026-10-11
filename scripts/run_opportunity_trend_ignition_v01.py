from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from datetime import datetime

import duckdb
import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from opportunity.trend_labels import add_trend_labels, PRIMARY, SECONDARY
from opportunity.trend_ignition import (
    synthetic_adjust_ohlc,
    add_symbol_features,
    add_benchmark_relative_features,
    add_trend_ignition_signal,
)
from opportunity.evaluate_trend_ignition import evaluate, fast_reject_gate

CFG = PROJECT_ROOT / "config/opportunity/trend_ignition_v0_1.yaml"


def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def qident(x: str) -> str:
    return '"' + str(x).replace('"', '""') + '"'


def table_exists(con, schema: str, table: str) -> bool:
    row = con.execute(
        """select count(*) from information_schema.tables
           where table_schema=? and table_name=?""",
        [schema, table],
    ).fetchone()
    return bool(row and row[0])


def describe_table(con, schema: str, table: str):
    return con.execute(
        """select column_name,data_type
           from information_schema.columns
           where table_schema=? and table_name=?
           order by ordinal_position""",
        [schema, table],
    ).fetchall()


def canonical_column_map(cols):
    names = {c[0].lower(): c[0] for c in cols}
    aliases = {
        "symbol": ["symbol", "stock_code", "code"],
        "trade_date": ["trade_date", "date"],
        "open": ["open"],
        "high": ["high"],
        "low": ["low"],
        "close": ["close"],
        "pre_close": ["pre_close", "preclose"],
        "volume": ["volume", "vol"],
    }
    out = {}
    for canonical, choices in aliases.items():
        hit = next((names[x] for x in choices if x in names), None)
        if hit:
            out[canonical] = hit
    return out


def load_membership(cfg) -> pd.DataFrame:
    p = Path(cfg["pit_membership_parquet"])
    if not p.exists():
        raise FileNotFoundError(f"PIT membership panel missing: {p}")

    df = pd.read_parquet(p)
    lower = {c.lower(): c for c in df.columns}
    symbol_col = lower.get("symbol")
    date_col = lower.get("trade_date") or lower.get("date") or lower.get("signal_date")
    if not symbol_col or not date_col:
        raise RuntimeError(
            f"Cannot resolve symbol/date in PIT panel; columns={list(df.columns)}"
        )

    x = df[[symbol_col, date_col]].rename(
        columns={symbol_col: "symbol", date_col: "trade_date"}
    )
    x["symbol"] = x["symbol"].astype(str)
    x["trade_date"] = (
        pd.to_datetime(x["trade_date"].astype(str), errors="coerce")
        .dt.strftime("%Y%m%d")
    )
    x = x.dropna().drop_duplicates(["trade_date", "symbol"])
    return x


def load_market(cfg, membership: pd.DataFrame):
    catalog = Path(cfg["market_catalog"])
    if not catalog.exists():
        raise FileNotFoundError(catalog)

    con = duckdb.connect(str(catalog), read_only=True)
    try:
        schema, table = cfg["market_table"].split(".", 1)
        if not table_exists(con, schema, table):
            raise RuntimeError(f"market table missing: {schema}.{table}")

        cols = describe_table(con, schema, table)
        cmap = canonical_column_map(cols)
        required = {"symbol", "trade_date", "open", "high", "low", "close", "pre_close", "volume"}
        missing = required - set(cmap)
        if missing:
            raise RuntimeError(
                f"market table missing canonical fields={sorted(missing)} columns={cols}"
            )

        # PIT membership stores dates as YYYYMMDD strings. DuckDB `cast(? as date)`
        # requires ISO YYYY-MM-DD parameter text, so normalize explicitly.
        min_date = pd.to_datetime(
            membership["trade_date"].min(), format="%Y%m%d", errors="raise"
        ).strftime("%Y-%m-%d")
        max_date = pd.to_datetime(
            membership["trade_date"].max(), format="%Y%m%d", errors="raise"
        ).strftime("%Y-%m-%d")

        # Add enough warmup / forward horizon around membership dates.
        symbols = sorted(set(membership["symbol"]))
        con.register("_opp_symbols", pd.DataFrame({"symbol": symbols}))

        sql = f"""
        select
          cast(m.{qident(cmap['symbol'])} as varchar) as symbol,
          strftime(cast(m.{qident(cmap['trade_date'])} as date), '%Y%m%d') as trade_date,
          cast(m.{qident(cmap['open'])} as double) as open,
          cast(m.{qident(cmap['high'])} as double) as high,
          cast(m.{qident(cmap['low'])} as double) as low,
          cast(m.{qident(cmap['close'])} as double) as close,
          cast(m.{qident(cmap['pre_close'])} as double) as pre_close,
          cast(m.{qident(cmap['volume'])} as double) as volume
        from {qident(schema)}.{qident(table)} m
        join _opp_symbols s
          on cast(m.{qident(cmap['symbol'])} as varchar)=s.symbol
        where cast(m.{qident(cmap['trade_date'])} as date)
              between (cast(? as date) - interval 180 day)
                  and (cast(? as date) + interval 220 day)
        order by symbol, trade_date
        """
        market = con.execute(sql, [min_date, max_date]).df()
        return market, cols, cmap
    finally:
        con.close()


def load_benchmark(cfg) -> pd.DataFrame:
    """Load the already-built PositionX benchmark index asset.

    Source lineage: px_assets/benchmark_index_daily.csv, built from iwaos_index_daily.
    Benchmark is a price index and therefore does not use stock corporate-action adjustment.
    """
    p = Path(cfg["benchmark_file"])
    if not p.exists():
        raise FileNotFoundError(f"benchmark file missing: {p}")

    actual_sha256 = sha256_file(p)
    expected_sha256 = str(cfg.get("benchmark_sha256") or "").lower()
    if expected_sha256 and actual_sha256.lower() != expected_sha256:
        raise RuntimeError(
            f"benchmark SHA256 mismatch: expected={expected_sha256} actual={actual_sha256} path={p}"
        )

    # trade_date is stored as YYYYMMDD in the accepted foundation CSV.
    # Force string dtype before parsing; otherwise pandas may interpret integer
    # values like 20200803 as nanoseconds since epoch (1970-01-01), silently
    # destroying the benchmark/date join and making RS features all-NaN.
    b = pd.read_csv(p, dtype={"trade_date": str, "symbol": str})
    required = {"trade_date", "symbol", "close"}
    missing = required - set(b.columns)
    if missing:
        raise RuntimeError(f"benchmark file missing columns={sorted(missing)}")

    b = b[b["symbol"].astype(str) == str(cfg["benchmark_symbol"])].copy()
    if b.empty:
        raise RuntimeError(
            f"benchmark {cfg['benchmark_symbol']} absent from {p}"
        )

    b["trade_date"] = (
        pd.to_datetime(
            b["trade_date"].astype(str).str.strip(),
            format="%Y%m%d",
            errors="raise",
        )
        .dt.strftime("%Y%m%d")
    )
    b["close"] = pd.to_numeric(b["close"], errors="coerce")
    b = b[b["close"].notna() & (b["close"] > 0)]
    b = b.sort_values("trade_date").drop_duplicates("trade_date", keep="last")
    if b.empty:
        raise RuntimeError("benchmark has no valid positive close rows")
    return b[["trade_date", "close"]]


def build_panel(cfg):
    membership = load_membership(cfg)
    market, cols, cmap = load_market(cfg, membership)
    benchmark = load_benchmark(cfg)

    # market.daily_bars_v1 is raw/unadjusted. Convert each stock to the same
    # corporate-action-safe synthetic adjusted state already used by PositionX 52WH.
    stocks = (
        market.groupby("symbol", group_keys=False)
        .apply(synthetic_adjust_ohlc)
        .reset_index(drop=True)
    )

    featured = (
        stocks.groupby("symbol", group_keys=False)
        .apply(add_symbol_features)
        .reset_index(drop=True)
    )
    featured = add_benchmark_relative_features(featured, benchmark)

    # Hard fail on benchmark/date join degradation. V0.1 requires RS to be a
    # real input, not an all-NaN silent no-op.
    rs_valid = featured["rs_accel"].notna()
    rs_valid_rate = float(rs_valid.mean()) if len(featured) else 0.0
    if rs_valid_rate < 0.90:
        raise RuntimeError(
            f"RS benchmark join coverage too low: rs_accel_valid_rate={rs_valid_rate:.6f}; "
            f"benchmark_date_min={benchmark['trade_date'].min()} "
            f"benchmark_date_max={benchmark['trade_date'].max()}"
        )

    featured = add_trend_ignition_signal(featured)
    labeled = add_trend_labels(featured)

    # Evaluate signals only on PIT membership dates. Warmup / future rows exist
    # solely to calculate features and forward labels.
    panel = membership.merge(
        labeled,
        on=["trade_date", "symbol"],
        how="left",
        validate="one_to_one",
    )
    diagnostics = {
        "benchmark_date_min": str(benchmark["trade_date"].min()),
        "benchmark_date_max": str(benchmark["trade_date"].max()),
        "benchmark_rows": int(len(benchmark)),
        "rs_accel_valid_rows": int(featured["rs_accel"].notna().sum()),
        "rs_accel_valid_rate": float(featured["rs_accel"].notna().mean()),
        "rs_accel_positive_rows": int((featured["rs_accel"] > 0).fillna(False).sum()),
        "rs_accel_positive_rate_on_valid": (
            float((featured.loc[featured["rs_accel"].notna(), "rs_accel"] > 0).mean())
            if featured["rs_accel"].notna().any() else None
        ),
        "max_ignition_score": int(featured["ignition_score_0_5"].max()),
    }
    return panel, membership, cols, cmap, diagnostics


def cmd_check(cfg):
    catalog = Path(cfg["market_catalog"])
    pit = Path(cfg["pit_membership_parquet"])
    benchmark_file = Path(cfg["benchmark_file"])
    benchmark_actual_sha256 = sha256_file(benchmark_file) if benchmark_file.exists() else None
    benchmark_expected_sha256 = str(cfg.get("benchmark_sha256") or "").lower() or None
    benchmark_hash_match = (
        benchmark_file.exists()
        and benchmark_expected_sha256 is not None
        and benchmark_actual_sha256.lower() == benchmark_expected_sha256
    )
    benchmark_parse_ok = False
    benchmark_symbol_rows = 0
    benchmark_date_min = None
    benchmark_date_max = None
    benchmark_parse_error = None
    if benchmark_file.exists() and benchmark_hash_match:
        try:
            bcheck = pd.read_csv(
                benchmark_file, dtype={"trade_date": str, "symbol": str}
            )
            bcheck = bcheck[
                bcheck["symbol"].astype(str) == str(cfg["benchmark_symbol"])
            ].copy()
            bcheck["trade_date"] = pd.to_datetime(
                bcheck["trade_date"].astype(str).str.strip(),
                format="%Y%m%d",
                errors="raise",
            ).dt.strftime("%Y%m%d")
            benchmark_symbol_rows = int(len(bcheck))
            benchmark_date_min = str(bcheck["trade_date"].min()) if len(bcheck) else None
            benchmark_date_max = str(bcheck["trade_date"].max()) if len(bcheck) else None
            benchmark_parse_ok = benchmark_symbol_rows > 0
        except Exception as e:
            benchmark_parse_error = f"{type(e).__name__}: {e}"

    result = {
        "status": "PASS_OPPORTUNITY_TREND_IGNITION_V0_1_CHECK"
        if (
            catalog.exists()
            and pit.exists()
            and benchmark_file.exists()
            and benchmark_hash_match
            and benchmark_parse_ok
        )
        else "BLOCKED_OPPORTUNITY_TREND_IGNITION_V0_1_CHECK",
        "project_root": str(PROJECT_ROOT),
        "market_catalog": str(catalog),
        "market_catalog_exists": catalog.exists(),
        "market_table": cfg["market_table"],
        "pit_membership_parquet": str(pit),
        "pit_membership_exists": pit.exists(),
        "benchmark_symbol": cfg["benchmark_symbol"],
        "benchmark_file": str(benchmark_file),
        "benchmark_file_exists": benchmark_file.exists(),
        "benchmark_expected_sha256": benchmark_expected_sha256,
        "benchmark_actual_sha256": benchmark_actual_sha256,
        "benchmark_hash_match": benchmark_hash_match,
        "benchmark_parse_ok": benchmark_parse_ok,
        "benchmark_symbol_rows": benchmark_symbol_rows,
        "benchmark_date_min": benchmark_date_min,
        "benchmark_date_max": benchmark_date_max,
        "benchmark_parse_error": benchmark_parse_error,
        "stock_price_state": "SYNTHETIC_ADJ_CLOSE_OVER_PRE_CLOSE",
        "research_universe": cfg["research_universe"],
        "purpose": "Fast-reject mechanism test only; not a tradable strategy.",
        "feature_contract": cfg["feature_contract"],
        "label_contract": cfg["label_contract"],
        "gate": cfg["fast_reject_gate"],
        "safety": {
            "market_catalog_read_only": True,
            "production_db": False,
            "broker": False,
            "qmt": False,
            "production_code_mutation_by_runner": False,
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"].startswith("PASS") else 3


def cmd_run(cfg):
    panel, membership, cols, cmap, diagnostics = build_panel(cfg)

    primary_eval = evaluate(panel, PRIMARY.name)
    secondary_eval = evaluate(panel, SECONDARY.name)
    gate = fast_reject_gate(primary_eval)

    out_dir = Path(cfg["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    keep = [
        "trade_date", "symbol",
        "rs20", "rs20_prev", "rs_accel",
        "vol_compression", "atr_compression", "volume_contraction",
        "distance_to_60d_high", "higher_low_20_vs_60",
        "ignition_score_0_5", "trend_ignition_v01",
        f"{PRIMARY.name}_label", f"{PRIMARY.name}_mfe",
        f"{PRIMARY.name}_mae_before_hit", f"{PRIMARY.name}_time_to_target",
        f"{SECONDARY.name}_label", f"{SECONDARY.name}_mfe",
        f"{SECONDARY.name}_mae_before_hit", f"{SECONDARY.name}_time_to_target",
    ]
    keep = [c for c in keep if c in panel.columns]
    panel[keep].to_parquet(out_dir / "trend_ignition_v0_1_panel.parquet", index=False)

    result = {
        "status": gate["status"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "research_id": "OPPORTUNITY_TREND_IGNITION_V0_1",
        "scope": {
            "universe": cfg["research_universe"],
            "membership_rows": int(len(membership)),
            "signal_rows": int(panel["trend_ignition_v01"].fillna(False).sum()),
            "date_min": str(membership["trade_date"].min()),
            "date_max": str(membership["trade_date"].max()),
            "symbols": int(membership["symbol"].nunique()),
            "benchmark": cfg["benchmark_symbol"],
            "benchmark_source": cfg["benchmark_file"],
            "benchmark_expected_sha256": cfg.get("benchmark_sha256"),
            "benchmark_actual_sha256": sha256_file(Path(cfg["benchmark_file"])),
            "stock_price_state": "SYNTHETIC_ADJ_CLOSE_OVER_PRE_CLOSE",
        },
        "implementation_diagnostics": diagnostics,
        "market_schema_resolution": {
            "source_table": cfg["market_table"],
            "column_map": cmap,
            "source_columns": cols,
        },
        "feature_contract": cfg["feature_contract"],
        "label_contract": cfg["label_contract"],
        "primary": primary_eval,
        "secondary": secondary_eval,
        "fast_reject_gate": gate,
        "interpretation": (
            "PASS means the mechanism deserves V0.2 research. "
            "It does NOT qualify a strategy or authorize Forward/Portfolio/Execution."
        ),
        "safety": {
            "catalog_read_only": True,
            "production_db": False,
            "broker": False,
            "qmt": False,
        },
    }
    (out_dir / "trend_ignition_v0_1_summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"OUTPUT: {out_dir}")
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["check", "run"])
    args = p.parse_args()
    cfg = load_cfg()
    try:
        return cmd_check(cfg) if args.command == "check" else cmd_run(cfg)
    except Exception as e:
        print(json.dumps({
            "status": "FAIL_CLOSED_OPPORTUNITY_TREND_IGNITION_V0_1",
            "error": f"{type(e).__name__}: {e}",
        }, ensure_ascii=False, indent=2))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
