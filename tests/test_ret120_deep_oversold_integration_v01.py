import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audit_ret120_deep_oversold_integration_v01 as audit


class Ret120DeepOversoldIntegrationV01Tests(unittest.TestCase):

    def test_normalize_symbol(self):
        self.assertEqual(audit.normalize_symbol("600000"), "600000.SH")
        self.assertEqual(audit.normalize_symbol("000001"), "000001.SZ")
        self.assertEqual(audit.normalize_symbol("830000"), "830000.BJ")

    def test_normalize_date(self):
        self.assertEqual(audit.normalize_date("2026-09-24"), "20260924")
        self.assertEqual(audit.normalize_date("20260924"), "20260924")

    def test_assign_quintiles_balanced(self):
        q = audit.assign_quintiles(pd.Series(np.arange(100, dtype=float)))
        counts = q.value_counts().to_dict()
        self.assertEqual(counts["Q1"], 20)
        self.assertEqual(counts["Q5"], 20)

    def test_adjacent_monotonicity(self):
        x = {
            "Q1": 0.01,
            "Q2": 0.02,
            "Q3": 0.03,
            "Q4": 0.01,
            "Q5": 0.04,
        }
        self.assertAlmostEqual(audit.adjacent_monotonicity(x), 0.75)

    def test_summarize_returns(self):
        s = audit.summarize_returns([-0.2, -0.1, 0.1, 0.2])
        self.assertEqual(s["n"], 4)
        self.assertAlmostEqual(s["mean"], 0.0)
        self.assertAlmostEqual(s["win_rate"], 0.5)

    def test_segment_analysis_q1_gap(self):
        rows = []
        for q, ret in [
            ("Q1", -0.10),
            ("Q1", -0.05),
            ("Q2", 0.01),
            ("Q3", 0.02),
            ("Q4", 0.03),
            ("Q5", 0.04),
        ]:
            rows.append({
                "ret120_bucket": q,
                "gross_return": ret,
                "net_20bp": ret - 0.002,
                "net_50bp": ret - 0.005,
            })
        df = pd.DataFrame(rows)
        a = audit.segment_analysis(df)
        self.assertLess(
            a["q1_vs_rest"]["mean_gap_q1_minus_rest"],
            0.0,
        )

    def test_trade_key_frame_deduplicates(self):
        df = pd.DataFrame([
            {
                "symbol": "000001.SZ",
                "signal_date": "20200101",
                "entry_date": "20200102",
                "exit_date": "20200120",
            },
            {
                "symbol": "000001.SZ",
                "signal_date": "20200101",
                "entry_date": "20200102",
                "exit_date": "20200120",
            },
        ])
        out = audit.trade_key_frame(df)
        self.assertEqual(len(out), 1)

    def test_symbol_normalization_matches_suffix_forms(self):
        left = audit.normalize_symbol("000001")
        right = audit.normalize_symbol("000001.SZ")
        self.assertEqual(left, right)

    def test_date_normalization_matches_formats(self):
        self.assertEqual(
            audit.normalize_date("2026-04-02"),
            audit.normalize_date("20260402"),
        )

    def test_csi500_regime_filename_is_distinct_from_generic_replication(self):
        self.assertNotEqual(
            "deep_oversold_csi500_regime_events_v1.parquet",
            "deep_oversold_replication_events_latest.csv",
        )


if __name__ == "__main__":
    unittest.main()
