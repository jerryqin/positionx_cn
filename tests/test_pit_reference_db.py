import unittest
from pathlib import Path
import pandas as pd

from research.pit_backtest_support import (
    CSI300ReferenceUniverse, PITCandidateConfig, candidate_metrics_at_signal,
    filter_events_point_in_time,
)

DB = Path(__file__).resolve().parents[1] / "data" / "research" / "positionx_reference.db"


class PITReferenceDBTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.u = CSI300ReferenceUniverse(DB)

    def test_dataset_ready_and_counts(self):
        self.assertEqual(self.u.current_member_count, 300)
        self.assertEqual(self.u.distinct_symbol_count, 474)
        self.assertEqual(self.u.trade_date_count, 1493)
        self.assertEqual(self.u.interval_count, 512)

    def test_union_members_is_474(self):
        self.assertEqual(len(self.u.union_members("20200801", "20260924")), 474)

    def test_reentry_semantics(self):
        self.assertFalse(self.u.contains("601298.SH", "20210104"))
        self.assertTrue(self.u.contains("601298.SH", "20250905"))

    def test_official_20250905_switch(self):
        self.assertTrue(self.u.contains("601989.SH", "20250904"))
        self.assertFalse(self.u.contains("601989.SH", "20250905"))
        self.assertFalse(self.u.contains("601298.SH", "20250904"))
        self.assertTrue(self.u.contains("601298.SH", "20250905"))

    def test_membership_is_300_on_spot_dates(self):
        for d in ("20200803", "20210601", "20210615", "20250304", "20260615", "20260924"):
            self.assertEqual(len(self.u.members_at(d)), 300, d)

    def test_fingerprint_is_stable_shape(self):
        self.assertEqual(len(self.u.source_sha256), 64)

    def test_candidate_gate_uses_historical_price_and_liquidity(self):
        idx = pd.date_range("2021-05-01", periods=25, freq="D")
        df = pd.DataFrame({
            "open": [10.0]*25, "high": [10.5]*25, "low": [9.5]*25,
            "close": [10.0]*25, "volume": [10_000_000.0]*25,
            "amount": [100_000_000.0]*25,
        }, index=idx)
        m = candidate_metrics_at_signal(df, 24, PITCandidateConfig())
        self.assertTrue(m["eligible"])
        self.assertEqual(m["amount_source"], "amount")

    def test_filter_rejects_nonmember_event(self):
        idx = pd.date_range("2021-05-08", periods=25, freq="D")
        df = pd.DataFrame({
            "open": [10.0]*25, "high": [10.5]*25, "low": [9.5]*25,
            "close": [10.0]*25, "volume": [10_000_000.0]*25,
            "amount": [100_000_000.0]*25,
        }, index=idx)
        # 601298.SH is not a CSI300 member in 2021.
        events = [{"symbol":"601298.SH", "strategy":"BREAKOUT_RETEST",
                   "signal_date":"20210601", "signal_pos":24}]
        accepted, audit = filter_events_point_in_time(
            {"601298.SH": df}, events, self.u, start_date="20200801", end_date="20260924"
        )
        self.assertEqual(accepted, [])
        self.assertEqual(audit["rejection_counts"]["NOT_CSI300_MEMBER"], 1)


if __name__ == "__main__":
    unittest.main()
