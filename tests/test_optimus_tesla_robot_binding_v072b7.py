from __future__ import annotations
import importlib.util, json, sys, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
B=ROOT/"scripts"/"run_optimus_external_classifier_v072b.py"
CFG=json.loads((ROOT/"config/research/optimus_external_classifier_v072b.json").read_text(encoding="utf-8"))

spec=importlib.util.spec_from_file_location("v072b7",B)
m=importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name]=m
spec.loader.exec_module(m)

def cap(event="2026-10-09T10:00:00+08:00"):
    return {
        "capture_identity":"CAP1",
        "source_id":"SRC_SSE_OFFICIAL",
        "source_role":"EXCHANGE_REGULATORY",
        "lane":"NEW_CANDIDATE_DISCOVERY",
        "entity_name":"成都豪能科技股份有限公司",
        "symbol":"603809.SH",
        "source_url":"https://big5.sse.com.cn/example.pdf",
        "document_title":"Official Q&A",
        "event_time":event,
        "published_at":event,
        "observed_at":"2026-10-10T11:00:00+08:00",
    }

HAONENG = """1、公司是机器人企业一级供应商吗？人形机器人作为未来方向，豪能在这方面有什么进展？
答：公司已形成高精密行星减速机、高精密新型摆线减速机等系列化产品，正积极直接或通过一级供应商对接主流机器人企业，全力推动现有产品及潜在研发储备产品在人形机器人、机器狗、外骨骼等下游应用领域的验证与批量应用。目前，公司已与多个企业开展业务交流及合作，部分产品已实现批量供货。
2、公司上半年差速器业务情况如何？
答：公司差速器产品配套比亚迪、吉利、小米、特斯拉、赛力斯等国内外主流车企，订单充足且产能规模不断爬升。"""

class TeslaRobotBindingTests(unittest.TestCase):
    def test_haoneng_automotive_tesla_order_is_not_robot_link(self):
        x=m.classify_capture(cap(),HAONENG,CFG)
        self.assertEqual(x["queue_state"],"EVIDENCE_PROPOSAL")
        self.assertIn("PURCHASE_ORDER",x["tesla_context_signal_tags"])
        self.assertEqual(x["tesla_robot_context_signal_tags"],[])
        self.assertFalse(x["tesla_robot_link_eligible"])

    def test_historical_haoneng_stays_historical_and_not_tesla_linked(self):
        x=m.classify_capture(cap("2025-09-12T17:00:00+08:00"),HAONENG,CFG)
        self.assertEqual(x["queue_state"],"HISTORICAL_BASELINE_ONLY")
        self.assertFalse(x["tesla_robot_link_eligible"])

    def test_explicit_tesla_optimus_batch_supply_is_stage2_candidate(self):
        text="答：公司已向特斯拉Optimus人形机器人项目批量供货相关关节零部件。"
        x=m.classify_capture(cap(),text,CFG)
        self.assertEqual(x["queue_state"],"STAGE2_GATE_CANDIDATE")
        self.assertIn("BATCH_SUPPLY",x["tesla_robot_context_signal_tags"])
        self.assertTrue(x["tesla_robot_link_eligible"])

    def test_tesla_auto_and_robot_validation_separate_answers_not_stage2(self):
        text="""1、机器人业务进展？
答：公司正在推进人形机器人控制器新产品验证。
2、汽车业务进展？
答：特斯拉汽车项目订单充足。"""
        x=m.classify_capture(cap(),text,CFG)
        self.assertEqual(x["queue_state"],"EVIDENCE_PROPOSAL")
        self.assertFalse(x["tesla_robot_link_eligible"])


    def test_optimus_chinese_adjacent_context_is_detected(self):
        text="答：Optimus关节零部件已进入小批量供货阶段。"
        x=m.classify_capture(cap(),text,CFG)
        self.assertEqual(x["queue_state"],"STAGE2_GATE_CANDIDATE")
        self.assertTrue(x["tesla_robot_link_eligible"])

    def test_optimus_named_context_counts_as_dual_context(self):
        text="答：Optimus关节零部件已进入小批量供货阶段。"
        x=m.classify_capture(cap(),text,CFG)
        self.assertEqual(x["queue_state"],"STAGE2_GATE_CANDIDATE")
        self.assertIn("SMALL_BATCH_DELIVERY",x["tesla_robot_context_signal_tags"])

if __name__=="__main__":
    unittest.main()
