import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audit_ret120_absolute_return_v01 as audit


class Ret120AbsoluteReturnV01Tests(unittest.TestCase):

    def test_quintiles_are_balanced(self):
        s = pd.Series(np.arange(100, dtype=float))
        q = audit.assign_quintiles(s)
        counts = q.value_counts().to_dict()
        self.assertEqual(counts["Q1"], 20)
        self.assertEqual(counts["Q5"], 20)

    def test_session_map_exact_s1_s120(self):
        cal = [f"202001{d:02d}" for d in range(1, 32)]
        # Use synthetic valid ordering only; function does not parse dates.
        cal += [f"202002{d:02d}" for d in range(1, 29)]
        cal += [f"202003{d:02d}" for d in range(1, 32)]
        cal += [f"202004{d:02d}" for d in range(1, 31)]
        cal += [f"202005{d:02d}" for d in range(1, 31)]

        out = audit.session_map([cal[0]], cal, 120)
        self.assertEqual(out.iloc[0]["entry_date"], cal[1])
        self.assertEqual(out.iloc[0]["exit_date"], cal[120])

    def test_trim_mean(self):
        x = np.arange(100, dtype=float)
        self.assertAlmostEqual(audit.trim_mean(x, 0.05), 49.5)

    def test_parse_market_symbol_shape(self):
        raw = {
            "000001.SZ": pd.DataFrame(
                {"open": [10.0], "close": [11.0]},
                index=["20260924"],
            )
        }
        out = audit.parse_market_ex(
            raw,
            ["000001.SZ"],
            ("open", "close"),
        )
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out.iloc[0]["open"], 10.0)

    def test_raw_return_math(self):
        panel = pd.DataFrame([{
            "universe": "csi300",
            "signal_date": "20200101",
            "symbol": "000001.SZ",
            "ret120": 0.1,
            "excess_120": 0.05,
            "bucket": "Q5",
        }])
        sessions = pd.DataFrame([{
            "signal_date": "20200101",
            "entry_date": "20200102",
            "exit_date": "20200630",
        }])
        market = pd.DataFrame([
            {"date": "20200102", "symbol": "000001.SZ", "open": 10.0, "close": 10.5},
            {"date": "20200630", "symbol": "000001.SZ", "open": 14.0, "close": 15.0},
        ])
        out = audit.attach_raw_returns(panel, market, sessions)
        self.assertAlmostEqual(out.iloc[0]["raw_return_120"], 0.5)

    def test_summarize(self):
        s = audit.summarize([0.1, 0.2, 0.3])
        self.assertEqual(s["n_dates"], 3)
        self.assertAlmostEqual(s["mean"], 0.2)


if __name__ == "__main__":
    unittest.main()
