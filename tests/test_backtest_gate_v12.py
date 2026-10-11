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


class BacktestGateV12Tests(unittest.TestCase):
    def cfg(self):
        return BacktestGateConfig(
            horizons=(20,), gate_horizon=20, min_events=1,
            assumed_round_trip_cost=0.0, min_profit_factor=0.0,
            folds=1, min_positive_folds=0,
        )

    def test_tail_dependent_distribution_is_detected(self):
        rows = [row(f"S{i}", f"2024{i//28+1:02d}{i%28+1:02d}", -0.001) for i in range(99)]
        rows.append(row("BIG", "20241231", 1.0))
        r = summarize_strategy("X", rows, self.cfg())["robustness"]
        self.assertEqual(r["status"], "TAIL_DEPENDENT")
        self.assertLess(r["trimmed_mean_5pct"], 0)
        self.assertGreater(r["top1pct_share_of_gross_profit"], 0.99)

    def test_cost_sensitivity_is_reported(self):
        rows = [row("A", "20240101", 0.01), row("B", "20240201", 0.004)]
        r = summarize_strategy("X", rows, self.cfg())["robustness"]
        costs = {x["round_trip_cost"]: x for x in r["cost_sensitivity"]}
        self.assertAlmostEqual(costs[0.005]["net_avg_return"], 0.002)
        self.assertLess(costs[0.010]["net_avg_return"], 0)

    def test_robust_candidate_requires_trimmed_and_half_percent_cost(self):
        rows = []
        for i in range(100):
            ret = 0.02 if i % 2 == 0 else -0.005
            rows.append(row(f"S{i}", f"2024{i//28+1:02d}{i%28+1:02d}", ret))
        r = summarize_strategy("X", rows, self.cfg())["robustness"]
        self.assertEqual(r["status"], "ROBUST_CANDIDATE")
        self.assertGreater(r["trimmed_mean_5pct"], 0)

    def test_quantiles_are_present(self):
        rows = [row("A", "20240101", -0.1), row("B", "20240201", 0.0), row("C", "20240301", 0.1)]
        q = summarize_strategy("X", rows, self.cfg())["robustness"]["quantiles"]
        self.assertEqual(set(q), {"p05", "p10", "p25", "p50", "p75", "p90", "p95"})
        self.assertAlmostEqual(q["p50"], 0.0)

    def test_v12_robustness_does_not_change_existing_gate_status(self):
        rows = [row("A", "20240101", 0.02), row("B", "20240201", 0.02)]
        s = summarize_strategy("X", rows, self.cfg())
        self.assertEqual(s["gate"]["status"], "PASS_CANDIDATE")
        self.assertIn("robustness", s)


if __name__ == "__main__":
    unittest.main()
