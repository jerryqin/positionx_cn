import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audit_deep_oversold_trade_lineage_v02 as audit


class DeepOversoldTradeLineageV02Tests(unittest.TestCase):

    def test_true_sql(self):
        s = audit.true_sql("standard_fillable")
        self.assertIn("standard_fillable", s)
        self.assertIn("'true'", s)

    def test_false_sql(self):
        s = audit.false_sql("is_suspended")
        self.assertIn("is_suspended", s)
        self.assertIn("'false'", s)

    def test_target_count_constant(self):
        self.assertEqual(audit.TARGET_COUNT, 1365)


if __name__ == "__main__":
    unittest.main()
