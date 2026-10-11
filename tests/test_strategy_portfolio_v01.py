import unittest
import pandas as pd
from research.strategy_portfolio import (
    StrategyPortfolioPolicy, PilotReadinessPolicy,
    combine_initial_capital_sleeves, max_drawdown, overlap_stats, readiness_summary,
)

class PortfolioTests(unittest.TestCase):
    def curve(self, vals):
        return pd.DataFrame({
            "trade_date":["20261008","20261009","20261012"],
            "equity":vals,
            "cash":[800000,790000,780000],
            "gross_market_value":[200000,220000,240000],
            "gross_weight":[.2,.22,.24],
            "active_positions":[10,11,12],
        })

    def test_weights_sum(self):
        p=StrategyPortfolioPolicy(); self.assertAlmostEqual(p.deep_initial_weight+p.ret120_initial_weight,1.0)
    def test_no_rebalance(self): self.assertFalse(StrategyPortfolioPolicy().rebalance)
    def test_combo_starts_at_one_million(self):
        x=combine_initial_capital_sleeves(self.curve([1000000,1010000,1020000]),self.curve([1000000,990000,1010000]))
        self.assertAlmostEqual(float(x.iloc[0].portfolio_equity),1000000.0)
    def test_combo_math(self):
        x=combine_initial_capital_sleeves(self.curve([1000000,1100000,1100000]),self.curve([1000000,1000000,900000]))
        self.assertAlmostEqual(float(x.iloc[1].portfolio_equity),1050000.0)
        self.assertAlmostEqual(float(x.iloc[2].portfolio_equity),1000000.0)
    def test_mdd(self):
        self.assertAlmostEqual(max_drawdown(pd.Series([100,110,99])), -0.1)
    def test_overlap(self):
        x=overlap_stats({"A","B"},{"B","C"}); self.assertEqual(x["same_symbol_count"],1); self.assertAlmostEqual(x["symbol_jaccard"],1/3)
    def test_readiness_not_ready_initially(self):
        p=PilotReadinessPolicy(); x=readiness_summary(0,0,0,None,False,False,False,p); self.assertFalse(x["ready"])
    def test_readiness_ready_all_gates(self):
        p=PilotReadinessPolicy(); x=readiness_summary(20,20,20,.95,True,True,True,p); self.assertTrue(x["ready"])
    def test_burnin_frozen_20(self): self.assertEqual(PilotReadinessPolicy().common_forward_burn_in_sessions,20)
    def test_auto_execute_false(self): self.assertFalse(PilotReadinessPolicy().auto_execute)

if __name__ == "__main__": unittest.main()
