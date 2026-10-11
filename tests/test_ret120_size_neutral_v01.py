import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audit_ret120_size_neutral_v01 as audit


class Ret120SizeNeutralV01Tests(unittest.TestCase):

    def test_zscore(self):
        z = audit.zscore(np.array([1.0, 2.0, 3.0]))
        self.assertAlmostEqual(float(z.mean()), 0.0)
        self.assertAlmostEqual(float(z.std(ddof=0)), 1.0)

    def test_ols_beta_recovers_signal(self):
        x = np.arange(20, dtype=float)
        y = 2.0 + 3.0 * x
        beta = audit.ols_beta(y, [x])
        self.assertAlmostEqual(beta[0], 2.0, places=10)
        self.assertAlmostEqual(beta[1], 3.0, places=10)

    def test_residualize_removes_linear_size(self):
        size = np.arange(30, dtype=float)
        x = 4.0 + 2.0 * size
        r = audit.residualize(x, size)
        self.assertLess(float(np.nanmax(np.abs(r))), 1e-10)

    def test_quintile_spread(self):
        score = np.arange(100, dtype=float)
        y = score / 100.0
        spread, q5, q1, q5n, q1n = audit.quintile_spread(score, y)
        self.assertGreater(spread, 0)
        self.assertEqual(q5n, 20)
        self.assertEqual(q1n, 20)
        self.assertGreater(q5, q1)

    def test_parse_market_data_symbol_shape(self):
        raw = {
            "000001.SZ": pd.DataFrame(
                {"close": [10.0, 11.0]},
                index=["20260923", "20260924"],
            )
        }
        out = audit.parse_market_data_ex(raw, ["000001.SZ"])
        self.assertEqual(len(out), 2)
        self.assertEqual(out.iloc[-1]["signal_date"], "20260924")
        self.assertAlmostEqual(out.iloc[-1]["close"], 11.0)

    def test_parse_market_data_field_shape(self):
        raw = {
            "close": pd.DataFrame(
                {"000001.SZ": [10.0, 11.0]},
                index=["20260923", "20260924"],
            )
        }
        out = audit.parse_market_data_ex(raw, ["000001.SZ"])
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(out.iloc[0]["close"], 10.0)


if __name__ == "__main__":
    unittest.main()
