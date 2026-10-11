import unittest

from research.forward_breakout_freshness import validate_strategy_snapshot_freshness


class ForwardBreakoutFreshnessTests(unittest.TestCase):
    def test_holiday_snapshot_generated_today_can_reference_previous_market_session(self):
        snapshot = {
            "generated_at": "2026-09-25T20:20:00",
            "input": {"history_refreshed": True},
        }
        rows = [
            {
                "strategy": "BREAKOUT_RETEST",
                "symbol": "600233.SH",
                "state": "RETEST_HOLD",
                "triggered": True,
                "latest_date": "20260924",
            }
        ]
        x = validate_strategy_snapshot_freshness(
            snapshot, rows, "20260924", "20260925"
        )
        self.assertTrue(x["ok"])
        self.assertEqual(x["explicit_market_asof"], "20260924")

    def test_generated_today_is_not_itself_market_asof(self):
        snapshot = {
            "generated_at": "2026-09-25T20:20:00",
            "input": {"history_refreshed": True},
        }
        rows = []
        x = validate_strategy_snapshot_freshness(
            snapshot, rows, "20260924", "20260925"
        )
        self.assertTrue(x["ok"])
        self.assertEqual(x["market_asof"], "20260924")

    def test_stale_generated_snapshot_is_blocked(self):
        snapshot = {
            "generated_at": "2026-09-24T20:20:00",
            "input": {"history_refreshed": True},
        }
        x = validate_strategy_snapshot_freshness(
            snapshot, [], "20260924", "20260925"
        )
        self.assertFalse(x["ok"])

    def test_explicit_market_asof_mismatch_is_blocked(self):
        snapshot = {
            "generated_at": "2026-09-28T20:20:00",
            "input": {"history_refreshed": True},
        }
        rows = [{"latest_date": "20260924"}]
        x = validate_strategy_snapshot_freshness(
            snapshot, rows, "20260928", "20260928"
        )
        self.assertFalse(x["ok"])


if __name__ == "__main__":
    unittest.main()
