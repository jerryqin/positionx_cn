import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "promote_sw_industry_pit_v1.py"

class T(unittest.TestCase):
    def test_expected_asset_identity_is_frozen(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("df94c64e5365b82d3f76cff76c1465f4e1f39b257757ec01151d3291ceb7c8f9", text)
        self.assertIn("SW_INDUSTRY_PIT_L1_V20260927", text)
        self.assertIn("research.sw_industry_membership_pit_v1", text)

    def test_promotion_is_versioned_and_registered(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("research.asset_registry_v1", text)
        self.assertIn("source_sha256", text)
        self.assertIn("metadata_json", text)

if __name__ == "__main__":
    unittest.main()
