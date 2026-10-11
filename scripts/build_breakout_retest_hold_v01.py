from __future__ import annotations

r"""
PositionX — Breakout -> Retest -> Hold Research V0.1

Fixed hypothesis, no parameter sweep:
- breakout cluster start: PIT comparable close > prior 60-session highest high,
  previous session not itself a breakout;
- retest: FIRST low touch of breakout level within next 10 sessions;
- hold: first-touch close >= breakout level;
- signal known at T close; entry T+1 exact OPEN; exit T+20 exact CLOSE;
- costs: 20bp main / 50bp shadow total round-trip;
- same-symbol dedup: first signal while flat within each profile.

Research only. No broker/order/production SQLite/catalog mutation.
Historical ST PIT is incomplete, so results remain provisional.
"""

import argparse
import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

CATALOG = Path(r"D:\positionx_data\catalog\market_data.duckdb")
OUT_ROOT = Path(r"D:\positionx_data\strategies\breakout_retest_hold_v0_1")
LOOKBACK_SESSIONS = 60
RETEST_MAX_SESSIONS = 10
EXIT_SESSIONS = 20
COST_MAIN = 0.002
COST_SHADOW = 0.005
PRICE_BASIS = "PIT_BACK_RATIO_COMPARABLE_V1"


def safe(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, bool)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if hasattr(v, "item"):
        try:
            return safe(v.item())
        except Exception:
            pass
    return str(v)


def qpath(p: Path) -> str:
    return str(p).replace("\\", "/").replace("'", "''")


def write_json_atomic(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=safe), encoding="utf-8")
    os.replace(tmp, path)


def era_of_date(v: Any) -> str:
    y = pd.Timestamp(v).year
    if y <= 1994: return "1990-1994"
    if y <= 1999: return "1995-1999"
    if y <= 2004: return "2000-2004"
    if y <= 2009: return "2005-2009"
    if y <= 2014: return "2010-2014"
    if y <= 2019: return "2015-2019"
    if y <= 2024: return "2020-2024"
    return "2025-2026"


