import unittest
import pandas as pd

from research.deep_oversold_economic_gate import (
    EconomicProfile,
    concurrency_demand,
    dedup_while_held,
    simulate_shared_equity,
)


class DeepOversoldEconomicGateTests(unittest.TestCase):
    def test_dedup_removes_overlapping_same_symbol(self):
        x = pd.DataFrame([
            {"symbol":"A","signal_date":"20260101","entry_date":"20260102","exit_date":"20260121"},
            {"symbol":"A","signal_date":"20260103","entry_date":"20260104","exit_date":"20260122"},
            {"symbol":"A","signal_date":"20260121","entry_date":"20260122","exit_date":"20260210"},
        ])
        y = dedup_while_held(x)
        self.assertEqual(len(y), 2)

    def test_demand_counts_overlap(self):
        e = pd.DataFrame([
            {"entry_date":"20260102","exit_date":"20260105"},
            {"entry_date":"20260103","exit_date":"20260106"},
        ])
        d = concurrency_demand(e, ["20260102","20260103","20260105","20260106"])
        self.assertEqual(d["active_max"], 2)

    def test_loader_accepts_replication_csv_without_raw_prices(self):
        import tempfile
        from research.deep_oversold_economic_gate import load_fillable_events
        df = pd.DataFrame([{
            "symbol":"A",
            "signal_date":"20260102",
            "entry_date":"20260105",
            "exit_date":"20260130",
            "standard_fillable":True,
            "open_excess_gross":0.03,
            "open_net_20bps":0.04,
        }])
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w", newline="", encoding="utf-8") as f:
            path = f.name
            df.to_csv(f, index=False)
        out = load_fillable_events(path)
        self.assertEqual(len(out), 1)
        self.assertNotIn("entry_open", out.columns)
        self.assertNotIn("exit_close", out.columns)

    def test_flat_trade_loses_round_trip_cost(self):
        dates = ["20260102","20260105","20260106"]
        hist = pd.DataFrame({
            "open":[100,100,100],
            "high":[100,100,100],
            "low":[100,100,100],
            "close":[100,100,100],
        }, index=dates)
        events = pd.DataFrame([{
            "symbol":"A",
            "signal_date":"20260102",
            "entry_date":"20260105",
            "exit_date":"20260106",
        }])
        sim = simulate_shared_equity(
            events, {"A":hist}, dates,
            EconomicProfile("T",0.10,0.10),
            round_trip_cost=0.002,
            initial_equity=100000,
        )
        self.assertEqual(sim["trades"], 1)
        self.assertLess(sim["total_return"], 0)
        self.assertAlmostEqual(
            sim["trade_stats"]["avg_net_return"], -0.002,
            places=8,
        )

    def test_same_day_entries_scale_pro_rata_not_rank(self):
        dates = ["20260102","20260105","20260106"]
        h = pd.DataFrame({
            "open":[100,100,100],
            "high":[100,100,100],
            "low":[100,100,100],
            "close":[100,100,100],
        }, index=dates)
        events = pd.DataFrame([
            {"symbol":"A","signal_date":"20260102","entry_date":"20260105","exit_date":"20260106"},
            {"symbol":"B","signal_date":"20260102","entry_date":"20260105","exit_date":"20260106"},
        ])
        sim = simulate_shared_equity(
            events, {"A":h,"B":h}, dates,
            EconomicProfile("T",0.10,0.10),
            round_trip_cost=0.0,
            initial_equity=100000,
        )
        self.assertEqual(sim["trades"], 2)
        self.assertEqual(sim["scaled_entry_days"], 1)


if __name__ == "__main__":
    unittest.main()
