import sys
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "audit_event_catalyst_assets_v01.py"
source = SCRIPT.read_text(encoding="utf-8")
ns = {"re": re, "Path": Path}
start = source.index("PIT_DATE_KEYS =")
end = source.index("def qident")
exec(source[start:end], ns)


class T(unittest.TestCase):
    def test_announcement_with_publish_date_is_pit_candidate(self):
        r = ns["classify_schema"](
            "market.announcements",
            ["symbol", "publish_date", "title", "content"],
        )
        self.assertEqual(r["status"], "PIT_EVIDENCE_CANDIDATE")
        self.assertEqual(r["pit_date_col"], "publish_date")

    def test_report_period_without_announcement_date_is_unsafe(self):
        r = ns["classify_schema"](
            "fundamental.financials",
            ["symbol", "report_date", "revenue", "net_profit"],
        )
        self.assertEqual(r["status"], "PERIOD_ONLY_PIT_UNSAFE")

    def test_consensus_asof_is_pit_candidate(self):
        r = ns["classify_schema"](
            "research.consensus",
            ["symbol", "as_of", "eps_estimate", "revenue_estimate"],
        )
        self.assertEqual(r["status"], "PIT_EVIDENCE_CANDIDATE")
        self.assertIn("EXPECTATION", r["families"])

    def test_current_snapshot_is_unsafe(self):
        r = ns["classify_schema"](
            "current_guidance",
            ["symbol", "guidance", "target_price"],
        )
        self.assertEqual(r["status"], "CURRENT_OR_TIMELESS_UNSAFE")

    def test_event_text_family(self):
        r = ns["classify_schema"](
            "company_notice",
            ["symbol", "announcement_date", "headline"],
        )
        self.assertIn("EVENT_TEXT", r["families"])


if __name__ == "__main__":
    unittest.main()