def dedup_same_symbol_while_flat(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    accepted: List[int] = []
    work = df.sort_values(["profile", "symbol", "signal_idx", "breakout_idx"]).reset_index(drop=True)
    for (_, _), g in work.groupby(["profile", "symbol"], sort=False):
        blocked_until = -1
        for idx, row in g.iterrows():
            entry_idx = int(row["signal_idx"]) + 1
            intended_exit_idx = int(row["signal_idx"]) + EXIT_SESSIONS
            if entry_idx <= blocked_until:
                continue
            accepted.append(idx)
            blocked_until = intended_exit_idx
    return work.loc[accepted].reset_index(drop=True)


def summarize(df: pd.DataFrame, group_cols: List[str]) -> pd.DataFrame:
    rows = []
    grouped = [((), df)] if not group_cols else df.groupby(group_cols, dropna=False, sort=True)
    for key, g in grouped:
        if group_cols:
            if not isinstance(key, tuple):
                key = (key,)
            row = {c: safe(v) for c, v in zip(group_cols, key)}
        else:
            row = {}
        complete = g[g["complete_20d"] == True].copy()
        fill = complete[complete["fillable_20d"] == True].copy()
        row.update({
            "signals": int(len(g)),
            "complete_20d": int(len(complete)),
            "fillable_20d": int(len(fill)),
            "fill_rate_on_complete": (float(len(fill) / len(complete)) if len(complete) else None),
            "avg_gross20": (float(fill["gross20"].mean()) if len(fill) else None),
            "median_gross20": (float(fill["gross20"].median()) if len(fill) else None),
            "win_rate20": (float((fill["gross20"] > 0).mean()) if len(fill) else None),
            "avg_net20bps": (float(fill["net20bps"].mean()) if len(fill) else None),
            "median_net20bps": (float(fill["net20bps"].median()) if len(fill) else None),
            "avg_net50bps": (float(fill["net50bps"].mean()) if len(fill) else None),
            "median_net50bps": (float(fill["net50bps"].median()) if len(fill) else None),
            "avg_alpha20_median_benchmark": (float(fill["alpha_median_20d"].mean()) if len(fill) else None),
            "median_alpha20_median_benchmark": (float(fill["alpha_median_20d"].median()) if len(fill) else None),
            "alpha_win20_median_benchmark": (float((fill["alpha_median_20d"] > 0).mean()) if len(fill) else None),
        })
        rows.append(row)
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="PositionX Breakout-Retest-Hold research V0.1")
    ap.add_argument("--catalog", default=str(CATALOG))
    ap.add_argument("--out-root", default=str(OUT_ROOT))
    args = ap.parse_args()

    catalog = Path(args.catalog)
    out_root = Path(args.out_root)
    if not catalog.exists():
        raise RuntimeError(f"Catalog not found: {catalog}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = out_root / f"full_{stamp}"
    out.mkdir(parents=True, exist_ok=False)

    import duckdb
    con = duckdb.connect(":memory:")
    try:
        con.execute(f"ATTACH '{qpath(catalog)}' AS cat (READ_ONLY)")
        base_stats = con.execute("""
            SELECT count(*), count(DISTINCT symbol), min(trade_date), max(trade_date),
                   count(*) - count(DISTINCT (symbol, trade_date))
            FROM cat.market.daily_bars_v1
        """).fetchone()
        if int(base_stats[4]) != 0:
            raise RuntimeError(f"FAIL_CLOSED duplicate symbol/date rows={base_stats[4]}")

        print("BUILD PIT comparable daily state ...")
        con.execute("""
            CREATE TEMP TABLE raw_seq AS
            SELECT symbol, trade_date, "open", "high", "low", "close", volume, amount,
                   pre_close, suspend_flag,
                   row_number() OVER (PARTITION BY symbol ORDER BY trade_date) AS idx,
                   lag("close") OVER (PARTITION BY symbol ORDER BY trade_date) AS prev_raw_close
            FROM cat.market.daily_bars_v1
        """)
        invalid_ratio = int(con.execute("""
            SELECT count(*) FROM raw_seq
            WHERE idx > 1 AND (
              prev_raw_close IS NULL OR prev_raw_close <= 0 OR pre_close IS NULL OR pre_close <= 0
            )
        """).fetchone()[0])
        if invalid_ratio:
            raise RuntimeError(f"FAIL_CLOSED invalid PIT daily-ratio prerequisites={invalid_ratio}")

        con.execute("""
            CREATE TEMP TABLE px AS
            WITH ratio AS (
              SELECT *, CASE WHEN idx=1 THEN 1.0 ELSE prev_raw_close/pre_close END AS daily_dr
              FROM raw_seq
            ), cum AS (
              SELECT *, exp(sum(ln(daily_dr)) OVER (
                PARTITION BY symbol ORDER BY trade_date
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
              )) AS cum_dr
              FROM ratio
            ), pit AS (
              SELECT *, "open"*cum_dr AS pit_open, "high"*cum_dr AS pit_high,
                        "low"*cum_dr AS pit_low, "close"*cum_dr AS pit_close
              FROM cum
            ), roll AS (
              SELECT *,
                count(*) OVER (PARTITION BY symbol ORDER BY trade_date ROWS BETWEEN 60 PRECEDING AND 1 PRECEDING) AS n_prev60,
                max(pit_high) OVER (PARTITION BY symbol ORDER BY trade_date ROWS BETWEEN 60 PRECEDING AND 1 PRECEDING) AS prev60_high
              FROM pit
            ), flag AS (
              SELECT *, (n_prev60=60 AND pit_close>prev60_high) AS is_breakout
              FROM roll
            )
            SELECT *, coalesce(lag(is_breakout) OVER (PARTITION BY symbol ORDER BY trade_date), false) AS prev_is_breakout
            FROM flag
        """)
        pit_bad = int(con.execute("""
            SELECT count(*) FROM px
            WHERE cum_dr IS NULL OR cum_dr <= 0 OR pit_open <= 0 OR pit_high <= 0 OR pit_low <= 0 OR pit_close <= 0
        """).fetchone()[0])
        if pit_bad:
            raise RuntimeError(f"FAIL_CLOSED bad PIT comparable rows={pit_bad}")

        print("BUILD breakout cluster starts and first-touch retests ...")
        con.execute("""
            CREATE TEMP TABLE breakouts AS
            SELECT symbol, trade_date AS breakout_date, idx AS breakout_idx,
                   prev60_high AS breakout_level, pit_close AS breakout_close
            FROM px
            WHERE is_breakout=true AND prev_is_breakout=false
        """)
        con.execute(f"""
            CREATE TEMP TABLE breakout_eval AS
            WITH first_touch AS (
              SELECT b.symbol, b.breakout_date, b.breakout_idx, b.breakout_level, b.breakout_close,
                     min(p.idx) FILTER (WHERE p.pit_low <= b.breakout_level) AS retest_idx
              FROM breakouts b
              LEFT JOIN px p ON p.symbol=b.symbol
               AND p.idx BETWEEN b.breakout_idx+1 AND b.breakout_idx+{RETEST_MAX_SESSIONS}
              GROUP BY b.symbol,b.breakout_date,b.breakout_idx,b.breakout_level,b.breakout_close
            )
            SELECT f.*, r.trade_date AS retest_date, r.pit_open AS retest_open,
                   r.pit_high AS retest_high, r.pit_low AS retest_low, r.pit_close AS retest_close,
                   CASE WHEN f.retest_idx IS NULL THEN 'NO_RETEST_10'
                        WHEN r.pit_close >= f.breakout_level THEN 'HOLD' ELSE 'FAIL' END AS retest_state,
                   CASE WHEN f.retest_idx IS NULL THEN NULL ELSE f.retest_idx-f.breakout_idx END AS retest_wait_sessions
            FROM first_touch f
            LEFT JOIN px r ON r.symbol=f.symbol AND r.idx=f.retest_idx
        """)
        counts = con.execute("""
            SELECT count(*), count(*) FILTER(WHERE retest_state='HOLD'),
                   count(*) FILTER(WHERE retest_state='FAIL'),
                   count(*) FILTER(WHERE retest_state='NO_RETEST_10')
            FROM breakout_eval
        """).fetchone()

        raw_signals = con.execute("""
            SELECT 'BREAKOUT_CLUSTER_START' AS profile, symbol, breakout_date, breakout_idx,
                   breakout_level, breakout_date AS signal_date, breakout_idx AS signal_idx,
                   NULL::BIGINT AS retest_wait_sessions
            FROM breakout_eval
            UNION ALL
            SELECT 'BREAKOUT_RETEST_HOLD', symbol, breakout_date, breakout_idx, breakout_level,
                   retest_date, retest_idx, retest_wait_sessions
            FROM breakout_eval WHERE retest_state='HOLD'
            UNION ALL
            SELECT 'BREAKOUT_RETEST_FAIL', symbol, breakout_date, breakout_idx, breakout_level,
                   retest_date, retest_idx, retest_wait_sessions
            FROM breakout_eval WHERE retest_state='FAIL'
        """).df()

        accepted = dedup_same_symbol_while_flat(raw_signals)
        accepted["signal_date"] = pd.to_datetime(accepted["signal_date"])
        accepted["breakout_date"] = pd.to_datetime(accepted["breakout_date"])
        accepted["era_5y"] = accepted["signal_date"].map(era_of_date)
        con.register("accepted_signals_df", accepted)

        print("BUILD T+1 OPEN -> T+20 CLOSE tradability/outcomes ...")
        trades = con.execute(f"""
            SELECT a.*, e.trade_date AS entry_date, x.trade_date AS exit_date,
                   e.pit_open AS entry_open_pit, x.pit_close AS exit_close_pit,
                   CASE WHEN e.symbol IS NULL OR x.symbol IS NULL THEN false ELSE true END AS complete_20d,
                   CASE
                     WHEN e.symbol IS NULL OR x.symbol IS NULL THEN false
                     WHEN e."open"=e."high" AND e."high"=e."low" AND e."open">e.pre_close THEN false
                     WHEN x."open"=x."high" AND x."high"=x."low" AND x."close"<x.pre_close THEN false
                     ELSE true END AS fillable_20d,
                   CASE WHEN e."open"=e."high" AND e."high"=e."low" AND e."open">e.pre_close THEN true ELSE false END AS blocked_one_price_upper_entry,
                   CASE WHEN x."open"=x."high" AND x."high"=x."low" AND x."close"<x.pre_close THEN true ELSE false END AS blocked_one_price_lower_exit,
                   CASE WHEN e.symbol IS NOT NULL AND x.symbol IS NOT NULL THEN x.pit_close/e.pit_open-1 END AS gross20
            FROM accepted_signals_df a
            LEFT JOIN px e ON e.symbol=a.symbol AND e.idx=a.signal_idx+1
            LEFT JOIN px x ON x.symbol=a.symbol AND x.idx=a.signal_idx+{EXIT_SESSIONS}
        """).df()

        signal_dates = pd.DataFrame({"signal_date": pd.to_datetime(trades["signal_date"]).drop_duplicates().sort_values()})
        con.register("signal_dates_df", signal_dates)
        print("BUILD same-signal-date whole-market benchmark ...")
        benchmark = con.execute(f"""
            WITH m AS (
              SELECT s.trade_date AS signal_date, x.pit_close/e.pit_open-1 AS market_ret20
              FROM px s
              JOIN signal_dates_df d ON s.trade_date=d.signal_date
              JOIN px e ON e.symbol=s.symbol AND e.idx=s.idx+1
              JOIN px x ON x.symbol=s.symbol AND x.idx=s.idx+{EXIT_SESSIONS}
              WHERE NOT (e."open"=e."high" AND e."high"=e."low" AND e."open">e.pre_close)
                AND NOT (x."open"=x."high" AND x."high"=x."low" AND x."close"<x.pre_close)
            )
            SELECT signal_date, count(*) AS market_n20, avg(market_ret20) AS market_mean_20d,
                   median(market_ret20) AS market_median_20d
            FROM m GROUP BY signal_date
        """).df()

        for c in ["signal_date", "entry_date", "exit_date"]:
            trades[c] = pd.to_datetime(trades[c])
        trades = trades.merge(benchmark, how="left", on="signal_date")
        trades["net20bps"] = trades["gross20"] - COST_MAIN
        trades["net50bps"] = trades["gross20"] - COST_SHADOW
        trades["alpha_median_20d"] = trades["gross20"] - trades["market_median_20d"]
        trades["alpha_mean_20d"] = trades["gross20"] - trades["market_mean_20d"]

        missing_bench = int(trades.loc[trades["complete_20d"] == True, "market_median_20d"].isna().sum())
        if missing_bench:
            raise RuntimeError(f"FAIL_CLOSED missing benchmark on complete trades={missing_bench}")

        con.register("trades_df", trades)
        con.execute(f"COPY trades_df TO '{qpath(out/'candidate_trades.parquet')}' (FORMAT PARQUET, COMPRESSION ZSTD)")

        strategy_summary = summarize(trades, ["profile"])
        era_summary = summarize(trades, ["profile", "era_5y"])
        hold = trades[trades["profile"] == "BREAKOUT_RETEST_HOLD"].copy()
        wait_summary = summarize(hold, ["retest_wait_sessions"])
        strategy_summary.to_csv(out/"strategy_summary.csv", index=False, encoding="utf-8-sig")
        era_summary.to_csv(out/"era_summary.csv", index=False, encoding="utf-8-sig")
        wait_summary.to_csv(out/"retest_wait_diagnostic.csv", index=False, encoding="utf-8-sig")

        manifest = {
            "status": "PASS_BREAKOUT_RETEST_HOLD_RESEARCH_V0_1",
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "catalog": str(catalog),
            "source_rows": int(base_stats[0]),
            "source_symbols": int(base_stats[1]),
            "source_first_date": safe(base_stats[2]),
            "source_last_date": safe(base_stats[3]),
            "duplicate_symbol_dates": int(base_stats[4]),
            "pit_ratio_invalid_rows": invalid_ratio,
            "pit_bad_rows": pit_bad,
            "price_basis": PRICE_BASIS,
            "fixed_hypothesis": {
                "lookback_sessions": LOOKBACK_SESSIONS,
                "breakout": "close > prior 60-session highest high; cluster start only",
                "retest": "FIRST low touch of breakout level within next 10 sessions",
                "hold": "first-touch close >= breakout level",
                "entry": "signal T+1 exact market-session OPEN",
                "exit": "signal T+20 exact market-session CLOSE",
                "same_symbol_dedup": "new entry strictly after prior intended T+20 exit",
                "cost_main_total_roundtrip_bps": 20,
                "cost_shadow_total_roundtrip_bps": 50,
                "parameter_sweep": False,
            },
            "breakout_clusters": int(counts[0]),
            "retest_hold_raw": int(counts[1]),
            "retest_fail_raw": int(counts[2]),
            "no_retest_10_raw": int(counts[3]),
            "accepted_signals": int(len(trades)),
            "strict_non_st_pit_applied": False,
            "historical_research_ready": False,
            "output_dir": str(out),
            "outputs": ["candidate_trades.parquet", "strategy_summary.csv", "era_summary.csv", "retest_wait_diagnostic.csv", "run_manifest.json"],
        }
        write_json_atomic(out/"run_manifest.json", manifest)
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
