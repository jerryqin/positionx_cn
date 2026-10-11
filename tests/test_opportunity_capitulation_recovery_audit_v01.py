import sys
import unittest
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from opportunity.capitulation_recovery_audit import (
    add_sequence_features,
    attach_same_day_ranks,
    nearest_control_matches,
    evaluate_capitulation_recovery,
)


class T(unittest.TestCase):
    def bars(self, symbol="A.SH", n=50):
        close = np.linspace(10.0, 11.0, n)
        return pd.DataFrame({
            "symbol": symbol,
            "trade_date": pd.date_range("2025-01-01", periods=n, freq="D").strftime("%Y%m%d"),
            "open": close * 0.995,
            "high": close * 1.02,
            "low": close * 0.98,
            "close": close,
            "pre_close": np.r_[close[0], close[:-1]],
            "volume": np.linspace(100, 140, n),
        })

    def test_sequence_future_columns(self):
        z = add_sequence_features(self.bars())
        for c in [
            "gap_1","fwd_close_ret_1","fwd_close_ret_3",
            "next1_intraday_return","next1_close_location",
            "close_above_t_close_1","close_above_t_high_3",
        ]:
            self.assertIn(c, z.columns)

    def test_future_return_identity(self):
        z = add_sequence_features(self.bars())
        r = z.iloc[20]
        expected = z.iloc[21]["adj_close"] / z.iloc[20]["adj_close"] - 1.0
        self.assertAlmostEqual(float(r["fwd_close_ret_1"]), float(expected))

    def mini_rows(self):
        rows = []
        for i, sym in enumerate(["E.SH","C1.SH","C2.SH","C3.SH","C4.SH","C5.SH","X.SH"]):
            rows.append({
                "trade_date":"20250110",
                "symbol":sym,
                "TREND25_60_label":1 if sym=="E.SH" else 0,
                "ret1":-0.05 + 0.001*i,
                "intraday_return":-0.04 + 0.001*i,
                "close_location":0.15 + 0.01*i,
                "range_vs_prior20":1.3 - 0.01*i,
                "gap_1":0.0,
                "fwd_close_ret_1":0.03 if sym=="E.SH" else 0.0,
                "fwd_close_ret_2":0.04 if sym=="E.SH" else 0.0,
                "fwd_close_ret_3":0.05 if sym=="E.SH" else 0.0,
                "fwd_close_ret_5":0.06 if sym=="E.SH" else 0.0,
                "next1_intraday_return":0.02 if sym=="E.SH" else 0.0,
                "next1_close_location":0.8 if sym=="E.SH" else 0.5,
                "close_above_t_close_1":sym=="E.SH",
                "close_above_t_high_1":False,
                "close_above_t_high_3":sym=="E.SH",
            })
        return pd.DataFrame(rows)

    def test_nearest_five(self):
        x = attach_same_day_ranks(self.mini_rows())
        events = x[x["symbol"]=="E.SH"]
        controls = x[x["TREND25_60_label"]==0]
        m = nearest_control_matches(events, controls, k=5)
        self.assertEqual(len(m), 5)
        self.assertEqual(m["neighbor_rank"].tolist(), [1,2,3,4,5])

    def test_full_audit_shape(self):
        seq = self.mini_rows().drop(columns=["TREND25_60_label"])
        panel = self.mini_rows()[["trade_date","symbol","TREND25_60_label"]]
        result, matches = evaluate_capitulation_recovery(panel, seq)
        self.assertEqual(result["cohort"]["events_available"], 1)
        self.assertEqual(len(matches), 5)
        self.assertIn("fwd_close_ret_1", result["continuous_recovery"])
        self.assertIn("close_above_t_close_1", result["boolean_recovery"])

    def test_matching_uses_only_t_shock_dims(self):
        # Contract guard: matching dimensions are T-known shock variables.
        from opportunity.capitulation_recovery_audit import SHOCK_DIMS
        self.assertEqual(
            SHOCK_DIMS,
            ("ret1","intraday_return","close_location","range_vs_prior20")
        )


if __name__ == "__main__":
    unittest.main()
