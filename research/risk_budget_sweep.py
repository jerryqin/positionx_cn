from __future__ import annotations
from typing import Any, Dict, Mapping

DEFAULT_RISK_SCALES = (
    {"name": "0.50x", "target_weight": 0.010, "max_total_weight": 0.40},
    {"name": "0.75x", "target_weight": 0.015, "max_total_weight": 0.60},
    {"name": "1.00x", "target_weight": 0.020, "max_total_weight": 0.80},
    {"name": "1.25x_STRESS_ONLY", "target_weight": 0.025, "max_total_weight": 1.00},
)

def risk_budget_verdict(base: Mapping[str, Any], stressed: Mapping[str, Any],
                        drawdown_limit: float = 0.20) -> Dict[str, Any]:
    base_dd = abs(float(base["max_drawdown"]))
    stress_dd = abs(float(stressed["max_drawdown"]))
    return {
        "drawdown_limit": drawdown_limit,
        "base_mdd_pass": base_dd <= drawdown_limit,
        "cost_50bps_mdd_pass": stress_dd <= drawdown_limit,
        "cost_50bps_total_return_positive": float(stressed["total_return"]) > 0,
        "risk_budget_eligible": (
            base_dd <= drawdown_limit
            and stress_dd <= drawdown_limit
            and float(stressed["total_return"]) > 0
        ),
    }
