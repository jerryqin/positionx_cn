from __future__ import annotations
import importlib.util, json, sys, tempfile, unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_optimus_seed_debt_v06.py"
spec = importlib.util.spec_from_file_location("v06", SCRIPT)
m = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name]=m
spec.loader.exec_module(m)

class Tests(unittest.TestCase):
    def test_source_count_and_authority(self):
        self.assertEqual(len(m.SOURCES),4)
        self.assertEqual(sum(1 for x in m.SOURCES if x["authority"]=="COMPANY_OFFICIAL"),3)
        self.assertEqual(sum(1 for x in m.SOURCES if x["authority"]=="SSEINFO_COMPANY_IR_RECORD"),1)

    def test_all_external_fact_sources_exist(self):
        ids={x["source_id"] for x in m.SOURCES}
        for f in m.CANONICAL_FACTS:
            if f.get("source_id"):
                self.assertIn(f["source_id"], ids)
            for sid in f.get("source_ids") or []:
                self.assertIn(sid, ids)

    def test_queue_count(self):
        self.assertEqual(len(m.QUEUE_DECISIONS),7)
        self.assertEqual(sum(1 for x in m.QUEUE_DECISIONS if x["decision"]=="REJECTED"),3)

    def test_lifecycle_count(self):
        self.assertEqual(len(m.LIFECYCLE_TRANSITIONS),5)
        self.assertTrue(all(x["from"]=="ACTIVE_SEED" and x["to"]=="ACTIVE_CANONICAL" for x in m.LIFECYCLE_TRANSITIONS))
        self.assertTrue(all(x["tesla_link"]=="T0" for x in m.LIFECYCLE_TRANSITIONS))

    def test_phrase_normalization(self):
        src={"required_all":["已向国内外客户交付机器人零部件相关产品","订单要求积极排产"]}
        text="2026 年上半年，公司 已向国内外客户交付机器人零部件相关产品，\n目前根据国内外客户的订单要求积极排产。"
        got=m.validate_source_text(src,text)
        self.assertTrue(all(x["pass"] for x in got))

if __name__=="__main__":
    unittest.main()
