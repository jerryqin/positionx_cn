import sys
import unittest
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from opportunity.d1_capitulation_recovery import (
    add_forward_labels_from_t4,
    add_same_day_shock_score,
    add_t_features,
    evaluate_d1,
    mark_setup_and_signal,
)


class T(unittest.TestCase):
    def bars(self, symbol="A.SH", n=150):
        close = np.linspace(10.0, 13.0, n)
        return pd.DataFrame({
            "symbol": symbol,
            "trade_date": pd.date_range("2025-01-01", periods=n, freq="D").strftime("%Y%m%d"),
            "open": close * 0.995,
            "high": close * 1.02,
            "low": close * 0.98,
            "close": close,
            "pre_close": np.r_[close[0], close[:-1]],
            "volume": np.linspace(100, 150, n),
        })

    def test_t_features_have_fixed_t3_t4(self):
        z = add_t_features(self.bars())
        r = z.iloc[40]
        self.assertAlmostEqual(
            float(r["recovery_3d"]),
            float(z.iloc[43]["adj_close"] / z.iloc[40]["adj_close"] - 1.0),
        )
        self.assertAlmostEqual(float(r["open_t4"]), float(z.iloc[44]["adj_open"]))

    def test_shock_score_direction(self):
        df = pd.DataFrame({
            "trade_date": ["20250101"] * 3,
            "ret1": [-0.10, 0.0, 0.10],
            "intraday_return": [-0.10, 0.0, 0.10],
            "close_location": [0.1, 0.5, 0.9],
            "range_vs_prior20": [2.0, 1.0, 0.5],
        })
        z = add_same_day_shock_score(df)
        self.assertGreater(z.loc[0, "capitulation_score"], z.loc[2, "capitulation_score"])

    def test_frozen_setup_and_confirmation(self):
        df = pd.DataFrame({
            "capitulation_score": [0.80, 0.80, 0.70],
            "recovery_3d": [0.01, -0.01, 0.02],
        })
        z = mark_setup_and_signal(df, 0.75)
        self.assertEqual(z["d1_signal"].tolist(), [True, False, False])

    def test_labels_are_from_t4_open(self):
        z = add_t_features(self.bars())
        z = add_forward_labels_from_t4(z)
        # At row 20, entry is row 24 open. Verify stored MFE uses row24 open denominator.
        i = 20
        entry = float(z.iloc[i + 4]["adj_open"])
        max_close = float(z.iloc[i + 4:i + 4 + 60]["adj_close"].max())
        expected = max_close / entry - 1.0
        self.assertAlmostEqual(float(z.iloc[i]["D1_TREND25_60_mfe"]), expected)

    def test_evaluation_baseline_is_setup(self):
        rows = []
        for y in range(2020, 2026):
            for i in range(10):
                rows.append({
                    "trade_date": f"{y}010{i}",
                    "d1_setup": True,
                    "d1_signal": i < 5,
                    "D1_TREND25_60_label": 1 if i < 4 else 0,
                    "D1_TREND25_60_mfe": 0.30,
                    "D1_TREND25_60_mae_before_hit": -0.05,
                    "D1_TREND25_60_time_to_target": 20.0,
                })
        df = pd.DataFrame(rows)
        r = evaluate_d1(
            df, "D1_TREND25_60_label",
            selected_min=1, lift_min=1.0, precision_delta_min=0.0, positive_years_min=1
        )
        self.assertEqual(r["setup_count"], 60)
        self.assertEqual(r["signal_count"], 30)
        self.assertGreater(r["signal_rate"], r["setup_rate"])


if __name__ == "__main__":
    unittest.main()
