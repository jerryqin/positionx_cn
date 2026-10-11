import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audit_deep_oversold_metric_provenance_v01 as audit


class DeepOversoldMetricProvenanceV01Tests(unittest.TestCase):

    def test_numeric_json_hit_1365(self):
        hits = audit.deep_find_numeric({"trades": 1365})
        self.assertTrue(any(h["matched"] == "1365" for h in hits))

    def test_numeric_json_hit_cagr(self):
        hits = audit.deep_find_numeric({"cagr": 0.1104})
        self.assertTrue(any(h["matched"] == "11.04%" for h in hits))

    def test_line_hits_context(self):
        p = Path("x.py")
        text = "a\nCSI500\ntrades = 1365\nc\n"
        hits = audit.line_hits(p, text)
        self.assertTrue(any("1365" in h["matched_targets"] for h in hits))


if __name__ == "__main__":
    unittest.main()
