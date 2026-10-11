import unittest
import pandas as pd

from research.portfolio_simulation import (
    BreakoutRetestPortfolioSimulator,
    PortfolioSimulationConfig,
    prepare_fixed_horizon_candidates,
)


def hist(symbol, opens, closes=None):
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


def event(symbol, signal_pos=1):
    return {
        "symbol": symbol,
        "strategy": "BREAKOUT_RETEST",
        "signal_pos": signal_pos,
        "signal_date": f"D{signal_pos:02d}",
        "level": 10.0,
    }


class PortfolioSimulationTests(unittest.TestCase):
    def test_prepare_uses_next_open_and_fixed_close(self):
        h = {"A.SH": hist("A.SH", [10, 11, 12, 13, 14], [10, 11, 12, 13, 15])}
        out = prepare_fixed_horizon_candidates(h, [event("A.SH", 1)], holding_days=2)
        self.assertEqual(out[0]["entry_date"], "D02")
        self.assertEqual(out[0]["entry_price"], 12.0)
        self.assertEqual(out[0]["exit_date"], "D03")
        self.assertEqual(out[0]["exit_price"], 13.0)

    def test_single_trade_zero_cost_return(self):
        h = {"A.SH": hist("A.SH", [10, 10, 10, 10, 10], [10, 10, 10, 11, 11])}
        sim = BreakoutRetestPortfolioSimulator(
            PortfolioSimulationConfig(
                initial_capital=1000,
                max_positions=1,
                holding_days=2,
                round_trip_cost=0.0,
            )
        )
        r = sim.simulate(h, [event("A.SH", 1)])
        self.assertEqual(r["summary"]["trades_executed"], 1)
        self.assertAlmostEqual(r["trades"][0]["net_return"], 0.10)
        self.assertAlmostEqual(r["summary"]["total_return"], 0.10)

    def test_max_positions_skips_second_same_day_signal(self):
        h = {
            "A.SH": hist("A.SH", [10] * 6),
            "B.SH": hist("B.SH", [10] * 6),
        }
        sim = BreakoutRetestPortfolioSimulator(
            PortfolioSimulationConfig(
                initial_capital=1000,
                max_positions=1,
                holding_days=3,
                round_trip_cost=0.0,
            )
        )
        r = sim.simulate(h, [event("B.SH", 1), event("A.SH", 1)])
        self.assertEqual(r["summary"]["trades_executed"], 1)
        self.assertEqual(r["summary"]["skipped_no_slot"], 1)
        self.assertEqual(r["trades"][0]["symbol"], "A.SH")

    def test_duplicate_symbol_is_skipped_while_held(self):
        h = {"A.SH": hist("A.SH", [10] * 8)}
        sim = BreakoutRetestPortfolioSimulator(
            PortfolioSimulationConfig(
                initial_capital=1000,
                max_positions=2,
                holding_days=4,
                round_trip_cost=0.0,
            )
        )
        r = sim.simulate(h, [event("A.SH", 1), event("A.SH", 2)])
        self.assertEqual(r["summary"]["trades_executed"], 1)
        self.assertEqual(r["summary"]["skipped_duplicate_symbol"], 1)

    def test_round_trip_cost_reduces_flat_trade(self):
        h = {"A.SH": hist("A.SH", [10] * 6)}
        sim = BreakoutRetestPortfolioSimulator(
            PortfolioSimulationConfig(
                initial_capital=1000,
                max_positions=1,
                holding_days=2,
                round_trip_cost=0.002,
            )
        )
        r = sim.simulate(h, [event("A.SH", 1)])
        self.assertLess(r["trades"][0]["net_return"], 0.0)
        self.assertAlmostEqual(r["trades"][0]["net_return"], -0.001998, places=5)
        self.assertLess(r["summary"]["total_return"], 0.0)

    def test_slot_not_reused_until_exit_close(self):
        h = {
            "A.SH": hist("A.SH", [10] * 7),
            "B.SH": hist("B.SH", [10] * 7),
        }
        sim = BreakoutRetestPortfolioSimulator(
            PortfolioSimulationConfig(
                initial_capital=1000,
                max_positions=1,
                holding_days=2,
                round_trip_cost=0.0,
            )
        )
        # A enters D02 and exits D03 close. B also wants D03 open -> no slot yet.
        r = sim.simulate(h, [event("A.SH", 1), event("B.SH", 2)])
        self.assertEqual(r["summary"]["trades_executed"], 1)
        self.assertEqual(r["summary"]["skipped_no_slot"], 1)


if __name__ == "__main__":
    unittest.main()
