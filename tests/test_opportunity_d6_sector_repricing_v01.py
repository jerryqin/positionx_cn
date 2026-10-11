import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from opportunity.d6_sector_repricing import (
    add_sector_repricing_features,
    evaluate_d6,
)


class T(unittest.TestCase):
    def make_panel(self, periods=180, members=20):
        dates = pd.date_range("2024-01-01", periods=periods, freq="D").strftime("%Y%m%d")
        rows = []
        for di, d in enumerate(dates):
            sector_boost = (di % 19) / 100.0
            for si in range(members):
                rows.append({
                    "trade_date": d,
                    "symbol": f"A{si:02d}",
                    "sector_key": "SW2021|L1|10",
                    "ret20": si / 100.0 + sector_boost,
                })
            for si in range(members):
                rows.append({
                    "trade_date": d,
                    "symbol": f"B{si:02d}",
                    "sector_key": "SW2021|L1|20",
                    "ret20": si / 100.0,
                })
        return pd.DataFrame(rows)

    def test_signal_subset_of_follower_setup(self):
        z = add_sector_repricing_features(
            self.make_panel(),
            history_sessions=20,
        )
        self.assertTrue((~z["d6_signal"] | z["follower_setup_d6"]).all())

    def test_current_day_excluded_threshold_exists(self):
        z = add_sector_repricing_features(
            self.make_panel(),
            history_sessions=20,
        )
        self.assertGreater(z["sector_repricing_prior_q75"].notna().sum(), 0)

    def test_follower_band(self):
        z = add_sector_repricing_features(
            self.make_panel(periods=5),
            history_sessions=2,
        )
        f = z[z["follower_setup_d6"]]
        self.assertTrue((f["sector_ret20_rank_d6"] >= 0.40).all())
        self.assertTrue((f["sector_ret20_rank_d6"] < 0.70).all())

    def test_excess_return_distinguishes_sector(self):
        z = add_sector_repricing_features(
            self.make_panel(periods=10),
            history_sessions=2,
        )
        day = z["trade_date"].iloc[-1]
        a = z[(z["trade_date"] == day) & (z["sector_key"] == "SW2021|L1|10")]
        b = z[(z["trade_date"] == day) & (z["sector_key"] == "SW2021|L1|20")]
        self.assertGreater(
            float(a["sector_excess_ret20_d6"].iloc[0]),
            float(b["sector_excess_ret20_d6"].iloc[0]),
        )

    def test_evaluation_baseline_is_followers(self):
        rows = []
        for y in range(2020, 2026):
            for i in range(20):
                rows.append({
                    "trade_date": f"{y}01{i+1:02d}",
                    "follower_setup_d6": True,
                    "d6_signal": i < 5,
                    "D4_TREND25_60_label": 1 if i < 4 else 0,
                })
        r = evaluate_d6(
            pd.DataFrame(rows),
            "D4_TREND25_60_label",
            selected_min=1,
            lift_min=1.0,
            precision_delta_min=0.0,
            positive_years_min=1,
        )
        self.assertEqual(r["setup_count"], 120)
        self.assertEqual(r["signal_count"], 30)
        self.assertGreater(r["signal_rate"], r["setup_rate"])


if __name__ == "__main__":
    unittest.main()
