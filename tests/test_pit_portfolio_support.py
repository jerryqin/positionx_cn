import unittest

from research.pit_portfolio_support import audit_next_market_session, add_capacity_rates


class PITPortfolioSupportTests(unittest.TestCase):
    def test_next_market_session_passes(self):
        events = [
            {"symbol": "A", "signal_date": "20260105", "entry_date": "20260106"},
            {"symbol": "B", "signal_date": "20260106", "entry_date": "20260108"},
        ]
        cal = ["20260105", "20260106", "20260108"]
        out = audit_next_market_session(events, cal)
        self.assertTrue(out["pass"])
        self.assertEqual(out["entry_next_market_session_mismatches"], 0)

    def test_next_market_session_detects_skipped_day(self):
        events = [
            {"symbol": "A", "signal_date": "20260105", "entry_date": "20260108"},
        ]
        cal = ["20260105", "20260106", "20260108"]
        out = audit_next_market_session(events, cal)
        self.assertFalse(out["pass"])
        self.assertEqual(out["mismatch_sample"][0]["expected_next_market_session"], "20260106")

    def test_capacity_rates(self):
        s = {
            "signals_executable": 100,
            "skipped_no_slot": 60,
            "skipped_duplicate_symbol": 5,
        }
        add_capacity_rates(s)
        self.assertAlmostEqual(s["capacity_skip_rate"], 0.60)
        self.assertAlmostEqual(s["total_skip_rate"], 0.65)


if __name__ == "__main__":
    unittest.main()
