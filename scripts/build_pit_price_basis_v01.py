from __future__ import annotations

"""
PositionX — PIT Comparable Price Basis V0.1 (SMOKE)

Purpose
-------
Derive a PIT-safe ratio-adjusted comparable price series from accepted RAW daily bars.

For symbol date t:
  prev_raw_close_t = raw close on previous symbol observation
  daily_dr_t       = prev_raw_close_t / pre_close_t
  cum_dr_t         = product(daily_dr_k, k <= t)
  pit_price_t      = raw_price_t * cum_dr_t

Why this works
--------------
On an ex-right/ex-dividend date, official pre_close is already adjusted to today's
basis. Since:
    pre_close_t * cum_dr_t == prev_raw_close_t * cum_dr_(t-1)
the comparable series is continuous across the corporate action boundary.

Only information effective on or before t is used. Future corporate actions do not
rewrite earlier dates.

V0.1 is smoke/truth validation only. It does not modify the catalog.
"""

import argparse
import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any, List

import pandas as pd

CATALOG = Path(r"D:\positionx_data\catalog\market_data.duckdb")
OUT_ROOT = Path(r"D:\positionx_data\price_basis\pit_comparable_v1")

SMOKE_SYMBOLS = [
    "001368.SZ",  # 通达创智 truth probe
    "603986.SH", "601689.SH", "002050.SZ", "600699.SH",
    "300308.SZ", "300502.SZ", "601138.SH", "688041.SH",
    "600519.SH", "300750.SZ", "688981.SH",
]


