import unittest

from research.backtest_gate import BacktestGateConfig, summarize_strategy


def row(symbol, date, ret):
    return {
        "strategy": "X",
        "symbol": symbol,
        "signal_date": date,
        "return_20d": ret,
        "mfe_20d": max(ret, 0.02),
        "mae_20d": min(ret, -0.01),
    }


class BacktestGateV11Tests(unittest.TestCase):
    def test_low_win_rate_positive_expectancy_can_pass(self):
        cfg = BacktestGateConfig(
            horizons=(20,),
            gate_horizon=20,
            min_events=6,
            assumed_round_trip_cost=0.0,
            min_profit_factor=1.05,
            folds=3,
            min_positive_folds=2,
        )
        rows = [
            row("A", "20240101", -0.04),
            row("B", "20240102", 0.10),
            row("C", "20240201", -0.04),
            row("D", "20240202", 0.10),
            row("E", "20240301", -0.04),
            row("F", "20240302", 0.10),
        ]
        s = summarize_strategy("X", rows, cfg)
        self.assertEqual(s["horizons"]["20"]["win_rate"], 0.5)
        self.assertGreater(s["horizons"]["20"]["profit_factor"], 1.0)
        self.assertEqual(s["gate"]["status"], "PASS_CANDIDATE")

    def test_positive_raw_return_can_fail_after_cost(self):
        cfg = BacktestGateConfig(
            horizons=(20,),
            gate_horizon=20,
            min_events=3,
            assumed_round_trip_cost=0.01,
            min_profit_factor=1.0,
            folds=3,
            min_positive_folds=1,
        )
        rows = [
            row("A", "20240101", 0.02),
            row("B", "20240201", -0.005),
            row("C", "20240301", 0.005),
        ]
        s = summarize_strategy("X", rows, cfg)
        self.assertLessEqual(s["horizons"]["20"]["net_avg_return"], 0)
        self.assertEqual(s["gate"]["status"], "FAIL")

    def test_temporal_instability_can_fail_gate(self):
        cfg = BacktestGateConfig(
            horizons=(20,),
            gate_horizon=20,
            min_events=6,
            assumed_round_trip_cost=0.0,
            min_profit_factor=1.0,
            folds=3,
            min_positive_folds=2,
        )
        rows = [
            row("A", "20240101", 0.20),
            row("B", "20240102", 0.20),
            row("C", "20240201", -0.05),
            row("D", "20240202", -0.05),
            row("E", "20240301", -0.05),
            row("F", "20240302", -0.05),
        ]
        s = summarize_strategy("X", rows, cfg)
        self.assertEqual(s["stability"]["positive_folds"], 1)
        self.assertEqual(s["gate"]["status"], "FAIL")

    def test_distribution_reports_payoff_median_and_ci(self):
        cfg = BacktestGateConfig(
            horizons=(20,),
            gate_horizon=20,
            min_events=4,
            assumed_round_trip_cost=0.0,
            min_profit_factor=1.0,
            folds=2,
            min_positive_folds=1,
        )
        rows = [
            row("A", "20240101", -0.02),
            row("B", "20240102", -0.01),
            row("C", "20240201", 0.03),
            row("D", "20240202", 0.06),
        ]
        h = summarize_strategy("X", rows, cfg)["horizons"]["20"]
        self.assertIsNotNone(h["median_return"])
        self.assertIsNotNone(h["payoff_ratio"])
        self.assertIsNotNone(h["profit_factor"])
        self.assertIsNotNone(h["naive_iid_ci95_low"])
        self.assertIsNotNone(h["naive_iid_ci95_high"])

    def test_concentration_is_reported(self):
        cfg = BacktestGateConfig(
            horizons=(20,),
            gate_horizon=20,
            min_events=4,
            assumed_round_trip_cost=0.0,
            min_profit_factor=1.0,
            folds=2,
            min_positive_folds=1,
        )
        rows = [
            row("A", "20240101", 0.02),
            row("A", "20240101", 0.03),
            row("A", "20240201", 0.01),
            row("B", "20240202", -0.01),
        ]
        c = summarize_strategy("X", rows, cfg)["concentration"]
        self.assertEqual(c["unique_symbols"], 2)
        self.assertAlmostEqual(c["max_symbol_share"], 0.75)
        self.assertAlmostEqual(c["max_signal_date_share"], 0.5)


if __name__ == "__main__":
    unittest.main()
