import sys
import unittest
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from opportunity.episode_anchor_audit import (
    extract_episode_anchors,
    audit_episode_anchors,
)


class T(unittest.TestCase):
    def sample(self):
        rows = []
        dates = pd.date_range("2025-01-01", periods=80, freq="D")
        for symbol in ["A.SH", "B.SH", "C.SH"]:
            for i, d in enumerate(dates):
                pos = symbol == "A.SH" and 35 <= i <= 40
                mfe = np.nan
                if pos:
                    mfe = [0.28, 0.31, 0.40, 0.36, 0.33, 0.30][i - 35]
                rows.append({
                    "symbol": symbol,
                    "trade_date": d.strftime("%Y%m%d"),
                    "TREND25_60_label": 1 if pos else 0,
                    "TREND25_60_mfe": mfe,
                    "rs20": 0.01 * i,
                    "rs_accel": 0.001 * i,
                    "vol_compression": 0.8 + 0.002 * i,
                    "volume_contraction": 0.9 + 0.001 * i,
                    "distance_to_60d_high": -0.2 + 0.002 * i,
                    "higher_low_20_vs_60": 0.01 + 0.001 * i,
                })
        return pd.DataFrame(rows)

    def test_anchor_offset(self):
        a = extract_episode_anchors(self.sample())
        self.assertEqual(len(a), 1)
        self.assertEqual(int(a.iloc[0]["max_mfe_offset_from_boundary"]), 2)

    def test_tie_break_is_earliest(self):
        df = self.sample()
        mask = (df["symbol"] == "A.SH") & (df["TREND25_60_label"] == 1)
        idx = df.index[mask].tolist()
        df.loc[idx[1], "TREND25_60_mfe"] = 0.5
        df.loc[idx[2], "TREND25_60_mfe"] = 0.5
        a = extract_episode_anchors(df)
        self.assertEqual(int(a.iloc[0]["max_mfe_offset_from_boundary"]), 1)

    def test_audit_has_both_trajectories(self):
        r, export = audit_episode_anchors(self.sample())
        self.assertIn("boundary_anchor_trajectory", r)
        self.assertIn("max_mfe_anchor_trajectory", r)
        self.assertEqual(r["cohort"]["episodes"], 1)
        self.assertEqual(len(export), 1)

    def test_no_new_thresholds_in_semantics(self):
        r, _ = audit_episode_anchors(self.sample())
        self.assertIn("No new detector", r["semantics"]["purpose"])

    def test_source_feature_trajectory_shape(self):
        r, _ = audit_episode_anchors(self.sample())
        self.assertIn("rs20", r["boundary_anchor_trajectory"])
        self.assertIn("d_m5_0", r["boundary_anchor_trajectory"]["rs20"])
        self.assertIn("post_d_0_10", r["max_mfe_anchor_trajectory"]["rs20"])


if __name__ == "__main__":
    unittest.main()
