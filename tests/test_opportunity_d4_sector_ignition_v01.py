import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from opportunity.d4_sector_ignition import (
    add_sector_ignition_features,
    attach_pit_industry,
    evaluate_d4,
)


class T(unittest.TestCase):
    def test_interval_join(self):
        panel = pd.DataFrame({
            "trade_date": ["20210729", "20210730", "20220101"],
            "symbol": ["A.SZ", "A.SZ", "A.SZ"],
        })
        membership = pd.DataFrame({
            "symbol": ["A.SZ", "A.SZ"],
            "effective_from": [pd.Timestamp("2014-02-21"), pd.Timestamp("2021-07-30")],
            "effective_to_exclusive": [pd.Timestamp("2021-07-30"), pd.NaT],
            "industry_code": ["OLD", "NEW"],
            "industry_name": ["OldIndustry", "NewIndustry"],
            "standard": ["SW2014", "SW2021"],
            "level": ["L1", "L1"],
        })
        z = attach_pit_industry(panel, membership)
        self.assertEqual(z["industry_code"].tolist(), ["OLD", "NEW", "NEW"])

    def test_sector_key_separates_taxonomy_versions(self):
        panel = pd.DataFrame({
            "trade_date": ["20210729", "20210730"],
            "symbol": ["A.SZ", "A.SZ"],
        })
        membership = pd.DataFrame({
            "symbol": ["A.SZ", "A.SZ"],
            "effective_from": [pd.Timestamp("2014-02-21"), pd.Timestamp("2021-07-30")],
            "effective_to_exclusive": [pd.Timestamp("2021-07-30"), pd.NaT],
            "industry_code": ["10", "10"],
            "industry_name": ["A", "B"],
            "standard": ["SW2014", "SW2021"],
            "level": ["L1", "L1"],
        })
        z = attach_pit_industry(panel, membership)
        self.assertNotEqual(z.loc[0, "sector_key"], z.loc[1, "sector_key"])

    def test_signal_subset_of_second_tier_setup(self):
        dates = pd.date_range("2024-01-01", periods=170, freq="D").strftime("%Y%m%d")
        rows = []
        for di, d in enumerate(dates):
            for si in range(20):
                rows.append({
                    "trade_date": d,
                    "symbol": f"S{si}",
                    "sector_key": "SW2021|L1|10",
                    "ret20": si / 100.0,
                    "above_ma20": si < (4 + di % 16),
                })
        z = add_sector_ignition_features(
            pd.DataFrame(rows),
            sector_min_members=10,
            history_sessions=20,
        )
        self.assertTrue((~z["d4_signal"] | z["second_tier_setup"]).all())

    def test_current_day_excluded_from_threshold(self):
        dates = pd.date_range("2024-01-01", periods=180, freq="D").strftime("%Y%m%d")
        rows = []
        for di, d in enumerate(dates):
            for si in range(20):
                rows.append({
                    "trade_date": d,
                    "symbol": f"S{si}",
                    "sector_key": "SW2021|L1|10",
                    "ret20": si / 100.0,
                    "above_ma20": si < (2 + di % 18),
                })
        z = add_sector_ignition_features(
            pd.DataFrame(rows),
            history_sessions=20,
        )
        self.assertGreater(z["sector_excess_prior_q75"].notna().sum(), 0)

    def test_evaluation_baseline_is_second_tier(self):
        rows = []
        for y in range(2020, 2026):
            for i in range(20):
                rows.append({
                    "trade_date": f"{y}01{i+1:02d}",
                    "second_tier_setup": True,
                    "d4_signal": i < 5,
                    "D4_TREND25_60_label": 1 if i < 4 else 0,
                })
        r = evaluate_d4(
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
