import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import probe_ret120_size_pit_source_v01 as probe


class Ret120SizePitSourceTests(unittest.TestCase):

    def test_parse_compact_date(self):
        self.assertEqual(probe.parse_date(20260924), "20260924")

    def test_parse_dash_date(self):
        self.assertEqual(probe.parse_date("2026-09-24"), "20260924")

    def test_parse_millisecond_timestamp(self):
        # 2024-01-01 00:00:00 UTC
        self.assertEqual(
            probe.parse_date(1704067200000),
            "20240101",
        )

    def test_parse_invalid_date(self):
        self.assertIsNone(probe.parse_date("20261399"))

    def test_safe_float_rejects_nan(self):
        self.assertTrue(probe.np.isnan(probe.safe_float(float("nan"))))


if __name__ == "__main__":
    unittest.main()
