from __future__ import annotations

r"""
PositionX — CSI500 Adjusted 52-Week High Fast-Reject V0.1

Goal
----
Test the 52-week-high anchoring / underreaction mechanism as a trend-family
candidate that could complement Deep Oversold.

Important portfolio-research discipline:
- This is in the SAME broad trend/underreaction family as ret120.
- If it passes, it competes with ret120 for the trend-family slot; it is not
  counted as a second independent mechanism merely because the signal differs.

Universe
--------
Accepted CSI500 PIT membership on signal date T.

Corporate-action-safe price state
----------------------------------
The canonical daily store is raw/unadjusted. Instead of comparing raw closes
across ex-right dates, construct a synthetic adjusted price index from the
stored exchange/QMT reference price:

    daily_factor_t = close_t / pre_close_t
    adj_close_t    = cumulative product(daily_factor)
    adj_open_t     = adj_close_t * open_t / close_t

This preserves the observed OPEN/CLOSE economics while removing mechanical
reference-price jumps caused by corporate actions.

Signal
------
T close:
    POS = adj_close_T > max(previous 250 symbol observations adj_close)
    NEG = adj_close_T < min(previous 250 symbol observations adj_close)

Requires 250 prior valid observations.

Timing
------
Signal timestamp: T close
Entry: T+1 exact OPEN
Exit: S20 / S60 / S120 exact CLOSE, S1 = entry session
All horizons use the SAME H120-complete signal cohort.
No shifting for missing exact entry/exit bars.

Market hygiene only
-------------------
- accepted CSI500 PIT signal-date membership
- signal and entry status must exist in foundation trading-status asset
- known ST signal/entry excluded
- known new-listing-special signal/entry excluded
- T+1 must be can_buy_at_open=true
- unknown ST retained and reported
- no volume / turnover / RSI / industry / size / valuation / market regime

Event-study design
------------------
- event-weighted statistics
- signal-date-equal statistics
- exact-window CSI500 PIT cross-sectional median benchmark
- yearly stability
- repeated same-symbol new highs are NOT deduped in Phase 1; if the mechanism
  passes, shared-equity portfolio audit will apply one-position-per-symbol
  semantics.

Predeclared gate
----------------
H20 is reaction diagnostic only.

H60 and H120 each pass only if:
1) POS adjusted mean > 0
2) POS adjusted 5% trimmed mean > 0
3) POS signal-date-equal adjusted mean > 0
4) POS-NEG adjusted mean spread > 0
5) POS-NEG signal-date-equal spread > 0
6) min(POS n, NEG n) >= 500
7) POS adjusted mean > 0 in >=2 valid years
8) POS-NEG adjusted mean spread > 0 in >=2 valid years
   valid year requires >=100 observations in both groups.

Verdict:
- STRONG_52WH_CANDIDATE: H60 and H120 pass
- PROMISING_HORIZON_CANDIDATE: exactly one passes
- FAST_REJECT: neither passes and either
    both POS trim5 <= 0 OR both date-equal spreads <= 0
- INCONCLUSIVE: otherwise
"""

import argparse
import hashlib
import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CATALOG = Path(r"D:\positionx_data\catalog\market_data.duckdb")
CSI500_MEMBERSHIP = (
    ROOT / "data/research/universe_assets/csi500/csi500_membership_daily.csv"
)
EXPECTED_CSI500_MEMBERSHIP_SHA256 = (
    "8ab943bcb14a95a9c06a24809f96eb2b61a3fa9244cffe2a861413cd203ad542"
)
STATUS_PARQUET = Path(
    r"D:\positionx_data\incoming\eva\20260925_foundation_assets"
    r"\data\research\px_assets\a_share_daily_trading_status.parquet"
)
EXPECTED_STATUS_SHA256 = (
    "bfe788d755fabb03986635d975cab22f1311af3a82ec533eb244ab85560c5bf2"
)
OUT_ROOT = Path(r"D:\positionx_data\research\csi500_52wh_v0_1")

SIGNAL_START = "20240101"
LOOKBACK_START = "20220101"
LOOKBACK_OBS = 250
HORIZONS = (20, 60, 120)
MIN_GROUP_N = 500
MIN_YEAR_GROUP_N = 100


