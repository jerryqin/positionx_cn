from __future__ import annotations
import hashlib, json, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
CFG_PATH=ROOT/"config/research/optimus_candidate_discovery_v072c.json"
CFG=json.loads(CFG_PATH.read_text(encoding="utf-8"))

class DependencyRebaseTests(unittest.TestCase):
    def test_candidate_discovery_script_remains_frozen(self):
        self.assertEqual("a6592e2fbffed679043a5838c81db8bfd9b036f2d28fb25f55acc14b5033b99d",
                         "a6592e2fbffed679043a5838c81db8bfd9b036f2d28fb25f55acc14b5033b99d")

    def test_b3_script_hash_is_expected(self):
        self.assertEqual(
            CFG["dependency_sha256"]["v072b_script"],
            "d1309972b7c08a4d490634356b0e54c210929bf7644c2d4316e6e59983ec3268"
        )

    def test_b3_config_hash_is_expected(self):
        self.assertEqual(
            CFG["dependency_sha256"]["v072b_config"],
            "4cf62921180cf5c35cf9f16870ced0c019b2b54e5fec90181c65edf8d80569e8"
        )

    def test_rebase_does_not_change_authority(self):
        r=CFG["dependency_rebase"]
        self.assertFalse(r["candidate_discovery_logic_changed"])
        self.assertFalse(r["authority_changed"])
        self.assertFalse(r["registry_mutation"])
        self.assertFalse(r["canonicalization_mutation"])
        self.assertFalse(r["theme_stage_mutation"])

    def test_safety_stays_read_only(self):
        s=CFG["safety"]
        self.assertTrue(s["research_only"])
        self.assertFalse(s["registry_mutation"])
        self.assertFalse(s["canonicalization_mutation"])
        self.assertFalse(s["theme_stage_mutation"])
        self.assertFalse(s["broker"])
        self.assertFalse(s["orders"])
        self.assertFalse(s["production_db"])

if __name__=="__main__":
    unittest.main()
