from __future__ import annotations

from dataclasses import dataclass
import numpy as np
import pandas as pd


@dataclass(frozen=True)
class TrendLabelSpec:
    name: str
    horizon: int
    target_return: float
    max_adverse_before_hit: float


PRIMARY = TrendLabelSpec(
    name="TREND25_60",
    horizon=60,
    target_return=0.25,
    max_adverse_before_hit=-0.10,
)

SECONDARY = TrendLabelSpec(
    name="TREND40_120",
    horizon=120,
    target_return=0.40,
    max_adverse_before_hit=-0.12,
)


def _label_one_symbol(g: pd.DataFrame, spec: TrendLabelSpec) -> pd.DataFrame:
    """Create forward trend-start labels using T+1 OPEN as the hypothetical entry.

    At signal date T close:
      entry = T+1 OPEN
      future window = T+1 ... T+horizon
      hit = first HIGH >= entry*(1+target_return)
      MAE-before-hit = min LOW/entry - 1 from T+1 through hit
      positive = target hit AND MAE-before-hit >= allowed adverse threshold

    No future values are used by the feature engine. These labels are research-only.
    """
    g = g.sort_values("trade_date").reset_index(drop=True).copy()
    n = len(g)
    out = {
        f"{spec.name}_entry_open": np.full(n, np.nan),
        f"{spec.name}_mfe": np.full(n, np.nan),
        f"{spec.name}_mae_before_hit": np.full(n, np.nan),
        f"{spec.name}_time_to_target": np.full(n, np.nan),
        f"{spec.name}_label": np.full(n, np.nan),
    }

    opens = g["open"].to_numpy(float)
    highs = g["high"].to_numpy(float)
    lows = g["low"].to_numpy(float)

    for i in range(n):
        start = i + 1
        end = min(n, i + 1 + spec.horizon)
        if start >= n or end - start < spec.horizon:
            continue

        entry = opens[start]
        if not np.isfinite(entry) or entry <= 0:
            continue

        wh = highs[start:end]
        wl = lows[start:end]
        if len(wh) != spec.horizon or not np.isfinite(wh).all() or not np.isfinite(wl).all():
            continue

        target = entry * (1.0 + spec.target_return)
        hit_idx = np.flatnonzero(wh >= target)
        mfe = float(np.max(wh) / entry - 1.0)

        if len(hit_idx) == 0:
            mae = float(np.min(wl) / entry - 1.0)
            ttt = np.nan
            label = 0.0
        else:
            j = int(hit_idx[0])
            mae = float(np.min(wl[: j + 1]) / entry - 1.0)
            ttt = float(j + 1)
            label = 1.0 if mae >= spec.max_adverse_before_hit else 0.0

        out[f"{spec.name}_entry_open"][i] = entry
        out[f"{spec.name}_mfe"][i] = mfe
        out[f"{spec.name}_mae_before_hit"][i] = mae
        out[f"{spec.name}_time_to_target"][i] = ttt
        out[f"{spec.name}_label"][i] = label

    for k, v in out.items():
        g[k] = v
    return g


def add_trend_labels(panel: pd.DataFrame,
                     specs: tuple[TrendLabelSpec, ...] = (PRIMARY, SECONDARY)) -> pd.DataFrame:
    required = {"symbol", "trade_date", "open", "high", "low", "close"}
    missing = required - set(panel.columns)
    if missing:
        raise ValueError(f"missing label columns: {sorted(missing)}")

    chunks = []
    for _, g in panel.groupby("symbol", sort=False):
        x = g.copy()
        for spec in specs:
            x = _label_one_symbol(x, spec)
        chunks.append(x)
    return pd.concat(chunks, ignore_index=True) if chunks else panel.copy()
