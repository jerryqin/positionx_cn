from __future__ import annotations

r"""
PositionX — RET120 Frozen-Signal Comparable Portfolio V0.2

Purpose
-------
P1 after RET120_FROZEN_SIGNAL_LINEAGE_V0_1 PASS.

This version corrects the key ambiguity in the earlier family V0.1 draft:
RET120 SIGNALS ARE NOT RECOMPUTED from the canonical price store.

Signal lineage is exact frozen v0.2.2:
    feature = frozen ret120
    bucket = exact daily Q5 recovered from frozen panel_v0221
    signal = T close

Economic measurement is normalized to the same price state and portfolio engine
used by 52WH H120:
    entry = T+1 exact OPEN
    exit = S120 exact CLOSE
    canonical corporate-action-safe synthetic adjusted price state
    1% target / position
    30% max target gross
    30 max active positions
    one position / symbol
    fractional research shares
    existing positions are not rebalanced
    same-day CLOSE exits are not reusable at that day's OPEN
    20bp main / 50bp stress
    SYMBOL_ASC baseline
    100 deterministic random same-day tie-break seeds

Comparison window
-----------------
Exactly the frozen 52WH H120 Phase-1 signal window. This makes the family
comparison time-comparable as well as capital-comparable.

Price semantics
---------------
P0 established that historical v0.2.2 persisted exact signal lineage but not
entry_open / exit_close levels. Therefore this P1 explicitly freezes:

    SIGNAL representation  = historical QMT back_ratio frozen lineage
    ECONOMIC representation = canonical 52WH synthetic adjusted price state

The canonical price state is used ONLY for entry/mark/exit economics. It never
changes Q5 membership.

Historical research convention
------------------------------
The same complete-outcome convention as 52WH is used: exact entry and exact
S120 exit prices are required; missing exact future exit rows are excluded.
This is a historical measurement convention, not a Forward suspension policy.

The script emits RET120 standalone metrics plus a same-family comparison with
frozen 52WH H120. It does not auto-optimize weights and does not approve Forward.
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

import audit_ret120_frozen_signal_lineage_v01 as frozen_lineage


ROOT = Path(__file__).resolve().parents[1]

CATALOG = Path(r"D:\positionx_data\catalog\market_data.duckdb")
CSI500_MEMBERSHIP = (
    ROOT / "data/research/universe_assets/csi500/csi500_membership_daily.csv"
)
STATUS_PARQUET = Path(
    r"D:\positionx_data\incoming\eva\20260925_foundation_assets"
    r"\data\research\px_assets\a_share_daily_trading_status.parquet"
)

RET120_LINEAGE_CANDIDATES = [
    ROOT / "data/research/edge_discovery/trend_persistence_scan_csi500_v022_latest.json",
    ROOT / "data/research/edge_discovery/trend_persistence_replication_v022_latest.json",
]

WH_PHASE1 = Path(r"D:\positionx_data\research\csi500_52wh_v0_1\audit_summary.json")
WH_PORTFOLIO = Path(r"D:\positionx_data\research\csi500_52wh_portfolio_v0_1\portfolio_summary.json")
WH_CURVE_20 = Path(r"D:\positionx_data\research\csi500_52wh_portfolio_v0_1\h120_equity_curve_20bps.csv")
WH_TRADES_20 = Path(r"D:\positionx_data\research\csi500_52wh_portfolio_v0_1\h120_trades_20bps.csv")
WH_RANDOM_20 = Path(r"D:\positionx_data\research\csi500_52wh_portfolio_v0_1\h120_random_tie_break_100.csv")
WH_EVENT_RETURNS = Path(r"D:\positionx_data\research\csi500_52wh_v0_1\event_returns.csv")

EXPECTED_MEMBERSHIP_SHA256 = "8ab943bcb14a95a9c06a24809f96eb2b61a3fa9244cffe2a861413cd203ad542"
EXPECTED_STATUS_SHA256 = "bfe788d755fabb03986635d975cab22f1311af3a82ec533eb244ab85560c5bf2"
EXPECTED_WH_PHASE1_SHA256 = "81905965522e3de13aa17bdfe3b76014ee974165ffdf3d8608713cf25cce74ab"
EXPECTED_WH_PORTFOLIO_SHA256 = "d336f644574fae322fb8e5df6f25b652b17e814e3c57c6284e29da1d89ecdae1"
EXPECTED_WH_CURVE_SHA256 = "5bb01c0d7e1c35e2ebe36298201c2508a2ef5dcd1c2cd5a7d011b726f50416ee"
EXPECTED_WH_RANDOM_SHA256 = "91e73c3648c085eac14a0a1d7a3d7a6db1706f84ca49abaadf6b717985463db1"

OUT_ROOT = Path(r"D:\positionx_data\research\ret120_vs_52wh_family_v0_2_frozen_signal")

INITIAL_EQUITY = 1_000_000.0
TARGET_WEIGHT = 0.01
MAX_GROSS = 0.30
MAX_POSITIONS = 30
MAIN_COST = 0.002
STRESS_COST = 0.005
RANDOM_SEEDS = 100
LOOKBACK_OBS = 120
EPS = 1e-12


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


def trim_mean(s: pd.Series, pct: float = 0.05) -> float | None:
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


def stable_key(seed: int, d: str, symbol: str) -> str:
    return hashlib.sha256(f"{seed}|{d}|{symbol}".encode("utf-8")).hexdigest()


def verify_exact(path: Path, expected: str, label: str) -> str:
    if not path.exists():
        raise RuntimeError(f"FAIL_CLOSED missing {label}: {path}")
    actual = sha256_file(path)
    if actual != expected:
        raise RuntimeError(
            f"FAIL_CLOSED {label} hash drift expected={expected} actual={actual}"
        )
    return actual


def recursive_ret120_candidate(obj: Any) -> bool:
    """
    Schema-agnostic lineage check. It intentionally does not infer numeric
    thresholds from the old JSON; it only confirms that the old accepted
    artifact still contains ret120 and a CANDIDATE verdict somewhere in the
    same object tree.
    """
    found_ret120 = False
    found_candidate = False

    def walk(x: Any) -> None:
        nonlocal found_ret120, found_candidate
        if isinstance(x, dict):
            for k, v in x.items():
                ks = str(k).lower()
                if "ret120" in ks:
                    found_ret120 = True
                if isinstance(v, str):
                    vs = v.lower()
                    if "ret120" in vs:
                        found_ret120 = True
                    if "candidate" in vs:
                        found_candidate = True
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, str):
            xs = x.lower()
            if "ret120" in xs:
                found_ret120 = True
            if "candidate" in xs:
                found_candidate = True

    walk(obj)
    return found_ret120 and found_candidate


def discover_ret120_lineage() -> dict:
    hits = []
    for p in RET120_LINEAGE_CANDIDATES:
        if not p.exists():
            continue
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            hits.append({
                "path": str(p),
                "sha256": sha256_file(p),
                "parse_error": f"{type(exc).__name__}: {exc}",
                "ret120_candidate_found": False,
            })
            continue
        ok = recursive_ret120_candidate(obj)
        hits.append({
            "path": str(p),
            "sha256": sha256_file(p),
            "ret120_candidate_found": bool(ok),
        })

    accepted = [x for x in hits if x.get("ret120_candidate_found")]
    if not accepted:
        raise RuntimeError(
            "FAIL_CLOSED could not prove historical ret120/CANDIDATE lineage; "
            f"checked={hits}"
        )
    # Prefer the CSI500-specific artifact if present.
    accepted.sort(key=lambda x: ("csi500" not in x["path"].lower(), x["path"]))
    return {"selected": accepted[0], "all_checked": hits}


def verify_52wh() -> dict:
    fp = {
        "phase1": verify_exact(WH_PHASE1, EXPECTED_WH_PHASE1_SHA256, "52WH Phase1"),
        "portfolio": verify_exact(WH_PORTFOLIO, EXPECTED_WH_PORTFOLIO_SHA256, "52WH portfolio"),
        "curve20": verify_exact(WH_CURVE_20, EXPECTED_WH_CURVE_SHA256, "52WH H120 curve"),
        "random20": verify_exact(WH_RANDOM_20, EXPECTED_WH_RANDOM_SHA256, "52WH H120 random seeds"),
    }
    for p, label in [
        (WH_TRADES_20, "52WH H120 trades"),
        (WH_EVENT_RETURNS, "52WH event returns"),
    ]:
        if not p.exists():
            raise RuntimeError(f"FAIL_CLOSED missing {label}: {p}")

    phase1 = json.loads(WH_PHASE1.read_text(encoding="utf-8"))
    portfolio = json.loads(WH_PORTFOLIO.read_text(encoding="utf-8"))
    if phase1.get("decision", {}).get("verdict") != "STRONG_52WH_CANDIDATE":
        raise RuntimeError("FAIL_CLOSED 52WH Phase1 verdict drift")
    h120 = portfolio.get("results", {}).get("h120", {})
    if h120.get("standalone_gate", {}).get("status") != "PASS_STANDALONE_PORTFOLIO_GATE":
        raise RuntimeError("FAIL_CLOSED 52WH H120 standalone gate drift")

    signal_start = str(phase1["window"]["signal_start"])
    signal_cutoff = str(phase1["window"]["signal_cutoff_h120"])
    latest = str(phase1["window"]["latest_market_session"])
    return {
        "fingerprints": fp,
        "phase1": phase1,
        "portfolio": portfolio,
        "signal_start": signal_start,
        "signal_cutoff": signal_cutoff,
        "latest_session": latest,
    }


def verify_inputs() -> dict:
    for p in (CATALOG, STATUS_PARQUET):
        if not p.exists():
            raise RuntimeError(f"FAIL_CLOSED missing input: {p}")

    status_sha = sha256_file(STATUS_PARQUET)
    if status_sha != EXPECTED_STATUS_SHA256:
        raise RuntimeError(
            f"FAIL_CLOSED status hash drift expected={EXPECTED_STATUS_SHA256} actual={status_sha}"
        )

    wh = verify_52wh()

    # P0 is replayed here, not merely trusted from chat output. This proves that
    # panel_v0221 -> exact daily Q5 -> frozen daily buckets / JSON still closes.
    p0 = frozen_lineage.build_audit(ROOT)
    if p0.get("status") != frozen_lineage.PASS_STATUS:
        raise RuntimeError(f"FAIL_CLOSED P0 lineage status={p0.get('status')}")

    return {
        "catalog_sha256": sha256_file(CATALOG),
        "status_sha256": status_sha,
        "52wh": wh,
        "ret120_lineage": p0,
    }



def normalize_frozen_bucket_labels(values: pd.Series) -> pd.Series:
    """Normalize exact frozen Q1..Q5 labels while preserving legitimate missing buckets.

    reconstruct_ret120() intentionally keeps full daily membership rows once the
    date-level feature coverage gate passes. Rows whose ret120 is unavailable
    therefore have bucket=NaN. Those rows are not signals and must remain missing;
    only an unexpected *non-null* label is a fail-closed condition.
    """
    mapping = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4, "Q5": 5}
    raw = values.astype("object")
    nonnull = raw[raw.notna()]
    bad = sorted({str(v) for v in nonnull if str(v) not in mapping})
    if bad:
        raise RuntimeError(
            "FAIL_CLOSED unexpected frozen bucket label "
            f"sample={bad[:10]}"
        )
    return raw.map(mapping).astype("Int64")

def load_frozen_signal_events(
    signal_start: str,
    signal_cutoff: str,
) -> tuple[pd.DataFrame, dict]:
    panel_path = ROOT / "data/research/edge_discovery/trend_persistence_scan_csi500_panel_v0221.parquet"
    panel = pd.read_parquet(
        panel_path,
        columns=[
            "signal_date", "symbol", "ret120",
            "excess_20", "excess_60", "excess_120",
            "label_quality_reject_20",
            "label_quality_reject_60",
            "label_quality_reject_120",
        ],
    )
    panel["signal_date"] = frozen_lineage.normalize_signal_date(panel["signal_date"])
    panel["symbol"] = panel["symbol"].astype(str)

    # Exact frozen ordering: daily quintiles are assigned on the full signal-date
    # panel BEFORE any horizon completeness filter is applied.
    membership, _ = frozen_lineage.reconstruct_ret120(panel)
    membership["bucket"] = normalize_frozen_bucket_labels(membership["bucket"])

    x = membership[
        (membership["signal_date"] >= signal_start)
        & (membership["signal_date"] <= signal_cutoff)
    ].copy()
    if x.empty:
        raise RuntimeError("FAIL_CLOSED no frozen ret120 events in common signal window")
    if x.duplicated(["signal_date", "symbol"]).any():
        raise RuntimeError("FAIL_CLOSED duplicate frozen signal keys")

    daily = x.groupby(["signal_date", "bucket"]).size().unstack(fill_value=0)
    q5 = x[x["bucket"] == 5]
    return x, {
        "source": str(panel_path),
        "signal_start": signal_start,
        "signal_cutoff": signal_cutoff,
        "rows": int(len(x)),
        "signal_dates": int(x["signal_date"].nunique()),
        "q5_rows_pre_hygiene": int(len(q5)),
        "q5_signal_dates_pre_hygiene": int(q5["signal_date"].nunique()),
        "per_date_bucket_size_min": int(daily.min().min()),
        "per_date_bucket_size_max": int(daily.max().max()),
        "signal_membership_recomputed_from_canonical_prices": False,
    }


def load_membership() -> pd.DataFrame:
    x = pd.read_csv(CSI500_MEMBERSHIP, dtype=str)
    cols = {c.lower(): c for c in x.columns}
    if "trade_date" not in cols or "symbol" not in cols:
        raise RuntimeError(f"FAIL_CLOSED membership columns={list(x.columns)}")
    x = x[[cols["trade_date"], cols["symbol"]]].copy()
    x.columns = ["trade_date", "symbol"]
    x["trade_date"] = (
        x["trade_date"].astype(str)
        .str.replace("-", "", regex=False)
        .str.replace(".0", "", regex=False)
        .str[:8]
    )
    x["symbol"] = x["symbol"].astype(str)
    if x.duplicated(["trade_date", "symbol"]).any():
        raise RuntimeError("FAIL_CLOSED duplicate CSI500 membership keys")
    counts = x.groupby("trade_date")["symbol"].nunique()
    bad = counts[counts != 500]
    if len(bad):
        raise RuntimeError(
            "FAIL_CLOSED CSI500 membership size anomaly "
            f"sample={bad.head(10).to_dict()}"
        )
    return x


def market_calendar(
    con: duckdb.DuckDBPyConnection, start: str, end: str
) -> list[str]:
    rows = con.execute(
        """
        SELECT DISTINCT strftime(CAST(trade_date AS DATE),'%Y%m%d') d
        FROM market.daily_bars_v1
        WHERE strftime(CAST(trade_date AS DATE),'%Y%m%d') BETWEEN ? AND ?
        ORDER BY d
        """,
        [start, end],
    ).fetchall()
    out = [str(r[0]) for r in rows if r and r[0]]
    if not out:
        raise RuntimeError("FAIL_CLOSED empty market calendar")
    return out


def load_panel(
    con: duckdb.DuckDBPyConnection,
    symbols: list[str],
    end: str,
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
                  BETWEEN '20220101' AND ?
            ORDER BY b.symbol, b.trade_date
            """,
            [end],
        ).df()
    finally:
        con.unregister("px_symbols")

    for c in ("open", "close", "pre_close"):
        x[c] = pd.to_numeric(x[c], errors="coerce")

    x["factor_valid"] = (
        np.isfinite(x["close"])
        & np.isfinite(x["pre_close"])
        & (x["close"] > 0)
        & (x["pre_close"] > 0)
    )
    x["daily_factor"] = np.where(
        x["factor_valid"], x["close"] / x["pre_close"], np.nan
    )
    x["log_factor"] = np.where(
        x["daily_factor"] > 0, np.log(x["daily_factor"]), np.nan
    )
    # Absolute level is irrelevant; continuity and ratios are what matter.
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

    g = x.groupby("symbol", sort=False)
    prior = g["adj_close"].shift(LOOKBACK_OBS)
    # Do not permit a bad factor anywhere inside the ret120 lookback window.
    invalid = (~x["factor_valid"]).astype(int)
    x["bad_factor_121"] = (
        invalid.groupby(x["symbol"], sort=False)
        .transform(
            lambda s: s.rolling(
                LOOKBACK_OBS + 1, min_periods=LOOKBACK_OBS + 1
            ).sum()
        )
    )
    x["ret120"] = np.where(
        (prior > 0) & (x["bad_factor_121"] == 0),
        x["adj_close"] / prior - 1.0,
        np.nan,
    )
    return x


