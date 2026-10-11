from __future__ import annotations
import json, os, sys
from pathlib import Path
from datetime import datetime
PROJECT_ROOT=Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path: sys.path.insert(0,str(PROJECT_ROOT))
from research.pilot_execution_policy_v01 import POLICY,evaluate_loss_state,evaluate_order_envelope

G6=Path(r"D:\positionx_data\research\pilot_live_readiness\risk_recovery_v0_1\risk_recovery_summary_v01.json")
OUT=Path(r"D:\positionx_data\research\pilot_live_readiness\pilot_policy_freeze_v0_1")

def read_json(p):
    return json.loads(p.read_text(encoding="utf-8-sig")) if p.exists() else {}

def write_atomic(p,obj):
    p.parent.mkdir(parents=True,exist_ok=True); t=p.with_suffix(p.suffix+".tmp")
    t.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding="utf-8"); os.replace(t,p)

def main():
    g6=read_json(G6)
    inv={
      "execution_only": POLICY.pilot_type=="EXECUTION_ONLY" and not POLICY.economic_evidence_eligible,
      "capital_300k": POLICY.execution_capital_envelope_rmb==300000,
      "gross_90k": POLICY.max_total_gross_rmb==90000,
      "order_50k": POLICY.max_order_value_rmb==50000,
      "positions_2": POLICY.max_concurrent_positions==2,
      "new_entries_1": POLICY.max_new_entries_per_session==1,
      "loss_budget_50k": POLICY.max_actual_loss_budget_rmb==50000,
      "ladder_ordered": 0<POLICY.loss_warning_rmb<POLICY.stop_new_entries_loss_rmb<POLICY.emergency_exit_plan_loss_rmb<POLICY.max_actual_loss_budget_rmb,
      "manual": POLICY.manual_approval_required,
      "auto_false": not POLICY.auto_execute,
    }
    scenarios={
      "warning": evaluate_loss_state(-20000,0)["state"]=="WARNING",
      "stop": not evaluate_loss_state(-30000,0)["allow_new_entries"],
      "emergency": evaluate_loss_state(-40000,0)["generate_emergency_exit_plan"],
      "breach": evaluate_loss_state(-50000,0)["state"]=="BUDGET_BREACH",
      "order_ok": evaluate_order_envelope(40000,0,0,0,evaluate_loss_state(0,0))["allow"],
      "order_over_50k_blocked": not evaluate_order_envelope(50001,0,0,0,evaluate_loss_state(0,0))["allow"],
      "gross_over_90k_blocked": not evaluate_order_envelope(50000,50000,0,0,evaluate_loss_state(0,0))["allow"],
      "positions_blocked": not evaluate_order_envelope(10000,0,2,0,evaluate_loss_state(0,0))["allow"],
      "daily_entry_blocked": not evaluate_order_envelope(10000,0,0,1,evaluate_loss_state(0,0))["allow"],
    }
    checks={"g6_pass":g6.get("status")=="PASS_PILOT_RISK_RECOVERY_AND_KILL_SWITCH_V0_1",
            "policy_invariants":all(inv.values()),"scenarios":all(scenarios.values())}
    passed=all(checks.values())
    result={
      "status":"PASS_G7_EXECUTION_PILOT_POLICY_FREEZE_V0_1" if passed else "BLOCKED_G7_EXECUTION_PILOT_POLICY_FREEZE_V0_1",
      "generated_at":datetime.now().isoformat(timespec="seconds"),
      "policy":POLICY.__dict__,
      "derived":{"max_total_gross_rmb":POLICY.max_total_gross_rmb,"policy_hash":POLICY.fingerprint()},
      "loss_ladder_rmb":{"warning":20000,"stop_new_entries":30000,"emergency_exit_plan":40000,"budget_breach":50000},
      "loss_budget_note":"50k is a user-owned budget, not a guaranteed stop; gaps/slippage/suspension can overshoot.",
      "evidence_boundary":"Execution-chain evidence only; never use this Pilot PnL as Deep/RET120 economic evidence.",
      "policy_invariants":inv,"scenario_checks":scenarios,"checks":checks,
      "no_qmt":True,"no_broker":True,"production_db_mutation":False
    }
    write_atomic(OUT/"pilot_policy_freeze_summary_v01.json",result)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    print(f"OUTPUT: {OUT}")
    return 0 if passed else 3

if __name__=="__main__": raise SystemExit(main())
