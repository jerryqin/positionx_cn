from __future__ import annotations

import importlib.util
import math
from pathlib import Path
import unittest

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "audit_ret120_frozen_signal_lineage_v01.py"
SPEC = importlib.util.spec_from_file_location("ret120_lineage", MODULE_PATH)
M = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(M)


class Ret120FrozenSignalLineageV01Tests(unittest.TestCase):
    def test_assign_quintiles_ties_are_deterministic_by_input_order(self):
        values = pd.Series([1.0] * 100)
        q = M.assign_quintiles(values)
        self.assertEqual((q == "Q1").sum(), 20)
        self.assertEqual((q == "Q5").sum(), 20)
        self.assertTrue((q.iloc[:20] == "Q1").all())
        self.assertTrue((q.iloc[-20:] == "Q5").all())

    def test_reconstruct_assigns_bucket_before_horizon_label_filter(self):
        n = 100
        panel = pd.DataFrame({
            "signal_date": ["20260105"] * n,
            "symbol": [f"{i:06d}.SZ" for i in range(n)],
            "ret120": np.arange(n, dtype=float),
            "excess_20": np.linspace(-0.1, 0.1, n),
            "excess_60": np.linspace(-0.2, 0.2, n),
            "excess_120": np.linspace(-0.3, 0.3, n),
        })
        # Remove S120 labels from the TOP-ranked names. If code incorrectly
        # filters labels before quintiling, Q5 membership would change.
        panel.loc[95:99, "excess_120"] = np.nan

        membership, buckets = M.reconstruct_ret120(panel)
        q5_members = membership[membership["bucket"] == "Q5"]["symbol"].tolist()

        self.assertEqual(len(q5_members), 20)
        self.assertEqual(q5_members[-1], "000099.SZ")

        h120_q5 = buckets[
            (buckets["horizon"] == 120) & (buckets["bucket"] == "Q5")
        ].iloc[0]
        self.assertEqual(int(h120_q5["n"]), 15)

    def test_monotonicity_is_one_for_strictly_increasing_bucket_means(self):
        qmeans = {f"Q{i}": float(i) for i in range(1, 6)}
        self.assertAlmostEqual(M.monotonicity_score(qmeans), 1.0, places=12)

    def test_normalize_folds_rejects_bad_shape(self):
        with self.assertRaises(M.FailClosed):
            M.normalize_folds([["F1", "20200101"]])

    def test_expected_checkpoint_table(self):
        self.assertEqual(M.EXPECTED_REPORTED[20]["positive_folds"], 2)
        self.assertEqual(M.EXPECTED_REPORTED[60]["positive_folds"], 3)
        self.assertEqual(M.EXPECTED_REPORTED[120]["positive_folds"], 3)


if __name__ == "__main__":
    unittest.main()
