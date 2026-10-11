from __future__ import annotations
import importlib.util, json, sys, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"scripts"/"run_optimus_candidate_discovery_v072c.py"
CFG=json.loads((ROOT/"config/research/optimus_candidate_discovery_v072c.json").read_text(encoding="utf-8"))

spec=importlib.util.spec_from_file_location("v072c",SCRIPT)
m=importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name]=m
spec.loader.exec_module(m)

def cap(entity="New Supplier",symbol="999999.XY",lane="NEW_CANDIDATE_DISCOVERY"):
    return {
        "capture_identity":"CAP1",
        "source_id":"SRC_SEC_GENERIC",
        "source_role":"EXCHANGE_REGULATORY",
        "lane":lane,
        "entity_name":entity,
        "symbol":symbol,
        "entity_identifier":None,
        "source_url":"https://www.sec.gov/Archives/edgar/x",
        "document_title":"Doc",
        "published_at":"2026-10-09T00:00:00+08:00",
        "event_time":"2026-10-09T00:00:00+08:00",
        "observed_at":"2026-10-10T10:00:00+08:00"
    }

def cls(q="EVIDENCE_PROPOSAL",robot=None,cross=None):
    return {
        "queue_state":q,
        "reason_code":"TEST",
        "temporal_state":"PROSPECTIVE_VERIFIED" if q!="HISTORICAL_BASELINE_ONLY" else "HISTORICAL_PRE_CASE_START",
        "robot_context_signal_tags":robot if robot is not None else ["MASS_PRODUCTION"],
        "tesla_context_signal_tags":[],
        "global_signal_tags":["MASS_PRODUCTION"],
        "cross_section_signal_tags":cross or []
    }

class DiscoveryTests(unittest.TestCase):
    def test_current_22_symbol_is_excluded(self):
        member,why=m.current_member("Top Group","601689.SH",CFG)
        self.assertTrue(member)
        self.assertEqual(why,"SYMBOL_ALREADY_IN_CURRENT_22")

    def test_current_22_company_name_is_excluded(self):
        member,why=m.current_member("拓普集团股份有限公司",None,CFG)
        self.assertTrue(member)
        self.assertEqual(why,"COMPANY_NAME_ALREADY_IN_CURRENT_22")

    def test_outside_candidate_becomes_discovery_proposal(self):
        p,a=m.discovery_from_classification(cap(),cls(),CFG)
        self.assertIsNotNone(p)
        self.assertEqual(p["state"],"DISCOVERY_PROPOSAL")
        self.assertFalse(p["automatic_registry_add"])
        self.assertFalse(p["automatic_active_canonical"])

    def test_cross_section_only_is_rejected(self):
        c=cls("NO_NEW_MATERIAL_EVIDENCE",robot=[],cross=["MASS_PRODUCTION"])
        p,a=m.discovery_from_classification(cap(),c,CFG)
        self.assertIsNone(p)
        self.assertEqual(a["reason"],"CROSS_SECTION_ONLY")

    def test_historical_material_can_only_support_discovery(self):
        p,a=m.discovery_from_classification(cap(),cls("HISTORICAL_BASELINE_ONLY"),CFG)
        self.assertIsNotNone(p)
        self.assertTrue(p["historical_context_only"])
        self.assertFalse(p["prospective_theme_effect"])
        self.assertEqual(p["state"],"DISCOVERY_PROPOSAL")

    def test_contradiction_never_auto_rejects_candidate(self):
        p,a=m.discovery_from_classification(cap(),cls("CONTRADICTION_ALERT",robot=[]),CFG)
        self.assertIsNotNone(p)
        self.assertEqual(p["state"],"HUMAN_REVIEW_REQUIRED")

    def test_non_discovery_lane_skipped(self):
        p,a=m.discovery_from_classification(cap(lane="SUPPLIER_GLOBAL_OFFICIAL"),cls(),CFG)
        self.assertIsNone(p)
        self.assertEqual(a["status"],"SKIP_NON_DISCOVERY_LANE")

    def test_state_machine_requires_separate_registry_plan(self):
        self.assertIn("SEPARATE_REGISTRY_PLAN_REQUIRED",CFG["discovery_state_machine"])
        self.assertFalse(CFG["candidate_contract"]["automatic_registry_add"])

if __name__=="__main__":
    unittest.main()
