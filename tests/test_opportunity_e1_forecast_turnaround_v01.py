import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from opportunity.e1_forecast_turnaround import (
    report_period_bucket,
    prepare_event_universe,
    synthetic_adjust_ohlc,
    attach_next_tradable_entry,
    _event_label,
    evaluate_e1,
)


class T(unittest.TestCase):
    def test_report_period_bucket(self):
        s = pd.Series(["2024_ANNUAL", "2025_H1", "2025_Q1", "2025_Q1_Q3", None])
        self.assertEqual(
            report_period_bucket(s).tolist(),
            ["ANNUAL", "H1", "Q1", "Q1_Q3", "OTHER"],
        )

    def test_turnaround_signal(self):
        df = pd.DataFrame({
            "event_time": ["2025-01-01", "2025-01-02"],
            "strategy_eligible_semantics": [True, True],
            "event_kind": ["ORIGINAL_FORECAST", "ORIGINAL_FORECAST"],
            "change_type": ["TURNAROUND", "IMPROVING"],
            "report_period": ["2024_ANNUAL", "2024_ANNUAL"],
        })
        z = prepare_event_universe(df)
        self.assertEqual(z["e1_setup"].tolist(), [True, True])
        self.assertEqual(z["e1_signal"].tolist(), [True, False])

    def test_next_tradable_is_strictly_after_event_date(self):
        events = pd.DataFrame({
            "symbol": ["A"],
            "event_date": [pd.Timestamp("2025-01-02")],
            "e1_setup": [True],
        })
        bars = pd.DataFrame({
            "symbol": ["A", "A"],
            "trade_date": [pd.Timestamp("2025-01-02"), pd.Timestamp("2025-01-03")],
            "adj_open": [1.0, 1.1],
        })
        z = attach_next_tradable_entry(events, bars)
        self.assertEqual(z.loc[0, "entry_date"], pd.Timestamp("2025-01-03"))

    def test_event_label_positive_before_full_horizon(self):
        g = pd.DataFrame({
            "adj_high": [1.0, 1.30, 1.10],
            "adj_low": [0.98, 0.95, 0.90],
        })
        self.assertEqual(_event_label(g, 0, 1.0, 0.25, 60, -0.10), 1)

    def test_event_label_negative_requires_full_horizon(self):
        g = pd.DataFrame({
            "adj_high": [1.0, 1.05, 1.10],
            "adj_low": [0.98, 0.97, 0.96],
        })
        self.assertTrue(pd.isna(_event_label(g, 0, 1.0, 0.25, 60, -0.10)))

    def test_evaluate_matched_gate(self):
        rows = []
        for y in range(2020, 2026):
            for i in range(100):
                sig = i < 20
                label = 1 if (sig and i < 8) or ((not sig) and i < 15) else 0
                rows.append({
                    "event_year": y,
                    "report_period_bucket": "ANNUAL",
                    "e1_setup": True,
                    "e1_signal": sig,
                    "E1_TREND25_60_label": label,
                })
        r = evaluate_e1(
            pd.DataFrame(rows),
            "E1_TREND25_60_label",
            selected_min=100,
            lift_min=1.25,
            precision_delta_min=0.03,
            positive_years_min=5,
            min_setup_per_stratum=30,
        )
        self.assertGreater(r["signal_rate_matched"], r["matched_setup_rate"])
        self.assertGreaterEqual(r["positive_years"], 5)


if __name__ == "__main__":
    unittest.main()
