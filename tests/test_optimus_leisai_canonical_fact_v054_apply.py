from __future__ import annotations
import importlib.util, json, sys, tempfile, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"scripts/run_optimus_leisai_canonical_fact_v054_apply.py"
CFG=json.loads((ROOT/"config/research/optimus_leisai_canonical_fact_v054_apply.json").read_text(encoding="utf-8"))

spec=importlib.util.spec_from_file_location("leisai_apply",SCRIPT)
m=importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name]=m
spec.loader.exec_module(m)

class LeisaiGuardedApplyTests(unittest.TestCase):
    def test_exact_approved_identity(self):
        a=CFG["approved_plan"]
        self.assertEqual(a["sha256"],"748c1ca8206ea355c2b42252817ec407d23ceca22ef2903c182e500733bfe4dc")
        self.assertEqual(a["batch_id"],"CB_7e2476e590e0bbe674ac8848")
        self.assertEqual(a["fact_id"],"CF_fe5714e699b2e5c1a34c979c")
        self.assertEqual(a["materialized_evidence_id"],"CFE_ef6afb1d4f48e8166d81bfdb")

    def test_post_counts(self):
        x=CFG["expected_post"]
        self.assertEqual(x["evidence_count"],163)
        self.assertEqual(x["review_decision_count"],150)
        self.assertEqual(x["canonical_fact_count"],25)
        self.assertEqual(x["materialized_fact_evidence_count"],25)
        self.assertEqual(x["provisional_evidence_count"],2)

    def test_no_tesla_or_theme_upgrade(self):
        x=CFG["expected_post"]
        self.assertEqual(x["target_max_tesla_link_level"],0)
        self.assertFalse(x["theme_stage_change"])
        self.assertFalse(x["tesla_supplier_claim"])

    def test_apply_requires_exact_approval_sha(self):
        s=SCRIPT.read_text(encoding="utf-8")
        self.assertIn("PLAN_SHA256_APPROVAL_MISMATCH",s)
        self.assertIn('choices=["check","simulate","apply","status"]',s)

    def test_safety_layers_present(self):
        s=SCRIPT.read_text(encoding="utf-8")
        self.assertIn("simulate(cfg,actor)",s)
        self.assertIn("shutil.copy2(db,backup)",s)
        self.assertIn("ROLLBACK",s)
        self.assertIn("audit_fact_materialization",s)

    def test_source_zero_tesla_is_not_treated_missing(self):
        s=SCRIPT.read_text(encoding="utf-8")
        self.assertIn("tv if tv is not None else -1",s)

if __name__=="__main__":
    unittest.main()
