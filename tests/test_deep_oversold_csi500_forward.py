import unittest

from research.forward_deep_oversold import (
    DeepOversoldForwardPolicy,
    choose_current_universe,
    entry_fillability,
    initial_state,
    public_health,
    review_not_before,
    lot_rounded_shares,
)


class DeepOversoldCSI500ForwardTests(unittest.TestCase):
    def test_policy_hash_is_stable(self):
        p = DeepOversoldForwardPolicy()
        self.assertEqual(p.fingerprint(), DeepOversoldForwardPolicy().fingerprint())

    def test_dual_current_universe_sources_must_match(self):
        a = {f"{i:06d}.SZ" for i in range(500)}
        b = set(a)
        r = choose_current_universe(a, b, 500)
        self.assertTrue(r["ok"])
        b.remove("000000.SZ")
        b.add("999999.SZ")
        r2 = choose_current_universe(a, b, 500)
        self.assertFalse(r2["ok"])
        self.assertEqual(r2["reason"], "CURRENT_UNIVERSE_SOURCE_MISMATCH")

    def test_fillability_allows_no_limit_day_when_not_one_price_locked(self):
        r = entry_fillability(10.0, None, 0, 0.01, 11.0, 9.0, 10.5, 9.5)
        self.assertTrue(r["fillable"])
        self.assertEqual(r["reason"], "STANDARD_OPEN_V1_NO_LIMIT")

    def test_fillability_blocks_one_price_up_when_limit_unknown(self):
        r = entry_fillability(10.0, None, 0, 0.01, 10.0, 10.0, 10.0, 9.0)
        self.assertFalse(r["fillable"])
        self.assertEqual(r["reason"], "ONE_PRICE_UP_NO_LIMIT_DATA")

    def test_fillability_blocks_open_at_limit(self):
        r = entry_fillability(11.0, 11.0, 0, 0.01)
        self.assertFalse(r["fillable"])
        self.assertEqual(r["reason"], "OPEN_AT_UP_LIMIT")

    def test_initial_state_is_armed(self):
        p = DeepOversoldForwardPolicy()
        s = initial_state(p)
        h = public_health(s, p, today="20260926")
        self.assertEqual(h["status"], "ARMED_WAIT_NEXT_SESSION")
        self.assertEqual(h["last_processed_date"], "20260924")
        self.assertFalse(h["review_ready"])

    def test_review_clock(self):
        self.assertEqual(review_not_before("20260928", 3), "20261228")

    def test_lot_rounding_never_forces_one_lot(self):
        self.assertEqual(lot_rounded_shares(9999, 100, 100), 0)
        self.assertEqual(lot_rounded_shares(10000, 100, 100), 100)
        self.assertEqual(lot_rounded_shares(25000, 100, 100), 200)


if __name__ == "__main__":
    unittest.main()
