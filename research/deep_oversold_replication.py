from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Sequence

import numpy as np
import pandas as pd


FOLDS = (
    ("F1", "20200803", "20221017"),
    ("F2", "20221018", "20240919"),
    ("F3", "20240920", "20260924"),
)


@dataclass(frozen=True)
class OversoldReplicationConfig:
    threshold_20d: float = -0.25
    horizon_from_signal: int = 20
    base_round_trip_cost: float = 0.002
    stress_round_trip_cost: float = 0.005
    trim_fraction: float = 0.05

    def validate(self) -> None:
        if not -1 < self.threshold_20d < 0:
            raise ValueError("threshold_20d must be negative")
        if self.horizon_from_signal < 2:
            raise ValueError("horizon_from_signal too small")


def norm_date(v: Any) -> str:
    s = "".join(ch for ch in str(v).replace("-", "").replace("/", "") if ch.isdigit())
    if len(s) < 8:
        raise ValueError(f"invalid date: {v!r}")
    return s[:8]


def normalize_history(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    x = df.copy()
    x.columns = [str(c).lower() for c in x.columns]
    idx = []
    for v in x.index:
        try:
            idx.append(pd.Timestamp(v).strftime("%Y%m%d"))
        except Exception:
            idx.append(norm_date(v))
    x.index = idx
    x = x[~x.index.duplicated(keep="last")].sort_index()
    for c in ("open", "high", "low", "close", "volume", "amount"):
        if c in x.columns:
            x[c] = pd.to_numeric(x[c], errors="coerce")
    return x


def expand_membership(intervals: pd.DataFrame, market_dates: Sequence[str]) -> pd.DataFrame:
    required = {"symbol", "valid_from", "valid_to_exclusive"}
    missing = required - set(intervals.columns)
    if missing:
        raise ValueError(f"missing interval columns: {sorted(missing)}")

    dates = np.asarray([norm_date(d) for d in market_dates], dtype=object)
    rows = []
    for r in intervals.itertuples(index=False):
        start = norm_date(r.valid_from)
        end = (
            norm_date(r.valid_to_exclusive)
            if pd.notna(r.valid_to_exclusive) and str(r.valid_to_exclusive).strip()
            else None
        )
        mask = dates >= start
        if end is not None:
            mask &= dates < end
        rows.extend((d, str(r.symbol)) for d in dates[mask])

    out = pd.DataFrame(rows, columns=["signal_date", "symbol"])
    if out.empty:
        raise ValueError("expanded membership is empty")
    if out.duplicated(["signal_date", "symbol"]).any():
        raise ValueError("duplicate PIT membership")
    return out


def net_return(entry: float, exit_: float, round_trip_cost: float) -> float:
    half = round_trip_cost / 2.0
    return (exit_ * (1.0 - half)) / (entry * (1.0 + half)) - 1.0


def generate_events(
    histories: Mapping[str, pd.DataFrame],
    benchmark_history: pd.DataFrame,
    membership: pd.DataFrame,
    market_dates: Sequence[str],
    cfg: OversoldReplicationConfig,
) -> pd.DataFrame:
    cfg.validate()
    dates = [norm_date(d) for d in market_dates]
    pos = {d: i for i, d in enumerate(dates)}

    mem = membership.copy()
    mem["signal_date"] = mem["signal_date"].map(norm_date)
    mem_by_symbol = {
        s: set(g["signal_date"].tolist())
        for s, g in mem.groupby("symbol")
    }

    bench = normalize_history(benchmark_history).reindex(dates)
    rows = []

    for symbol, raw in histories.items():
        x = normalize_history(raw).reindex(dates)
        if x.empty or "close" not in x or "open" not in x:
            continue
        ret20 = x["close"] / x["close"].shift(20) - 1.0
        member_dates = mem_by_symbol.get(str(symbol), set())
        signal_mask = ret20 <= cfg.threshold_20d

        for signal_date in x.index[signal_mask.fillna(False)]:
            if signal_date not in member_dates:
                continue
            i = pos[signal_date]
            entry_i = i + 1
            exit_i = i + cfg.horizon_from_signal
            if entry_i >= len(dates) or exit_i >= len(dates):
                continue

            entry_date = dates[entry_i]
            exit_date = dates[exit_i]

            values = {
                "entry_open": x.at[entry_date, "open"],
                "entry_close": x.at[entry_date, "close"],
                "exit_close": x.at[exit_date, "close"],
                "bench_entry_open": bench.at[entry_date, "open"],
                "bench_entry_close": bench.at[entry_date, "close"],
                "bench_exit_close": bench.at[exit_date, "close"],
            }
            if any(pd.isna(v) for v in values.values()):
                rows.append({
                    "symbol": str(symbol),
                    "signal_date": signal_date,
                    "entry_date": entry_date,
                    "exit_date": exit_date,
                    "signal_ret20": float(ret20.at[signal_date]),
                    "complete_outcome": False,
                })
                continue
            if any(float(v) <= 0 or not math.isfinite(float(v)) for v in values.values()):
                continue

            open_gross = float(values["exit_close"]) / float(values["entry_open"]) - 1.0
            close_gross = float(values["exit_close"]) / float(values["entry_close"]) - 1.0
            bench_open = float(values["bench_exit_close"]) / float(values["bench_entry_open"]) - 1.0
            bench_close = float(values["bench_exit_close"]) / float(values["bench_entry_close"]) - 1.0

            rows.append({
                "symbol": str(symbol),
                "signal_date": signal_date,
                "entry_date": entry_date,
                "exit_date": exit_date,
                "signal_ret20": float(ret20.at[signal_date]),
                "complete_outcome": True,
                **{k: float(v) for k, v in values.items()},
                "open_gross_return": open_gross,
                "close_gross_return": close_gross,
                "open_excess_gross": open_gross - bench_open,
                "close_excess_gross": close_gross - bench_close,
                "open_net_20bps": net_return(
                    float(values["entry_open"]), float(values["exit_close"]),
                    cfg.base_round_trip_cost,
                ),
                "open_net_50bps": net_return(
                    float(values["entry_open"]), float(values["exit_close"]),
                    cfg.stress_round_trip_cost,
                ),
                "close_net_20bps": net_return(
                    float(values["entry_close"]), float(values["exit_close"]),
                    cfg.base_round_trip_cost,
                ),
                "close_net_50bps": net_return(
                    float(values["entry_close"]), float(values["exit_close"]),
                    cfg.stress_round_trip_cost,
                ),
            })

    return pd.DataFrame(rows)


def _as_bool(series: pd.Series, missing_default: bool) -> pd.Series:
    def conv(v):
        if pd.isna(v):
            return missing_default
        if isinstance(v, str):
            return v.strip().lower() in {"1", "true", "t", "yes", "y"}
        return bool(v)
    return series.map(conv).astype(bool)


def apply_standard_fillability(events: pd.DataFrame, status_rows: pd.DataFrame) -> pd.DataFrame:
    if events.empty:
        return events.copy()

    e = events.copy()
    e["entry_date"] = e["entry_date"].map(norm_date)

    s = status_rows.copy()
    s["trade_date"] = s["trade_date"].map(norm_date)
    s["symbol"] = s["symbol"].astype(str)

    keep_cols = [
        "trade_date", "symbol", "is_suspended", "new_listing_special",
        "can_buy_at_open", "st_known",
    ]
    missing = set(keep_cols) - set(s.columns)
    if missing:
        raise ValueError(f"status columns missing: {sorted(missing)}")

    out = e.merge(
        s[keep_cols],
        left_on=["entry_date", "symbol"],
        right_on=["trade_date", "symbol"],
        how="left",
        validate="many_to_one",
    )
    out["status_found"] = out["trade_date"].notna()

    suspended = _as_bool(out["is_suspended"], True)
    new_special = _as_bool(out["new_listing_special"], True)
    can_buy = _as_bool(out["can_buy_at_open"], False)

    out["standard_fillable"] = (
        out["status_found"] & ~suspended & ~new_special & can_buy
    )
    return out.drop(columns=["trade_date"])


def profit_factor(values: pd.Series) -> float | None:
    x = pd.to_numeric(values, errors="coerce").dropna()
    gains = float(x[x > 0].sum())
    losses = float(-x[x < 0].sum())
    return gains / losses if losses > 0 else None


def trimmed_mean(values: pd.Series, trim: float) -> float | None:
    x = pd.to_numeric(values, errors="coerce").dropna().sort_values()
    if len(x) == 0:
        return None
    k = int(len(x) * trim)
    if 2 * k >= len(x):
        return float(x.mean())
    return float(x.iloc[k:len(x)-k].mean())


def summarize_series(values: pd.Series, trim: float) -> Dict[str, Any]:
    x = pd.to_numeric(values, errors="coerce").dropna()
    if len(x) == 0:
        return {
            "n": 0, "avg": None, "median": None, "win_rate": None,
            "profit_factor": None, "trimmed_mean_5pct": None,
        }
    return {
        "n": int(len(x)),
        "avg": float(x.mean()),
        "median": float(x.median()),
        "win_rate": float((x > 0).mean()),
        "profit_factor": profit_factor(x),
        "trimmed_mean_5pct": trimmed_mean(x, trim),
    }


def summarize_variant(events: pd.DataFrame, prefix: str, cfg: OversoldReplicationConfig) -> Dict[str, Any]:
    net20 = f"{prefix}_net_20bps"
    net50 = f"{prefix}_net_50bps"
    excess = f"{prefix}_excess_gross"

    summary = {
        "net_20bps": summarize_series(events[net20], cfg.trim_fraction),
        "net_50bps": summarize_series(events[net50], cfg.trim_fraction),
        "excess_gross": summarize_series(events[excess], cfg.trim_fraction),
        "folds": [],
        "yearly": [],
    }
    for name, start, end in FOLDS:
        g = events[(events["signal_date"] >= start) & (events["signal_date"] <= end)]
        summary["folds"].append({
            "fold": name, "start": start, "end": end, "events": int(len(g)),
            "avg_excess": float(g[excess].mean()) if len(g) else None,
            "avg_net_20bps": float(g[net20].mean()) if len(g) else None,
        })
    for year, g in events.assign(year=events["signal_date"].str[:4]).groupby("year"):
        summary["yearly"].append({
            "year": str(year), "events": int(len(g)),
            "avg_excess": float(g[excess].mean()),
            "avg_net_20bps": float(g[net20].mean()),
            "median_excess": float(g[excess].median()),
        })
    return summary


def replication_verdict(open_summary: Mapping[str, Any]) -> Dict[str, Any]:
    excess = open_summary["excess_gross"]
    folds = open_summary["folds"]
    positive_folds = sum(
        1 for x in folds if x["avg_excess"] is not None and x["avg_excess"] > 0
    )
    core = {
        "mean_excess_positive": excess.get("avg") is not None and excess["avg"] > 0,
        "trimmed_excess_positive": (
            excess.get("trimmed_mean_5pct") is not None
            and excess["trimmed_mean_5pct"] > 0
        ),
        "positive_folds": positive_folds,
        "folds_total": len(folds),
    }
    core["replicated_core"] = (
        core["mean_excess_positive"]
        and core["trimmed_excess_positive"]
        and positive_folds >= 2
    )
    return core
