import unittest
import pandas as pd

from research.backtest_gate import (
    BacktestGateConfig,
    attach_forward_outcomes,
    breakout_retest_events,
    summarize_strategy,
)


def bars(n=60):
    idx = [f"D{i:03d}" for i in range(n)]
    return pd.DataFrame({
        "open": [10.0] * n,
        "high": [10.0] * n,
        "low": [9.9] * n,
        "close": [10.0] * n,
        "volume": [100.0] * n,
    }, index=idx)


class BacktestGateTests(unittest.TestCase):
    def test_breakout_retest_event_and_next_open_entry(self):
        df = bars()
        df.loc[df.index[-4], ["high", "low", "close", "volume"]] = [10.3, 10.0, 10.05, 120.0]
        df.loc[df.index[-3], ["high", "low", "close", "volume"]] = [10.1, 10.02, 10.08, 100.0]
        df.loc[df.index[-2], ["high", "low", "close", "volume"]] = [10.05, 9.99, 10.03, 100.0]
        df.loc[df.index[-1], ["open", "high", "low", "close"]] = [10.10, 10.20, 10.00, 10.15]
        ev = breakout_retest_events("A.SH", df)
        self.assertEqual(len(ev), 1)
        out = attach_forward_outcomes(df, ev, horizons=(1,))
        self.assertEqual(out[0]["entry_date"], df.index[-1])
        self.assertAlmostEqual(out[0]["entry_price"], 10.10)

    def test_failed_breakout_produces_no_event(self):
        df = bars()
        df.loc[df.index[-4], ["high", "low", "close", "volume"]] = [10.3, 10.0, 10.05, 120.0]
        df.loc[df.index[-3], ["high", "low", "close", "volume"]] = [10.0, 9.7, 9.80, 100.0]
        df.loc[df.index[-2], ["high", "low", "close", "volume"]] = [9.9, 9.8, 9.85, 100.0]
        self.assertEqual(breakout_retest_events("A.SH", df), [])

    def test_summary_insufficient_sample(self):
        cfg = BacktestGateConfig(min_events=3, horizons=(20,), gate_horizon=20)
        rows = [
            {"strategy": "X", "return_20d": 0.10, "mfe_20d": 0.2, "mae_20d": -0.05},
            {"strategy": "X", "return_20d": -0.02, "mfe_20d": 0.1, "mae_20d": -0.06},
        ]
        s = summarize_strategy("X", rows, cfg)
        self.assertEqual(s["gate"]["status"], "INSUFFICIENT_SAMPLE")

    def test_summary_pass_candidate(self):
        cfg = BacktestGateConfig(
            min_events=2, horizons=(20,), gate_horizon=20,
            min_win_rate=0.5, min_avg_return=0.0
        )
        rows = [
            {"strategy": "X", "return_20d": 0.10, "mfe_20d": 0.2, "mae_20d": -0.05},
            {"strategy": "X", "return_20d": -0.02, "mfe_20d": 0.1, "mae_20d": -0.06},
        ]
        s = summarize_strategy("X", rows, cfg)
        self.assertEqual(s["gate"]["status"], "PASS_CANDIDATE")

    def test_summary_fail(self):
        cfg = BacktestGateConfig(
            min_events=2, horizons=(20,), gate_horizon=20,
            min_win_rate=0.5, min_avg_return=0.0
        )
        rows = [
            {"strategy": "X", "return_20d": -0.10, "mfe_20d": 0.03, "mae_20d": -0.15},
            {"strategy": "X", "return_20d": -0.02, "mfe_20d": 0.02, "mae_20d": -0.08},
        ]
        s = summarize_strategy("X", rows, cfg)
        self.assertEqual(s["gate"]["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
