import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audit_deep_oversold_trade_lineage_v01 as audit


class DeepOversoldTradeLineageV01Tests(unittest.TestCase):

    def test_pairwise_subset(self):
        sets = {
            "a": {("x", 1), ("y", 2)},
            "b": {("x", 1), ("y", 2), ("z", 3)},
        }
        rows = audit.pairwise_sets(sets)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["a_subset_b"])
        self.assertFalse(rows[0]["b_subset_a"])
        self.assertEqual(rows[0]["b_only"], 1)

    def test_pairwise_exact(self):
        sets = {
            "a": {("x", 1), ("y", 2)},
            "b": {("x", 1), ("y", 2)},
        }
        rows = audit.pairwise_sets(sets)
        self.assertTrue(rows[0]["a_subset_b"])
        self.assertTrue(rows[0]["b_subset_a"])
        self.assertEqual(rows[0]["jaccard"], 1.0)


if __name__ == "__main__":
    unittest.main()
