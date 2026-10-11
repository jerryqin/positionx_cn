import unittest
from research.pilot_capital_floor_v02 import (
    CapitalFloorV02Policy, board_of, official_round_down_shares,
    nearest_executable_shares, evaluate_official, evaluate_nearest_sensitivity,
    guaranteed_required_total_capital, empirical_floor_from_required,
)
import pandas as pd


class T(unittest.TestCase):
    def test_policy_official_unchanged(self):
        p=CapitalFloorV02Policy(); self.assertEqual(p.official_sizing_policy,"ROUND_DOWN_NEVER_EXCEED_TARGET")
        self.assertAlmostEqual(p.portfolio_target_fraction,.005)
    def test_main_round_down(self): self.assertEqual(official_round_down_shares(9999,33,"SSE_MAIN"),300)
    def test_star_minimum(self):
        self.assertEqual(official_round_down_shares(1000,10,"STAR"),0)
        self.assertEqual(official_round_down_shares(2000,10,"STAR"),200)
    def test_nearest_main_can_over_target(self): self.assertEqual(nearest_executable_shares(5500,60,"SSE_MAIN"),100)
    def test_nearest_star(self): self.assertEqual(nearest_executable_shares(2050,10,"STAR"),205)
    def test_official_fidelity(self):
        x=evaluate_official(60,1_600_000,.5,.01,"SSE_MAIN",.25) # target 8000, one lot 6000 -> 25%
        self.assertTrue(x["fidelity_pass"])
    def test_nearest_sensitivity(self):
        x=evaluate_nearest_sensitivity(60,1_000_000,.5,.01,"SSE_MAIN",.25) # target 5000, one lot 6000 +20%
        self.assertTrue(x["fidelity_pass"]); self.assertTrue(x["over_target"])
    def test_guaranteed_main_floor(self):
        self.assertAlmostEqual(guaranteed_required_total_capital(50,"SSE_MAIN",.005,.25),4_000_000)
    def test_guaranteed_star_floor(self):
        self.assertAlmostEqual(guaranteed_required_total_capital(50,"STAR",.005,.25),2_000_000)
    def test_empirical_floor(self):
        s=pd.Series([1,2,3,4,5,6,7,8,9,10],dtype=float)
        self.assertEqual(empirical_floor_from_required(s,.9),9.0)
    def test_board(self): self.assertEqual(board_of("688001.SH"),"STAR")

if __name__=='__main__': unittest.main()
