import unittest
from research.pilot_capital_floor import board_of, executable_buy_shares, evaluate_trade, CapitalFloorPolicy
class T(unittest.TestCase):
    def test_board_star(self): self.assertEqual(board_of('688001.SH'),'STAR')
    def test_main_100_lot(self): self.assertEqual(executable_buy_shares(10500,50,'SSE_MAIN'),200)
    def test_main_below_lot(self): self.assertEqual(executable_buy_shares(4999,50,'SSE_MAIN'),0)
    def test_star_min_200(self): self.assertEqual(executable_buy_shares(199*50,50,'STAR'),0)
    def test_star_one_share_increment(self): self.assertEqual(executable_buy_shares(205*50,50,'STAR'),205)
    def test_fidelity(self):
        x=evaluate_trade(50,1_000_000,.5,.01,'SSE_MAIN',.25); self.assertEqual(x['target_notional'],5000); self.assertEqual(x['shares'],100); self.assertTrue(x['fidelity_pass'])
    def test_small_capital_blocked(self):
        x=evaluate_trade(100,300_000,.5,.01,'SSE_MAIN',.25); self.assertTrue(x['blocked_one_lot'])
    def test_policy_threshold(self): self.assertEqual(CapitalFloorPolicy().fidelity_min,.90)
if __name__=='__main__': unittest.main()