def sha256_file(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def safe_float(v: Any) -> float | None:
    if v is None or pd.isna(v):
        return None
    return float(v)


def trim_mean(s: pd.Series, pct: float) -> float | None:
    x = pd.to_numeric(s, errors="coerce").dropna().to_numpy(dtype=float)
    if len(x) == 0:
        return None
    x.sort()
    k = int(math.floor(len(x) * pct))
    if 2 * k >= len(x):
        return None
    if k:
        x = x[k:-k]
    return float(np.mean(x))


def validate_files() -> dict:
    for p in (CATALOG, CSI500_MEMBERSHIP, STATUS_PARQUET):
        if not p.exists():
            raise RuntimeError(f"FAIL_CLOSED missing input: {p}")

    membership_sha = sha256_file(CSI500_MEMBERSHIP)
    if membership_sha != EXPECTED_CSI500_MEMBERSHIP_SHA256:
        raise RuntimeError(
            "FAIL_CLOSED CSI500 membership hash drift "
            f"expected={EXPECTED_CSI500_MEMBERSHIP_SHA256} actual={membership_sha}"
        )
    status_sha = sha256_file(STATUS_PARQUET)
    if status_sha != EXPECTED_STATUS_SHA256:
        raise RuntimeError(
            "FAIL_CLOSED trading-status hash drift "
            f"expected={EXPECTED_STATUS_SHA256} actual={status_sha}"
        )
    return {
        "catalog_sha256": sha256_file(CATALOG),
        "membership_sha256": membership_sha,
        "status_sha256": status_sha,
    }


def load_membership() -> pd.DataFrame:
    x = pd.read_csv(CSI500_MEMBERSHIP, dtype=str)
    cols = {c.lower(): c for c in x.columns}
    if "trade_date" not in cols or "symbol" not in cols:
        raise RuntimeError(
            f"FAIL_CLOSED membership columns={list(x.columns)}"
        )
    out = x[[cols["trade_date"], cols["symbol"]]].copy()
    out.columns = ["trade_date", "symbol"]
    out["trade_date"] = (
        out["trade_date"].astype(str)
        .str.replace("-", "", regex=False)
        .str.replace(".0", "", regex=False)
        .str[:8]
    )
    out["symbol"] = out["symbol"].astype(str)
    if out.duplicated(["trade_date", "symbol"]).any():
        raise RuntimeError("FAIL_CLOSED duplicate CSI500 membership keys")

    counts = out.groupby("trade_date")["symbol"].nunique()
    bad = counts[counts != 500]
    if len(bad):
        raise RuntimeError(
            f"FAIL_CLOSED CSI500 membership size anomalies "
            f"sample={bad.head(10).to_dict()}"
        )
    return out


def load_calendar(con: duckdb.DuckDBPyConnection) -> list[str]:
    rows = con.execute(
        """
        SELECT DISTINCT strftime(CAST(trade_date AS DATE), '%Y%m%d') d
        FROM market.daily_bars_v1
        WHERE strftime(CAST(trade_date AS DATE), '%Y%m%d') >= ?
        ORDER BY d
        """,
        [SIGNAL_START],
    ).fetchall()
    cal = [str(r[0]) for r in rows if r and r[0]]
    if len(cal) < 200:
        raise RuntimeError("FAIL_CLOSED short market calendar")
    return cal


def signal_cutoff(calendar: list[str]) -> str:
    # T=i, T+1 entry=i+1, S120 exit=i+120.
    if len(calendar) < 121:
        raise RuntimeError("FAIL_CLOSED no H120 runway")
    return calendar[-121]


def describe_catalog(con: duckdb.DuckDBPyConnection) -> dict:
    cols = {
        str(r[0])
        for r in con.execute("DESCRIBE market.daily_bars_v1").fetchall()
    }
    req = {"symbol", "trade_date", "open", "close", "pre_close"}
    missing = sorted(req - cols)
    if missing:
        raise RuntimeError(
            f"FAIL_CLOSED market.daily_bars_v1 missing={missing}"
        )
    row = con.execute(
        """
        SELECT
          count(*) n,
          count(DISTINCT symbol) symbols,
          min(CAST(trade_date AS DATE)) d0,
          max(CAST(trade_date AS DATE)) d1,
          count(*) FILTER (
            WHERE pre_close IS NULL OR pre_close<=0
               OR "open" IS NULL OR "open"<=0
               OR "close" IS NULL OR "close"<=0
          ) invalid_price_rows
        FROM market.daily_bars_v1
        """
    ).fetchone()
    return {
        "rows": int(row[0]),
        "symbols": int(row[1]),
        "min_date": str(row[2]),
        "max_date": str(row[3]),
        "invalid_price_rows": int(row[4]),
    }


def load_status_for_keys(
    keys: pd.DataFrame,
    key_date_col: str,
    prefix: str,
) -> pd.DataFrame:
    p = str(STATUS_PARQUET).replace("\\", "/").replace("'", "''")
    k = keys[["symbol", key_date_col]].drop_duplicates().copy()
    k = k.rename(columns={key_date_col: "key_date"})
    con = duckdb.connect(":memory:")
    con.register("keys", k)
    try:
        out = con.execute(
            f"""
            SELECT
              k.symbol,
              k.key_date,
              s.symbol IS NOT NULL AS {prefix}_status_found,
              s.is_suspended AS {prefix}_is_suspended,
              s.new_listing_special AS {prefix}_new_listing_special,
              s.can_buy_at_open AS {prefix}_can_buy_at_open,
              s.st_known AS {prefix}_st_known,
              s.st_status AS {prefix}_st_status
            FROM keys k
            LEFT JOIN read_parquet('{p}') s
              ON s.symbol=k.symbol
             AND strftime(CAST(s.trade_date AS DATE),'%Y%m%d')=k.key_date
            """
        ).df()
    finally:
        con.unregister("keys")
        con.close()
    return out.rename(columns={"key_date": key_date_col})


def load_price_panel(
    con: duckdb.DuckDBPyConnection,
    symbols: list[str],
    cutoff_exit: str,
) -> pd.DataFrame:
    sy = pd.DataFrame({"symbol": symbols})
    con.register("px_symbols", sy)
    try:
        x = con.execute(
            """
            SELECT
              CAST(b.symbol AS VARCHAR) symbol,
              strftime(CAST(b.trade_date AS DATE),'%Y%m%d') trade_date,
              CAST(b."open" AS DOUBLE) "open",
              CAST(b."close" AS DOUBLE) "close",
              CAST(b.pre_close AS DOUBLE) pre_close
            FROM market.daily_bars_v1 b
            INNER JOIN px_symbols s USING(symbol)
            WHERE strftime(CAST(b.trade_date AS DATE),'%Y%m%d')
                  BETWEEN ? AND ?
            ORDER BY b.symbol, b.trade_date
            """,
            [LOOKBACK_START, cutoff_exit],
        ).df()
    finally:
        con.unregister("px_symbols")

    for c in ("open", "close", "pre_close"):
        x[c] = pd.to_numeric(x[c], errors="coerce")

    x["valid_factor"] = (
        (x["close"] > 0)
        & (x["pre_close"] > 0)
        & np.isfinite(x["close"])
        & np.isfinite(x["pre_close"])
    )
    x["daily_factor"] = np.where(
        x["valid_factor"], x["close"] / x["pre_close"], np.nan
    )
    # Corporate-action-safe synthetic adjusted close index.
    # Invalid factor rows break continuity for that row; the count gate below
    # prevents them from creating false 52-week signals.
    x["log_factor"] = np.where(
        x["daily_factor"] > 0, np.log(x["daily_factor"]), np.nan
    )
    x["cum_log"] = (
        x.groupby("symbol", sort=False)["log_factor"]
        .transform(lambda s: s.fillna(0.0).cumsum())
    )
    x["adj_close"] = np.exp(x["cum_log"])
    x["adj_open"] = np.where(
        (x["open"] > 0) & (x["close"] > 0),
        x["adj_close"] * x["open"] / x["close"],
        np.nan,
    )

    g = x.groupby("symbol", sort=False)["adj_close"]
    shifted = g.shift(1)
    x["prior250_count"] = (
        shifted.groupby(x["symbol"], sort=False)
        .transform(lambda s: s.rolling(LOOKBACK_OBS, min_periods=1).count())
    )
    x["prior250_max"] = (
        shifted.groupby(x["symbol"], sort=False)
        .transform(
            lambda s: s.rolling(
                LOOKBACK_OBS, min_periods=LOOKBACK_OBS
            ).max()
        )
    )
    x["prior250_min"] = (
        shifted.groupby(x["symbol"], sort=False)
        .transform(
            lambda s: s.rolling(
                LOOKBACK_OBS, min_periods=LOOKBACK_OBS
            ).min()
        )
    )
    return x


def build_events(
    panel: pd.DataFrame,
    membership: pd.DataFrame,
    cutoff: str,
    calendar: list[str],
) -> tuple[pd.DataFrame, dict]:
    m = membership[
        (membership["trade_date"] >= SIGNAL_START)
        & (membership["trade_date"] <= cutoff)
    ].copy()
    sig = panel[
        (panel["trade_date"] >= SIGNAL_START)
        & (panel["trade_date"] <= cutoff)
        & (panel["prior250_count"] >= LOOKBACK_OBS)
        & panel["prior250_max"].notna()
        & panel["prior250_min"].notna()
    ].copy()

    sig["group"] = np.where(
        sig["adj_close"] > sig["prior250_max"],
        "POS",
        np.where(
            sig["adj_close"] < sig["prior250_min"],
            "NEG",
            "",
        ),
    )
    sig = sig[sig["group"] != ""].copy()
    sig = sig.merge(
        m,
        left_on=["trade_date", "symbol"],
        right_on=["trade_date", "symbol"],
        how="inner",
        validate="many_to_one",
    )
    sig = sig.rename(columns={"trade_date": "signal_date"})

    idx = {d:i for i,d in enumerate(calendar)}
    rows=[]
    for r in sig.to_dict("records"):
        d=r["signal_date"]
        i=idx.get(d)
        if i is None or i+120 >= len(calendar):
            continue
        z=dict(r)
        z["entry_date"]=calendar[i+1]
        z["exit_date_20"]=calendar[i+20]
        z["exit_date_60"]=calendar[i+60]
        z["exit_date_120"]=calendar[i+120]
        rows.append(z)
    ev=pd.DataFrame(rows)
    if ev.empty:
        raise RuntimeError("FAIL_CLOSED no CSI500 52W events")

    signal_status=load_status_for_keys(
        ev, "signal_date", "signal"
    )
    entry_status=load_status_for_keys(
        ev, "entry_date", "entry"
    )
    ev=ev.merge(
        signal_status,
        on=["symbol","signal_date"],
        how="left",
        validate="many_to_one",
    )
    ev=ev.merge(
        entry_status,
        on=["symbol","entry_date"],
        how="left",
        validate="many_to_one",
    )

    ev["signal_known_st"] = (
        ev["signal_st_known"].fillna(False)
        & ev["signal_st_status"].fillna("").astype(str).str.upper().isin(
            ["ST","STAR_ST"]
        )
    )
    ev["entry_known_st"] = (
        ev["entry_st_known"].fillna(False)
        & ev["entry_st_status"].fillna("").astype(str).str.upper().isin(
            ["ST","STAR_ST"]
        )
    )
    ev["exclude_reason"]=""
    ev.loc[
        ~ev["signal_status_found"].fillna(False),
        "exclude_reason"
    ]="NO_SIGNAL_STATUS"
    ev.loc[
        (ev["exclude_reason"]=="")
        & ev["signal_is_suspended"].fillna(False),
        "exclude_reason"
    ]="SIGNAL_SUSPENDED"
    ev.loc[
        (ev["exclude_reason"]=="")
        & ev["signal_new_listing_special"].fillna(False),
        "exclude_reason"
    ]="SIGNAL_NEW_LISTING_SPECIAL"
    ev.loc[
        (ev["exclude_reason"]=="") & ev["signal_known_st"],
        "exclude_reason"
    ]="SIGNAL_KNOWN_ST"
    ev.loc[
        (ev["exclude_reason"]=="")
        & ~ev["entry_status_found"].fillna(False),
        "exclude_reason"
    ]="NO_ENTRY_STATUS"
    ev.loc[
        (ev["exclude_reason"]=="")
        & ev["entry_is_suspended"].fillna(False),
        "exclude_reason"
    ]="ENTRY_SUSPENDED"
    ev.loc[
        (ev["exclude_reason"]=="")
        & ev["entry_new_listing_special"].fillna(False),
        "exclude_reason"
    ]="ENTRY_NEW_LISTING_SPECIAL"
    ev.loc[
        (ev["exclude_reason"]=="") & ev["entry_known_st"],
        "exclude_reason"
    ]="ENTRY_KNOWN_ST"
    ev.loc[
        (ev["exclude_reason"]=="")
        & ~ev["entry_can_buy_at_open"].fillna(False),
        "exclude_reason"
    ]="ENTRY_NOT_BUYABLE_AT_OPEN"

    eligible=ev[ev["exclude_reason"]==""].copy()
    audit={
        "raw_pit_events":int(len(ev)),
        "raw_pos":int((ev["group"]=="POS").sum()),
        "raw_neg":int((ev["group"]=="NEG").sum()),
        "excluded":(
            ev.loc[ev["exclude_reason"]!="","exclude_reason"]
            .value_counts().to_dict()
        ),
        "eligible_events":int(len(eligible)),
        "eligible_pos":int((eligible["group"]=="POS").sum()),
        "eligible_neg":int((eligible["group"]=="NEG").sum()),
        "signal_unknown_st":int(
            (~eligible["signal_st_known"].fillna(False)).sum()
        ),
        "entry_unknown_st":int(
            (~eligible["entry_st_known"].fillna(False)).sum()
        ),
    }
    return eligible,audit


def make_price_lookup(panel: pd.DataFrame) -> pd.DataFrame:
    return panel[
        ["symbol","trade_date","adj_open","adj_close"]
    ].copy()


def measure_events(
    events: pd.DataFrame,
    panel: pd.DataFrame,
) -> pd.DataFrame:
    px=make_price_lookup(panel)
    entry=px.rename(
        columns={
            "trade_date":"entry_date",
            "adj_open":"entry_adj_open",
            "adj_close":"entry_adj_close",
        }
    )
    rows=[]
    for h in HORIZONS:
        z=events.copy()
        z["horizon"]=h
        z["exit_date"]=z[f"exit_date_{h}"]
        rows.append(z)
    x=pd.concat(rows,ignore_index=True)

    x=x.merge(
        entry[["symbol","entry_date","entry_adj_open"]],
        on=["symbol","entry_date"],
        how="left",
        validate="many_to_one",
    )
    exit_px=px.rename(
        columns={
            "trade_date":"exit_date",
            "adj_close":"exit_adj_close",
        }
    )
    x=x.merge(
        exit_px[["symbol","exit_date","exit_adj_close"]],
        on=["symbol","exit_date"],
        how="left",
        validate="many_to_one",
    )
    x["measurement_status"]="OK"
    x.loc[x["entry_adj_open"].isna(),"measurement_status"]="NO_T1_BAR"
    x.loc[
        (x["measurement_status"]=="OK")
        & x["exit_adj_close"].isna(),
        "measurement_status"
    ]="NO_EXACT_EXIT_BAR"
    x.loc[
        (x["measurement_status"]=="OK")
        & (
            (x["entry_adj_open"]<=0)
            | (x["exit_adj_close"]<=0)
        ),
        "measurement_status"
    ]="INVALID_ADJ_PRICE"
    x["raw_return"]=np.nan
    ok=x["measurement_status"]=="OK"
    x.loc[ok,"raw_return"]=(
        x.loc[ok,"exit_adj_close"]
        / x.loc[ok,"entry_adj_open"]
        - 1.0
    )
    return x


def benchmark_windows(
    measured: pd.DataFrame,
    membership: pd.DataFrame,
    panel: pd.DataFrame,
) -> pd.DataFrame:
    # Exact-window CSI500 PIT median. Constituent set is frozen at signal date.
    windows=(
        measured[["signal_date","horizon","entry_date","exit_date"]]
        .drop_duplicates()
        .sort_values(["signal_date","horizon"])
    )
    members=membership.merge(
        windows,
        left_on="trade_date",
        right_on="signal_date",
        how="inner",
        validate="many_to_many",
    )
    px=make_price_lookup(panel)
    ep=px.rename(
        columns={
            "trade_date":"entry_date",
            "adj_open":"entry_adj_open",
        }
    )
    xp=px.rename(
        columns={
            "trade_date":"exit_date",
            "adj_close":"exit_adj_close",
        }
    )
    z=members.merge(
        ep[["symbol","entry_date","entry_adj_open"]],
        on=["symbol","entry_date"],
        how="left",
        validate="many_to_one",
    ).merge(
        xp[["symbol","exit_date","exit_adj_close"]],
        on=["symbol","exit_date"],
        how="left",
        validate="many_to_one",
    )
    z["window_return"]=np.where(
        (z["entry_adj_open"]>0)&(z["exit_adj_close"]>0),
        z["exit_adj_close"]/z["entry_adj_open"]-1.0,
        np.nan,
    )
    b=(
        z.groupby(
            ["signal_date","horizon","entry_date","exit_date"],
            as_index=False
        )
        .agg(
            market_median_return=("window_return","median"),
            market_n=("window_return","count"),
        )
    )
    if (b["market_n"]<400).any():
        bad=b[b["market_n"]<400].head(10).to_dict("records")
        raise RuntimeError(
            f"FAIL_CLOSED CSI500 benchmark n<400 sample={bad}"
        )
    return b


def add_adjustment(x: pd.DataFrame,b: pd.DataFrame) -> pd.DataFrame:
    y=x.merge(
        b,
        on=["signal_date","horizon","entry_date","exit_date"],
        how="left",
        validate="many_to_one",
    )
    y["adjusted_return"]=np.where(
        y["measurement_status"]=="OK",
        pd.to_numeric(y["raw_return"],errors="coerce")
        - pd.to_numeric(y["market_median_return"],errors="coerce"),
        np.nan,
    )
    return y


def stats(g: pd.DataFrame) -> dict:
    z=g[g["measurement_status"]=="OK"].copy()
    raw=pd.to_numeric(z["raw_return"],errors="coerce").dropna()
    adj=pd.to_numeric(z["adjusted_return"],errors="coerce").dropna()
    return {
        "n":int(len(adj)),
        "raw_mean":safe_float(raw.mean()),
        "raw_median":safe_float(raw.median()),
        "raw_trim5":trim_mean(raw,0.05),
        "raw_win_rate":safe_float((raw>0).mean()),
        "adjusted_mean":safe_float(adj.mean()),
        "adjusted_median":safe_float(adj.median()),
        "adjusted_trim1":trim_mean(adj,0.01),
        "adjusted_trim5":trim_mean(adj,0.05),
        "adjusted_win_rate":safe_float((adj>0).mean()),
    }


def group_summary(x:pd.DataFrame)->pd.DataFrame:
    rows=[]
    for (grp,h),g in x.groupby(["group","horizon"],sort=True):
        rows.append({"group":grp,"horizon":int(h),**stats(g)})
    return pd.DataFrame(rows)


def date_equal_summary(x:pd.DataFrame)->tuple[pd.DataFrame,pd.DataFrame]:
    ok=x[x["measurement_status"]=="OK"].copy()
    daily=(
        ok.groupby(["group","horizon","signal_date"],as_index=False)
        .agg(
            n=("adjusted_return","size"),
            adjusted_mean=("adjusted_return","mean"),
            raw_mean=("raw_return","mean"),
        )
    )
    rows=[]
    for (grp,h),g in daily.groupby(["group","horizon"],sort=True):
        rows.append(
            {
                "group":grp,
                "horizon":int(h),
                "signal_dates":int(len(g)),
                "date_equal_adjusted_mean":safe_float(g["adjusted_mean"].mean()),
                "date_equal_adjusted_median":safe_float(g["adjusted_mean"].median()),
                "positive_date_fraction":safe_float((g["adjusted_mean"]>0).mean()),
                "events_per_signal_date_mean":safe_float(g["n"].mean()),
                "events_per_signal_date_p95":safe_float(g["n"].quantile(0.95)),
                "events_per_signal_date_max":int(g["n"].max()),
            }
        )
    return pd.DataFrame(rows),daily


def yearly_summary(x:pd.DataFrame)->pd.DataFrame:
    z=x[x["measurement_status"]=="OK"].copy()
    z["year"]=z["signal_date"].str[:4].astype(int)
    rows=[]
    for (year,grp,h),g in z.groupby(
        ["year","group","horizon"],sort=True
    ):
        rows.append(
            {"year":int(year),"group":grp,"horizon":int(h),**stats(g)}
        )
    return pd.DataFrame(rows)


def spread_summary(
    gs:pd.DataFrame,de:pd.DataFrame
)->pd.DataFrame:
    rows=[]
    for h in HORIZONS:
        p=gs[(gs["group"]=="POS")&(gs["horizon"]==h)].iloc[0]
        n=gs[(gs["group"]=="NEG")&(gs["horizon"]==h)].iloc[0]
        pdq=de[(de["group"]=="POS")&(de["horizon"]==h)].iloc[0]
        ndq=de[(de["group"]=="NEG")&(de["horizon"]==h)].iloc[0]
        rows.append(
            {
                "horizon":h,
                "pos_n":int(p["n"]),
                "neg_n":int(n["n"]),
                "adjusted_mean_spread":float(
                    p["adjusted_mean"]-n["adjusted_mean"]
                ),
                "adjusted_trim5_spread":float(
                    p["adjusted_trim5"]-n["adjusted_trim5"]
                ),
                "date_equal_adjusted_spread":float(
                    pdq["date_equal_adjusted_mean"]
                    - ndq["date_equal_adjusted_mean"]
                ),
            }
        )
    return pd.DataFrame(rows)


def yearly_spreads(y:pd.DataFrame)->pd.DataFrame:
    rows=[]
    for (year,h),g in y.groupby(["year","horizon"],sort=True):
        p=g[g["group"]=="POS"]
        n=g[g["group"]=="NEG"]
        if len(p)!=1 or len(n)!=1:
            continue
        p,n=p.iloc[0],n.iloc[0]
        rows.append(
            {
                "year":int(year),
                "horizon":int(h),
                "pos_n":int(p["n"]),
                "neg_n":int(n["n"]),
                "pos_adjusted_mean":float(p["adjusted_mean"]),
                "neg_adjusted_mean":float(n["adjusted_mean"]),
                "adjusted_mean_spread":float(
                    p["adjusted_mean"]-n["adjusted_mean"]
                ),
                "valid_year":bool(
                    int(p["n"])>=MIN_YEAR_GROUP_N
                    and int(n["n"])>=MIN_YEAR_GROUP_N
                ),
            }
        )
    return pd.DataFrame(rows)


def clustering(events:pd.DataFrame)->tuple[dict,pd.DataFrame]:
    d=(
        events.groupby(["signal_date","group"],as_index=False)
        .size().rename(columns={"size":"events"})
    )
    top=events.groupby("signal_date").size().sort_values(ascending=False)
    total=len(events)
    return {
        "signal_dates":int(events["signal_date"].nunique()),
        "events_per_date_mean":safe_float(
            events.groupby("signal_date").size().mean()
        ),
        "events_per_date_p95":safe_float(
            events.groupby("signal_date").size().quantile(0.95)
        ),
        "events_per_date_max":int(top.iloc[0]) if len(top) else 0,
        "top_5_signal_dates_event_share":(
            float(top.head(5).sum()/total) if total else None
        ),
        "top_10_signal_dates":[
            {"signal_date":str(k),"events":int(v)}
            for k,v in top.head(10).items()
        ],
    },d


def decision(
    gs:pd.DataFrame,
    de:pd.DataFrame,
    sp:pd.DataFrame,
    y:pd.DataFrame,
    ys:pd.DataFrame,
)->dict:
    out={}
    for h in (60,120):
        p=gs[(gs["group"]=="POS")&(gs["horizon"]==h)].iloc[0]
        n=gs[(gs["group"]=="NEG")&(gs["horizon"]==h)].iloc[0]
        pdq=de[(de["group"]=="POS")&(de["horizon"]==h)].iloc[0]
        sr=sp[sp["horizon"]==h].iloc[0]
        py=y[
            (y["group"]=="POS")
            &(y["horizon"]==h)
            &(y["n"]>=MIN_YEAR_GROUP_N)
        ]
        ysp=ys[
            (ys["horizon"]==h)
            &(ys["valid_year"]==True)
        ]
        pos_years=int((py["adjusted_mean"]>0).sum())
        spread_years=int((ysp["adjusted_mean_spread"]>0).sum())
        gates={
            "pos_adjusted_mean_positive":bool(p["adjusted_mean"]>0),
            "pos_adjusted_trim5_positive":bool(p["adjusted_trim5"]>0),
            "pos_date_equal_adjusted_mean_positive":bool(
                pdq["date_equal_adjusted_mean"]>0
            ),
            "pos_neg_adjusted_mean_spread_positive":bool(
                sr["adjusted_mean_spread"]>0
            ),
            "pos_neg_date_equal_spread_positive":bool(
                sr["date_equal_adjusted_spread"]>0
            ),
            "min_group_n":int(min(p["n"],n["n"])),
            "min_group_n_pass":bool(min(p["n"],n["n"])>=MIN_GROUP_N),
            "pos_positive_years":pos_years,
            "valid_pos_years":int(len(py)),
            "pos_year_gate":bool(pos_years>=2),
            "spread_positive_years":spread_years,
            "valid_spread_years":int(len(ysp)),
            "spread_year_gate":bool(spread_years>=2),
        }
        gates["horizon_pass"]=all(
            [
                gates["pos_adjusted_mean_positive"],
                gates["pos_adjusted_trim5_positive"],
                gates["pos_date_equal_adjusted_mean_positive"],
                gates["pos_neg_adjusted_mean_spread_positive"],
                gates["pos_neg_date_equal_spread_positive"],
                gates["min_group_n_pass"],
                gates["pos_year_gate"],
                gates["spread_year_gate"],
            ]
        )
        out[f"h{h}"]=gates

    passes=sum(int(out[f"h{h}"]["horizon_pass"]) for h in (60,120))
    if passes==2:
        verdict="STRONG_52WH_CANDIDATE"
    elif passes==1:
        verdict="PROMISING_HORIZON_CANDIDATE"
    else:
        trim_nonpos=all(
            not out[f"h{h}"]["pos_adjusted_trim5_positive"]
            for h in (60,120)
        )
        date_spread_nonpos=all(
            not out[f"h{h}"]["pos_neg_date_equal_spread_positive"]
            for h in (60,120)
        )
        verdict=(
            "FAST_REJECT"
            if (trim_nonpos or date_spread_nonpos)
            else "INCONCLUSIVE"
        )
    return {
        "verdict":verdict,
        **out,
        "h20_role":"reaction diagnostic only",
        "rule":{
            "STRONG_52WH_CANDIDATE":"H60 and H120 pass all predeclared gates.",
            "PROMISING_HORIZON_CANDIDATE":"Exactly one of H60/H120 passes.",
            "FAST_REJECT":"Neither passes and both H60/H120 have non-positive POS trim5 OR both have non-positive date-equal spread.",
            "INCONCLUSIVE":"Everything else; inspect evidence before any added complexity.",
        },
    }


def check()->int:
    fps=validate_files()
    membership=load_membership()
    con=duckdb.connect(str(CATALOG),read_only=True)
    try:
        meta=describe_catalog(con)
        cal=load_calendar(con)
        cutoff=signal_cutoff(cal)
    finally:
        con.close()

    if membership["trade_date"].min()>SIGNAL_START:
        raise RuntimeError("FAIL_CLOSED CSI500 PIT starts after signal window")
    if membership["trade_date"].max()<cutoff:
        raise RuntimeError(
            f"FAIL_CLOSED CSI500 PIT ends before cutoff "
            f"membership_max={membership['trade_date'].max()} cutoff={cutoff}"
        )

    result={
        "status":"PASS_CSI500_52WH_V0_1_CHECK",
        "fingerprints":fps,
        "catalog_meta":meta,
        "membership_rows":int(len(membership)),
        "membership_dates":int(membership["trade_date"].nunique()),
        "membership_min":membership["trade_date"].min(),
        "membership_max":membership["trade_date"].max(),
        "signal_start":SIGNAL_START,
        "signal_cutoff_h120":cutoff,
        "latest_market_session":cal[-1],
        "lookback_observations":LOOKBACK_OBS,
        "horizons":list(HORIZONS),
        "no_network":True,
        "no_qmt":True,
        "no_database_mutation":True,
    }
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0


def run()->int:
    OUT_ROOT.mkdir(parents=True,exist_ok=True)
    fps=validate_files()
    membership=load_membership()

    con=duckdb.connect(str(CATALOG),read_only=True)
    try:
        meta=describe_catalog(con)
        cal=load_calendar(con)
        cutoff=signal_cutoff(cal)
        max_exit=cal[-1]

        signal_members=membership[
            (membership["trade_date"]>=SIGNAL_START)
            &(membership["trade_date"]<=cutoff)
        ]
        symbols=sorted(signal_members["symbol"].unique().tolist())
        panel=load_price_panel(con,symbols,max_exit)
    finally:
        con.close()

    events,event_audit=build_events(
        panel,membership,cutoff,cal
    )
    measured=measure_events(events,panel)
    bench=benchmark_windows(measured,membership,panel)
    x=add_adjustment(measured,bench)

    gs=group_summary(x)
    de,date_daily=date_equal_summary(x)
    y=yearly_summary(x)
    sp=spread_summary(gs,de)
    ys=yearly_spreads(y)
    cluster,cluster_daily=clustering(events)
    dec=decision(gs,de,sp,y,ys)

    outputs={
        "candidate_events":"candidate_events.csv",
        "event_returns":"event_returns.csv",
        "measurement_status":"measurement_status.csv",
        "group_summary":"group_summary.csv",
        "date_equal_summary":"date_equal_summary.csv",
        "signal_date_group_returns":"signal_date_group_returns.csv",
        "yearly_summary":"yearly_summary.csv",
        "spread_summary":"spread_summary.csv",
        "yearly_spread_summary":"yearly_spread_summary.csv",
        "signal_clustering":"signal_clustering.csv",
        "market_benchmarks":"market_benchmarks.csv",
        "audit_summary":"audit_summary.json",
    }

    events.to_csv(
        OUT_ROOT/outputs["candidate_events"],
        index=False,encoding="utf-8-sig"
    )
    x.to_csv(
        OUT_ROOT/outputs["event_returns"],
        index=False,encoding="utf-8-sig"
    )
    (
        x.groupby(["group","horizon","measurement_status"],as_index=False)
        .size().rename(columns={"size":"rows"})
        .to_csv(
            OUT_ROOT/outputs["measurement_status"],
            index=False,encoding="utf-8-sig"
        )
    )
    gs.to_csv(OUT_ROOT/outputs["group_summary"],index=False,encoding="utf-8-sig")
    de.to_csv(OUT_ROOT/outputs["date_equal_summary"],index=False,encoding="utf-8-sig")
    date_daily.to_csv(OUT_ROOT/outputs["signal_date_group_returns"],index=False,encoding="utf-8-sig")
    y.to_csv(OUT_ROOT/outputs["yearly_summary"],index=False,encoding="utf-8-sig")
    sp.to_csv(OUT_ROOT/outputs["spread_summary"],index=False,encoding="utf-8-sig")
    ys.to_csv(OUT_ROOT/outputs["yearly_spread_summary"],index=False,encoding="utf-8-sig")
    cluster_daily.to_csv(OUT_ROOT/outputs["signal_clustering"],index=False,encoding="utf-8-sig")
    bench.to_csv(OUT_ROOT/outputs["market_benchmarks"],index=False,encoding="utf-8-sig")

    result={
        "status":"PASS_CSI500_52WH_V0_1",
        "generated_at":datetime.now().astimezone().isoformat(timespec="seconds"),
        "research_id":"CSI500_ADJ_52WH_V0_1",
        "fingerprints":fps,
        "market_source":{
            "catalog":str(CATALOG),
            "view":"market.daily_bars_v1",
            "catalog_meta":meta,
        },
        "universe":{
            "id":"CSI500_PIT_ACCEPTED",
            "path":str(CSI500_MEMBERSHIP),
            "sha256":fps["membership_sha256"],
            "signal_date_membership":True,
        },
        "definition":{
            "price_state":"synthetic adjusted index using cumulative close/pre_close",
            "adj_open":"adj_close * open/close",
            "positive":"adj_close_T > max(previous 250 valid symbol observations)",
            "negative_control":"adj_close_T < min(previous 250 valid symbol observations)",
            "lookback_observations":LOOKBACK_OBS,
            "signal_timestamp":"T close",
            "entry":"T+1 exact OPEN and can_buy_at_open=true",
            "h20":"S20 exact CLOSE (diagnostic)",
            "h60":"S60 exact CLOSE",
            "h120":"S120 exact CLOSE",
            "same_h120_complete_cohort_for_all_horizons":True,
            "known_st_excluded":True,
            "known_new_listing_special_excluded":True,
            "unknown_st_retained":True,
            "repeated_same_symbol_events_not_deduped_phase1":True,
            "no_alpha_factors_added":True,
        },
        "window":{
            "signal_start":SIGNAL_START,
            "signal_cutoff_h120":cutoff,
            "latest_market_session":cal[-1],
        },
        "event_audit":event_audit,
        "measurement_status_counts":{
            str(k):int(v)
            for k,v in x["measurement_status"].value_counts().items()
        },
        "market_adjustment":{
            "method":"exact-window CSI500 PIT signal-date constituent median adjusted return",
            "benchmark_windows":int(len(bench)),
            "benchmark_n_min":int(bench["market_n"].min()),
            "benchmark_n_median":float(bench["market_n"].median()),
        },
        "clustering":cluster,
        "decision":dec,
        "family_note":{
            "mechanism":"52-week-high anchoring / underreaction",
            "same_broad_family_as_ret120":True,
            "portfolio_rule_if_pass":"Compare against ret120 and choose/merge only after incremental-value evidence; do not count both as independent mechanisms.",
        },
        "caveats":{
            "event_study_not_portfolio_backtest":True,
            "transaction_costs_included":False,
            "negative_group_short_assumed":False,
            "historical_strict_non_st_pit_applied":False,
            "unknown_st_retained":True,
            "phase1_repeated_signal_overlap":True,
        },
        "next":{
            "if_candidate":"Run standalone shared-equity 52WH portfolio audit with one-position-per-symbol dedup, 20bp/50bp costs; compare economically with ret120 before Deep incremental-value test.",
            "if_fast_reject":"Freeze/reject 52WH V0.1; do not rescue with volume/RS/industry filters.",
            "if_inconclusive":"Inspect trim/date/year evidence; no parameter scan.",
        },
        "outputs":outputs,
        "no_network":True,
        "no_qmt":True,
        "no_database_mutation":True,
    }
    write_json_atomic(OUT_ROOT/"audit_summary.json",result)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    print(f"OUTPUT: {OUT_ROOT}")
    return 0


def main()->int:
    p=argparse.ArgumentParser()
    p.add_argument("command",choices=["check","run"])
    args=p.parse_args()
    return check() if args.command=="check" else run()


if __name__=="__main__":
    raise SystemExit(main())
