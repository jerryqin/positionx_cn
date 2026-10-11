from __future__ import annotations
import json, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
CFG=json.loads((ROOT/"config/research/optimus_global_discovery_monitor_v072.json").read_text(encoding="utf-8"))

class ContractTests(unittest.TestCase):
    def test_three_lanes_exist(self):
        self.assertEqual(set(CFG["lanes"]), {"THEME_OFFICIAL","SUPPLIER_GLOBAL_OFFICIAL","NEW_CANDIDATE_DISCOVERY"})

    def test_search_results_are_recall_only(self):
        lane=CFG["lanes"]["THEME_OFFICIAL"]
        self.assertEqual(lane["search_engine_role"],"RECALL_ONLY")
        self.assertFalse(lane["search_result_is_fact"])

    def test_discovery_never_auto_adds(self):
        lane=CFG["lanes"]["NEW_CANDIDATE_DISCOVERY"]
        self.assertFalse(lane["automatic_registry_add"])
        self.assertFalse(lane["automatic_active_canonical"])
        self.assertTrue(lane["human_review_required"])

    def test_t2_is_not_tesla(self):
        self.assertTrue(CFG["evidence_contract"]["t2_not_tesla_confirmation"])
        self.assertTrue(CFG["evidence_contract"]["named_tesla_link_required_for_t3_plus"])

    def test_theme_never_auto_changes(self):
        self.assertFalse(CFG["lanes"]["THEME_OFFICIAL"]["automatic_stage_change"])
        self.assertFalse(CFG["safety"]["theme_stage_mutation"])

    def test_all_implementation_phases_are_read_only(self):
        self.assertTrue(all(not x["mutation"] for x in CFG["implementation_phases"]))

    def test_safety(self):
        s=CFG["safety"]
        self.assertTrue(s["research_only"])
        self.assertFalse(s["broker"])
        self.assertFalse(s["orders"])
        self.assertFalse(s["production_db"])
        self.assertFalse(s["registry_mutation"])
        self.assertFalse(s["canonicalization_mutation"])

if __name__=="__main__":
    unittest.main()
