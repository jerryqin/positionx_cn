import sys
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "audit_sector_pit_assets_v02.py"
source = SCRIPT.read_text(encoding="utf-8")
ns = {"Path": Path, "re": re}
start = source.index("STRONG_CLASS_KEYS =")
end = source.index("def table_stats")
exec(source[start:end], ns)

class T(unittest.TestCase):
    def test_board_is_rejected(self):
        status, detail = ns["semantic_status"](
            ["trade_date", "symbol", "board", "open_date", "expire_date"]
        )
        self.assertEqual(status, "NO_TRUE_SECTOR_OR_INDUSTRY_FIELD")
        self.assertIsNone(detail["classification_col"])

    def test_industry_with_date_is_pit_candidate(self):
        status, detail = ns["semantic_status"](
            ["trade_date", "symbol", "industry_name"]
        )
        self.assertEqual(status, "PIT_CANDIDATE_REQUIRES_TEMPORAL_VALIDATION")
        self.assertEqual(detail["classification_col"], "industry_name")

    def test_current_industry_is_unsafe(self):
        status, _ = ns["semantic_status"](["symbol", "industry_name"])
        self.assertEqual(status, "CURRENT_ONLY_PIT_UNSAFE")

    def test_sw_l1_is_strong_classification(self):
        status, detail = ns["semantic_status"](
            ["effective_date", "ts_code", "sw_l1"]
        )
        self.assertEqual(status, "PIT_CANDIDATE_REQUIRES_TEMPORAL_VALIDATION")
        self.assertEqual(detail["classification_col"], "sw_l1")

    def test_qident(self):
        self.assertEqual(ns["qident"]('close'), '"close"')

if __name__ == "__main__":
    unittest.main()
