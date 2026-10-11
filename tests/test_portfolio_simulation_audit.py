import unittest
import pandas as pd

from research.portfolio_simulation import BreakoutRetestPortfolioSimulator, PortfolioSimulationConfig
from research.portfolio_simulation_audit import candidate_net_return, reconcile_accounting, selection_audit


def hist(opens, closes=None):
    closes = closes or opens
    n = len(opens)
    idx = [f"D{i:02d}" for i in range(n)]
    return pd.DataFrame({
        "open": opens,
        "high": [max(o, c) for o, c in zip(opens, closes)],
        "low": [min(o, c) for o, c in zip(opens, closes)],
        "close": closes,
        "volume": [100.0] * n,
    }, index=idx)


def event(symbol, signal_pos):
    return {"symbol": symbol, "strategy": "BREAKOUT_RETEST", "signal_pos": signal_pos, "signal_date": f"D{signal_pos:02d}", "level": 10.0}


class PortfolioSimulationAuditTests(unittest.TestCase):
    def test_candidate_net_return_matches_fee_formula(self):
        c = {"entry_price": 10.0, "exit_price": 10.0}
        self.assertAlmostEqual(candidate_net_return(c, 0.002), -0.001998001998, places=9)

    def test_slot_product_reconciles_final_equity(self):
        h = {"A.SH": hist([10] * 7, [10, 10, 10, 11, 11, 11, 11])}
        sim = BreakoutRetestPortfolioSimulator(PortfolioSimulationConfig(initial_capital=1000, max_positions=1, holding_days=2, round_trip_cost=0.0))
        r = sim.simulate(h, [event("A.SH", 1)])
        a = reconcile_accounting(r)
        self.assertTrue(a["accounting_reconciles"])
        self.assertAlmostEqual(a["actual_final_equity"], a["expected_final_equity_from_slot_products"])

    def test_selection_audit_separates_executed_and_skipped(self):
        h = {
            "A.SH": hist([10] * 7, [10, 10, 10, 11, 11, 11, 11]),
            "B.SH": hist([10] * 7, [10, 10, 10, 9, 9, 9, 9]),
        }
        events = [event("A.SH", 1), event("B.SH", 1)]
        sim = BreakoutRetestPortfolioSimulator(PortfolioSimulationConfig(initial_capital=1000, max_positions=1, holding_days=2, round_trip_cost=0.0))
        r = sim.simulate(h, events)
        s = selection_audit(h, events, r)
        self.assertEqual(s["executed_counterfactual"]["n"], 1)
        self.assertEqual(s["skipped_counterfactual"]["n"], 1)
        self.assertGreater(s["executed_counterfactual"]["avg"], s["skipped_counterfactual"]["avg"])

    def test_annualized_turnover_is_reported(self):
        h = {"A.SH": hist([10] * 7)}
        sim = BreakoutRetestPortfolioSimulator(PortfolioSimulationConfig(initial_capital=1000, max_positions=1, holding_days=2, round_trip_cost=0.0))
        r = sim.simulate(h, [event("A.SH", 1)])
        a = reconcile_accounting(r)
        self.assertIn("annualized_turnover", a)


if __name__ == "__main__":
    unittest.main()
