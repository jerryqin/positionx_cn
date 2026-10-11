from __future__ import annotations
import importlib.util, json, sys, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"scripts/run_optimus_steady_state_rebase_v0741.py"
CFG=json.loads((ROOT/"config/research/optimus_steady_state_rebase_v0741.json").read_text(encoding="utf-8"))

spec=importlib.util.spec_from_file_location("rebase_v0741",SCRIPT)
m=importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name]=m
spec.loader.exec_module(m)

class RebaseTests(unittest.TestCase):
    def test_exact_post_registry(self):
        p=CFG["post_apply"]
        self.assertEqual(p["registry_sha256"],"d51bbc34dc0731667cd7c2991b11cb954fd3747c9b1c9c6c6154f40cbbc9ccaa")
        self.assertEqual(p["evidence_count"],163)
        self.assertEqual(p["review_decision_count"],150)
        self.assertEqual(p["canonical_fact_count"],25)
        self.assertEqual(p["materialized_fact_evidence_count"],25)

    def test_exact_governance_identity(self):
        p=CFG["post_apply"]
        self.assertEqual(p["batch_id"],"CB_7e2476e590e0bbe674ac8848")
        self.assertEqual(p["fact_id"],"CF_fe5714e699b2e5c1a34c979c")
        self.assertEqual(p["result_manifest_sha256"],"e79e591e4e0db9d1efbd379c74a4891042d2d57b9b613660913d11ed335965ba")

    def test_baseline_builder_only_rebases_expected_fields(self):
        old={
            "baseline_id":"OLD","baseline_date":"x","mode":"STEADY_STATE_OPERATIONS",
            "registry":{
                "registry_sha256":"old","candidate_count":24,"active_canonical":22,
                "discovery_only":2,"active_seed":0,"canonical_candidates":22,
                "needs_review_candidates":2,"evidence_count":161,"review_decision_count":149,
                "provisional_evidence_count":2,"canonical_fact_count":24,
                "materialized_fact_evidence_count":24,"f_status_counts":{},
                "maintenance_counts":{}
            },
            "monitoring_partition":{"current_canonical_monitor_count":22},
            "theme":{"stage":"Stage 1"},
            "frozen_history":{"v0_7_3":{"status":"FROZEN"}},
        }
        new=m.build_new_baseline(CFG,old)
        self.assertEqual(new["registry"]["registry_sha256"],CFG["post_apply"]["registry_sha256"])
        self.assertEqual(new["registry"]["canonical_fact_count"],25)
        self.assertEqual(new["monitoring_partition"],old["monitoring_partition"])
        self.assertEqual(new["theme"],old["theme"])
        self.assertEqual(new["frozen_history"]["v0_7_3"],old["frozen_history"]["v0_7_3"])
        self.assertIn("v0_5_4_leisai_canonicalization",new["frozen_history"])

    def test_no_research_db_write_api_in_rebase(self):
        s=SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("duckdb.connect",s)
        self.assertNotIn("INSERT INTO",s)
        self.assertNotIn("UPDATE ",s)
        self.assertIn("BASELINE_CONFIG_ONLY",s)

if __name__=="__main__":
    unittest.main()
