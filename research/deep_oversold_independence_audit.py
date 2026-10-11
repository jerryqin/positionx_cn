from __future__ import annotations

from typing import Any, Dict, Mapping

import pandas as pd


FOLDS = (
    ("F1", "20200803", "20221017"),
    ("F2", "20221018", "20240919"),
    ("F3", "20240920", "20260924"),
)


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").dropna()


def profit_factor(s: pd.Series) -> float | None:
    x = _num(s)
    gains = float(x[x > 0].sum())
    losses = float(-x[x < 0].sum())
    return gains / losses if losses > 0 else None


def trimmed_mean(s: pd.Series, p: float = 0.05) -> float | None:
    x = _num(s).sort_values()
    if len(x) == 0:
        return None
    k = int(len(x) * p)
    if 2 * k >= len(x):
        return float(x.mean())
    return float(x.iloc[k:len(x)-k].mean())


def stats(s: pd.Series) -> Dict[str, Any]:
    x = _num(s)
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
        "trimmed_mean_5pct": trimmed_mean(x),
    }


def load_fillable_events(path: str) -> pd.DataFrame:
    x = pd.read_csv(path, dtype={"symbol": str, "signal_date": str, "entry_date": str, "exit_date": str})
    required = {
        "symbol", "signal_date", "entry_date", "exit_date", "standard_fillable",
        "open_excess_gross", "open_net_20bps", "open_net_50bps",
        "close_excess_gross", "close_net_20bps", "close_net_50bps",
    }
    missing = required - set(x.columns)
    if missing:
        raise ValueError(f"event CSV missing columns: {sorted(missing)}")
    for c in ("signal_date", "entry_date", "exit_date"):
        x[c] = x[c].astype(str).str.replace("-", "", regex=False).str[:8]
    fill = x["standard_fillable"]
    if fill.dtype == object:
        mask = fill.astype(str).str.lower().isin({"true", "1", "yes"})
    else:
        mask = fill.astype(bool)
    return x.loc[mask].sort_values(["signal_date", "symbol"]).reset_index(drop=True)


def dedup_while_held(events: pd.DataFrame) -> pd.DataFrame:
    """
    Keep the first signal for a symbol while flat.
    A new signal is allowed on/after the prior position's exit date because it
    would enter on the following market session.
    """
    kept = []
    last_exit: Dict[str, str] = {}
    for row in events.sort_values(["signal_date", "symbol"]).to_dict("records"):
        symbol = str(row["symbol"])
        signal_date = str(row["signal_date"])
        prior_exit = last_exit.get(symbol)
        if prior_exit is not None and signal_date < prior_exit:
            continue
        kept.append(row)
        last_exit[symbol] = str(row["exit_date"])
    return pd.DataFrame(kept, columns=events.columns)


def equal_weight_by_signal_date(events: pd.DataFrame, value_col: str) -> pd.DataFrame:
    """
    Eva-compatible estimator shape:
      first average all triggered stocks on each signal date,
      then each signal date gets equal weight.
    """
    g = (
        events.groupby("signal_date", as_index=False)
        .agg(
            value=(value_col, "mean"),
            stocks=("symbol", "nunique"),
        )
        .sort_values("signal_date")
    )
    return g


def folds_for_values(df: pd.DataFrame, date_col: str, value_col: str) -> list[Dict[str, Any]]:
    out = []
    for name, start, end in FOLDS:
        g = df[(df[date_col] >= start) & (df[date_col] <= end)]
        out.append({
            "fold": name,
            "start": start,
            "end": end,
            "n": int(len(g)),
            "avg": float(g[value_col].mean()) if len(g) else None,
            "median": float(g[value_col].median()) if len(g) else None,
        })
    return out


def summarize_method(events: pd.DataFrame, prefix: str) -> Dict[str, Any]:
    excess = f"{prefix}_excess_gross"
    net20 = f"{prefix}_net_20bps"
    net50 = f"{prefix}_net_50bps"

    date_excess = equal_weight_by_signal_date(events, excess)
    date_net20 = equal_weight_by_signal_date(events, net20)
    date_net50 = equal_weight_by_signal_date(events, net50)

    return {
        "stock_event_weighted": {
            "excess": stats(events[excess]),
            "net20": stats(events[net20]),
            "net50": stats(events[net50]),
            "folds_excess": folds_for_values(events, "signal_date", excess),
        },
        "signal_date_equal_weighted": {
            "excess": stats(date_excess["value"]),
            "net20": stats(date_net20["value"]),
            "net50": stats(date_net50["value"]),
            "unique_signal_dates": int(events["signal_date"].nunique()),
            "folds_excess": folds_for_values(date_excess, "signal_date", "value"),
        },
    }


def concentration(events: pd.DataFrame) -> Dict[str, Any]:
    by_date = (
        events.groupby("signal_date")
        .agg(events=("symbol", "size"), symbols=("symbol", "nunique"), avg_excess=("open_excess_gross", "mean"))
        .sort_values("events", ascending=False)
    )
    by_symbol = (
        events.groupby("symbol")
        .agg(events=("signal_date", "size"), avg_excess=("open_excess_gross", "mean"))
        .sort_values("events", ascending=False)
    )
    n = len(events)
    top10_date_events = int(by_date.head(10)["events"].sum()) if len(by_date) else 0
    top10_symbol_events = int(by_symbol.head(10)["events"].sum()) if len(by_symbol) else 0

    return {
        "events": int(n),
        "unique_signal_dates": int(events["signal_date"].nunique()),
        "unique_symbols": int(events["symbol"].nunique()),
        "events_per_signal_date": {
            "median": float(by_date["events"].median()) if len(by_date) else None,
            "p90": float(by_date["events"].quantile(0.90)) if len(by_date) else None,
            "max": int(by_date["events"].max()) if len(by_date) else None,
        },
        "top10_signal_dates_event_share": top10_date_events / n if n else None,
        "top10_symbols_event_share": top10_symbol_events / n if n else None,
        "top_signal_dates": [
            {"signal_date": idx, **row.to_dict()}
            for idx, row in by_date.head(15).iterrows()
        ],
        "top_symbols": [
            {"symbol": idx, **row.to_dict()}
            for idx, row in by_symbol.head(15).iterrows()
        ],
    }


def audit(events: pd.DataFrame) -> Dict[str, Any]:
    dedup = dedup_while_held(events)

    return {
        "all_fillable_stock_days": {
            "count": int(len(events)),
            "open": summarize_method(events, "open"),
            "close": summarize_method(events, "close"),
            "concentration": concentration(events),
        },
        "dedup_symbol_while_held": {
            "count": int(len(dedup)),
            "removed": int(len(events) - len(dedup)),
            "kept_fraction": float(len(dedup) / len(events)) if len(events) else None,
            "open": summarize_method(dedup, "open"),
            "close": summarize_method(dedup, "close"),
            "concentration": concentration(dedup),
        },
    }
