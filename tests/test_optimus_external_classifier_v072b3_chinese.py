from __future__ import annotations
import importlib.util, json, sys, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"scripts"/"run_optimus_external_classifier_v072b.py"
CFG=json.loads((ROOT/"config/research/optimus_external_classifier_v072b.json").read_text(encoding="utf-8"))

spec=importlib.util.spec_from_file_location("v072b3",SCRIPT)
m=importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name]=m
spec.loader.exec_module(m)

def cap(lane="NEW_CANDIDATE_DISCOVERY",event="2026-09-16T10:01:30+08:00"):
    return {
        "capture_identity":"CN1",
        "source_id":"SRC_SSE_OFFICIAL",
        "source_role":"EXCHANGE_REGULATORY",
        "lane":lane,
        "entity_name":"Test Co",
        "symbol":"999999.SH",
        "source_url":"https://roadshow.sseinfo.com/stayTuned/99999",
        "document_title":"Test",
        "event_time":event,
        "published_at":event,
        "observed_at":"2026-10-10T10:30:00+08:00",
    }

class ChineseClassifierTests(unittest.TestCase):
    def test_chinese_humanoid_batch_supply_is_contextual_signal(self):
        text="公司布局人形机器人关节核心零部件，无框力矩电机已与多家客户达成合作并实现规模化批量供货。"
        x=m.classify_capture(cap(),text,CFG)
        self.assertEqual(x["queue_state"],"HISTORICAL_BASELINE_ONLY")
        self.assertIn("BATCH_SUPPLY",x["robot_context_signal_tags"])
        self.assertIn("MASS_PRODUCTION",x["robot_context_signal_tags"])

    def test_chinese_small_batch_supply_detected(self):
        text="公司高耐弯折机器人线缆已经实现对北美某头部人形机器人客户小批量供货。"
        x=m.classify_capture(cap(),text,CFG)
        self.assertIn("SMALL_BATCH_DELIVERY",x["robot_context_signal_tags"])

    def test_chinese_robot_product_validation_is_proposal_if_prospective(self):
        text="公司正在推进人形机器人控制器等新产品验证。"
        x=m.classify_capture(cap(event="2026-10-08T16:14:31+08:00"),text,CFG)
        self.assertEqual(x["queue_state"],"EVIDENCE_PROPOSAL")
        self.assertIn("CUSTOMER_VALIDATION",x["robot_context_signal_tags"])

    def test_unrelated_chinese_mass_production_cross_section_rejected(self):
        text="""人形机器人
公司持续进行前沿研发。
汽车电子
公司汽车电子产品已经实现量产。"""
        x=m.classify_capture(cap(event=None),text,CFG)
        self.assertEqual(x["queue_state"],"NO_NEW_MATERIAL_EVIDENCE")
        self.assertEqual(x["reason_code"],"CROSS_SECTION_SIGNAL_REJECTED")
        self.assertIn("MASS_PRODUCTION",x["global_signal_tags"])
        self.assertNotIn("MASS_PRODUCTION",x["robot_context_signal_tags"])

    def test_named_tesla_chinese_batch_supply_is_stage2_candidate_only_if_prospective(self):
        text="公司已向特斯拉Optimus人形机器人项目批量供货相关关节零部件。"
        x=m.classify_capture(cap(lane="SUPPLIER_GLOBAL_OFFICIAL",event="2026-10-09T10:00:00+08:00"),text,CFG)
        self.assertEqual(x["queue_state"],"STAGE2_GATE_CANDIDATE")

    def test_chinese_denial_is_contradiction(self):
        text="公司未向特斯拉供货人形机器人相关产品。"
        x=m.classify_capture(cap(lane="SUPPLIER_GLOBAL_OFFICIAL",event="2026-10-09T10:00:00+08:00"),text,CFG)
        self.assertEqual(x["queue_state"],"CONTRADICTION_ALERT")

if __name__=="__main__":
    unittest.main()
