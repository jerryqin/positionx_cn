from __future__ import annotations
import importlib.util, json, sys, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
B=ROOT/"scripts"/"run_optimus_external_classifier_v072b.py"
CFG=json.loads((ROOT/"config/research/optimus_external_classifier_v072b.json").read_text(encoding="utf-8"))

spec=importlib.util.spec_from_file_location("v072b6",B)
m=importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name]=m
spec.loader.exec_module(m)

def cap(entity="成都豪能科技股份有限公司", symbol="603809.SH", event="2025-09-12T17:00:00+08:00"):
    return {
        "capture_identity":"CAP_QA",
        "source_id":"SRC_SSE_OFFICIAL",
        "source_role":"EXCHANGE_REGULATORY",
        "lane":"NEW_CANDIDATE_DISCOVERY",
        "entity_name":entity,
        "symbol":symbol,
        "source_url":"https://static.sse.com.cn/example.pdf",
        "document_title":"Official Q&A",
        "event_time":event,
        "published_at":event,
        "observed_at":"2026-10-10T10:47:00+08:00",
    }

class QaContextTests(unittest.TestCase):
    def test_haoneng_pdf_line_wraps_recover_robot_batch_supply(self):
        text="""1、公司是机器人企业一级供应商吗？人形机器人作为未来方向，豪能在这
方面有什么进展？给哪些头部公司供货或者提供样品？
答：公司已形成高精密行星减速机、高精密新型摆线减速机等系列化产品，
具备为机器人企业提供相关零部件的技术能力，正积极直接或通过一级供应商对接主流机器人企业，全力推动现有产品及潜在研发储备产品在人形机器人、机器
狗、外骨骼等下游应用领域的验证与批量应用。目前，公司已与多个企业开展业
务交流及合作，部分产品已实现批量供货。具体信息请以公司在指定信息披露媒
体发布的公告为准。
2、公司上半年差速器业务情况如何？"""
        x=m.classify_capture(cap(),text,CFG)
        self.assertEqual(x["queue_state"],"HISTORICAL_BASELINE_ONLY")
        self.assertIn("BATCH_SUPPLY",x["robot_context_signal_tags"])
        self.assertIn("MASS_PRODUCTION",x["robot_context_signal_tags"])

    def test_xinje_reply_line_wraps_recover_scaled_batch_supply(self):
        text="""1、请问公司人形机器人业务进展如何？
回复：尊敬的投资者您好。公司依托伺服与运动控制技术积累布局人形机
器人赛道，聚焦关节核心零部件研发。公司已攻克无框力矩电机、空心杯电
机、高性能编码器等关键技术，电感式编码器实现全栈自主可控；无框力矩电
机已与多家客户达成合作并实现规模化批量供货，目前公司多条自动化产线已
投入使用。公司搭建专项研发与市场团队，参与机器人关节相关国家标准制
定；同时加快推进机器人智能驱控生产基地建设，为产业化放量提供产能支
撑。
2、请问公司怎么看大型PLC的前景？"""
        x=m.classify_capture(
            cap("无锡信捷电气股份有限公司","603416.SH","2026-09-16T10:01:30+08:00"),
            text,CFG
        )
        self.assertEqual(x["queue_state"],"HISTORICAL_BASELINE_ONLY")
        self.assertIn("MASS_PRODUCTION",x["robot_context_signal_tags"])

    def test_long_answer_does_not_borrow_distant_unrelated_batch_supply(self):
        filler="这是其他业务描述。"*40
        text=f"""1、请介绍公司新业务。
答：公司氮化铝基板已实现批量供货，并获得多个客户订单。{filler}
军工军品板块将深化航空航天精密结构件业务，推进人形机器人控制器等新产品验证。
2、下一题。"""
        x=m.classify_capture(
            cap("成都旭光电子股份有限公司","600353.SH","2026-10-08T16:21:30+08:00"),
            text,CFG
        )
        self.assertEqual(x["queue_state"],"EVIDENCE_PROPOSAL")
        self.assertIn("CUSTOMER_VALIDATION",x["robot_context_signal_tags"])
        self.assertNotIn("BATCH_SUPPLY",x["robot_context_signal_tags"])
        self.assertNotIn("MASS_PRODUCTION",x["robot_context_signal_tags"])

    def test_question_only_robot_context_does_not_authorize_answer_batch_supply(self):
        text="""1、请问人形机器人业务怎么样？
答：公司汽车差速器产品已实现批量供货。
2、下一题。"""
        x=m.classify_capture(cap(),text,CFG)
        self.assertEqual(x["queue_state"],"NO_NEW_MATERIAL_EVIDENCE")

    def test_non_qa_cross_section_rule_unchanged(self):
        text="""Tesla Optimus
Create a general purpose humanoid robot.
FSD Chip
Bring the silicon chip to mass production in our vehicles."""
        x=m.classify_capture({
            **cap("Tesla, Inc.","TSLA","2026-10-09T10:00:00+08:00"),
            "source_id":"SRC_TESLA_CORPORATE_OFFICIAL",
            "source_role":"NAMED_CUSTOMER_OFFICIAL",
            "lane":"THEME_OFFICIAL",
            "source_url":"https://www.tesla.com/AI",
        },text,CFG)
        self.assertEqual(x["queue_state"],"NO_NEW_MATERIAL_EVIDENCE")
        self.assertEqual(x["reason_code"],"CROSS_SECTION_SIGNAL_REJECTED")

if __name__=="__main__":
    unittest.main()
