from __future__ import annotations
import importlib.util, json, sys, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"scripts"/"run_optimus_prospective_monitor_v07.py"
CFG=ROOT/"config"/"research"/"optimus_prospective_monitor_v07.json"

spec=importlib.util.spec_from_file_location("v07",SCRIPT)
m=importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name]=m
spec.loader.exec_module(m)

class ContractTests(unittest.TestCase):
    def setUp(self):
        self.cfg=json.loads(CFG.read_text(encoding="utf-8"))

    def test_universe_is_exactly_22(self):
        self.assertEqual(len(self.cfg["universe"]),22)
        self.assertEqual(len({x["symbol"] for x in self.cfg["universe"]}),22)

    def test_seed_debt_is_closed(self):
        b=self.cfg["baseline"]
        self.assertEqual(b["active_canonical"],22)
        self.assertEqual(b["active_seed"],0)
        self.assertEqual(b["seed_audit_debt"],0)

    def test_theme_does_not_auto_promote(self):
        self.assertEqual(self.cfg["theme"]["stage"],1)
        self.assertFalse(self.cfg["theme"]["auto_stage_mutation"])
        self.assertFalse(self.cfg["decision_contract"]["auto_theme_stage_change"])

    def test_no_auto_canonical_or_state_mutation(self):
        d=self.cfg["decision_contract"]
        self.assertFalse(d["auto_canonical"])
        self.assertFalse(d["auto_candidate_state_change"])
        self.assertTrue(d["human_review_required"])

    def test_tesla_named_evidence_required(self):
        self.assertTrue(self.cfg["decision_contract"]["tesla_link_requires_named_verifiable_evidence"])
        self.assertIn("T2 unnamed head-customer clue", self.cfg["signal_families"]["NOISE_ONLY"])

    def test_safety(self):
        s=self.cfg["safety"]
        self.assertTrue(s["research_only"])
        self.assertFalse(s["broker"])
        self.assertFalse(s["orders"])
        self.assertFalse(s["production_db"])
        self.assertFalse(s["registry_mutation"])
        self.assertFalse(s["canonicalization_mutation"])

if __name__=="__main__":
    unittest.main()
