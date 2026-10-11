import unittest
import pandas as pd
import numpy as np

from research.deep_oversold_replication import (
    OversoldReplicationConfig,
    apply_standard_fillability,
    expand_membership,
    generate_events,
    net_return,
    replication_verdict,
)


class DeepOversoldReplicationTests(unittest.TestCase):
    def test_membership_end_is_exclusive(self):
        intervals = pd.DataFrame([{
            "symbol": "A",
            "valid_from": "20260102",
            "valid_to_exclusive": "20260105",
        }])
        out = expand_membership(
            intervals,
            ["20260101", "20260102", "20260103", "20260105"],
        )
        self.assertEqual(out["signal_date"].tolist(), ["20260102", "20260103"])

    def test_event_uses_exact_t_plus_one_and_signal_plus_20(self):
        dates = pd.bdate_range("2026-01-01", periods=50).strftime("%Y%m%d").tolist()
        close = np.linspace(100, 100, 50)
        close[20] = 70.0  # -30% vs day 0
        open_ = close.copy()
        open_[21] = 71.0
        close[21] = 72.0
        close[40] = 80.0

        stock = pd.DataFrame({
            "open": open_, "high": np.maximum(open_, close)+1,
            "low": np.minimum(open_, close)-1, "close": close,
            "volume": 1000, "amount": 1e8,
        }, index=dates)
        bench = pd.DataFrame({
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
            "volume": 1000, "amount": 1e8,
        }, index=dates)
        membership = pd.DataFrame({
            "signal_date": dates,
            "symbol": ["A"]*len(dates),
        })
        ev = generate_events(
            {"A": stock}, bench, membership, dates,
            OversoldReplicationConfig(),
        )
        row = ev.loc[ev["signal_date"] == dates[20]].iloc[0]
        self.assertEqual(row["entry_date"], dates[21])
        self.assertEqual(row["exit_date"], dates[40])
        self.assertAlmostEqual(row["entry_open"], 71.0)
        self.assertAlmostEqual(row["entry_close"], 72.0)

    def test_standard_fillability_blocks_suspended(self):
        events = pd.DataFrame([{
            "symbol": "A", "entry_date": "20260105", "complete_outcome": True
        }])
        status = pd.DataFrame([{
            "trade_date": "20260105", "symbol": "A",
            "is_suspended": True, "new_listing_special": False,
            "can_buy_at_open": False, "st_known": True,
        }])
        out = apply_standard_fillability(events, status)
        self.assertFalse(bool(out.iloc[0]["standard_fillable"]))

    def test_replication_verdict_requires_two_positive_folds(self):
        s = {
            "excess_gross": {"avg": 0.01, "trimmed_mean_5pct": 0.005},
            "folds": [
                {"avg_excess": 0.01},
                {"avg_excess": 0.02},
                {"avg_excess": -0.01},
            ],
        }
        self.assertTrue(replication_verdict(s)["replicated_core"])


if __name__ == "__main__":
    unittest.main()
