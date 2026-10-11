import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from opportunity.d1_absorption import (
    add_absorption_features,
    add_forward_labels_from_t1,
    evaluate_absorption,
    mark_pressure_and_absorption,
)


class T(unittest.TestCase):
    def bars(self, symbol="A.SH", n=160):
        close = np.linspace(10.0, 13.0, n)
        close = close * (1.0 + 0.003 * np.sin(np.arange(n)))
        return pd.DataFrame({
            "symbol": symbol,
            "trade_date": pd.date_range("2025-01-01", periods=n, freq="D").strftime("%Y%m%d"),
            "open": close * 0.995,
            "high": close * 1.02,
            "low": close * 0.98,
            "close": close,
            "pre_close": np.r_[close[0], close[:-1]],
            "volume": 100.0 + 20.0 * (1 + np.sin(np.arange(n) / 3.0)),
        })

    def test_absorption_features_exist(self):
        z = add_absorption_features(self.bars())
        for c in ["volume_ratio20", "down_volume_intensity_5", "downside_damage_5", "damage_per_pressure_5"]:
            self.assertIn(c, z.columns)

    def test_low_damage_high_pressure_gets_higher_absorption_score(self):
        df = pd.DataFrame({
            "trade_date": ["20250101"] * 8,
            "down_volume_intensity_5": [8,7,6,5,4,3,2,1],
            "damage_per_pressure_5": [0.01,0.02,0.03,0.04,0.05,0.06,0.07,0.08],
        })
        z = mark_pressure_and_absorption(df, 0.75, 0.75)
        setup = z[z["pressure_setup"]]
        self.assertGreaterEqual(len(setup), 2)
        self.assertEqual(setup["absorption_score"].idxmax(), 0)

    def test_labels_are_from_t1_open(self):
        z = add_absorption_features(self.bars())
        z = add_forward_labels_from_t1(z)
        i = 30
        entry = float(z.iloc[i + 1]["adj_open"])
        max_close = float(z.iloc[i + 1:i + 1 + 60]["adj_close"].max())
        expected = max_close / entry - 1.0
        self.assertAlmostEqual(float(z.iloc[i]["D1A_TREND25_60_mfe"]), expected)

    def test_evaluation_baseline_is_pressure_setup(self):
        rows = []
        for y in range(2020, 2026):
            for i in range(12):
                rows.append({
                    "trade_date": f"{y}01{i+1:02d}",
                    "pressure_setup": True,
                    "d1_absorption_signal": i < 4,
                    "D1A_TREND25_60_label": 1 if i < 3 else 0,
                    "D1A_TREND25_60_mfe": 0.30,
                    "D1A_TREND25_60_mae_before_hit": -0.05,
                    "D1A_TREND25_60_time_to_target": 20.0,
                })
        df = pd.DataFrame(rows)
        r = evaluate_absorption(df, "D1A_TREND25_60_label", selected_min=1, lift_min=1.0, precision_delta_min=0.0, positive_years_min=1)
        self.assertEqual(r["pressure_setup_count"], 72)
        self.assertEqual(r["absorption_signal_count"], 24)
        self.assertGreater(r["absorption_signal_rate"], r["pressure_setup_rate"])

    def test_signal_is_subset_of_pressure_setup(self):
        df = pd.DataFrame({
            "trade_date": ["20250101"] * 12,
            "down_volume_intensity_5": np.arange(12, 0, -1, dtype=float),
            "damage_per_pressure_5": np.linspace(0.01, 0.12, 12),
        })
        z = mark_pressure_and_absorption(df, 0.75, 0.75)
        self.assertTrue((~z["d1_absorption_signal"] | z["pressure_setup"]).all())


if __name__ == "__main__":
    unittest.main()
