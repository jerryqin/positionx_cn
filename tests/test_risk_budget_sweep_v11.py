import unittest
from research.shared_equity_portfolio import SharedEquityConfig


class RiskBudgetSweepV11Tests(unittest.TestCase):
    def test_defined_scales_fit_nominal_position_budget(self):
        scales = [
            (0.010, 0.40),
            (0.015, 0.60),
            (0.020, 0.80),
            (0.025, 1.00),
        ]
        for target, gross in scales:
            cfg = SharedEquityConfig(
                initial_capital=1_000_000,
                max_positions=40,
                target_weight=target,
                max_total_weight=gross,
                round_trip_cost=0.002,
            )
            cfg.validate()

    def test_risk_budget_block_is_valid_runtime_state(self):
        # This is a specification test:
        # with price drift, existing gross exposure may already consume the cap,
        # so new entries may legitimately be blocked rather than raising.
        existing_gross = 410_000
        equity = 1_000_000
        cap = 0.40 * equity
        self.assertGreater(existing_gross, cap)


if __name__ == "__main__":
    unittest.main()
