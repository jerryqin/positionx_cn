import unittest
from research.risk_budget_sweep import risk_budget_verdict

class RiskBudgetSweepTests(unittest.TestCase):
    def test_pass(self):
        v = risk_budget_verdict(
            {"max_drawdown": -0.15, "total_return": 0.10},
            {"max_drawdown": -0.19, "total_return": 0.01}, 0.20)
        self.assertTrue(v["risk_budget_eligible"])

    def test_fail_stress_drawdown(self):
        v = risk_budget_verdict(
            {"max_drawdown": -0.15, "total_return": 0.10},
            {"max_drawdown": -0.21, "total_return": 0.02}, 0.20)
        self.assertFalse(v["risk_budget_eligible"])

    def test_fail_stress_return(self):
        v = risk_budget_verdict(
            {"max_drawdown": -0.15, "total_return": 0.10},
            {"max_drawdown": -0.18, "total_return": -0.01}, 0.20)
        self.assertFalse(v["risk_budget_eligible"])

if __name__ == "__main__":
    unittest.main()