def load_status_for_keys(keys: pd.DataFrame, date_col: str, prefix: str) -> pd.DataFrame:
    p = str(STATUS_PARQUET).replace("\\", "/").replace("'", "''")
    k = keys[["symbol", date_col]].drop_duplicates().copy()
    k = k.rename(columns={date_col: "key_date"})
    con = duckdb.connect(":memory:")
    con.register("keys", k)
    try:
        z = con.execute(
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
    return z.rename(columns={"key_date": date_col})


def assign_quintiles(
    membership: pd.DataFrame,
    panel: pd.DataFrame,
    signal_start: str,
    signal_cutoff: str,
) -> tuple[pd.DataFrame, dict]:
    m = membership[
        (membership["trade_date"] >= signal_start)
        & (membership["trade_date"] <= signal_cutoff)
    ].copy()

    s = panel[
        (panel["trade_date"] >= signal_start)
        & (panel["trade_date"] <= signal_cutoff)
    ][["symbol", "trade_date", "ret120"]].copy()

    x = m.merge(
        s,
        on=["symbol", "trade_date"],
        how="left",
        validate="one_to_one",
    )
    x = x.rename(columns={"trade_date": "signal_date"})
    valid_counts = x.groupby("signal_date")["ret120"].count()
    if (valid_counts < 450).any():
        bad = valid_counts[valid_counts < 450].head(10).to_dict()
        raise RuntimeError(
            f"FAIL_CLOSED too few valid ret120 members on some dates sample={bad}"
        )

    pieces = []
    for d, g in x.groupby("signal_date", sort=True):
        z = g[g["ret120"].notna()].copy()
        z = z.sort_values(["ret120", "symbol"], kind="mergesort")
        n = len(z)
        z["bucket"] = (
            np.floor(np.arange(n, dtype=float) * 5.0 / n).astype(int) + 1
        )
        z.loc[z["bucket"] > 5, "bucket"] = 5
        pieces.append(z)

    out = pd.concat(pieces, ignore_index=True)
    daily = (
        out.groupby(["signal_date", "bucket"])
        .size().unstack(fill_value=0)
    )
    return out, {
        "signal_dates": int(out["signal_date"].nunique()),
        "valid_members_per_date_min": int(valid_counts.min()),
        "valid_members_per_date_median": float(valid_counts.median()),
        "valid_members_per_date_max": int(valid_counts.max()),
        "bucket_counts_total": {
            str(int(k)): int(v)
            for k, v in out["bucket"].value_counts().sort_index().items()
        },
        "per_date_bucket_size_min": int(daily.min().min()),
        "per_date_bucket_size_max": int(daily.max().max()),
    }


def add_timing(
    buckets: pd.DataFrame,
    calendar: list[str],
) -> pd.DataFrame:
    idx = {d: i for i, d in enumerate(calendar)}
    rows = []
    for r in buckets.to_dict("records"):
        i = idx.get(str(r["signal_date"]))
        if i is None or i + 120 >= len(calendar):
            continue
        z = dict(r)
        z["entry_date"] = calendar[i + 1]
        z["exit_date"] = calendar[i + 120]
        rows.append(z)
    out = pd.DataFrame(rows)
    if out.empty:
        raise RuntimeError("FAIL_CLOSED no timed ret120 events")
    return out


def apply_hygiene(
    timed: pd.DataFrame,
    panel: pd.DataFrame,
) -> tuple[pd.DataFrame, dict]:
    sig = load_status_for_keys(timed, "signal_date", "signal")
    ent = load_status_for_keys(timed, "entry_date", "entry")
    x = timed.merge(
        sig, on=["symbol", "signal_date"], how="left", validate="many_to_one"
    ).merge(
        ent, on=["symbol", "entry_date"], how="left", validate="many_to_one"
    )

    x["signal_known_st"] = (
        x["signal_st_known"].fillna(False)
        & x["signal_st_status"].fillna("").astype(str).str.upper().isin(["ST", "STAR_ST"])
    )
    x["entry_known_st"] = (
        x["entry_st_known"].fillna(False)
        & x["entry_st_status"].fillna("").astype(str).str.upper().isin(["ST", "STAR_ST"])
    )

    x["exclude_reason"] = ""
    rules = [
        (~x["signal_status_found"].fillna(False), "NO_SIGNAL_STATUS"),
        (x["signal_is_suspended"].fillna(False), "SIGNAL_SUSPENDED"),
        (x["signal_new_listing_special"].fillna(False), "SIGNAL_NEW_LISTING_SPECIAL"),
        (x["signal_known_st"], "SIGNAL_KNOWN_ST"),
        (~x["entry_status_found"].fillna(False), "NO_ENTRY_STATUS"),
        (x["entry_is_suspended"].fillna(False), "ENTRY_SUSPENDED"),
        (x["entry_new_listing_special"].fillna(False), "ENTRY_NEW_LISTING_SPECIAL"),
        (x["entry_known_st"], "ENTRY_KNOWN_ST"),
        (~x["entry_can_buy_at_open"].fillna(False), "ENTRY_NOT_BUYABLE_AT_OPEN"),
    ]
    for mask, reason in rules:
        x.loc[(x["exclude_reason"] == "") & mask, "exclude_reason"] = reason

    px = panel[["symbol", "trade_date", "adj_open", "adj_close"]].copy()
    ep = px.rename(
        columns={"trade_date": "entry_date", "adj_open": "entry_adj_open"}
    )[["symbol", "entry_date", "entry_adj_open"]]
    xp = px.rename(
        columns={"trade_date": "exit_date", "adj_close": "exit_adj_close"}
    )[["symbol", "exit_date", "exit_adj_close"]]

    x = x.merge(
        ep, on=["symbol", "entry_date"], how="left", validate="many_to_one"
    ).merge(
        xp, on=["symbol", "exit_date"], how="left", validate="many_to_one"
    )

    x.loc[
        (x["exclude_reason"] == "") & x["entry_adj_open"].isna(),
        "exclude_reason",
    ] = "NO_EXACT_ENTRY_PRICE"
    x.loc[
        (x["exclude_reason"] == "") & x["exit_adj_close"].isna(),
        "exclude_reason",
    ] = "NO_EXACT_EXIT_PRICE"

    eligible = x[x["exclude_reason"] == ""].copy()
    eligible["forward_h120"] = (
        eligible["exit_adj_close"] / eligible["entry_adj_open"] - 1.0
    )

    audit = {
        "pre_hygiene_rows": int(len(x)),
        "excluded": {
            str(k): int(v)
            for k, v in x.loc[x["exclude_reason"] != "", "exclude_reason"]
            .value_counts().items()
        },
        "eligible_rows": int(len(eligible)),
        "eligible_q1": int((eligible["bucket"] == 1).sum()),
        "eligible_q5": int((eligible["bucket"] == 5).sum()),
        "signal_unknown_st": int(
            (~eligible["signal_st_known"].fillna(False)).sum()
        ),
        "entry_unknown_st": int(
            (~eligible["entry_st_known"].fillna(False)).sum()
        ),
    }
    return eligible, audit


def ret120_event_revalidation(eligible: pd.DataFrame) -> dict:
    q1 = eligible[eligible["bucket"] == 1].copy()
    q5 = eligible[eligible["bucket"] == 5].copy()

    def st(g: pd.DataFrame) -> dict:
        s = pd.to_numeric(g["forward_h120"], errors="coerce").dropna()
        daily = g.groupby("signal_date")["forward_h120"].mean()
        return {
            "n": int(len(s)),
            "mean": safe_float(s.mean()),
            "median": safe_float(s.median()),
            "trim5": trim_mean(s, 0.05),
            "win_rate": safe_float((s > 0).mean()),
            "signal_dates": int(len(daily)),
            "date_equal_mean": safe_float(daily.mean()),
        }

    a = st(q1)
    b = st(q5)
    checks = {
        "q5_mean_positive": bool(b["mean"] is not None and b["mean"] > 0),
        "q5_minus_q1_mean_positive": bool(
            b["mean"] is not None and a["mean"] is not None and b["mean"] > a["mean"]
        ),
        "q5_minus_q1_trim5_positive": bool(
            b["trim5"] is not None and a["trim5"] is not None and b["trim5"] > a["trim5"]
        ),
        "q5_minus_q1_date_equal_positive": bool(
            b["date_equal_mean"] is not None
            and a["date_equal_mean"] is not None
            and b["date_equal_mean"] > a["date_equal_mean"]
        ),
        "min_group_n_ge_500": bool(min(a["n"], b["n"]) >= 500),
    }
    passed = all(checks.values())
    return {
        "status": (
            "PASS_CANONICAL_RET120_Q5_REVALIDATION"
            if passed
            else "FAST_REJECT_CANONICAL_RET120_Q5"
        ),
        "q1": a,
        "q5": b,
        "spreads": {
            "mean": b["mean"] - a["mean"],
            "trim5": b["trim5"] - a["trim5"],
            "date_equal_mean": b["date_equal_mean"] - a["date_equal_mean"],
        },
        "checks": checks,
    }


def price_maps(panel: pd.DataFrame) -> tuple[dict, dict]:
    op, cl = {}, {}
    for r in panel[["symbol", "trade_date", "adj_open", "adj_close"]].itertuples(index=False):
        if pd.notna(r.adj_open) and float(r.adj_open) > 0:
            op[(str(r.symbol), str(r.trade_date))] = float(r.adj_open)
        if pd.notna(r.adj_close) and float(r.adj_close) > 0:
            cl[(str(r.symbol), str(r.trade_date))] = float(r.adj_close)
    return op, cl


def curve_metrics(curve: pd.DataFrame) -> dict:
    eq = pd.to_numeric(curve["equity"], errors="raise").astype(float)
    d = pd.to_datetime(curve["date"].astype(str), format="%Y%m%d")
    years = max((d.iloc[-1] - d.iloc[0]).days / 365.2425, 1 / 365.2425)
    total = float(eq.iloc[-1] / INITIAL_EQUITY - 1.0)
    cagr = float((eq.iloc[-1] / INITIAL_EQUITY) ** (1.0 / years) - 1.0)
    ret = eq.pct_change(fill_method=None)
    ret.iloc[0] = eq.iloc[0] / INITIAL_EQUITY - 1.0
    sd = float(ret.std(ddof=1))
    sharpe = float(ret.mean() / sd * math.sqrt(252)) if sd > 0 else None
    dd = eq / eq.cummax() - 1.0

    yy = pd.DataFrame({"date": curve["date"].astype(str), "ret": ret})
    yy["year"] = yy["date"].str[:4]
    yearly = {
        str(y): float((1.0 + g["ret"]).prod() - 1.0)
        for y, g in yy.groupby("year", sort=True)
    }

    avg_gross = float(curve["gross_weight"].mean())
    return {
        "sessions": int(len(curve)),
        "start": str(curve["date"].iloc[0]),
        "end": str(curve["date"].iloc[-1]),
        "total_return": total,
        "cagr": cagr,
        "max_drawdown": float(dd.min()),
        "sharpe": sharpe,
        "avg_gross": avg_gross,
        "max_gross": float(curve["gross_weight"].max()),
        "avg_active_positions": float(curve["active_positions"].mean()),
        "max_active_positions": int(curve["active_positions"].max()),
        "invested_day_fraction": float((curve["active_positions"] > 0).mean()),
        "capital_efficiency": cagr / avg_gross if avg_gross > 0 else None,
        "calendar_year_returns": yearly,
    }


def simulate(
    q5: pd.DataFrame,
    calendar: list[str],
    open_map: dict,
    close_map: dict,
    round_trip_cost: float,
    seed: int | None,
) -> dict:
    by_entry = {
        str(d): g.to_dict("records")
        for d, g in q5.groupby("entry_date", sort=True)
    }
    half = round_trip_cost / 2.0
    cash = INITIAL_EQUITY
    active: dict[str, dict] = {}
    last_close = {}
    trades = []
    skipped = []
    curve = []

    start = str(q5["entry_date"].min())
    end = str(q5["exit_date"].max())
    dates = [d for d in calendar if start <= d <= end]
    if not dates:
        raise RuntimeError("FAIL_CLOSED no simulation dates")

    scaled_days = 0
    no_slot_days = 0

    for d in dates:
        open_mv = 0.0
        for sym, pos in active.items():
            px = open_map.get((sym, d), last_close.get(sym))
            if px is None:
                raise RuntimeError(f"FAIL_CLOSED no OPEN mark {sym} {d}")
            pos["open_mark"] = px
            open_mv += pos["shares"] * px

        equity_open = cash + open_mv
        remaining_gross = max(0.0, MAX_GROSS * equity_open - open_mv)
        remaining_cash = max(0.0, cash / (1.0 + half))
        available = min(remaining_gross, remaining_cash)

        todays = list(by_entry.get(d, []))
        eligible = []
        for r in todays:
            sym = str(r["symbol"])
            if sym in active:
                skipped.append({
                    "symbol": sym,
                    "signal_date": str(r["signal_date"]),
                    "entry_date": d,
                    "reason": "DUPLICATE_WHILE_HELD",
                })
            else:
                eligible.append(r)

        if seed is None:
            eligible.sort(key=lambda r: (str(r["symbol"]), str(r["signal_date"])))
        else:
            eligible.sort(key=lambda r: stable_key(seed, d, str(r["symbol"])))

        slots = max(0, MAX_POSITIONS - len(active))
        selected = eligible[:slots]
        if eligible and slots == 0:
            no_slot_days += 1
        for r in eligible[slots:]:
            skipped.append({
                "symbol": str(r["symbol"]),
                "signal_date": str(r["signal_date"]),
                "entry_date": d,
                "reason": "NO_SLOT",
            })

        desired_each = TARGET_WEIGHT * equity_open
        desired_total = desired_each * len(selected)
        alloc_total = min(desired_total, available)
        scale = alloc_total / desired_total if desired_total > 0 else 0.0
        if selected and scale < 1.0 - 1e-12:
            scaled_days += 1

        if scale <= EPS:
            for r in selected:
                skipped.append({
                    "symbol": str(r["symbol"]),
                    "signal_date": str(r["signal_date"]),
                    "entry_date": d,
                    "reason": "NO_GROSS_OR_CASH_CAPACITY",
                })
        else:
            notional = desired_each * scale
            for r in selected:
                sym = str(r["symbol"])
                px = open_map.get((sym, d))
                if px is None or px <= 0:
                    raise RuntimeError(
                        f"FAIL_CLOSED missing exact entry despite precheck {sym} {d}"
                    )
                fee = notional * half
                if notional + fee > cash + 1e-8:
                    raise RuntimeError("FAIL_CLOSED cash invariant")
                shares = notional / px
                cash -= notional + fee
                active[sym] = {
                    "symbol": sym,
                    "signal_date": str(r["signal_date"]),
                    "entry_date": d,
                    "exit_date": str(r["exit_date"]),
                    "ret120": float(r["ret120"]),
                    "entry_price": px,
                    "entry_notional": notional,
                    "entry_fee": fee,
                    "shares": shares,
                }

        # mark close before exact exits
        for sym, pos in active.items():
            px = close_map.get((sym, d), last_close.get(sym))
            if px is None:
                raise RuntimeError(f"FAIL_CLOSED no CLOSE mark {sym} {d}")
            pos["close_mark"] = px
            last_close[sym] = px

        for sym in list(active.keys()):
            pos = active[sym]
            if pos["exit_date"] != d:
                continue
            px = close_map.get((sym, d))
            if px is None or px <= 0:
                raise RuntimeError(f"FAIL_CLOSED missing exact exit {sym} {d}")
            gross = pos["shares"] * px
            exit_fee = gross * half
            cash += gross - exit_fee
            pnl = gross - exit_fee - pos["entry_notional"] - pos["entry_fee"]
            trades.append({
                **pos,
                "exit_price": px,
                "exit_notional": gross,
                "exit_fee": exit_fee,
                "net_pnl": pnl,
                "net_return_on_entry_notional": pnl / pos["entry_notional"],
            })
            del active[sym]

        close_mv = sum(
            p["shares"] * p["close_mark"] for p in active.values()
        )
        equity = cash + close_mv
        curve.append({
            "date": d,
            "equity": equity,
            "cash": cash,
            "gross_market_value": close_mv,
            "gross_weight": close_mv / equity if equity > 0 else 0.0,
            "active_positions": len(active),
        })

    if active:
        raise RuntimeError(
            f"FAIL_CLOSED positions remain after final date count={len(active)}"
        )

    curve_df = pd.DataFrame(curve)
    trades_df = pd.DataFrame(trades)
    skipped_df = pd.DataFrame(skipped)
    metrics = curve_metrics(curve_df)

    skip_counts = (
        {str(k): int(v) for k, v in skipped_df["reason"].value_counts().items()}
        if len(skipped_df) else {}
    )

    return {
        "metrics": metrics,
        "curve": curve_df,
        "trades": trades_df,
        "skipped": skipped_df,
        "diagnostics": {
            "candidate_signals": int(len(q5)),
            "executed_trades": int(len(trades_df)),
            "coverage": float(len(trades_df) / len(q5)) if len(q5) else None,
            "scaled_entry_days": int(scaled_days),
            "no_slot_days": int(no_slot_days),
            "skip_counts": skip_counts,
            "trade_avg_net_return": (
                safe_float(trades_df["net_return_on_entry_notional"].mean())
                if len(trades_df) else None
            ),
            "trade_median_net_return": (
                safe_float(trades_df["net_return_on_entry_notional"].median())
                if len(trades_df) else None
            ),
            "trade_win_rate": (
                safe_float((trades_df["net_return_on_entry_notional"] > 0).mean())
                if len(trades_df) else None
            ),
        },
    }


def qtile(s: pd.Series) -> dict:
    x = pd.to_numeric(s, errors="coerce").dropna()
    return {
        "min": safe_float(x.min()),
        "p10": safe_float(x.quantile(0.10)),
        "p25": safe_float(x.quantile(0.25)),
        "median": safe_float(x.median()),
        "p75": safe_float(x.quantile(0.75)),
        "p90": safe_float(x.quantile(0.90)),
        "max": safe_float(x.max()),
        "mean": safe_float(x.mean()),
    }


def standalone_gate(main: dict, stress: dict, random_df: pd.DataFrame) -> dict:
    yearly = main["metrics"]["calendar_year_returns"]
    positive_years = sum(1 for v in yearly.values() if v > 0)
    checks = {
        "cagr20_positive": bool(main["metrics"]["cagr"] > 0),
        "cagr50_positive": bool(stress["metrics"]["cagr"] > 0),
        "sharpe20_ge_0_5": bool(
            main["metrics"]["sharpe"] is not None
            and main["metrics"]["sharpe"] >= 0.5
        ),
        "mdd20_within_20pct": bool(main["metrics"]["max_drawdown"] >= -0.20),
        "at_least_2_positive_calendar_years": bool(positive_years >= 2),
        "random_seed_median_cagr_positive": bool(random_df["cagr"].median() > 0),
        "random_seed_p10_cagr_positive": bool(
            random_df["cagr"].quantile(0.10) > 0
        ),
    }
    return {
        "status": (
            "PASS_STANDALONE_PORTFOLIO_GATE"
            if all(checks.values())
            else "FAIL_STANDALONE_PORTFOLIO_GATE"
        ),
        "checks": checks,
        "positive_calendar_years": positive_years,
    }


def curve_daily_returns(curve: pd.DataFrame) -> pd.DataFrame:
    z = curve[["date", "equity"]].copy()
    z["date"] = z["date"].astype(str)
    z["daily_return"] = pd.to_numeric(z["equity"], errors="raise").pct_change(
        fill_method=None
    )
    z.loc[z.index[0], "daily_return"] = (
        float(z.loc[z.index[0], "equity"]) / INITIAL_EQUITY - 1.0
    )
    return z


def metrics_from_returns(dates: pd.Series, ret: pd.Series) -> dict:
    r = pd.to_numeric(ret, errors="coerce").fillna(0.0).astype(float)
    eq = INITIAL_EQUITY * (1.0 + r).cumprod()
    d = pd.to_datetime(dates.astype(str), format="%Y%m%d")
    years = max((d.iloc[-1] - d.iloc[0]).days / 365.2425, 1 / 365.2425)
    cagr = float((eq.iloc[-1] / INITIAL_EQUITY) ** (1 / years) - 1)
    sd = float(r.std(ddof=1))
    sharpe = float(r.mean() / sd * math.sqrt(252)) if sd > 0 else None
    dd = eq / eq.cummax() - 1.0
    return {
        "sessions": int(len(r)),
        "start": str(dates.iloc[0]),
        "end": str(dates.iloc[-1]),
        "total_return": float(eq.iloc[-1] / INITIAL_EQUITY - 1),
        "cagr": cagr,
        "max_drawdown": float(dd.min()),
        "sharpe": sharpe,
    }


def load_52wh_random_summary(wh_portfolio: dict) -> dict:
    return wh_portfolio["results"]["h120"]["random_tie_break_20bps"]


def holding_overlap(
    a_trades: pd.DataFrame,
    b_trades: pd.DataFrame,
    dates: list[str],
) -> pd.DataFrame:
    def normalize(t: pd.DataFrame) -> pd.DataFrame:
        z = t.copy()
        for c in ("entry_date", "exit_date"):
            z[c] = (
                z[c].astype(str)
                .str.replace("-", "", regex=False)
                .str.replace(".0", "", regex=False)
                .str[:8]
            )
        z["symbol"] = z["symbol"].astype(str)
        return z

    a = normalize(a_trades)
    b = normalize(b_trades)
    rows = []
    for d in dates:
        # close-of-day holdings: positions exiting at today's close are gone.
        sa = set(
            a.loc[
                (a["entry_date"] <= d) & (a["exit_date"] > d),
                "symbol",
            ]
        )
        sb = set(
            b.loc[
                (b["entry_date"] <= d) & (b["exit_date"] > d),
                "symbol",
            ]
        )
        inter = sa & sb
        union = sa | sb
        rows.append({
            "date": d,
            "ret120_active": len(sa),
            "wh52_active": len(sb),
            "same_symbol_active": len(inter),
            "jaccard": len(inter) / len(union) if union else 0.0,
        })
    return pd.DataFrame(rows)


def family_comparison(
    ret_main: dict,
    ret_stress: dict,
    ret_random: pd.DataFrame,
    ret_gate: dict,
    ret_q5: pd.DataFrame,
    wh: dict,
) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    wh_port = wh["portfolio"]
    wh_main = wh_port["results"]["h120"]["main_20bps"]["metrics"]
    wh_stress = wh_port["results"]["h120"]["stress_50bps"]["metrics"]
    wh_gate = wh_port["results"]["h120"]["standalone_gate"]["status"]
    wh_random_summary = load_52wh_random_summary(wh_port)

    ret_curve = ret_main["curve"].copy()
    wh_curve = pd.read_csv(WH_CURVE_20, dtype={"date": str})
    ret_daily = curve_daily_returns(ret_curve).rename(
        columns={"equity": "ret120_equity", "daily_return": "ret120_return"}
    )
    wh_daily = curve_daily_returns(wh_curve).rename(
        columns={"equity": "wh52_equity", "daily_return": "wh52_return"}
    )
    common = ret_daily.merge(
        wh_daily, on="date", how="inner", validate="one_to_one"
    )
    if len(common) < 200:
        raise RuntimeError(
            f"FAIL_CLOSED too short common-period curve rows={len(common)}"
        )

    ret_common = metrics_from_returns(common["date"], common["ret120_return"])
    wh_common = metrics_from_returns(common["date"], common["wh52_return"])
    corr = float(
        common[["ret120_return", "wh52_return"]].corr().iloc[0, 1]
    )

    wh_trades = pd.read_csv(WH_TRADES_20, dtype={"symbol": str})
    overlap_daily = holding_overlap(
        ret_main["trades"], wh_trades, common["date"].astype(str).tolist()
    )

    wh_events = pd.read_csv(WH_EVENT_RETURNS, dtype={"symbol": str})
    wh_events["horizon"] = pd.to_numeric(wh_events["horizon"], errors="coerce")
    wh_sig = wh_events[
        (wh_events["group"] == "POS")
        & (wh_events["horizon"] == 120)
        & (wh_events["measurement_status"] == "OK")
    ][["symbol", "signal_date"]].copy()
    wh_sig["signal_date"] = (
        wh_sig["signal_date"].astype(str)
        .str.replace("-", "", regex=False)
        .str.replace(".0", "", regex=False)
        .str[:8]
    )
    ret_sig = ret_q5[["symbol", "signal_date"]].drop_duplicates().copy()
    A = set(map(tuple, ret_sig.astype(str).to_numpy()))
    B = set(map(tuple, wh_sig.astype(str).to_numpy()))
    exact_signal_intersection = len(A & B)
    exact_signal_union = len(A | B)

    rt = ret_main["trades"][["symbol", "signal_date"]].copy()
    wt = wh_trades[["symbol", "signal_date"]].copy()
    for z in (rt, wt):
        z["symbol"] = z["symbol"].astype(str)
        z["signal_date"] = (
            z["signal_date"].astype(str)
            .str.replace("-", "", regex=False)
            .str.replace(".0", "", regex=False)
            .str[:8]
        )
    RA = set(map(tuple, rt.astype(str).to_numpy()))
    WB = set(map(tuple, wt.astype(str).to_numpy()))

    ret_random_summary = {
        "cagr": qtile(ret_random["cagr"]),
        "max_drawdown": qtile(ret_random["max_drawdown"]),
        "sharpe": qtile(ret_random["sharpe"]),
        "avg_gross": qtile(ret_random["avg_gross"]),
        "capital_efficiency": qtile(ret_random["capital_efficiency"]),
        "trades": qtile(ret_random["trades"]),
        "coverage": qtile(ret_random["coverage"]),
    }

    result = {
        "status": "PASS_RET120_VS_52WH_FAMILY_COMPARISON_V0_2_FROZEN_SIGNAL",
        "family": "trend_underreaction",
        "common_policy": {
            "target_weight": TARGET_WEIGHT,
            "max_target_gross": MAX_GROSS,
            "max_positions": MAX_POSITIONS,
            "holding_sessions": 120,
            "main_round_trip_cost": MAIN_COST,
            "stress_round_trip_cost": STRESS_COST,
            "random_tie_break_seeds": RANDOM_SEEDS,
        },
        "standalone_status": {
            "ret120": ret_gate["status"],
            "52wh_h120": wh_gate,
        },
        "main_20bps_full_sample": {
            "ret120": ret_main["metrics"],
            "52wh_h120": wh_main,
            "delta_ret120_minus_52wh": {
                "cagr": ret_main["metrics"]["cagr"] - wh_main["cagr"],
                "max_drawdown": (
                    ret_main["metrics"]["max_drawdown"] - wh_main["max_drawdown"]
                ),
                "sharpe": (
                    ret_main["metrics"]["sharpe"] - wh_main["sharpe"]
                    if ret_main["metrics"]["sharpe"] is not None
                    and wh_main["sharpe"] is not None
                    else None
                ),
                "avg_gross": ret_main["metrics"]["avg_gross"] - wh_main["avg_gross"],
                "capital_efficiency": (
                    ret_main["metrics"]["capital_efficiency"]
                    - wh_main["capital_efficiency"]
                ),
            },
        },
        "stress_50bps_full_sample": {
            "ret120": ret_stress["metrics"],
            "52wh_h120": wh_stress,
            "delta_ret120_minus_52wh": {
                "cagr": ret_stress["metrics"]["cagr"] - wh_stress["cagr"],
                "max_drawdown": (
                    ret_stress["metrics"]["max_drawdown"]
                    - wh_stress["max_drawdown"]
                ),
                "sharpe": (
                    ret_stress["metrics"]["sharpe"] - wh_stress["sharpe"]
                    if ret_stress["metrics"]["sharpe"] is not None
                    and wh_stress["sharpe"] is not None
                    else None
                ),
            },
        },
        "random_tie_break_20bps": {
            "ret120": ret_random_summary,
            "52wh_h120": wh_random_summary,
            "delta_ret120_minus_52wh": {
                "cagr_p10": (
                    ret_random["cagr"].quantile(0.10)
                    - wh_random_summary["cagr"]["p10"]
                ),
                "cagr_median": (
                    ret_random["cagr"].median()
                    - wh_random_summary["cagr"]["median"]
                ),
                "sharpe_p10": (
                    ret_random["sharpe"].quantile(0.10)
                    - wh_random_summary["sharpe"]["p10"]
                ),
                "sharpe_median": (
                    ret_random["sharpe"].median()
                    - wh_random_summary["sharpe"]["median"]
                ),
            },
        },
        "common_period_20bps": {
            "start": str(common["date"].iloc[0]),
            "end": str(common["date"].iloc[-1]),
            "sessions": int(len(common)),
            "ret120": ret_common,
            "52wh_h120": wh_common,
            "daily_return_correlation": corr,
            "delta_ret120_minus_52wh": {
                "cagr": ret_common["cagr"] - wh_common["cagr"],
                "max_drawdown": (
                    ret_common["max_drawdown"] - wh_common["max_drawdown"]
                ),
                "sharpe": (
                    ret_common["sharpe"] - wh_common["sharpe"]
                    if ret_common["sharpe"] is not None
                    and wh_common["sharpe"] is not None
                    else None
                ),
            },
        },
        "selection_overlap": {
            "exact_signal_key_intersection": exact_signal_intersection,
            "exact_signal_key_jaccard": (
                exact_signal_intersection / exact_signal_union
                if exact_signal_union else 0.0
            ),
            "executed_signal_key_intersection": len(RA & WB),
            "executed_signal_key_jaccard": (
                len(RA & WB) / len(RA | WB) if (RA | WB) else 0.0
            ),
            "holding_overlap_common_period": {
                "days_with_same_symbol": int(
                    (overlap_daily["same_symbol_active"] > 0).sum()
                ),
                "fraction_days_with_same_symbol": float(
                    (overlap_daily["same_symbol_active"] > 0).mean()
                ),
                "mean_same_symbol_count": float(
                    overlap_daily["same_symbol_active"].mean()
                ),
                "max_same_symbol_count": int(
                    overlap_daily["same_symbol_active"].max()
                ),
                "mean_jaccard": float(overlap_daily["jaccard"].mean()),
            },
        },
        "decision_note": (
            "No automatic family winner is forced by V0.1. If ret120 fails its "
            "standalone gate while 52WH remains PASS, freeze ret120 as non-survivor. "
            "If both pass, inspect the emitted common-period deltas, random-seed "
            "robustness, capital efficiency, and overlap before freezing one family representative."
        ),
    }
    return result, common, overlap_daily


def check() -> int:
    fp = verify_inputs()
    wh = fp["52wh"]
    frozen_events, frozen_audit = load_frozen_signal_events(
        wh["signal_start"], wh["signal_cutoff"]
    )

    out = {
        "status": "PASS_RET120_FROZEN_SIGNAL_COMPARABLE_V0_2_1_CHECK",
        "signal_semantics": "EXACT frozen v0.2.2 ret120 / daily CSI500 PIT Q5",
        "signal_representation": "historical QMT back_ratio lineage",
        "economic_representation": (
            "canonical market.daily_bars_v1 synthetic adjusted state; "
            "identical economic representation family as 52WH"
        ),
        "signal_recomputed_from_canonical_prices": False,
        "ret120_lineage_status": fp["ret120_lineage"]["status"],
        "frozen_signal_audit": frozen_audit,
        "52wh_fingerprints": wh["fingerprints"],
        "signal_start": wh["signal_start"],
        "signal_cutoff": wh["signal_cutoff"],
        "latest_session": wh["latest_session"],
        "portfolio_policy": {
            "target_weight": TARGET_WEIGHT,
            "max_target_gross": MAX_GROSS,
            "max_positions": MAX_POSITIONS,
            "holding_sessions": 120,
            "main_cost": MAIN_COST,
            "stress_cost": STRESS_COST,
            "random_seeds": RANDOM_SEEDS,
        },
        "no_network": True,
        "no_qmt": True,
        "no_broker": True,
        "no_database_mutation": True,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def run() -> int:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    fp = verify_inputs()
    wh = fp["52wh"]

    sig_start = wh["signal_start"]
    sig_cutoff = wh["signal_cutoff"]
    latest = wh["latest_session"]

    frozen_events, frozen_audit = load_frozen_signal_events(sig_start, sig_cutoff)
    # Q1 is retained only as a descriptive economic-control diagnostic.
    # It is NOT a gate and it cannot alter Q5 membership.
    relevant = frozen_events[frozen_events["bucket"].isin([1, 5])].copy()
    symbols = sorted(relevant["symbol"].unique())

    con = duckdb.connect(str(CATALOG), read_only=True)
    try:
        panel = load_panel(con, symbols, latest)
        calendar = market_calendar(con, sig_start, latest)
    finally:
        con.close()

    timed = add_timing(relevant, calendar)
    eligible, hygiene_audit = apply_hygiene(timed, panel)

    q5 = eligible[eligible["bucket"] == 5].copy()
    q1 = eligible[eligible["bucket"] == 1].copy()
    if q5.empty:
        raise RuntimeError("FAIL_CLOSED no eligible exact-frozen Q5 events")

    # Descriptive only. Unlike the earlier V0.1 draft, this does not fast-reject
    # or redefine the already frozen signal if canonical economics differ.
    economic_control = ret120_event_revalidation(eligible)

    q5.to_csv(
        OUT_ROOT / "ret120_q5_candidate_events.csv",
        index=False, encoding="utf-8-sig"
    )
    q1.to_csv(
        OUT_ROOT / "ret120_q1_control_events.csv",
        index=False, encoding="utf-8-sig"
    )

    open_map, close_map = price_maps(panel)
    main = simulate(q5, calendar, open_map, close_map, MAIN_COST, seed=None)
    stress = simulate(q5, calendar, open_map, close_map, STRESS_COST, seed=None)

    random_rows = []
    for seed in range(RANDOM_SEEDS):
        z = simulate(q5, calendar, open_map, close_map, MAIN_COST, seed=seed)
        random_rows.append({
            "seed": seed,
            "cagr": z["metrics"]["cagr"],
            "max_drawdown": z["metrics"]["max_drawdown"],
            "sharpe": z["metrics"]["sharpe"],
            "avg_gross": z["metrics"]["avg_gross"],
            "capital_efficiency": z["metrics"]["capital_efficiency"],
            "trades": z["diagnostics"]["executed_trades"],
            "coverage": z["diagnostics"]["coverage"],
        })
    rdf = pd.DataFrame(random_rows)
    gate = standalone_gate(main, stress, rdf)

    main["curve"].to_csv(
        OUT_ROOT / "ret120_equity_curve_20bps.csv",
        index=False, encoding="utf-8-sig"
    )
    main["trades"].to_csv(
        OUT_ROOT / "ret120_trades_20bps.csv",
        index=False, encoding="utf-8-sig"
    )
    main["skipped"].to_csv(
        OUT_ROOT / "ret120_skipped_20bps.csv",
        index=False, encoding="utf-8-sig"
    )
    stress["curve"].to_csv(
        OUT_ROOT / "ret120_equity_curve_50bps.csv",
        index=False, encoding="utf-8-sig"
    )
    rdf.to_csv(
        OUT_ROOT / "ret120_random_tie_break_100.csv",
        index=False, encoding="utf-8-sig"
    )

    comparison, common, overlap = family_comparison(
        main, stress, rdf, gate, q5, wh
    )
    comparison["status"] = "PASS_RET120_VS_52WH_FAMILY_COMPARISON_V0_2_FROZEN_SIGNAL"
    comparison["signal_lineage"] = {
        "ret120": "exact frozen v0.2.2 Q5",
        "signal_recomputed_from_canonical_prices": False,
        "economic_price_representation_normalized_to_52wh": True,
    }
    common.to_csv(
        OUT_ROOT / "common_period_daily_20bps.csv",
        index=False, encoding="utf-8-sig"
    )
    overlap.to_csv(
        OUT_ROOT / "holding_overlap_daily.csv",
        index=False, encoding="utf-8-sig"
    )
    write_json_atomic(
        OUT_ROOT / "family_comparison_summary.json", comparison
    )

    summary = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "research_id": "RET120_FROZEN_SIGNAL_COMPARABLE_V0_2",
        "status": "PASS_RET120_FROZEN_SIGNAL_COMPARABLE_PORTFOLIO_AUDIT_V0_2",
        "portfolio_run": True,
        "fingerprints": {
            "catalog_sha256": fp["catalog_sha256"],
            "status_sha256": fp["status_sha256"],
            "52wh": wh["fingerprints"],
            "ret120_frozen_panel_sha256": fp["ret120_lineage"]["frozen_artifacts"]["panel_sha256"],
            "ret120_frozen_daily_buckets_sha256": fp["ret120_lineage"]["frozen_artifacts"]["daily_buckets_sha256"],
        },
        "definition": {
            "feature": "frozen ret120",
            "bucket": "exact historical v0.2.2 daily CSI500 PIT Q5",
            "signal_time": "T close",
            "entry": "T+1 exact OPEN",
            "exit": "S120 exact CLOSE",
            "signal_start": sig_start,
            "signal_cutoff": sig_cutoff,
            "latest_session": latest,
            "signal_price_representation": "QMT back_ratio frozen lineage",
            "economic_price_state": (
                "canonical synthetic adjusted state from cumulative close/pre_close; "
                "same economic representation as 52WH"
            ),
            "signal_recomputed_from_canonical_prices": False,
            "price_levels_frozen_in_v022": False,
        },
        "p0_lineage_status": fp["ret120_lineage"]["status"],
        "frozen_signal_audit": frozen_audit,
        "hygiene_audit": hygiene_audit,
        "economic_q1_q5_diagnostic_not_gate": economic_control,
        "main_20bps": {
            "metrics": main["metrics"],
            "diagnostics": main["diagnostics"],
        },
        "stress_50bps": {
            "metrics": stress["metrics"],
            "diagnostics": stress["diagnostics"],
        },
        "random_tie_break_20bps": {
            "seeds": RANDOM_SEEDS,
            "cagr": qtile(rdf["cagr"]),
            "max_drawdown": qtile(rdf["max_drawdown"]),
            "sharpe": qtile(rdf["sharpe"]),
            "avg_gross": qtile(rdf["avg_gross"]),
            "capital_efficiency": qtile(rdf["capital_efficiency"]),
            "trades": qtile(rdf["trades"]),
            "coverage": qtile(rdf["coverage"]),
        },
        "standalone_gate": gate,
        "family_comparison_file": str(
            OUT_ROOT / "family_comparison_summary.json"
        ),
        "caveats": {
            "exact_frozen_signal_lineage": True,
            "economic_price_state_normalized_to_52wh": True,
            "complete_outcome_research_cohort": True,
            "missing_exact_future_exit_rows_excluded": True,
            "historical_strict_non_st_pit_applied": False,
            "unknown_st_retained": True,
            "lot_size_not_modeled": True,
            "fractional_shares_research_only": True,
            "capacity_liquidity_not_audited": True,
            "forward_not_approved": True,
        },
        "no_network": True,
        "no_qmt": True,
        "no_broker": True,
        "no_database_mutation": True,
    }

    write_json_atomic(OUT_ROOT / "ret120_comparable_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print()
    print("FAMILY COMPARISON")
    print(json.dumps(comparison, ensure_ascii=False, indent=2))
    print(f"OUTPUT: {OUT_ROOT}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["check", "run"])
    args = p.parse_args()
    return check() if args.command == "check" else run()


if __name__ == "__main__":
    raise SystemExit(main())
