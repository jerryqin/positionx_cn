from __future__ import annotations
import json
from pathlib import Path

import pandas as pd

CATALOG = Path(r"D:\positionx_data\catalog\market_data.duckdb")
SYMBOL = "001368.SZ"
START = "20260915"
END = "20260924"
DIVID_START = "20260101"
DIVID_END = "20260929"


def _norm_df(obj):
    if isinstance(obj, dict):
        if SYMBOL in obj and isinstance(obj[SYMBOL], pd.DataFrame):
            return obj[SYMBOL].copy()
        # Some xtdata variants return field->DataFrame.
        fields = {}
        for k, v in obj.items():
            if isinstance(v, pd.DataFrame):
                if SYMBOL in v.index:
                    fields[k] = v.loc[SYMBOL]
                elif SYMBOL in v.columns:
                    fields[k] = v[SYMBOL]
        if fields:
            return pd.DataFrame(fields)
    if isinstance(obj, pd.DataFrame):
        return obj.copy()
    raise TypeError(f"Unsupported market-data shape: {type(obj)!r}")


def main():
    import duckdb
    from xtquant import xtdata

    print("="*100)
    print("POSITIONX EVENT PRICE BASIS DIAGNOSTIC V0.1")
    print("="*100)

    # 1) Accepted catalog rows: authoritative raw base currently used by Event V0.1.
    con = duckdb.connect(str(CATALOG), read_only=True)
    try:
        cat = con.execute("""
            SELECT symbol, trade_date, "open", "high", "low", "close",
                   volume, amount, pre_close, suspend_flag
            FROM market.daily_bars_v1
            WHERE symbol=? AND trade_date BETWEEN DATE '2026-09-15' AND DATE '2026-09-24'
            ORDER BY trade_date
        """, [SYMBOL]).fetchdf()
    finally:
        con.close()

    print("\n[CATALOG market.daily_bars_v1]")
    print(cat.to_string(index=False))

    # 2) Same local QMT bars under explicit adjustment modes.
    modes = ["none", "front", "front_ratio", "back", "back_ratio"]
    qmt = {}
    for mode in modes:
        try:
            raw = xtdata.get_market_data_ex(
                field_list=["open","high","low","close","preClose"],
                stock_list=[SYMBOL],
                period="1d",
                start_time=START,
                end_time=END,
                count=-1,
                dividend_type=mode,
                fill_data=False,
            )
            df = _norm_df(raw)
            qmt[mode] = df
            print(f"\n[QMT dividend_type={mode}]")
            print(df.to_string())
        except Exception as e:
            print(f"\n[QMT dividend_type={mode}] ERROR: {type(e).__name__}: {e}")

    # 3) Corporate-action / ex-right factors from QMT.
    try:
        div = xtdata.get_divid_factors(SYMBOL, start_time=DIVID_START, end_time=DIVID_END)
        print("\n[QMT get_divid_factors]")
        if isinstance(div, pd.DataFrame):
            print(div.to_string())
        else:
            print(repr(div))
    except Exception as e:
        print(f"\n[QMT get_divid_factors] ERROR: {type(e).__name__}: {e}")

    # 4) Compact compare for the two truth-probe dates.
    print("\n[COMPACT CLOSE COMPARE]")
    rows = []
    for mode, df in qmt.items():
        for d in ("20260921", "20260922", "20260923", "20260924"):
            val = None
            try:
                idx = [str(x).replace("-","")[:8] for x in df.index]
                m = [i for i, x in enumerate(idx) if x == d]
                if m and "close" in df.columns:
                    val = float(df.iloc[m[-1]]["close"])
            except Exception:
                pass
            rows.append({"mode": mode, "date": d, "close": val})
    cmp = pd.DataFrame(rows)
    print(cmp.pivot(index="date", columns="mode", values="close").to_string())

    print("\nREAD-ONLY: no downloads, no writes to catalog, no broker/order/execution.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