def safe(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        if isinstance(v, float) and not math.isfinite(v):
            return None
        return v
    if hasattr(v, "item"):
        try:
            return safe(v.item())
        except Exception:
            pass
    return str(v)


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=safe), encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", help="comma-separated smoke symbols")
    a = ap.parse_args()

    symbols = (
        [x.strip().upper() for x in a.symbols.split(",") if x.strip()]
        if a.symbols else list(SMOKE_SYMBOLS)
    )

    import duckdb
    con = duckdb.connect(str(CATALOG), read_only=True)
    try:
        ph = ",".join("?" for _ in symbols)
        df = con.execute(
            f"""
            WITH x AS (
              SELECT
                symbol,
                trade_date,
                "open", "high", "low", "close",
                volume, amount, pre_close, suspend_flag,
                lag("close") OVER (
                  PARTITION BY symbol ORDER BY trade_date
                ) AS prev_raw_close
              FROM market.daily_bars_v1
              WHERE symbol IN ({ph})
            ),
            r AS (
              SELECT *,
                CASE
                  WHEN prev_raw_close IS NULL THEN 1.0
                  WHEN prev_raw_close <= 0 OR pre_close <= 0 THEN NULL
                  ELSE prev_raw_close / pre_close
                END AS daily_dr
              FROM x
            ),
            f AS (
              SELECT *,
                exp(
                  sum(
                    CASE
                      WHEN daily_dr IS NULL THEN 0.0
                      ELSE ln(daily_dr)
                    END
                  ) OVER (
                    PARTITION BY symbol ORDER BY trade_date
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                  )
                ) AS cum_dr
              FROM r
            ),
            p AS (
              SELECT *,
                "open" * cum_dr AS pit_open,
                "high" * cum_dr AS pit_high,
                "low" * cum_dr AS pit_low,
                "close" * cum_dr AS pit_close,
                pre_close * cum_dr AS pit_pre_close
              FROM f
            ),
            h AS (
              SELECT *,
                count(*) OVER (
                  PARTITION BY symbol ORDER BY trade_date
                  ROWS BETWEEN 250 PRECEDING AND 1 PRECEDING
                ) AS prior_250_n,
                max(pit_close) OVER (
                  PARTITION BY symbol ORDER BY trade_date
                  ROWS BETWEEN 250 PRECEDING AND 1 PRECEDING
                ) AS prev_250_max_pit_close,
                min(pit_close) OVER (
                  PARTITION BY symbol ORDER BY trade_date
                  ROWS BETWEEN 250 PRECEDING AND 1 PRECEDING
                ) AS prev_250_min_pit_close,
                max(pit_close) OVER (
                  PARTITION BY symbol ORDER BY trade_date
                  ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                ) AS prev_all_max_pit_close,
                min(pit_close) OVER (
                  PARTITION BY symbol ORDER BY trade_date
                  ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                ) AS prev_all_min_pit_close
              FROM p
            )
            SELECT *,
              abs(coalesce(daily_dr, 1.0) - 1.0) > 1e-10 AS corporate_action_boundary,
              prior_250_n = 250 AND pit_close > prev_250_max_pit_close AS price_52w_high,
              prior_250_n = 250 AND pit_close < prev_250_min_pit_close AS price_52w_low,
              prev_all_max_pit_close IS NOT NULL AND pit_close > prev_all_max_pit_close AS price_all_time_high,
              prev_all_min_pit_close IS NOT NULL AND pit_close < prev_all_min_pit_close AS price_all_time_low
            FROM h
            ORDER BY symbol, trade_date
            """,
            symbols,
        ).fetchdf()
    finally:
        con.close()

    if df.empty:
        raise RuntimeError("No data returned.")

    # Hard continuity audit on adjustment boundaries.
    d = df.copy()
    d["prev_pit_close"] = d.groupby("symbol")["pit_close"].shift(1)
    boundaries = d[d["corporate_action_boundary"] & d["prev_pit_close"].notna()].copy()
    boundaries["continuity_error"] = boundaries["pit_pre_close"] - boundaries["prev_pit_close"]
    max_err = float(boundaries["continuity_error"].abs().max()) if len(boundaries) else 0.0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = OUT_ROOT / f"smoke_{stamp}"
    out.mkdir(parents=True, exist_ok=False)

    df.to_parquet(out / "pit_price_basis_smoke.parquet", index=False)
    df.to_csv(out / "pit_price_basis_smoke.csv", index=False, encoding="utf-8-sig")
    boundaries.to_csv(out / "corporate_action_boundaries.csv", index=False, encoding="utf-8-sig")

    probe = df[
        (df["symbol"] == "001368.SZ")
        & (df["trade_date"] >= pd.Timestamp("2026-09-15"))
        & (df["trade_date"] <= pd.Timestamp("2026-09-24"))
    ].copy()
    cols = [
        "symbol","trade_date","close","pre_close","prev_raw_close",
        "daily_dr","cum_dr","pit_close","prev_250_max_pit_close",
        "prev_all_max_pit_close","corporate_action_boundary",
        "price_52w_high","price_all_time_high",
    ]
    probe[cols].to_csv(
        out / "truth_probe_001368_SZ_20260915_20260924.csv",
        index=False, encoding="utf-8-sig"
    )

    manifest = {
        "status": "PASS_PIT_PRICE_BASIS_SMOKE" if max_err < 1e-8 else "FAIL_CONTINUITY",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "symbols": symbols,
        "rows": int(len(df)),
        "corporate_action_boundaries": int(len(boundaries)),
        "max_boundary_continuity_error": max_err,
        "formula": {
            "daily_dr": "prev_raw_close / pre_close",
            "cum_dr": "cumulative product of daily_dr through T",
            "pit_price": "raw_price * cum_dr",
        },
        "semantics": {
            "future_action_rewrites_past": False,
            "price_basis": "PIT_BACK_RATIO_COMPARABLE_V1",
            "raw_market_data_unchanged": True,
        },
        "output_dir": str(out),
    }
    write_json(out / "run_manifest.json", manifest)

    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print("\n[001368.SZ truth probe]")
    print(probe[cols].to_string(index=False))
    print("\nREAD-ONLY catalog; no broker/order/execution; no catalog mutation.")
    return 0 if manifest["status"].startswith("PASS") else 3


if __name__ == "__main__":
    raise SystemExit(main())
