import unittest

from research.forward_breakout import (
    ForwardPolicy,
    board_limit_ratio,
    fillability_at_open,
    forward_health,
    init_state,
    payload_hash,
    policy_payload,
    recursive_match_rows,
    triggered_breakout_signals,
)


class ForwardBreakoutTests(unittest.TestCase):
    def test_extracts_only_triggered_retest_hold(self):
        payload = {
            "rows": [
                {
                    "strategy": "BREAKOUT_RETEST",
                    "symbol": "600001.SH",
                    "state": "RETEST_HOLD",
                    "triggered": True,
                    "latest_date": "20260928",
                },
                {
                    "strategy": "BREAKOUT_RETEST",
                    "symbol": "600002.SH",
                    "state": "ARMED",
                    "triggered": False,
                    "latest_date": "20260928",
                },
                {
                    "strategy": "MA60_SUPPORT",
                    "symbol": "600003.SH",
                    "state": "SUPPORT_CONFIRMED",
                    "triggered": True,
                    "latest_date": "20260928",
                },
            ]
        }
        rows = recursive_match_rows(payload)
        x = triggered_breakout_signals(rows, "20260928", "20260924")
        self.assertEqual([r["symbol"] for r in x], ["600001.SH"])

    def test_open_at_limit_is_blocked(self):
        bar = {"open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0}
        x = fillability_at_open("600001.SH", bar, 10.0)
        self.assertFalse(x["fillable"])
        self.assertEqual(x["reason"], "OPEN_AT_OR_ABOVE_UP_LIMIT")

    def test_chinext_uses_twenty_percent(self):
        self.assertEqual(board_limit_ratio("300001.SZ"), 0.20)

    def test_policy_hash_changes_when_matcher_changes(self):
        p = ForwardPolicy()
        a = payload_hash(policy_payload(p, {"lookback": 20}))
        b = payload_hash(policy_payload(p, {"lookback": 21}))
        self.assertNotEqual(a, b)

    def test_health_is_armed_before_first_true_forward_session(self):
        p = ForwardPolicy()
        pp = policy_payload(p, {"lookback": 20})
        state = init_state(pp, payload_hash(pp), p.historical_cutoff, p.initial_capital)
        h = forward_health(state, p)
        self.assertEqual(h["status"], "ARMED_WAIT_NEXT_SESSION")
        self.assertFalse(h["trades_ready"])


if __name__ == "__main__":
    unittest.main()
