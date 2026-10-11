from __future__ import annotations
import json, os, sys
from pathlib import Path
from datetime import datetime
PROJECT_ROOT=Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:sys.path.insert(0,str(PROJECT_ROOT))
from research.pilot_execution_policy_v02 import POLICY,evaluate_loss_state,evaluate_new_entry,promotion_status
G6=Path(r"D:\positionx_data\research\pilot_live_readiness\risk_recovery_v0_1\risk_recovery_summary_v01.json")
OUT=Path(r"D:\positionx_data\research\pilot_live_readiness\pilot_policy_freeze_v0_2_600k")
def read_json(p): return json.loads(p.read_text(encoding='utf-8-sig')) if p.exists() else {}
def write_atomic(p,obj):
    p.parent.mkdir(parents=True,exist_ok=True);t=p.with_suffix(p.suffix+'.tmp');t.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(t,p)
def main():
    g6=read_json(G6)
    inv={'capital_600k':POLICY.capital_envelope_rmb==600000,'max_order_50k':POLICY.max_order_value_rmb==50000,
         'planned_gross_150k':POLICY.planned_gross_cap_rmb==150000,'hard_gross_180k':POLICY.hard_gross_guard_rmb==180000,
         'expanded_positions_3':POLICY.max_concurrent_positions_expanded==3,'expanded_new_entries_2':POLICY.max_new_entries_per_session_expanded==2,
         'canary_clean_orders_5':POLICY.canary_required_clean_order_lifecycles==5,'loss_budget_50k':POLICY.max_actual_loss_budget_rmb==50000,
         'manual':POLICY.manual_approval_required,'auto_false':not POLICY.auto_execute,'execution_only':not POLICY.economic_evidence_eligible}
    normal=evaluate_loss_state(0,0)
    scenarios={'canary_first_50k_allowed':evaluate_new_entry(50000,0,0,0,0,0,normal,'CANARY')['allow'],
               'canary_second_same_day_blocked':not evaluate_new_entry(50000,50000,50000,0,1,1,normal,'CANARY')['allow'],
               'expanded_two_same_day_allowed':evaluate_new_entry(50000,50000,50000,0,1,1,normal,'EXPANDED')['allow'],
               'expanded_third_allowed_later':evaluate_new_entry(50000,100000,100000,0,2,0,normal,'EXPANDED')['allow'],
               'expanded_fourth_blocked':not evaluate_new_entry(50000,150000,150000,0,3,0,normal,'EXPANDED')['allow'],
               'same_symbol_over_50k_blocked':not evaluate_new_entry(10000,0,0,50000,0,0,normal,'EXPANDED')['allow'],
               'promotion_before_5_blocked':not promotion_status(4,0,0,0,0)['eligible_for_expanded'],
               'promotion_after_5_clean_manual':promotion_status(5,0,0,0,0)['eligible_for_expanded'] and not promotion_status(5,0,0,0,0)['automatic_promotion']}
    checks={'g6_pass':g6.get('status')=='PASS_PILOT_RISK_RECOVERY_AND_KILL_SWITCH_V0_1','policy_invariants':all(inv.values()),'scenarios':all(scenarios.values())}
    passed=all(checks.values())
    result={'status':'PASS_G7_EXECUTION_PILOT_POLICY_V0_2_600K_FREEZE' if passed else 'BLOCKED_G7_EXECUTION_PILOT_POLICY_V0_2_600K_FREEZE',
            'generated_at':datetime.now().isoformat(timespec='seconds'),'policy':POLICY.__dict__,
            'derived':{'capital_envelope_rmb':600000,'expanded_planned_gross_rmb':150000,'hard_gross_guard_rmb':180000,
                       'capital_utilization_at_planned_gross':0.25,'hard_gross_fraction':0.30,'three_50k_representative_tickets_fit':True,'policy_hash':POLICY.fingerprint()},
            'rationale':{'why_600k_not_300k':'300k at 30% gross allows only 90k, so two 50k representative tickets cannot coexist. 600k permits up to three 50k tickets with headroom.',
                         'why_not_raise_ticket_size':'Keep the already-tested 50k blast-radius guard. Added capital increases live coverage/concurrency, not per-order risk.',
                         'why_execution_only':'Full-book economic replication remains separate; historical minimum tested dynamic capital is 12m.'},
            'stages':{'CANARY':'first 5 clean broker-facing order lifecycles; max 1 new entry/session; max 2 concurrent; planned gross <=100k',
                      'EXPANDED':'manual promotion only; max 2 new entries/session; max 3 concurrent; planned gross <=150k; hard gross guard 180k'},
            'loss_ladder_rmb':{'warning':20000,'stop_new_entries':30000,'emergency_exit_plan':40000,'budget_breach':50000},
            'policy_invariants':inv,'scenario_checks':scenarios,'checks':checks,
            'safety':{'manual_approval':True,'auto_execute':False,'no_qmt':True,'no_broker':True,'production_db_mutation':False}}
    write_atomic(OUT/'pilot_policy_freeze_summary_v02_600k.json',result);print(json.dumps(result,ensure_ascii=False,indent=2));print(f'OUTPUT: {OUT}');return 0 if passed else 3
if __name__=='__main__':raise SystemExit(main())
