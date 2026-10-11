from __future__ import annotations
from dataclasses import dataclass, asdict
import hashlib, json

@dataclass(frozen=True)
class ExecutionPilotPolicyV02:
    policy_id: str = "EXECUTION_PILOT_POLICY_V0_2_600K"
    pilot_type: str = "EXECUTION_ONLY"
    economic_evidence_eligible: bool = False
    capital_envelope_rmb: float = 600_000.0
    max_order_value_rmb: float = 50_000.0
    planned_gross_cap_rmb: float = 150_000.0
    hard_gross_guard_rmb: float = 180_000.0
    max_concurrent_positions_expanded: int = 3
    max_new_entries_per_session_expanded: int = 2
    canary_required_clean_order_lifecycles: int = 5
    canary_planned_gross_cap_rmb: float = 100_000.0
    canary_max_concurrent_positions: int = 2
    canary_max_new_entries_per_session: int = 1
    max_actual_loss_budget_rmb: float = 50_000.0
    loss_warning_rmb: float = 20_000.0
    stop_new_entries_loss_rmb: float = 30_000.0
    emergency_exit_plan_loss_rmb: float = 40_000.0
    manual_approval_required: bool = True
    auto_execute: bool = False
    broker_holdings_source_of_truth: bool = True
    same_symbol_live_gross_cap_rmb: float = 50_000.0
    def fingerprint(self) -> str:
        raw=json.dumps(asdict(self),sort_keys=True,ensure_ascii=False).encode('utf-8')
        return hashlib.sha256(raw).hexdigest()
POLICY=ExecutionPilotPolicyV02()

def evaluate_loss_state(realized_pnl, unrealized_pnl, fees=0.0, policy=POLICY):
    total=float(realized_pnl)+float(unrealized_pnl)-float(fees)
    loss=max(0.0,-total)
    if loss >= policy.max_actual_loss_budget_rmb: state='BUDGET_BREACH'
    elif loss >= policy.emergency_exit_plan_loss_rmb: state='EMERGENCY_EXIT_PLAN_REQUIRED'
    elif loss >= policy.stop_new_entries_loss_rmb: state='STOP_NEW_ENTRIES'
    elif loss >= policy.loss_warning_rmb: state='WARNING'
    else: state='NORMAL'
    return {'state':state,'pilot_total_pnl_rmb':total,'loss_used_rmb':loss,
            'remaining_loss_budget_rmb':max(0.0,policy.max_actual_loss_budget_rmb-loss),
            'allow_new_entries':state in ('NORMAL','WARNING'),
            'cancel_pending_buys':state in ('STOP_NEW_ENTRIES','EMERGENCY_EXIT_PLAN_REQUIRED','BUDGET_BREACH'),
            'generate_emergency_exit_plan':state in ('EMERGENCY_EXIT_PLAN_REQUIRED','BUDGET_BREACH'),
            'auto_liquidate':False}

def stage_limits(stage,policy=POLICY):
    s=stage.upper()
    if s=='CANARY':
        return {'planned_gross_cap_rmb':policy.canary_planned_gross_cap_rmb,
                'max_concurrent_positions':policy.canary_max_concurrent_positions,
                'max_new_entries_per_session':policy.canary_max_new_entries_per_session}
    if s=='EXPANDED':
        return {'planned_gross_cap_rmb':policy.planned_gross_cap_rmb,
                'max_concurrent_positions':policy.max_concurrent_positions_expanded,
                'max_new_entries_per_session':policy.max_new_entries_per_session_expanded}
    raise ValueError(stage)

def promotion_status(clean_order_lifecycles, unresolved_reconcile_issues,
                     duplicate_intent_incidents, policy_mismatch_incidents, kill_switch_incidents, policy=POLICY):
    checks={'enough_clean_order_lifecycles':clean_order_lifecycles>=policy.canary_required_clean_order_lifecycles,
            'zero_unresolved_reconcile_issues':unresolved_reconcile_issues==0,
            'zero_duplicate_intent_incidents':duplicate_intent_incidents==0,
            'zero_policy_mismatch_incidents':policy_mismatch_incidents==0,
            'zero_kill_switch_incidents':kill_switch_incidents==0}
    return {'eligible_for_expanded':all(checks.values()),'checks':checks,
            'automatic_promotion':False,'manual_approval_required':True}

def evaluate_new_entry(order_value,current_planned_gross,current_marked_gross,symbol_live_gross,
                       concurrent_positions,new_entries_today,loss_state,stage='CANARY',policy=POLICY):
    limits=stage_limits(stage,policy); reasons=[]
    if not loss_state.get('allow_new_entries',False): reasons.append('LOSS_LADDER_BLOCK')
    if order_value<=0: reasons.append('NON_POSITIVE_ORDER')
    if order_value>policy.max_order_value_rmb: reasons.append('MAX_ORDER_VALUE_BREACH')
    if current_planned_gross+order_value>limits['planned_gross_cap_rmb']: reasons.append('PLANNED_GROSS_CAP_BREACH')
    if current_marked_gross+order_value>policy.hard_gross_guard_rmb: reasons.append('HARD_GROSS_GUARD_BREACH')
    if symbol_live_gross+order_value>policy.same_symbol_live_gross_cap_rmb: reasons.append('SAME_SYMBOL_LIVE_GROSS_CAP')
    if concurrent_positions>=limits['max_concurrent_positions']: reasons.append('MAX_CONCURRENT_POSITIONS')
    if new_entries_today>=limits['max_new_entries_per_session']: reasons.append('MAX_NEW_ENTRIES_PER_SESSION')
    return {'allow':not reasons,'action':'ALLOW_ORDER_PREVIEW' if not reasons else 'BLOCK_NEW_ENTRY',
            'stage':stage.upper(),'reasons':reasons,'limits':limits,'policy_id':policy.policy_id,'policy_hash':policy.fingerprint()}
