import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import probe_ret120_exposure_sources_v01 as probe


class Ret120ExposureSourceProbeTests(unittest.TestCase):

    def test_keyword_hits_market_cap(self):
        hits = probe.kw_hits("symbol total_mv circ_mv")
        self.assertIn("total_mv", hits)
        self.assertIn("circ_mv", hits)

    def test_keyword_hits_industry(self):
        hits = probe.kw_hits("shenwan_industry")
        self.assertIn("industry", hits)
        self.assertIn("shenwan", hits)

    def test_irrelevant_text_has_no_hits(self):
        self.assertEqual(probe.kw_hits("ret120 excess return fold"), [])


if __name__ == "__main__":
    unittest.main()
