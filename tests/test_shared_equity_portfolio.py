import unittest
import pandas as pd

from research.shared_equity_portfolio import SharedEquityConfig, simulate_shared_equity


def hist(rows):
    return pd.DataFrame(rows).set_index("date")


class SharedEquityPortfolioTests(unittest.TestCase):
    def test_flat_trade_loses_round_trip_cost(self):
        history = {
            "A": hist([
                {"date": "2026-01-02", "open": 10.0, "close": 10.0},
                {"date": "2026-01-05", "open": 10.0, "close": 10.0},
            ])
        }
        candidates = [{"symbol":"A","entry_date":"20260102","exit_date":"20260105","signal_date":"20260101"}]
        cfg = SharedEquityConfig(
            initial_capital=100000,
            max_positions=1,
            target_weight=0.8,
            max_total_weight=0.8,
            round_trip_cost=0.002,
        )
        r = simulate_shared_equity(history, candidates, ["20260102","20260105"], cfg)
        self.assertEqual(r["summary"]["trades_executed"], 1)
        self.assertLess(r["summary"]["final_equity"], 100000)

    def test_capacity_blocks_second_same_day(self):
        history = {
            "A": hist([{"date":"2026-01-02","open":10,"close":10},{"date":"2026-01-05","open":10,"close":10}]),
            "B": hist([{"date":"2026-01-02","open":10,"close":10},{"date":"2026-01-05","open":10,"close":10}]),
        }
        candidates = [
            {"symbol":"A","entry_date":"20260102","exit_date":"20260105"},
            {"symbol":"B","entry_date":"20260102","exit_date":"20260105"},
        ]
        cfg = SharedEquityConfig(100000,1,0.8,0.8,0.0)
        r = simulate_shared_equity(history,candidates,["20260102","20260105"],cfg)
        self.assertEqual(r["summary"]["trades_executed"],1)
        self.assertEqual(r["summary"]["skipped_no_slot"],1)

    def test_duplicate_is_blocked_while_held(self):
        history = {
            "A": hist([
                {"date":"2026-01-02","open":10,"close":10},
                {"date":"2026-01-05","open":10,"close":10},
                {"date":"2026-01-06","open":10,"close":10},
            ])
        }
        candidates = [
            {"symbol":"A","entry_date":"20260102","exit_date":"20260106"},
            {"symbol":"A","entry_date":"20260105","exit_date":"20260106"},
        ]
        cfg = SharedEquityConfig(100000,10,0.05,0.8,0.0)
        r = simulate_shared_equity(history,candidates,["20260102","20260105","20260106"],cfg)
        self.assertEqual(r["summary"]["trades_executed"],1)
        self.assertEqual(r["summary"]["skipped_duplicate_symbol"],1)

    def test_same_day_exit_not_reused_at_open(self):
        history = {
            "A": hist([{"date":"2026-01-02","open":10,"close":10},{"date":"2026-01-05","open":10,"close":10}]),
            "B": hist([{"date":"2026-01-05","open":10,"close":10},{"date":"2026-01-06","open":10,"close":10}]),
        }
        candidates = [
            {"symbol":"A","entry_date":"20260102","exit_date":"20260105"},
            {"symbol":"B","entry_date":"20260105","exit_date":"20260106"},
        ]
        cfg = SharedEquityConfig(100000,1,0.8,0.8,0.0)
        r = simulate_shared_equity(history,candidates,["20260102","20260105","20260106"],cfg)
        self.assertEqual(r["summary"]["trades_executed"],1)
        self.assertEqual(r["summary"]["skipped_no_slot"],1)

    def test_config_rejects_impossible_weight_budget(self):
        with self.assertRaises(ValueError):
            SharedEquityConfig(
                initial_capital=100000,
                max_positions=40,
                target_weight=0.03,
                max_total_weight=0.80,
            ).validate()


if __name__ == "__main__":
    unittest.main()
