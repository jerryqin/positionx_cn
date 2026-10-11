from __future__ import annotations

"""
PositionX — QMT front-adjustment PIT behavior diagnostic V0.1
Read-only. No download, no catalog write, no broker/order/execution.

Question:
Does QMT dividend_type='front' respect the query end date (PIT-safe), or does it
use future corporate actions already known in today's factor table?
"""

from datetime import datetime
import pandas as pd

SYMBOL = "001368.SZ"
START = "20260101"
ENDS = ["20260921", "20260922", "20260923", "20260924"]
MODES = ["none", "front", "front_ratio"]


def normalize(obj):
    if isinstance(obj, dict):
        if SYMBOL in obj and isinstance(obj[SYMBOL], pd.DataFrame):
            return obj[SYMBOL].copy()
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
    raise TypeError(type(obj))


def fetch(xtdata, mode, end):
    raw = xtdata.get_market_data_ex(
        field_list=["open", "high", "low", "close", "preClose"],
        stock_list=[SYMBOL],
        period="1d",
        start_time=START,
        end_time=end,
        count=-1,
        dividend_type=mode,
        fill_data=False,
    )
    return normalize(raw)


def row_close(df, d):
    key = d
    if key in df.index:
        return float(df.loc[key, "close"])
    # tolerate date-like indices
    for idx in df.index:
        s = str(idx).replace("-", "")[:8]
        if s == d:
            return float(df.loc[idx, "close"])
    return None


def max_close_before(df, d):
    pairs = []
    for idx, row in df.iterrows():
        s = str(idx).replace("-", "")[:8]
        if s < d:
            try:
                pairs.append((s, float(row["close"])))
            except Exception:
                pass
    return max(pairs, key=lambda x: x[1]) if pairs else (None, None)


def main():
    from xtquant import xtdata

    print("=" * 108)
    print("POSITIONX — QMT FRONT ADJUSTMENT PIT BEHAVIOR DIAGNOSTIC V0.1")
    print("=" * 108)

    results = {}

    for end in ENDS:
        print(f"\n{'#' * 36} QUERY END = {end} {'#' * 36}")
        results[end] = {}
        for mode in MODES:
            try:
                df = fetch(xtdata, mode, end)
                results[end][mode] = df
                print(f"\n[{mode}] last 5 rows")
                print(df.tail(5).to_string())
                prev_date, prev_max = max_close_before(df, end)
                today = row_close(df, end)
                print(
                    f"[{mode}] end_close={today} "
                    f"previous_all_time_max={prev_max} previous_max_date={prev_date} "
                    f"is_all_time_high={None if today is None or prev_max is None else today > prev_max}"
                )
            except Exception as e:
                print(f"\n[{mode}] ERROR {type(e).__name__}: {e}")

    print("\n" + "=" * 108)
    print("CROSS-END STABILITY TEST")
    print("=" * 108)
    probe_dates = ["20260918", "20260921", "20260922"]
    for mode in ["front", "front_ratio"]:
        print(f"\n[{mode}] same historical close as query end advances")
        rows = []
        for d in probe_dates:
            row = {"probe_date": d}
            for end in ENDS:
                df = results.get(end, {}).get(mode)
                row[f"end_{end}"] = row_close(df, d) if df is not None else None
            rows.append(row)
        print(pd.DataFrame(rows).to_string(index=False))

    print("\nINTERPRETATION:")
    print("If the same past close changes when query end moves from 20260922 to 20260923,")
    print("QMT 'front' is end-date-sensitive and may be usable for PIT reconstruction.")
    print("If 20260922 query already contains the 20260923 ex-right adjustment, it is NOT PIT-safe.")
    print("\nREAD-ONLY: no download / no catalog write / no broker / no execution.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
