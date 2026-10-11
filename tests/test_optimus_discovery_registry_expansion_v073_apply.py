from __future__ import annotations
import importlib.util, json, sys, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"scripts"/"run_optimus_discovery_registry_expansion_v073_apply.py"
CFG=json.loads((ROOT/"config/research/optimus_discovery_registry_expansion_v073_apply.json").read_text(encoding="utf-8"))

spec=importlib.util.spec_from_file_location("v073apply",SCRIPT)
m=importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name]=m
spec.loader.exec_module(m)

class ApplyContractTests(unittest.TestCase):
    def test_exact_approved_symbols(self):
        self.assertEqual(CFG["approved_plan"]["symbols"],["603416.SH","603809.SH"])

    def test_approval_is_exact_and_recorded(self):
        self.assertTrue(CFG["approval"]["approved"])
        self.assertEqual(CFG["approval"]["approval_token"],"V0.7.3_603416_603809_APPROVED")

    def test_precondition_registry_sha_frozen(self):
        self.assertEqual(
            CFG["registry_precondition"]["registry_sha256"],
            "da749d2c79b64695fddbbda575900d6e047be39e479e3a836eb73c0d1fbbe2ca"
        )

    def test_plan_asset_hashes_frozen(self):
        p=CFG["approved_plan"]
        self.assertEqual(p["manifest_payload_sha256"],"cf6dad84cb60e67d3e27962b5b8dcf80da0ae862b561abb25c148143d0d5142e")
        self.assertEqual(p["planned_evidence_sha256"],"72c351f96216b69d2161e24d376a834c35fcac1a1eef5d1cb4be37184686a91f")
        self.assertEqual(p["human_review_sha256"],"856c335676669507a0ec2f3b0c2460d6c8ac7029847ba22cf3c2a6459d06b27a")

    def test_postconditions_keep_22_canonical(self):
        p=CFG["postconditions"]
        self.assertEqual(p["candidate_count"],24)
        self.assertEqual(p["active_canonical"],22)
        self.assertEqual(p["discovery_only"],2)
        self.assertEqual(p["canonical_candidates"],22)

    def test_new_candidates_are_discovery_only_speculative(self):
        for sym,x in CFG["postconditions"]["symbols"].items():
            self.assertEqual(x["lifecycle_state"],"DISCOVERY_ONLY")
            self.assertEqual(x["f_status"],"SPECULATIVE")
            self.assertFalse(x["canonical"])
            self.assertTrue(x["needs_review"])
            self.assertEqual(x["max_tesla_link_level"],0)
            self.assertEqual(x["provisional_evidence_count"],1)

    def test_wrapper_has_simulation_backup_and_restore(self):
        src=SCRIPT.read_text(encoding="utf-8")
        self.assertIn("simulation(cfg,evidence_file,mod,rcfg,pre_status,out)",src)
        self.assertIn("copy_db_backup",src)
        self.assertIn("restore_db",src)
        self.assertIn("REAL_SIM_REGISTRY_SHA_MISMATCH",src)

    def test_no_canonical_review_is_performed(self):
        src=SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn('add_review_decision(',src)
        self.assertIn('allow_canonical_input=False',src)

if __name__=="__main__":
    unittest.main()
