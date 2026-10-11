from __future__ import annotations
import importlib.util, json, sys, tempfile, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"scripts"/"run_optimus_prospective_intake_v071.py"
CFG=ROOT/"config"/"research"/"optimus_prospective_intake_v071.json"

spec=importlib.util.spec_from_file_location("v071",SCRIPT)
m=importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name]=m
spec.loader.exec_module(m)

class IntakeContractTests(unittest.TestCase):
    def setUp(self):
        self.cfg=json.loads(CFG.read_text(encoding="utf-8"))

    def test_read_only_safety(self):
        s=self.cfg["safety"]
        self.assertTrue(s["research_only"])
        self.assertFalse(s["registry_mutation"])
        self.assertFalse(s["canonicalization_mutation"])
        self.assertFalse(s["evidence_inbox_write"])
        self.assertFalse(s["broker"])
        self.assertFalse(s["orders"])
        self.assertFalse(s["production_db"])

    def test_stage2_requires_named_link(self):
        p=self.cfg["queue_policy"]
        self.assertEqual(p["named_tesla_link_min_level"],3)
        self.assertEqual(p["named_tesla_link_output"],"STAGE2_GATE_CANDIDATE")

    def test_t2_is_not_stage2(self):
        self.assertEqual(m.queue_class({
            "tesla_link_level":2,
            "contradiction_severity":None
        }),"HUMAN_REVIEW_REQUIRED")

    def test_named_t3_is_stage2_candidate_only(self):
        self.assertEqual(m.queue_class({
            "tesla_link_level":3,
            "contradiction_severity":None
        }),"STAGE2_GATE_CANDIDATE")

    def test_hard_contradiction_has_priority(self):
        self.assertEqual(m.queue_class({
            "tesla_link_level":4,
            "contradiction_severity":"HARD"
        }),"CONTRADICTION_ALERT")

    def test_other_material_evidence_is_proposal(self):
        self.assertEqual(m.queue_class({
            "tesla_link_level":0,
            "contradiction_severity":None
        }),"EVIDENCE_PROPOSAL")

    def test_previous_queue_dedupe(self):
        with tempfile.TemporaryDirectory() as td:
            r=Path(td)
            p=r/"20261010"/"x"/"proposal_queue.jsonl"
            p.parent.mkdir(parents=True)
            p.write_text(json.dumps({"evidence_id":"E1"})+"\n",encoding="utf-8")
            self.assertEqual(m.previous_monitor_ids(r),{"E1"})

if __name__=="__main__":
    unittest.main()
