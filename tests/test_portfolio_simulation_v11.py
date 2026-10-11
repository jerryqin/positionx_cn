import math
import unittest
import pandas as pd

from research.portfolio_simulation import (
    BreakoutRetestPortfolioSimulator,
    PortfolioSimulationConfig,
    prepare_fixed_horizon_candidates,
)


def hist(opens, closes=None, highs=None, lows=None, volumes=None, amounts=None):
    closes = closes or opens
    n = len(opens)
    highs = highs or [max(o, c) for o, c in zip(opens, closes)]
    lows = lows or [min(o, c) for o, c in zip(opens, closes)]
    volumes = volumes or [100.0] * n
    data = {
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    }
    if amounts is not None:
        data["amount"] = amounts
    return pd.DataFrame(data, index=[f"202601{i+1:02d}" for i in range(n)])


def event(symbol, pos=1):
    return {"symbol": symbol, "strategy": "BREAKOUT_RETEST", "signal_pos": pos,
            "signal_date": f"202601{pos+1:02d}", "level": 10.0}


class PortfolioSimulationV11Tests(unittest.TestCase):
    def test_skip_details_are_persisted_by_reason(self):
        h = {"A.SH": hist([10]*7), "B.SH": hist([10]*7)}
        sim = BreakoutRetestPortfolioSimulator(
            PortfolioSimulationConfig(initial_capital=1000, max_positions=1, holding_days=3, round_trip_cost=0.0)
        )
        r = sim.simulate(h, [event("B.SH"), event("A.SH")])
        self.assertEqual(r["summary"]["skipped_no_slot"], 1)
        self.assertEqual(len(r["skipped"]), 1)
        self.assertEqual(r["skipped"][0]["reason"], "no_slot")
        self.assertIn("counterfactual_net_return", r["skipped"][0])
        self.assertIn("allocation_rank", r["skipped"][0])

    def test_first_day_return_is_included_in_sharpe_series(self):
        h = {"A.SH": hist([10, 10, 10, 10, 10], [10, 10, 11, 11, 11])}
        sim = BreakoutRetestPortfolioSimulator(
            PortfolioSimulationConfig(initial_capital=1000, max_positions=1, holding_days=2, round_trip_cost=0.0)
        )
        r = sim.simulate(h, [event("A.SH", 0)])
        self.assertIn("sharpe_actual_sessions_including_first_day", r["summary"])
        self.assertEqual(r["summary"]["sharpe"], r["summary"]["sharpe_actual_sessions_including_first_day"])

    def test_turnover_is_explicitly_labeled(self):
        h = {"A.SH": hist([10]*7)}
        sim = BreakoutRetestPortfolioSimulator(
            PortfolioSimulationConfig(initial_capital=1000, max_positions=1, holding_days=2, round_trip_cost=0.0)
        )
        r = sim.simulate(h, [event("A.SH")])
        s = r["summary"]
        self.assertEqual(s["turnover"], s["turnover_cumulative_double_sided"])
        self.assertIn("turnover_annualized_double_sided", s)
        self.assertIn("turnover_annualized_single_sided", s)

    def test_trade_reports_capital_weight_and_geometric_average(self):
        h = {"A.SH": hist([10]*7, [10,10,10,11,11,11,11])}
        sim = BreakoutRetestPortfolioSimulator(
            PortfolioSimulationConfig(initial_capital=1000, max_positions=1, holding_days=2, round_trip_cost=0.0)
        )
        r = sim.simulate(h, [event("A.SH")])
        t = r["trades"][0]
        self.assertIn("slot_cash_at_entry", t)
        self.assertIn("entry_weight_vs_portfolio", t)
        self.assertAlmostEqual(r["summary"]["avg_trade_return_capital_weighted"], t["net_return"])
        self.assertAlmostEqual(r["summary"]["geometric_avg_trade_return"], t["net_return"])

    def test_one_price_gap_is_flagged_diagnostic_only(self):
        opens = [10, 10, 10.5, 10.5, 10.5]
        closes = [10, 10, 10.5, 10.5, 10.5]
        highs = [10, 10, 10.5, 10.5, 10.5]
        lows = [10, 10, 10.5, 10.5, 10.5]
        h = {"A.SH": hist(opens, closes, highs, lows)}
        c = prepare_fixed_horizon_candidates(h, [event("A.SH", 1)], holding_days=2)
        self.assertTrue(c[0]["entry_one_price"])
        self.assertAlmostEqual(c[0]["entry_open_gap_vs_prev_close"], 0.05)
        self.assertTrue(c[0]["fillability_suspect"])


if __name__ == "__main__":
    unittest.main()
