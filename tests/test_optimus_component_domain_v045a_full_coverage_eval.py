from __future__ import annotations
import importlib.util, json, sys, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"scripts/run_optimus_component_domain_v045a_full_coverage_eval.py"
CFG=json.loads((ROOT/"config/research/optimus_component_domain_v045a_full_coverage_eval.json").read_text(encoding="utf-8"))

spec=importlib.util.spec_from_file_location("v045a_eval",SCRIPT)
m=importlib.util.module_from_spec(spec); assert spec and spec.loader
sys.modules[spec.name]=m; spec.loader.exec_module(m)

class EvalTests(unittest.TestCase):
    def test_audit_asset_is_exactly_frozen(self):
        a=CFG["audit_asset"]
        self.assertEqual(a["manifest_payload_sha256"],"789c79c1e505b6d9e68d5cac3dfff6fb56f558bfd5fa9e7263edc7447dc080c0")
        self.assertEqual(a["rows_sha256"],"da7e49b69df9a0ceb2facc8dd27fba8d8e47eefccd7cac67cd615d74d09a4ad8")
        self.assertEqual(a["expected_counts"]["recomputed_rows"],50)
        self.assertEqual(a["expected_counts"]["first_match_minority_risk_rows"],8)

    def test_leisai_candidate_selects_control(self):
        rows=[
            {"map_index":9,"domain":"control / encoders","window_hit_count":17,"occurrence_count":35},
            {"map_index":2,"domain":"motors / motor components","window_hit_count":11,"occurrence_count":22},
            {"map_index":1,"domain":"reducers / precision transmission","window_hit_count":3,"occurrence_count":3},
        ]
        x=m.candidate_domain(CFG,"reducers / precision transmission",rows)
        self.assertEqual(x["candidate_domain"],"control / encoders")
        self.assertEqual(x["decision"],"NEW_UNIQUE_DOMINANT_DOMAIN")

    def test_tie_is_ambiguous(self):
        rows=[
            {"map_index":1,"domain":"reducers / precision transmission","window_hit_count":3,"occurrence_count":3},
            {"map_index":2,"domain":"motors / motor components","window_hit_count":3,"occurrence_count":9},
        ]
        x=m.candidate_domain(CFG,"reducers / precision transmission",rows)
        self.assertEqual(x["candidate_domain"],CFG["candidate_policy"]["ambiguous_domain"])

    def test_weak_new_top_is_ambiguous(self):
        rows=[
            {"map_index":2,"domain":"motors / motor components","window_hit_count":5,"occurrence_count":10},
            {"map_index":1,"domain":"reducers / precision transmission","window_hit_count":4,"occurrence_count":4},
        ]
        x=m.candidate_domain(CFG,"reducers / precision transmission",rows)
        self.assertEqual(x["candidate_domain"],CFG["candidate_policy"]["ambiguous_domain"])

    def test_old_unique_top_is_kept(self):
        rows=[
            {"map_index":1,"domain":"reducers / precision transmission","window_hit_count":7,"occurrence_count":9},
            {"map_index":2,"domain":"motors / motor components","window_hit_count":4,"occurrence_count":8},
        ]
        x=m.candidate_domain(CFG,"reducers / precision transmission",rows)
        self.assertEqual(x["candidate_domain"],"reducers / precision transmission")

    def test_read_only_no_pdf_reparse(self):
        s=SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("extract_pdf_text",s)
        self.assertNotIn("duckdb",s)
        self.assertNotIn("INSERT INTO",s)
        self.assertNotIn("UPDATE ",s)
        self.assertNotIn("DELETE FROM",s)

if __name__=="__main__":
    unittest.main()
