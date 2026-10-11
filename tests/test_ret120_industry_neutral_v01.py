import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audit_ret120_industry_neutral_v01 as audit


class Ret120IndustryNeutralV01Tests(unittest.TestCase):

    def test_industry_demean_zeroes_group_means(self):
        x = np.array([1.0, 3.0, 10.0, 14.0])
        g = np.array(["A", "A", "B", "B"], dtype=object)
        r, sizes = audit.industry_demean(x, g)
        self.assertAlmostEqual(float(r[:2].mean()), 0.0)
        self.assertAlmostEqual(float(r[2:].mean()), 0.0)
        self.assertTrue(np.all(sizes == 2))

    def test_industry_demean_singleton_identified(self):
        x = np.array([1.0, 3.0, 10.0])
        g = np.array(["A", "A", "B"], dtype=object)
        r, sizes = audit.industry_demean(x, g)
        self.assertEqual(int(sizes[-1]), 1)
        self.assertAlmostEqual(float(r[-1]), 0.0)

    def test_industry_only_confounder_disappears_after_demean(self):
        # Within each industry x has variation, but y only depends on industry.
        x = np.array([1.0, 2.0, 3.0, 10.0, 11.0, 12.0])
        g = np.array(["A", "A", "A", "B", "B", "B"], dtype=object)
        y = np.array([5.0, 5.0, 5.0, -4.0, -4.0, -4.0])

        xw, sizes = audit.industry_demean(x, g)
        yw, _ = audit.industry_demean(y, g)
        beta = audit.ols_beta(yw[sizes >= 2], [audit.zscore(xw[sizes >= 2])])

        self.assertAlmostEqual(float(beta[1]), 0.0, places=12)

    def test_stock_signal_survives_industry_demean(self):
        x = np.array([1.0, 2.0, 3.0, 10.0, 11.0, 12.0])
        g = np.array(["A", "A", "A", "B", "B", "B"], dtype=object)

        xw, sizes = audit.industry_demean(x, g)
        y = 0.5 * xw + np.array([5.0, 5.0, 5.0, -4.0, -4.0, -4.0])
        yw, _ = audit.industry_demean(y, g)

        beta = audit.ols_beta(yw[sizes >= 2], [audit.zscore(xw[sizes >= 2])])
        self.assertGreater(float(beta[1]), 0.0)

    def test_active_share_zero_when_selected_matches_base(self):
        groups = np.array(["A", "A", "B", "B"], dtype=object)
        selected = np.array([True, False, True, False])
        active, max_ow, _, _ = audit.active_industry_share(groups, selected)
        self.assertAlmostEqual(active, 0.0)
        self.assertAlmostEqual(max_ow, 0.0)

    def test_quintile_spread_positive(self):
        score = np.arange(100, dtype=float)
        y = score / 100.0
        spread, q5, q1, q5n, q1n, selected = audit.quintile_spread(score, y)
        self.assertGreater(spread, 0.0)
        self.assertGreater(q5, q1)
        self.assertEqual(q5n, 20)
        self.assertEqual(q1n, 20)
        self.assertEqual(int(selected.sum()), 20)

    def test_summarize_tail_metrics(self):
        x = np.arange(100, dtype=float)
        s = audit.summarize(x)
        self.assertEqual(s["n_dates"], 100)
        self.assertTrue(np.isfinite(s["trim5"]))
        self.assertLess(s["ex_top5"], s["mean"])


if __name__ == "__main__":
    unittest.main()
