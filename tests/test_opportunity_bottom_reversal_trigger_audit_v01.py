import sys
import unittest
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from opportunity.bottom_reversal_trigger_audit import (
    add_trigger_features,
    build_boundary_anchors,
    evaluate_bottom_reversal_trigger_audit,
)


class T(unittest.TestCase):
    def bars(self, symbol="A.SH", n=50):
        close = np.linspace(10.0, 11.0, n)
        df = pd.DataFrame({
            "symbol": symbol,
            "trade_date": pd.date_range("2025-01-01", periods=n, freq="D").strftime("%Y%m%d"),
            "open": close * 0.995,
            "high": close * 1.02,
            "low": close * 0.98,
            "close": close,
            "pre_close": np.r_[close[0], close[:-1]],
            "volume": np.linspace(100, 140, n),
        })
        return df

    def test_trigger_columns(self):
        z = add_trigger_features(self.bars())
        for c in [
            "ret1", "prior5_return", "close_location",
            "lower_wick_share", "range_vs_prior20", "volume_vs_prior20",
            "failed_breakdown_5d", "failed_breakdown_20d",
        ]:
            self.assertIn(c, z.columns)

    def test_failed_breakdown_reclaim(self):
        df = self.bars()
        i = 30
        prior5_low = df.loc[i-5:i-1, "low"].min()
        df.loc[i, "low"] = prior5_low * 0.98
        df.loc[i, "open"] = prior5_low * 0.99
        df.loc[i, "close"] = prior5_low * 1.01
        df.loc[i, "high"] = prior5_low * 1.03
        z = add_trigger_features(df)
        self.assertTrue(bool(z.loc[z["trade_date"] == df.loc[i, "trade_date"], "failed_breakdown_5d"].iloc[0]))

    def test_boundary_anchor(self):
        p = pd.DataFrame({
            "symbol": ["A.SH"] * 6,
            "trade_date": [f"2025010{i}" for i in range(1, 7)],
            "TREND25_60_label": [0, 0, 1, 1, 0, 1],
        })
        a = build_boundary_anchors(p)
        self.assertEqual(a["trade_date"].tolist(), ["20250103", "20250106"])

    def test_audit_shape(self):
        rows = []
        for sym in ["A.SH", "B.SH"]:
            for i in range(30):
                rows.append({
                    "trade_date": f"2025{i//20+1:02d}{i%20+1:02d}",
                    "symbol": sym,
                    "TREND25_60_label": 1 if (sym == "A.SH" and i in [10,11]) else 0,
                })
        panel = pd.DataFrame(rows)
        trig = panel[["trade_date","symbol"]].copy()
        for c in [
            "ret1","prior5_return","intraday_return","close_location",
            "lower_wick_share","upper_wick_share","range_vs_prior20",
            "volume_vs_prior20","recovery_from_low","distance_to_prior20_low"
        ]:
            trig[c] = np.linspace(-1,1,len(trig))
        for c in [
            "failed_breakdown_5d","failed_breakdown_20d",
            "bullish_reversal_day","lower_wick_dominant"
        ]:
            trig[c] = False
        result, events = evaluate_bottom_reversal_trigger_audit(panel, trig)
        self.assertEqual(result["cohort"]["events"], 1)
        self.assertIn("close_location", result["continuous_metrics"])
        self.assertIn("failed_breakdown_5d", result["boolean_patterns"])
        self.assertEqual(len(events), 1)

    def test_no_future_feature_names(self):
        z = add_trigger_features(self.bars())
        self.assertFalse(any("future" in c.lower() for c in z.columns))


if __name__ == "__main__":
    unittest.main()
