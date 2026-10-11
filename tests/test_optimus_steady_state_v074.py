from __future__ import annotations
import json, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
CFG=json.loads((ROOT/"config/research/optimus_steady_state_baseline_v074.json").read_text(encoding="utf-8"))
SCRIPT=(ROOT/"scripts/run_optimus_steady_state_v074.py").read_text(encoding="utf-8")
RUNBOOK=(ROOT/"docs/optimus/OPTIMUS_STEADY_STATE_RUNBOOK_V0_7_4.md").read_text(encoding="utf-8")

class SteadyStateBaselineTests(unittest.TestCase):
    def test_registry_shape(self):
        r=CFG["registry"]
        self.assertEqual((r["candidate_count"],r["active_canonical"],r["discovery_only"],r["active_seed"]),(24,22,2,0))
        self.assertEqual(r["canonical_candidates"],22)
        self.assertEqual(r["provisional_evidence_count"],2)

    def test_current22_and_shadow_are_disjoint(self):
        p=CFG["monitoring_partition"]
        current=set(p["current_canonical_monitor_symbols"])
        shadow={x["symbol"] for x in p["discovery_shadow"]}
        self.assertEqual(len(current),22)
        self.assertEqual(shadow,{"603416.SH","603809.SH"})
        self.assertFalse(current & shadow)

    def test_600353_remains_unresolved(self):
        x=CFG["monitoring_partition"]["unresolved_outsider"]
        self.assertEqual(x["symbol"],"600353.SH")
        self.assertEqual(x["state"],"WAITING_CAPTURE_REPAIR")
        self.assertEqual(x["registry_action"],"NONE")

    def test_b8_real_probe(self):
        x=CFG["validated_runtime"]["b8_dedupe_probe"]
        self.assertEqual((x["classified_count"],x["queue_count"],x["skipped_already_classified"],x["error_count"]),(0,0,1,0))

    def test_c8_real_scan(self):
        x=CFG["validated_runtime"]["c8_scan"]
        self.assertEqual(x["proposal_count"],0)
        self.assertEqual(x["waiting_capture_repair_count"],1)
        self.assertEqual(x["existing_registry_skipped_before_classification"],4)

    def test_governance(self):
        g=CFG["governance"]
        self.assertTrue(g["registry_mutation_requires_separate_plan_and_explicit_apply_approval"])
        self.assertTrue(g["canonicalization_requires_separate_review"])
        self.assertTrue(g["theme_stage_mutation_requires_separate_human_review"])
        self.assertTrue(g["provisional_evidence_never_auto_promotes"])
        self.assertFalse(g["broker"])
        self.assertFalse(g["orders"])

    def test_frozen_history(self):
        self.assertIn("DO_NOT_RERUN",CFG["frozen_history"]["v0_6"]["status"])
        self.assertIn("DO_NOT_RERUN",CFG["frozen_history"]["v0_7_3"]["status"])
        self.assertIn("Do not rerun V0.7.3 PLAN/APPLY",RUNBOOK)

    def test_checker_is_read_only(self):
        self.assertIn('choices=["check","status"]',SCRIPT)
        self.assertNotIn('add_parser("apply")',SCRIPT)
        self.assertNotIn('add_parser("plan")',SCRIPT)
        self.assertNotIn('add_parser("ingest")',SCRIPT)

if __name__=="__main__":
    unittest.main()
