from __future__ import annotations
from dataclasses import dataclass, asdict
import hashlib, json

@dataclass(frozen=True)
class ExecutionPilotPolicy:
    policy_id: str = "EXECUTION_PILOT_POLICY_V0_1"
    pilot_type: str = "EXECUTION_ONLY"
    economic_evidence_eligible: bool = False
    execution_capital_envelope_rmb: float = 300_000.0
    max_target_gross_fraction: float = 0.30
    max_order_value_rmb: float = 50_000.0
    max_concurrent_positions: int = 2
    max_new_entries_per_session: int = 1
    max_actual_loss_budget_rmb: float = 50_000.0
    loss_warning_rmb: float = 20_000.0
    stop_new_entries_loss_rmb: float = 30_000.0
    emergency_exit_plan_loss_rmb: float = 40_000.0
    manual_approval_required: bool = True
    auto_execute: bool = False
    broker_holdings_source_of_truth: bool = True

    @property
    def max_total_gross_rmb(self) -> float:
        return self.execution_capital_envelope_rmb * self.max_target_gross_fraction

    def fingerprint(self) -> str:
        raw=json.dumps(asdict(self),sort_keys=True,ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

POLICY=ExecutionPilotPolicy()

def evaluate_loss_state(realized_pnl, unrealized_pnl, fees=0.0, policy=POLICY):
    total=float(realized_pnl)+float(unrealized_pnl)-float(fees)
    loss=max(0.0,-total)
    if loss >= policy.max_actual_loss_budget_rmb:
        state="BUDGET_BREACH"
    elif loss >= policy.emergency_exit_plan_loss_rmb:
        state="EMERGENCY_EXIT_PLAN_REQUIRED"
    elif loss >= policy.stop_new_entries_loss_rmb:
        state="STOP_NEW_ENTRIES"
    elif loss >= policy.loss_warning_rmb:
        state="WARNING"
    else:
        state="NORMAL"
    return {
        "state":state,
        "pilot_total_pnl_rmb":total,
        "loss_used_rmb":loss,
        "remaining_loss_budget_rmb":max(0.0,policy.max_actual_loss_budget_rmb-loss),
        "allow_new_entries":state in ("NORMAL","WARNING"),
        "cancel_pending_buys":state in ("STOP_NEW_ENTRIES","EMERGENCY_EXIT_PLAN_REQUIRED","BUDGET_BREACH"),
        "generate_emergency_exit_plan":state in ("EMERGENCY_EXIT_PLAN_REQUIRED","BUDGET_BREACH"),
        "auto_liquidate":False,
    }

def evaluate_order_envelope(order_value, current_gross, concurrent_positions, new_entries_today, loss_state, policy=POLICY):
    reasons=[]
    if not loss_state.get("allow_new_entries",False): reasons.append("LOSS_LADDER_BLOCK")
    if order_value <= 0: reasons.append("NON_POSITIVE_ORDER")
    if order_value > policy.max_order_value_rmb: reasons.append("MAX_ORDER_VALUE_BREACH")
    if current_gross + order_value > policy.max_total_gross_rmb: reasons.append("PILOT_GROSS_ENVELOPE_BREACH")
    if concurrent_positions >= policy.max_concurrent_positions: reasons.append("MAX_CONCURRENT_POSITIONS")
    if new_entries_today >= policy.max_new_entries_per_session: reasons.append("MAX_NEW_ENTRIES_PER_SESSION")
    return {"allow":not reasons,"action":"ALLOW_ORDER_PREVIEW" if not reasons else "BLOCK_NEW_ENTRY","reasons":reasons,
            "policy_id":policy.policy_id,"policy_hash":policy.fingerprint()}
