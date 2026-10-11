import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from opportunity.trend_onset_transition import (
    FEATURES,
    add_episode_onsets,
    add_event_window_features,
    evaluate_transition_study,
)


class T(unittest.TestCase):
    def sample(self):
        rows = []
        dates = pd.date_range("2025-01-01", periods=80, freq="D")
        for symbol, bump in [("A.SH", 1.0), ("B.SH", 0.0), ("C.SH", -0.2)]:
            labels = np.zeros(80)
            if symbol == "A.SH":
                labels[40:46] = 1
            base = np.linspace(0, 1, 80) + bump
            for i, d in enumerate(dates):
                rows.append({
                    "symbol": symbol,
                    "trade_date": d.strftime("%Y%m%d"),
                    "TREND25_60_label": int(labels[i]),
                    "rs20": base[i] + (0.5 if symbol == "A.SH" and i >= 35 else 0),
                    "rs_accel": (0.05 if symbol == "A.SH" and i >= 35 else -0.01),
                    "vol_compression": 0.8 + 0.01*i + (0.15 if symbol == "A.SH" and i >= 36 else 0),
                    "volume_contraction": 0.7 + 0.005*i + (0.20 if symbol == "A.SH" and i >= 38 else 0),
                    "distance_to_60d_high": -0.2 + 0.002*i + (0.05 if symbol == "A.SH" and i >= 35 else 0),
                    "higher_low_20_vs_60": 0.01 + 0.001*i + (0.03 if symbol == "A.SH" and i >= 35 else 0),
                })
        return pd.DataFrame(rows)

    def test_one_episode_onset(self):
        x = add_episode_onsets(self.sample())
        a = x[x["symbol"] == "A.SH"]
        self.assertEqual(int(a["trend_episode_onset"].sum()), 1)

    def test_offset_direction(self):
        x = add_event_window_features(add_episode_onsets(self.sample()))
        a = x[(x["symbol"] == "A.SH") & (x["trade_date"] == "20250210")].iloc[0]
        self.assertAlmostEqual(
            float(a["rs20__t-5"]),
            float(self.sample().query("symbol=='A.SH' and trade_date=='20250205'")["rs20"].iloc[0]),
        )

    def test_curve_identity(self):
        x = add_event_window_features(add_episode_onsets(self.sample()))
        r = x[(x["symbol"] == "A.SH") & (x["trade_date"] == "20250210")].iloc[0]
        expected = (r["rs20__t+0"] - r["rs20__t-5"]) - (r["rs20__t-5"] - r["rs20__t-10"])
        self.assertAlmostEqual(float(r["rs20__curve_5"]), float(expected))

    def test_study_has_controls_and_events(self):
        result, events = evaluate_transition_study(self.sample())
        self.assertEqual(result["cohort"]["episodes_total"], 1)
        self.assertGreater(result["cohort"]["controls_pre_window"], 0)
        self.assertEqual(len(events), 1)

    def test_transition_shortlist_is_diagnostic_only(self):
        result, _ = evaluate_transition_study(self.sample())
        for row in result["transition_shortlist"]:
            self.assertEqual(
                row["research_effect"],
                "SHORTLIST_FOR_NEW_INDEPENDENT_MECHANISM_ONLY",
            )

    def test_future_offsets_separated(self):
        result, _ = evaluate_transition_study(self.sample())
        self.assertIn("post_onset_descriptive", result)
        self.assertNotIn("rs20__post_d_0_5", result["transition_metrics"])


if __name__ == "__main__":
    unittest.main()
