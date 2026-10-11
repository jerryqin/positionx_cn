import unittest
import pandas as pd

from research.deep_oversold_execution_robustness import (
    BASELINE,
    CONSERVATIVE,
    STANDARD,
    apply_exit_policy,
    exit_blocked,
    resolve_exit_date,
)


class DeepOversoldExecutionRobustnessTests(unittest.TestCase):
    def test_standard_defers_one_price_down(self):
        dates = ["20260105","20260106","20260107"]
        status = {
            ("20260105","A"): {
                "is_suspended":False,
                "is_one_price_down_limit":True,
                "is_close_at_down_limit":True,
            },
            ("20260106","A"): {
                "is_suspended":False,
                "is_one_price_down_limit":False,
                "is_close_at_down_limit":False,
            },
        }
        r = resolve_exit_date("20260105","A",dates,status,STANDARD)
        self.assertEqual(r["actual_exit"], "20260106")
        self.assertEqual(r["defer_sessions"], 1)

    def test_conservative_defers_close_at_down_limit(self):
        dates = ["20260105","20260106"]
        status = {
            ("20260105","A"): {
                "is_suspended":False,
                "is_one_price_down_limit":False,
                "is_close_at_down_limit":True,
            },
            ("20260106","A"): {
                "is_suspended":False,
                "is_one_price_down_limit":False,
                "is_close_at_down_limit":False,
            },
        }
        r = resolve_exit_date("20260105","A",dates,status,CONSERVATIVE)
        self.assertEqual(r["actual_exit"], "20260106")

    def test_baseline_never_defers(self):
        dates = ["20260105"]
        r = resolve_exit_date("20260105","A",dates,{},BASELINE)
        self.assertEqual(r["actual_exit"], "20260105")

    def test_actual_deferred_exit_extends_dedup_window(self):
        events = pd.DataFrame([
            {
                "symbol":"A","signal_date":"20260101",
                "entry_date":"20260102","exit_date":"20260105",
            },
            {
                "symbol":"A","signal_date":"20260106",
                "entry_date":"20260107","exit_date":"20260110",
            },
        ])
        status = pd.DataFrame([
            {
                "trade_date":"20260105","symbol":"A",
                "is_suspended":False,"is_one_price_down_limit":True,
                "is_close_at_down_limit":True,
            },
            {
                "trade_date":"20260106","symbol":"A",
                "is_suspended":False,"is_one_price_down_limit":True,
                "is_close_at_down_limit":True,
            },
            {
                "trade_date":"20260107","symbol":"A",
                "is_suspended":False,"is_one_price_down_limit":False,
                "is_close_at_down_limit":False,
            },
            {
                "trade_date":"20260110","symbol":"A",
                "is_suspended":False,"is_one_price_down_limit":False,
                "is_close_at_down_limit":False,
            },
        ])
        dates = ["20260105","20260106","20260107","20260110"]
        out, audit = apply_exit_policy(events, dates, status, STANDARD)
        self.assertEqual(len(out), 1)
        self.assertEqual(out.iloc[0]["exit_date"], "20260107")
        self.assertEqual(audit["deferred_exit_events"], 1)

    def test_resolution_calendar_may_extend_but_simulation_should_stop_at_actual_exit(self):
        # This is the exact class of bug fixed in v1.1:
        # extra flat days after the last actual exit must not be included in CAGR.
        resolution_dates = ["20260105","20260106","20260107","20260108"]
        events = pd.DataFrame([{
            "symbol":"A","signal_date":"20260101",
            "entry_date":"20260102","exit_date":"20260106",
        }])
        status = pd.DataFrame([
            {
                "trade_date":"20260106","symbol":"A",
                "is_suspended":False,"is_one_price_down_limit":False,
                "is_close_at_down_limit":False,
            },
        ])
        out, audit = apply_exit_policy(events, resolution_dates, status, BASELINE)
        self.assertEqual(out["exit_date"].max(), "20260106")
        sim_dates = [d for d in resolution_dates if d <= out["exit_date"].max()]
        self.assertEqual(sim_dates[-1], "20260106")
        self.assertNotIn("20260108", sim_dates)


if __name__ == "__main__":
    unittest.main()
