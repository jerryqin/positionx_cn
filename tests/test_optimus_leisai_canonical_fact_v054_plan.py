from __future__ import annotations
import importlib.util, json, sys, tempfile, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"scripts/run_optimus_leisai_canonical_fact_v054_plan.py"
CFG=json.loads((ROOT/"config/research/optimus_leisai_canonical_fact_v054_plan.json").read_text(encoding="utf-8"))

spec=importlib.util.spec_from_file_location("leisai_plan",SCRIPT)
m=importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name]=m
spec.loader.exec_module(m)

class LeisaiPlanTests(unittest.TestCase):
    def test_exact_target(self):
        t=CFG["target"]
        self.assertEqual(t["evidence_id"],"EACQ_5afda139f7d4dd9a1f33b7bb")
        self.assertEqual(t["symbol"],"002979.SZ")
        self.assertEqual(t["required_evidence_level"],6)
        self.assertEqual(t["required_tesla_link_level"],0)

    def test_fact_semantics(self):
        f=CFG["fact"]
        self.assertEqual(f["canonical_level"],6)
        self.assertEqual(f["canonical_tesla_link_level"],0)
        self.assertIn("人形机器人",f["fact_text"])
        self.assertIn("40%、70%、100%",f["fact_text"])
        self.assertIn("不证明Tesla/Optimus",f["fact_text"])

    def test_fact_not_pdf_policy(self):
        p=CFG["source_review_policy"]
        self.assertEqual(p["canonical_unit"],"FACT_NOT_PDF")
        self.assertEqual(p["anchor_post_materialization_state"],"REJECTED")
        self.assertIn("SOURCE_ABSORBED_BY_CANONICAL_FACT_V0_5_1",p["anchor_reject_reason_template"])

    def test_expected_state_does_not_upgrade_tesla_or_theme(self):
        x=CFG["expected_post_apply_semantics"]
        self.assertEqual(x["target_f_status"],"POSSIBLE")
        self.assertEqual(x["target_max_tesla_link_level"],0)
        self.assertFalse(x["theme_stage_change"])
        self.assertFalse(x["tesla_supplier_claim"])

    def test_apply_not_implemented(self):
        s=SCRIPT.read_text(encoding="utf-8")
        self.assertIn('choices=["check","plan","status"]',s)
        self.assertNotIn('choices=["check","plan","apply"',s)
        self.assertIn('"apply_implemented":False',s)

    def test_proposal_to_anchor_restores_registry_schema(self):
        q={
            "evidence_id":"E1","symbol":"002979.SZ","company":"雷赛智能",
            "component_domain":"control / encoders","evidence_level":6,
            "tesla_link_level":0,"source_type":"PRIMARY_CNINFO_OFFICIAL",
            "source_ref":"u","source_sha256":"s","event_time":"2026-10-09T00:00:00+08:00",
            "observed_at":"2026-10-10T14:55:45+08:00",
            "contradiction_kind":None,"contradiction_severity":None,
            "parser_version":"OPTIMUS_EVIDENCE_ACQUISITION_V0_4_4",
            "interpretation":"x","acquisition":{"pdf_path":"x.pdf"}
        }
        a=m.proposal_to_anchor(q)
        self.assertEqual(a["evidence_type"],"OFFICIAL_DISCLOSURE_FULLTEXT_HIT")
        self.assertEqual(a["review_state"],"PROVISIONAL")
        self.assertEqual(a["confidence"],0.82)

if __name__=="__main__":
    unittest.main()
