import unittest
import pandas as pd

from research.deep_oversold_capacity import (
    adv20_traded_days,
    enrich_capacity_events,
    research_buy_lot,
    simulate_capacity,
)


class DeepOversoldCapacityTests(unittest.TestCase):
    def test_adv20_uses_only_t_observable_history(self):
        idx = [f"202601{i:02d}" for i in range(1, 22)]
        df = pd.DataFrame({"amount":[100.0]*20 + [999999.0]}, index=idx)
        r = adv20_traded_days(df, "20260120", 20)
        self.assertTrue(r["ok"])
        self.assertEqual(r["adv20_amount"], 100.0)
        self.assertEqual(r["last_date"], "20260120")

    def test_board_lot_proxy(self):
        self.assertEqual(research_buy_lot("688001.SH"), 200)
        self.assertEqual(research_buy_lot("689009.SH"), 200)
        self.assertEqual(research_buy_lot("600000.SH"), 100)
        self.assertEqual(research_buy_lot("300001.SZ"), 100)

    def test_enrichment_computes_account_floor_and_liquidity_capacity(self):
        dates = [f"202601{i:02d}" for i in range(1, 23)]
        raw = pd.DataFrame({
            "open":[50.0]*22,
            "amount":[100_000_000.0]*22,
        }, index=dates)
        events = pd.DataFrame([{
            "symbol":"600000.SH",
            "signal_date":"20260120",
            "entry_date":"20260121",
            "exit_date":"20260122",
            "entry_open":49.0,
        }])
        out, audit = enrich_capacity_events(events, {"600000.SH":raw})
        self.assertEqual(audit["capacity_ready_events"], 1)
        self.assertAlmostEqual(out.iloc[0]["one_lot_notional"], 5000.0)
        self.assertAlmostEqual(out.iloc[0]["min_account_for_one_lot_at_1pct"], 500000.0)
        self.assertAlmostEqual(
            out.iloc[0]["account_capacity_full_1pct_25bp"],
            25_000_000.0,
        )

    def test_small_account_cannot_force_one_lot(self):
        dates = ["20260120","20260121","20260122"]
        hist = pd.DataFrame({
            "open":[100,100,100],
            "close":[100,100,110],
        }, index=dates)
        events = pd.DataFrame([{
            "symbol":"600000.SH",
            "signal_date":"20260120",
            "entry_date":"20260121",
            "exit_date":"20260122",
            "entry_open":100.0,
            "raw_entry_open":100.0,
            "buy_lot":100,
            "adv20_amount":1_000_000_000.0,
        }])
        r = simulate_capacity(
            events, {"600000.SH":hist}, dates,
            initial_equity=500_000.0,
            participation_rate=None,
            round_trip_cost=0.0,
        )
        self.assertEqual(r["executed_trades"], 0)
        self.assertGreater(r["skipped_lot"], 0)

    def test_liquidity_cap_can_block_even_large_account(self):
        dates = ["20260120","20260121","20260122"]
        hist = pd.DataFrame({
            "open":[100,100,100],
            "close":[100,100,110],
        }, index=dates)
        events = pd.DataFrame([{
            "symbol":"600000.SH",
            "signal_date":"20260120",
            "entry_date":"20260121",
            "exit_date":"20260122",
            "entry_open":100.0,
            "raw_entry_open":100.0,
            "buy_lot":100,
            "adv20_amount":1_000_000.0,
        }])
        r = simulate_capacity(
            events, {"600000.SH":hist}, dates,
            initial_equity=10_000_000.0,
            participation_rate=0.0025,
            round_trip_cost=0.0,
        )
        self.assertEqual(r["executed_trades"], 0)
        self.assertEqual(r["skipped_liquidity_below_lot"], 1)


if __name__ == "__main__":
    unittest.main()
