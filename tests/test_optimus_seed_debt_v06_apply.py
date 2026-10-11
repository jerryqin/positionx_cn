from __future__ import annotations
import importlib.util, json, hashlib, sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_optimus_seed_debt_v06_apply.py"

spec = importlib.util.spec_from_file_location("v06apply", SCRIPT)
m = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = m
spec.loader.exec_module(m)

# In the ZIP/source tree the plan exists under ./artifacts.
# After installation the authoritative plan is intentionally stored under
# D:\positionx_data\research\big_opportunity\optimus\plans\...
PACKAGE_PLAN = ROOT / "artifacts" / "OPTIMUS_SEED_DEBT_V0_6_APPLY_PLAN.json"
PLAN = PACKAGE_PLAN if PACKAGE_PLAN.exists() else m.APPLY_PLAN

class Tests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(PLAN.exists(), f"APPLY plan not found: {PLAN}")
        self.plan = json.loads(PLAN.read_text(encoding="utf-8"))

    def test_plan_sha_is_frozen(self):
        got = hashlib.sha256(PLAN.read_bytes()).hexdigest()
        self.assertEqual(got, m.APPLY_PLAN_SHA256)

    def test_plan_shape(self):
        self.assertEqual(self.plan["schema_version"], "OPTIMUS_CANONICALIZATION_PLAN_V0_5")
        self.assertEqual(len(self.plan["facts"]), 6)
        self.assertEqual(len({x["symbol"] for x in self.plan["facts"]}), 5)
        self.assertEqual(len(self.plan["reject_only"]), 3)
        self.assertTrue(all(x["canonical_tesla_link_level"] == 0 for x in self.plan["facts"]))

    def test_exact_seed_scope_and_target(self):
        self.assertEqual(m.SEED_SYMBOLS, {"601100.SH","603009.SH","603119.SH","601689.SH","600699.SH"})
        post = self.plan["expected_post_state"]
        self.assertEqual(post["active_canonical"], 22)
        self.assertEqual(post["active_seed"], 0)
        self.assertEqual(post["canonical_fact_count"], 24)
        self.assertFalse(post["theme_stage_change"])
        self.assertEqual(post["new_tesla_links_for_five"], "T0")

    def test_all_7_bridge_proposals_are_accounted_for_once(self):
        covered = []
        for f in self.plan["facts"]:
            covered.append(f["anchor_evidence_id"])
            covered.extend(f.get("support_evidence_ids", []))
        covered.extend(x["evidence_id"] for x in self.plan["reject_only"])
        raw = [x for x in covered if x.startswith("EACQ_")]
        self.assertEqual(set(raw), set(m.RAW_EXPECTED))
        self.assertEqual(len(raw), 7)

    def test_beite_l8_is_not_canonicalized(self):
        fact = next(x for x in self.plan["facts"] if x["symbol"] == "603009.SH")
        self.assertEqual(fact["canonical_level"], 2)
        self.assertEqual(fact["anchor_evidence_id"], m.CUSTOM_IDS["BEITE_L2"])
        rejects = {x["evidence_id"]: x["reason_code"] for x in self.plan["reject_only"]}
        self.assertIn("LEVEL_OVERCLASSIFICATION", rejects["EACQ_c0be286111b64a60dae0aaea"])

    def test_external_ids_are_distinct(self):
        vals = list(m.CUSTOM_IDS.values())
        self.assertEqual(len(vals), len(set(vals)))
        self.assertEqual(len(vals), 5)

    def test_review_queue_expectation(self):
        self.assertEqual(len(m.RAW_EXPECTED), 7)
        self.assertTrue(all(x["level"] in {1,2,8,9} for x in m.RAW_EXPECTED.values()))

if __name__ == "__main__":
    unittest.main()
