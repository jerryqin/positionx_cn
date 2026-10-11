import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audit_ret120_deep_oversold_integration_v02 as audit


class Ret120DeepOversoldIntegrationV02Tests(unittest.TestCase):

    def test_assign_quintiles_balanced(self):
        s = pd.Series(range(100), dtype=float)
        q = audit.assign_quintiles(s)
        counts = q.value_counts().to_dict()
        self.assertEqual(counts["Q1"], 20)
        self.assertEqual(counts["Q5"], 20)

    def test_q1_vs_rest_negative_gap(self):
        df = pd.DataFrame({
            "ret120_bucket": ["Q1", "Q1", "Q2", "Q3"],
            "open_gross_return": [-0.1, -0.05, 0.02, 0.03],
            "open_excess_gross": [-0.08, -0.04, 0.01, 0.02],
            "open_net_20bps": [-0.102, -0.052, 0.018, 0.028],
            "open_net_50bps": [-0.105, -0.055, 0.015, 0.025],
        })
        r = audit.q1_vs_rest(df)
        self.assertLess(
            r["open_excess_gross"]["mean_gap_q1_minus_rest"],
            0,
        )

    def test_paired_date_comparison(self):
        df = pd.DataFrame({
            "signal_date": ["20200101"] * 4,
            "ret120_bucket": ["Q1", "Q1", "Q2", "Q3"],
            "open_excess_gross": [-0.08, -0.04, 0.01, 0.03],
        })
        r = audit.paired_date_q1_vs_rest(
            df,
            "open_excess_gross",
        )
        self.assertEqual(r["paired_dates"], 1)
        self.assertLess(r["mean_gap"], 0)


if __name__ == "__main__":
    unittest.main()
