import unittest
import pandas as pd
import numpy as np

from research.deep_oversold_replication import (
    OversoldReplicationConfig,
    apply_standard_fillability,
    expand_membership,
    generate_events,
    replication_verdict,
)

class DeepOversoldReplicationV11Tests(unittest.TestCase):
    def test_date_normalization_accepts_dash_date(self):
        events = pd.DataFrame([{
            "symbol": "A", "entry_date": "20260105", "complete_outcome": True
        }])
        status = pd.DataFrame([{
            "trade_date": "2026-01-05", "symbol": "A",
            "is_suspended": False, "new_listing_special": False,
            "can_buy_at_open": True, "st_known": True,
        }])
        out = apply_standard_fillability(events, status)
        self.assertTrue(bool(out.iloc[0]["status_found"]))
        self.assertTrue(bool(out.iloc[0]["standard_fillable"]))

    def test_missing_status_fails_closed_at_row_level(self):
        events = pd.DataFrame([{
            "symbol": "A", "entry_date": "20260105", "complete_outcome": True
        }])
        status = pd.DataFrame(columns=[
            "trade_date","symbol","is_suspended","new_listing_special",
            "can_buy_at_open","st_known"
        ])
        out = apply_standard_fillability(events, status)
        self.assertFalse(bool(out.iloc[0]["status_found"]))
        self.assertFalse(bool(out.iloc[0]["standard_fillable"]))

    def test_event_uses_exact_t_plus_one_and_signal_plus_20(self):
        dates = pd.bdate_range("2026-01-01", periods=50).strftime("%Y%m%d").tolist()
        close = np.ones(50)*100
        close[20] = 70
        open_ = close.copy()
        open_[21] = 71
        close[21] = 72
        close[40] = 80
        stock = pd.DataFrame({
            "open": open_, "high": np.maximum(open_,close)+1,
            "low": np.minimum(open_,close)-1, "close": close,
            "volume": 1000, "amount": 1e8,
        }, index=dates)
        bench = pd.DataFrame({
            "open": 100., "high": 101., "low": 99., "close": 100.,
            "volume": 1000, "amount": 1e8,
        }, index=dates)
        membership = pd.DataFrame({"signal_date": dates, "symbol": ["A"]*len(dates)})
        ev = generate_events(
            {"A": stock}, bench, membership, dates, OversoldReplicationConfig()
        )
        row = ev.loc[ev["signal_date"] == dates[20]].iloc[0]
        self.assertEqual(row["entry_date"], dates[21])
        self.assertEqual(row["exit_date"], dates[40])

    def test_replication_verdict(self):
        s = {
            "excess_gross": {"avg": .01, "trimmed_mean_5pct": .005},
            "folds": [{"avg_excess": .01},{"avg_excess": .02},{"avg_excess": -.01}],
        }
        self.assertTrue(replication_verdict(s)["replicated_core"])

if __name__ == "__main__":
    unittest.main()
