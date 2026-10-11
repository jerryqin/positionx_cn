from __future__ import annotations
import importlib.util, json, sys, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"scripts/run_optimus_component_domain_v045_candidate_eval.py"
CFG=json.loads((ROOT/"config/research/optimus_component_domain_v045_candidate_eval.json").read_text(encoding="utf-8"))

spec=importlib.util.spec_from_file_location("v045_eval",SCRIPT)
m=importlib.util.module_from_spec(spec); assert spec and spec.loader
sys.modules[spec.name]=m; spec.loader.exec_module(m)

class EvalTests(unittest.TestCase):
    def test_leisai_threshold_would_select_control(self):
        rows=[
            {"map_index":9,"domain":"control / encoders","window_hit_count":17,"occurrence_count":35},
            {"map_index":2,"domain":"motors / motor components","window_hit_count":11,"occurrence_count":22},
            {"map_index":1,"domain":"reducers / precision transmission","window_hit_count":3,"occurrence_count":3},
        ]
        x=m.candidate_domain(CFG,"reducers / precision transmission",rows)
        self.assertEqual(x["candidate_domain"],"control / encoders")
        self.assertEqual(x["decision"],"NEW_UNIQUE_DOMINANT_DOMAIN")

    def test_tie_becomes_ambiguous(self):
        rows=[
            {"map_index":1,"domain":"reducers / precision transmission","window_hit_count":3,"occurrence_count":3},
            {"map_index":2,"domain":"motors / motor components","window_hit_count":3,"occurrence_count":9},
        ]
        x=m.candidate_domain(CFG,"reducers / precision transmission",rows)
        self.assertEqual(x["candidate_domain"],CFG["candidate_policy"]["ambiguous_domain"])

    def test_unique_old_top_is_kept(self):
        rows=[
            {"map_index":1,"domain":"reducers / precision transmission","window_hit_count":7,"occurrence_count":9},
            {"map_index":2,"domain":"motors / motor components","window_hit_count":4,"occurrence_count":8},
        ]
        x=m.candidate_domain(CFG,"reducers / precision transmission",rows)
        self.assertEqual(x["candidate_domain"],"reducers / precision transmission")

    def test_weak_new_top_becomes_ambiguous(self):
        rows=[
            {"map_index":2,"domain":"motors / motor components","window_hit_count":5,"occurrence_count":10},
            {"map_index":1,"domain":"reducers / precision transmission","window_hit_count":4,"occurrence_count":4},
        ]
        x=m.candidate_domain(CFG,"reducers / precision transmission",rows)
        self.assertEqual(x["candidate_domain"],CFG["candidate_policy"]["ambiguous_domain"])

    def test_read_only(self):
        s=SCRIPT.read_text(encoding="utf-8")
        self.assertIn("read_only=True",s)
        self.assertNotIn("INSERT INTO",s)
        self.assertNotIn("UPDATE ",s)
        self.assertNotIn("DELETE FROM",s)

    def test_future_only_contract(self):
        self.assertFalse(CFG["candidate_policy"]["historical_reclassification"])
        self.assertEqual(CFG["candidate_policy"]["future_parser_version_if_adopted"],
                         "OPTIMUS_EVIDENCE_ACQUISITION_V0_4_5")

if __name__=="__main__":
    unittest.main()
