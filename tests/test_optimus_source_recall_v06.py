from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
SCRIPT = HERE.parents[1] / "scripts" / "audit_optimus_source_recall_v06.py"
spec = importlib.util.spec_from_file_location("r1probe", SCRIPT)
m = importlib.util.module_from_spec(spec)
assert spec and spec.loader
import sys
sys.modules[spec.name] = m
spec.loader.exec_module(m)

class R1ProbeTests(unittest.TestCase):
    def test_normalize_symbol(self):
        self.assertEqual(m.normalize_symbol("601689"), "601689.SH")
        self.assertEqual(m.normalize_symbol("603119"), "603119.SH")
        self.assertEqual(m.normalize_symbol("300433"), "300433.SZ")
        self.assertEqual(m.normalize_symbol("688017"), "688017.SH")

    def test_valid_a_share(self):
        self.assertTrue(m.is_a_share_code("601100"))
        self.assertTrue(m.is_a_share_code("300580"))
        self.assertFalse(m.is_a_share_code("900901"))
        self.assertFalse(m.is_a_share_code(""))

    def test_cninfo_body_matches_blank_contract(self):
        spec = m.QuerySpec(name="x", query_date="2026-08-28")
        text = m.cninfo_body(spec, 1).decode()
        kv = dict(x.split("=", 1) for x in text.split("&"))
        self.assertEqual(kv["pageNum"], "1")
        self.assertEqual(kv["pageSize"], "30")
        self.assertEqual(kv["searchkey"], "")
        self.assertEqual(kv["stock"], "")
        self.assertEqual(kv["seDate"], "2026-08-28")

    def test_diagnose_global_gap_when_keyword_recovers(self):
        anchor = {
            "query_date": "2026-08-28",
            "announcement_id": "1225521853",
            "sec_code": "601689",
            "company": "拓普集团",
        }
        keyword = [{
            "announcement_id": "1225521853",
            "sec_code": "601689",
            "normalized_symbol": "601689.SH",
            "valid_a_share": True,
            "page_num": 1,
        }]
        got = m.diagnose_anchor(
            anchor,
            blank_rows=[],
            keyword_rows=keyword,
            stock_rows=None,
            static_probe={"reachable": True},
            seed_v02={"601689.SH"},
        )
        self.assertEqual(got["diagnosis"], "Q0_GLOBAL_BLANK_QUERY_RECALL_GAP")
        self.assertTrue(got["in_seed_v02"])
        self.assertFalse(got["classifier_called"])
        self.assertFalse(got["state_mutation"])

    def test_diagnose_present_global(self):
        anchor = {
            "query_date": "2026-08-25",
            "announcement_id": "1225499629",
            "sec_code": "601100",
            "company": "恒立液压",
        }
        row = {
            "announcement_id": "1225499629",
            "sec_code": "601100",
            "normalized_symbol": "601100.SH",
            "valid_a_share": True,
            "page_num": 99,
        }
        got = m.diagnose_anchor(
            anchor,
            blank_rows=[row],
            keyword_rows=[],
            stock_rows=None,
            static_probe={"reachable": True},
            seed_v02={"601100.SH"},
        )
        self.assertEqual(got["diagnosis"], "RAW_GLOBAL_PRESENT_DOWNSTREAM_REVIEW_REQUIRED")
        self.assertEqual(got["raw_global_page"], 99)

    def test_payload_total_helpers(self):
        self.assertEqual(m.payload_total_pages({"totalpages": 227}), 227)
        self.assertEqual(m.payload_total_count({"totalAnnouncement": 6810}), 6810)
        self.assertIsNone(m.payload_total_count({}))

    def test_row_view(self):
        row = {
            "announcementId": "1",
            "secCode": "601689",
            "secName": "拓普集团",
            "orgId": "gssh0601689",
            "announcementTitle": "<em>2026年半年度报告</em>",
        }
        v = m.row_view(row, 3, "q")
        self.assertEqual(v["normalized_symbol"], "601689.SH")
        self.assertEqual(v["announcement_title"], "2026年半年度报告")
        self.assertEqual(v["org_id"], "gssh0601689")

if __name__ == "__main__":
    unittest.main()
