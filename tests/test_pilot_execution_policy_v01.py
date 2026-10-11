import unittest,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from research.pilot_execution_policy_v01 import POLICY,evaluate_loss_state,evaluate_order_envelope
class T(unittest.TestCase):
    def test_budget(self): self.assertEqual(POLICY.max_actual_loss_budget_rmb,50000)
    def test_envelope(self): self.assertEqual(POLICY.execution_capital_envelope_rmb,300000); self.assertEqual(POLICY.max_total_gross_rmb,90000)
    def test_warning(self): self.assertEqual(evaluate_loss_state(-20000,0)["state"],"WARNING")
    def test_stop(self): self.assertFalse(evaluate_loss_state(-30000,0)["allow_new_entries"])
    def test_emergency(self): self.assertTrue(evaluate_loss_state(-40000,0)["generate_emergency_exit_plan"])
    def test_breach(self): self.assertEqual(evaluate_loss_state(-50000,0)["state"],"BUDGET_BREACH")
    def test_order_cap(self): self.assertFalse(evaluate_order_envelope(50001,0,0,0,evaluate_loss_state(0,0))["allow"])
    def test_gross_cap(self): self.assertFalse(evaluate_order_envelope(50000,50000,0,0,evaluate_loss_state(0,0))["allow"])
    def test_position_cap(self): self.assertFalse(evaluate_order_envelope(10000,0,2,0,evaluate_loss_state(0,0))["allow"])
    def test_daily_cap(self): self.assertFalse(evaluate_order_envelope(10000,0,0,1,evaluate_loss_state(0,0))["allow"])
    def test_manual_no_auto(self): self.assertTrue(POLICY.manual_approval_required); self.assertFalse(POLICY.auto_execute)
if __name__=="__main__":unittest.main()
