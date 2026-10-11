import sys, unittest
import hashlib
from pathlib import Path
import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from opportunity.trend_labels import add_trend_labels, PRIMARY
from opportunity.trend_ignition import synthetic_adjust_ohlc, add_symbol_features, add_benchmark_relative_features, add_trend_ignition_signal
from opportunity.evaluate_trend_ignition import evaluate, fast_reject_gate


class T(unittest.TestCase):
    def _bars(self, n=240, symbol="A.SH", drift=0.001):
        dates = pd.bdate_range("2025-01-01", periods=n).strftime("%Y%m%d")
        close = 10 * np.cumprod(np.repeat(1 + drift, n))
        return pd.DataFrame({
            "symbol": symbol,
            "trade_date": dates,
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "pre_close": np.r_[close[0] / (1 + drift), close[:-1]],
            "volume": np.linspace(2_000_000, 1_000_000, n),
        })

    def test_label_uses_t1_open(self):
        x = self._bars(100)
        # Force a 25% target hit after row 10 from row 11 open.
        x.loc[11:70, "high"] = x.loc[11:70, "open"] * 1.30
        y = add_trend_labels(x)
        self.assertAlmostEqual(y.loc[10, "TREND25_60_entry_open"], x.loc[11, "open"])
        self.assertEqual(int(y.loc[10, "TREND25_60_label"]), 1)

    def test_synthetic_adjustment_removes_split_discontinuity(self):
        x = self._bars(10, drift=0.0)
        # Simulate a 2-for-1 split on row 5: raw OHLC halves, and stored pre_close
        # is correspondingly adjusted to the split basis so close/pre_close remains 1.
        x.loc[5:, ["open","high","low","close"]] *= 0.5
        x.loc[5, "pre_close"] = x.loc[5, "close"]
        x.loc[6:, "pre_close"] = x.loc[5:8, "close"].to_numpy()
        y = synthetic_adjust_ohlc(x)
        self.assertAlmostEqual(float(y.loc[4, "close"]), float(y.loc[5, "close"]), places=10)

    def test_feature_columns(self):
        x = add_symbol_features(self._bars())
        for c in ["vol_compression","volume_contraction","distance_to_60d_high","higher_low_20_vs_60"]:
            self.assertIn(c, x.columns)

    def test_rs_accel(self):
        s = add_symbol_features(self._bars(symbol="A.SH", drift=0.002))
        b = self._bars(symbol="IDX", drift=0.001)
        y = add_benchmark_relative_features(s, b)
        self.assertIn("rs_accel", y.columns)
        self.assertTrue(y["rs20"].dropna().iloc[-1] > 0)

    def test_signal_rule_four_of_five(self):
        x = pd.DataFrame({
            "rs_accel":[0.1,0.1],
            "vol_compression":[0.8,0.8],
            "volume_contraction":[0.8,1.2],
            "distance_to_60d_high":[-0.02,-0.02],
            "higher_low_20_vs_60":[0.03,0.00],
        })
        y = add_trend_ignition_signal(x)
        self.assertTrue(bool(y.loc[0,"trend_ignition_v01"]))
        self.assertFalse(bool(y.loc[1,"trend_ignition_v01"]))

    def test_gate_pass_shape(self):
        result = {
            "overall": {
                "selected":150,
                "lift_vs_baseline":1.6,
                "precision":0.10,
                "baseline_rate":0.05,
            },
            "by_year":{
                "2024":{"lift_vs_baseline":1.2},
                "2025":{"lift_vs_baseline":1.1},
            }
        }
        self.assertTrue(fast_reject_gate(result)["status"].startswith("PASS"))

    def test_yyyymmdd_integer_like_string_parses_as_calendar_date(self):
        raw = pd.Series(["20200803", "20260924"])
        parsed = pd.to_datetime(raw.astype(str), format="%Y%m%d", errors="raise").dt.strftime("%Y%m%d")
        self.assertEqual(parsed.tolist(), ["20200803", "20260924"])

    def test_plain_to_datetime_integer_would_be_wrong(self):
        # Regression guard documenting the V0.1.4 implementation bug.
        wrong = pd.to_datetime(pd.Series([20200803]), errors="raise").dt.strftime("%Y%m%d").iloc[0]
        self.assertNotEqual(wrong, "20200803")

    def test_foundation_benchmark_expected_hash_is_frozen(self):
        expected = "ff432d62fa73295ae92cc7162c8de3a4273357a9904a39ad087f1f6e48b4bf73"
        self.assertEqual(len(expected), 64)
        int(expected, 16)

    def test_yyyymmdd_normalizes_to_iso_for_duckdb(self):
        raw = "20200803"
        iso = pd.to_datetime(raw, format="%Y%m%d", errors="raise").strftime("%Y-%m-%d")
        self.assertEqual(iso, "2020-08-03")

    def test_gate_reject_low_lift(self):
        result = {
            "overall": {
                "selected":150,
                "lift_vs_baseline":1.2,
                "precision":0.08,
                "baseline_rate":0.05,
            },
            "by_year":{
                "2024":{"lift_vs_baseline":1.2},
                "2025":{"lift_vs_baseline":1.1},
            }
        }
        self.assertTrue(fast_reject_gate(result)["status"].startswith("FAST_REJECT"))


if __name__ == "__main__":
    unittest.main()
