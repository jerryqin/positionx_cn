import unittest

from research.forward_breakout import (
    ForwardPolicy,
    forward_health,
    init_state,
    payload_hash,
    policy_payload,
    triggered_breakout_signals,
)


class ForwardBreakoutV11Tests(unittest.TestCase):
    def test_historical_cutoff_signal_is_never_admitted(self):
        rows = [{
            "strategy": "BREAKOUT_RETEST",
            "symbol": "600233.SH",
            "state": "RETEST_HOLD",
            "triggered": True,
            "latest_date": "20260924",
        }]
        self.assertEqual(
            triggered_breakout_signals(rows, "20260924", "20260924"),
            [],
        )

    def test_first_new_session_signal_can_be_admitted(self):
        rows = [{
            "strategy": "BREAKOUT_RETEST",
            "symbol": "600233.SH",
            "state": "RETEST_HOLD",
            "triggered": True,
            "latest_date": "20260928",
        }]
        x = triggered_breakout_signals(rows, "20260928", "20260924")
        self.assertEqual(len(x), 1)

    def test_initial_health_is_armed_not_collecting(self):
        p = ForwardPolicy()
        pp = policy_payload(p, {"lookback": 20})
        state = init_state(pp, payload_hash(pp), p.historical_cutoff, p.initial_capital)
        h = forward_health(state, p)
        self.assertEqual(h["status"], "ARMED_WAIT_NEXT_SESSION")
        self.assertIsNone(h["started_at"])

    def test_review_clock_starts_on_first_forward_session(self):
        p = ForwardPolicy()
        pp = policy_payload(p, {"lookback": 20})
        state = init_state(pp, payload_hash(pp), p.historical_cutoff, p.initial_capital)
        state["started_at"] = "20260928"
        state["last_processed_date"] = "20260928"
        h = forward_health(state, p)
        self.assertEqual(h["review_not_before"], "20261228")


if __name__ == "__main__":
    unittest.main()
