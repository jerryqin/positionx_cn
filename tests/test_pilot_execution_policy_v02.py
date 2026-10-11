import unittest,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from research.pilot_execution_policy_v02 import POLICY,evaluate_loss_state,evaluate_new_entry,promotion_status
class T(unittest.TestCase):
    def test_capital(self):self.assertEqual(POLICY.capital_envelope_rmb,600000)
    def test_three_tickets(self):self.assertEqual(POLICY.planned_gross_cap_rmb,150000);self.assertEqual(POLICY.max_concurrent_positions_expanded,3)
    def test_hard_guard(self):self.assertEqual(POLICY.hard_gross_guard_rmb,180000)
    def test_ticket_unchanged(self):self.assertEqual(POLICY.max_order_value_rmb,50000)
    def test_loss_budget_unchanged(self):self.assertEqual(POLICY.max_actual_loss_budget_rmb,50000)
    def test_canary_one_new(self):
        n=evaluate_loss_state(0,0);self.assertFalse(evaluate_new_entry(50000,50000,50000,0,1,1,n,'CANARY')['allow'])
    def test_expanded_two_new(self):
        n=evaluate_loss_state(0,0);self.assertTrue(evaluate_new_entry(50000,50000,50000,0,1,1,n,'EXPANDED')['allow'])
    def test_expanded_three_positions(self):
        n=evaluate_loss_state(0,0);self.assertTrue(evaluate_new_entry(50000,100000,100000,0,2,0,n,'EXPANDED')['allow'])
    def test_fourth_blocked(self):
        n=evaluate_loss_state(0,0);self.assertFalse(evaluate_new_entry(50000,150000,150000,0,3,0,n,'EXPANDED')['allow'])
    def test_same_symbol_cap(self):
        n=evaluate_loss_state(0,0);self.assertFalse(evaluate_new_entry(10000,0,0,50000,0,0,n,'EXPANDED')['allow'])
    def test_promotion_manual(self):
        x=promotion_status(5,0,0,0,0);self.assertTrue(x['eligible_for_expanded']);self.assertFalse(x['automatic_promotion'])
    def test_promotion_incident_blocks(self):self.assertFalse(promotion_status(5,1,0,0,0)['eligible_for_expanded'])
    def test_no_auto(self):self.assertFalse(POLICY.auto_execute)
if __name__=='__main__':unittest.main()
