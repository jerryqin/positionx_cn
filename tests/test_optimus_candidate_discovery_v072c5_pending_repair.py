from __future__ import annotations
import json, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
C=ROOT/"scripts"/"run_optimus_candidate_discovery_v072c.py"
CFG=json.loads((ROOT/"config/research/optimus_candidate_discovery_v072c.json").read_text(encoding="utf-8"))

class PendingRepairContractTests(unittest.TestCase):
    def test_failed_capture_policy_is_fail_closed(self):
        p=CFG["scan_error_policy"]
        self.assertTrue(p["immutable_failed_capture_is_not_deleted"])
        self.assertEqual(p["classification_failure_disposition"],"WAITING_CAPTURE_REPAIR")
        self.assertTrue(p["continue_independent_captures"])
        self.assertTrue(p["failed_capture_creates_no_discovery_inference"])
        self.assertTrue(p["partial_scan_is_explicit"])

    def test_scan_source_contains_explicit_pending_repair(self):
        src=C.read_text(encoding="utf-8")
        self.assertIn("WAITING_CAPTURE_REPAIR",src)
        self.assertIn("PARTIAL_OPTIMUS_CANDIDATE_DISCOVERY_SCAN_V0_7_2C_WAITING_CAPTURE_REPAIR",src)
        self.assertIn("waiting_capture_repair_count",src)

    def test_safety_unchanged(self):
        s=CFG["safety"]
        self.assertFalse(s["registry_mutation"])
        self.assertFalse(s["canonicalization_mutation"])
        self.assertFalse(s["theme_stage_mutation"])
        self.assertFalse(s["broker"])
        self.assertFalse(s["orders"])

if __name__=="__main__":
    unittest.main()
