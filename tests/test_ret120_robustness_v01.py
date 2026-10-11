import sys
import unittest
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audit_ret120_robustness_v01 as audit


class Ret120RobustnessV01Tests(unittest.TestCase):

    def test_trimmed_mean_removes_both_tails(self):
        x = list(range(100))

        result = audit.trimmed_mean(
            x,
            proportion=0.05,
        )

        expected = np.mean(
            np.arange(5, 95)
        )

        self.assertAlmostEqual(
            result,
            expected,
        )

    def test_remove_top_tail_detects_right_tail_dependency(self):
        x = [0.01] * 95 + [1.00] * 5

        full = np.mean(x)
        ex_top5 = audit.mean_without_top(
            x,
            0.05,
        )

        self.assertGreater(
            full,
            ex_top5,
        )

        self.assertAlmostEqual(
            ex_top5,
            0.01,
        )

    def test_broad_based_classification(self):
        stats = audit.distribution_stats(
            [0.01] * 100
        )

        self.assertEqual(
            audit.classify_edge_shape(stats),
            "BROAD_BASED",
        )

    def test_avoidance_only_classification(self):
        result = audit.classify_selection(
            q5_vs_universe=-0.001,
            q1_vs_universe=-0.020,
        )

        self.assertEqual(
            result,
            "AVOIDANCE_ONLY",
        )


if __name__ == "__main__":
    unittest.main()
