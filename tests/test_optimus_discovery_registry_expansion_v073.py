from __future__ import annotations
import json, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
CFG=json.loads((ROOT/"config/research/optimus_discovery_registry_expansion_v073_plan.json").read_text(encoding="utf-8"))
SCRIPT=(ROOT/"scripts/run_optimus_discovery_registry_expansion_v073.py").read_text(encoding="utf-8")

class RegistryExpansionPlanTests(unittest.TestCase):
    def test_exact_two_recommended_symbols(self):
        self.assertEqual(
            {x["symbol"] for x in CFG["human_review_recommendations"]},
            {"603416.SH","603809.SH"}
        )

    def test_600353_is_deferred(self):
        self.assertEqual(CFG["deferred_candidates"][0]["symbol"],"600353.SH")
        self.assertEqual(CFG["deferred_candidates"][0]["registry_action"],"NONE")

    def test_tesla_link_is_zero_for_both(self):
        self.assertEqual(CFG["registry_bridge_contract"]["tesla_link_level"],0)
        self.assertTrue(all(not x["required_tesla_robot_link_eligible"] for x in CFG["human_review_recommendations"]))

    def test_registry_bridge_is_provisional_discovery_only(self):
        b=CFG["registry_bridge_contract"]
        self.assertEqual(b["review_state"],"PROVISIONAL")
        self.assertEqual(b["expected_lifecycle_after_apply"],"DISCOVERY_ONLY")
        self.assertEqual(b["expected_f_status_after_apply"],"SPECULATIVE")
        self.assertFalse(b["canonical"])
        self.assertTrue(b["separate_apply_approval_required"])

    def test_plan_has_no_apply_command(self):
        self.assertNotIn('sp.add_parser("apply")',SCRIPT)
        self.assertIn('"apply_implemented":False',SCRIPT)

    def test_expected_post_apply_preview(self):
        x=CFG["expected_post_apply_preview"]
        self.assertEqual(x["candidate_count"],24)
        self.assertEqual(x["active_canonical"],22)
        self.assertEqual(x["discovery_only"],2)
        self.assertEqual(x["canonical_candidates"],22)

if __name__=="__main__":
    unittest.main()
