from __future__ import annotations
import numpy as np
import pandas as pd


def _metrics(df: pd.DataFrame, label_col: str, signal_col: str) -> dict:
    d = df[df[label_col].notna()].copy()
    if d.empty:
        return {"rows": 0}

    y = d[label_col].astype(int)
    s = d[signal_col].astype(bool)

    baseline = float(y.mean())
    selected = int(s.sum())
    positives = int(y.sum())
    tp = int(((s) & (y == 1)).sum())
    precision = float(tp / selected) if selected else None
    recall = float(tp / positives) if positives else None
    lift = float(precision / baseline) if precision is not None and baseline > 0 else None

    return {
        "rows": int(len(d)),
        "positives": positives,
        "baseline_rate": baseline,
        "selected": selected,
        "selected_rate": float(selected / len(d)),
        "true_positives": tp,
        "precision": precision,
        "recall": recall,
        "lift_vs_baseline": lift,
    }


def evaluate(panel: pd.DataFrame,
             label_prefix: str = "TREND25_60",
             signal_col: str = "trend_ignition_v01") -> dict:
    label_col = f"{label_prefix}_label"
    ttt_col = f"{label_prefix}_time_to_target"
    mfe_col = f"{label_prefix}_mfe"
    mae_col = f"{label_prefix}_mae_before_hit"

    overall = _metrics(panel, label_col, signal_col)

    selected = panel[(panel[label_col] == 1) & (panel[signal_col])].copy()
    overall["median_time_to_target"] = (
        float(selected[ttt_col].median()) if not selected.empty else None
    )
    overall["median_mfe"] = float(selected[mfe_col].median()) if not selected.empty else None
    overall["median_mae_before_hit"] = float(selected[mae_col].median()) if not selected.empty else None

    by_year = {}
    p = panel.copy()
    p["year"] = p["trade_date"].astype(str).str[:4]
    for year, g in p.groupby("year"):
        by_year[str(year)] = _metrics(g, label_col, signal_col)

    return {"overall": overall, "by_year": by_year}


def fast_reject_gate(result: dict) -> dict:
    """V0.1 predeclared mechanism gate.

    PASS_MECHANISM if:
      - at least 100 selected observations;
      - overall lift >= 1.50;
      - precision exceeds baseline by >= 3 percentage points;
      - at least 2 calendar years each have lift > 1.0 where defined.

    This does NOT qualify a tradable strategy. It only decides whether
    the mechanism deserves V0.2 research.
    """
    o = result["overall"]
    selected = int(o.get("selected") or 0)
    lift = o.get("lift_vs_baseline")
    precision = o.get("precision")
    baseline = o.get("baseline_rate")

    positive_years = 0
    for y in result.get("by_year", {}).values():
        l = y.get("lift_vs_baseline")
        if l is not None and l > 1.0:
            positive_years += 1

    checks = {
        "selected_ge_100": selected >= 100,
        "lift_ge_1_50": lift is not None and lift >= 1.50,
        "precision_minus_baseline_ge_3pct": (
            precision is not None and baseline is not None and precision - baseline >= 0.03
        ),
        "positive_lift_years_ge_2": positive_years >= 2,
    }
    return {
        "status": "PASS_TREND_IGNITION_MECHANISM_V0_1"
        if all(checks.values())
        else "FAST_REJECT_TREND_IGNITION_MECHANISM_V0_1",
        "checks": checks,
        "positive_lift_years": positive_years,
    }
