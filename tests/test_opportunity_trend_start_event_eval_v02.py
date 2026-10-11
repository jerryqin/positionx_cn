import sys
import unittest
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from opportunity.trend_start_event_eval import evaluate_event_onsets, reconstruct_frozen_v01_conditions


class T(unittest.TestCase):
    def sample(self):
        rows = []
        # One symbol, two favorable-path episodes.
        labels = [0,0,1,1,1,0,0,1,1,0]
        signals = [0,1,1,0,0,0,0,0,1,0]
        scores =  [1,2,4,3,2,1,1,2,4,1]
        for i in range(10):
            rows.append({
                "symbol":"A.SH",
                "trade_date":f"202501{i+1:02d}",
                "TREND25_60_label":labels[i],
                "trend_ignition_v01":bool(signals[i]),
                "ignition_score_0_5":scores[i],
                "c_rs_accel": scores[i] >= 2,
                "c_vol_compress": scores[i] >= 3,
                "c_volume_contract": scores[i] >= 4,
                "c_near_breakout": scores[i] >= 2,
                "c_higher_low": scores[i] >= 4,
            })
        return pd.DataFrame(rows)

    def test_episode_count(self):
        r = evaluate_event_onsets(self.sample(), early_window_sessions=2)
        self.assertEqual(r["event_metrics"]["episodes"], 2)

    def test_signal_runs(self):
        r = evaluate_event_onsets(self.sample(), early_window_sessions=2)
        self.assertEqual(r["event_metrics"]["signal_runs"], 2)

    def test_both_episodes_matched(self):
        r = evaluate_event_onsets(self.sample(), early_window_sessions=2)
        self.assertEqual(r["event_metrics"]["matched_episodes"], 2)
        self.assertAlmostEqual(r["event_metrics"]["event_recall"], 1.0)

    def test_signal_run_start_before_onset_can_match_if_overlaps(self):
        r = evaluate_event_onsets(self.sample(), early_window_sessions=2)
        self.assertLessEqual(r["event_metrics"]["median_signal_offset_sessions"], 0.0)

    def test_reconstruct_conditions_from_numeric_features(self):
        df = pd.DataFrame({
            "rs_accel":[0.1],
            "vol_compression":[0.8],
            "volume_contraction":[0.8],
            "distance_to_60d_high":[-0.02],
            "higher_low_20_vs_60":[0.03],
        })
        y = reconstruct_frozen_v01_conditions(df)
        self.assertTrue(bool(y.loc[0, "c_rs_accel"]))
        self.assertTrue(bool(y.loc[0, "c_vol_compress"]))
        self.assertTrue(bool(y.loc[0, "c_volume_contract"]))
        self.assertTrue(bool(y.loc[0, "c_near_breakout"]))
        self.assertTrue(bool(y.loc[0, "c_higher_low"]))

    def test_existing_condition_columns_do_not_require_numeric_features(self):
        df = pd.DataFrame({
            "c_rs_accel":[True],
            "c_vol_compress":[False],
            "c_volume_contract":[True],
            "c_near_breakout":[False],
            "c_higher_low":[True],
        })
        y = reconstruct_frozen_v01_conditions(df)
        self.assertTrue(bool(y.loc[0, "c_rs_accel"]))
        self.assertFalse(bool(y.loc[0, "c_vol_compress"]))

    def test_condition_diagnostic_present(self):
        r = evaluate_event_onsets(self.sample(), early_window_sessions=2)
        self.assertIn("c_rs_accel", r["condition_diagnostic"])


if __name__ == "__main__":
    unittest.main()
