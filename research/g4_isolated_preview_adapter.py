from __future__ import annotations

PILOT_STRATEGY_NAME="PositionX:G4_EXECUTION_PILOT"

def incremental_target_weight(current_market_value:float, total_asset:float,
                              pilot_target_notional:float)->float:
    if total_asset <= 0:
        raise ValueError("total_asset must be > 0")
    if pilot_target_notional <= 0:
        raise ValueError("pilot_target_notional must be > 0")
    if current_market_value < 0:
        raise ValueError("current_market_value must be >= 0")
    return (float(current_market_value)+float(pilot_target_notional))/float(total_asset)

def preview_evidence(preview_row:dict, requested_pilot_notional:float,
                     confirmed_shadow_notional:float)->dict:
    status=str(preview_row.get("status") or "")
    actual=float(preview_row.get("notional") or 0.0)
    requested=float(requested_pilot_notional)
    shadow=float(confirmed_shadow_notional)
    checks={
      "requested_positive":requested>0,
      "requested_never_exceeds_shadow":requested<=shadow+1e-9,
      "requested_never_exceeds_50k":requested<=50000+1e-9,
      "preview_never_exceeds_requested":(
          actual<=requested+0.01 if status=="ORDER_PREVIEW" else True
      ),
      "no_submit_status":status in ("ORDER_PREVIEW","BLOCKED","NO_ACTION"),
    }
    return {
      "status":status,
      "requested_pilot_notional_rmb":requested,
      "confirmed_shadow_notional_rmb":shadow,
      "preview_notional_rmb":actual,
      "implementation_shortfall_vs_requested_rmb":requested-actual if status=="ORDER_PREVIEW" else None,
      "implementation_fidelity":actual/requested if status=="ORDER_PREVIEW" and requested>0 else None,
      "checks":checks,
      "pass":all(checks.values()),
    }
