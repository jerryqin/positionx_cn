from __future__ import annotations
import importlib.util, json, sys, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"scripts/run_optimus_component_domain_audit_v075.py"
CFG=json.loads((ROOT/"config/research/optimus_component_domain_audit_v075.json").read_text(encoding="utf-8"))

spec=importlib.util.spec_from_file_location("domain_audit",SCRIPT)
m=importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name]=m
spec.loader.exec_module(m)

class FakeEvidence:
    COMPONENT_MAP=[
        (("谐波减速器","减速器","精密齿轮"),"reducers / precision transmission"),
        (("无框力矩电机","空心杯电机","伺服电机","电机转子"),"motors / motor components"),
        (("控制器","编码器","运动控制"),"control / encoders"),
    ]
    @staticmethod
    def context_windows(text):
        return [text]
    @staticmethod
    def component_domain(text):
        low=text.lower()
        for terms,domain in FakeEvidence.COMPONENT_MAP:
            if any(t.lower() in low for t in terms):
                return domain
        return "UNKNOWN_PENDING_REVIEW"

class AuditTests(unittest.TestCase):
    def test_exact_post_rebase_contract(self):
        p=CFG["post_rebase_contract"]
        self.assertEqual(p["registry_sha256"],"d51bbc34dc0731667cd7c2991b11cb954fd3747c9b1c9c6c6154f40cbbc9ccaa")
        self.assertEqual(p["canonical_fact_count"],25)
        self.assertEqual(p["materialized_fact_evidence_count"],25)

    def test_exact_target(self):
        t=CFG["target"]
        self.assertEqual(t["source_evidence_id"],"EACQ_5afda139f7d4dd9a1f33b7bb")
        self.assertEqual(t["canonical_evidence_id"],"CFE_ef6afb1d4f48e8166d81bfdb")

    def test_first_match_order_sensitivity_is_detected(self):
        text="减速器。无框力矩电机 无框力矩电机 空心杯电机。运动控制 控制器 编码器。"
        d=m.diagnose_text(FakeEvidence,text,"reducers / precision transmission")
        self.assertTrue(d["order_sensitive"])
        self.assertEqual(d["recomputed_first_match_domain"],"reducers / precision transmission")
        self.assertIn("motors / motor components",d["matched_domains"])
        self.assertIn("control / encoders",d["matched_domains"])

    def test_minority_risk_is_detected(self):
        class F(FakeEvidence):
            @staticmethod
            def context_windows(text):
                return ["减速器", "无框力矩电机", "无框力矩电机 空心杯电机"]
        d=m.diagnose_text(F,"x","reducers / precision transmission")
        self.assertTrue(d["first_match_minority_risk"])
        self.assertEqual(d["dominant_by_window_count"],["motors / motor components"])

    def test_audit_is_read_only(self):
        s=SCRIPT.read_text(encoding="utf-8")
        self.assertIn("read_only=True",s)
        self.assertNotIn("read_only=False",s)
        self.assertNotIn("INSERT INTO",s)
        self.assertNotIn("UPDATE ",s)
        self.assertNotIn("DELETE FROM",s)

    def test_no_ocr(self):
        self.assertTrue(CFG["scope"]["no_ocr"])

if __name__=="__main__":
    unittest.main()
