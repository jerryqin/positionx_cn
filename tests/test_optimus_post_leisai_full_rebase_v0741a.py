from __future__ import annotations
import hashlib, json, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
CFG=json.loads((ROOT/"config/research/optimus_post_leisai_full_rebase_v0741a.json").read_text(encoding="utf-8"))

def sha(p:Path)->str:
    return hashlib.sha256(p.read_bytes()).hexdigest()

class FullRebaseTests(unittest.TestCase):
    def test_payload_hashes_exact(self):
        for rel,spec in CFG["files"].items():
            self.assertEqual(sha(ROOT/"payload"/rel),spec["new_sha256"],rel)

    def test_windows_newline_bug_removed(self):
        s=(ROOT/"scripts/run_optimus_post_leisai_full_rebase_v0741a.py").read_text(encoding="utf-8")
        self.assertIn("tmp.write_bytes(src.read_bytes())",s)
        self.assertNotIn("tmp.write_text(",s)

    def test_all_post_apply_contracts_rebased(self):
        required={
            "scripts/run_optimus_prospective_monitor_v07.py",
            "config/research/optimus_prospective_monitor_v07.json",
            "config/research/optimus_prospective_intake_v071.json",
            "scripts/run_optimus_global_discovery_monitor_v072.py",
            "config/research/optimus_global_discovery_monitor_v072.json",
            "scripts/run_optimus_external_inbox_v072a.py",
            "config/research/optimus_external_inbox_v072a.json",
            "config/research/optimus_external_classifier_v072b.json",
            "config/research/optimus_candidate_discovery_v072c.json",
            "config/research/optimus_steady_state_baseline_v074.json",
        }
        self.assertEqual(set(CFG["files"]),required)

    def test_new_monitor_contract_points_to_post_apply_state(self):
        v=json.loads((ROOT/"payload/config/research/optimus_prospective_monitor_v07.json").read_text(encoding="utf-8"))
        self.assertEqual(v["baseline"]["registry_sha256"],"d51bbc34dc0731667cd7c2991b11cb954fd3747c9b1c9c6c6154f40cbbc9ccaa")
        self.assertEqual(v["baseline"]["canonical_fact_count"],25)
        self.assertEqual(v["baseline"]["materialized_fact_evidence_count"],25)
        self.assertEqual(v["baseline"]["latest_batch_id"],"CB_7e2476e590e0bbe674ac8848")

    def test_current22_partition_unchanged(self):
        v=json.loads((ROOT/"payload/config/research/optimus_prospective_monitor_v07.json").read_text(encoding="utf-8"))
        self.assertEqual(len(v["universe"]),22)
        syms={x["symbol"] for x in v["universe"]}
        self.assertNotIn("603416.SH",syms); self.assertNotIn("603809.SH",syms)

    def test_steady_baseline_code_freeze_matches_payload(self):
        b=json.loads((ROOT/"payload/config/research/optimus_steady_state_baseline_v074.json").read_text(encoding="utf-8"))
        for rel,spec in CFG["files"].items():
            if rel=="config/research/optimus_steady_state_baseline_v074.json": continue
            if rel in b["code_freeze"]: self.assertEqual(b["code_freeze"][rel],spec["new_sha256"],rel)
        self.assertEqual(b["registry"]["registry_sha256"],"d51bbc34dc0731667cd7c2991b11cb954fd3747c9b1c9c6c6154f40cbbc9ccaa")
        self.assertEqual(b["registry"]["evidence_count"],163)
        self.assertEqual(b["registry"]["review_decision_count"],150)
        self.assertEqual(b["registry"]["canonical_fact_count"],25)

    def test_no_research_db_mutation_code(self):
        s=(ROOT/"scripts/run_optimus_post_leisai_full_rebase_v0741a.py").read_text(encoding="utf-8")
        self.assertNotIn("duckdb.connect",s)
        self.assertNotIn("INSERT INTO",s)
        self.assertNotIn("UPDATE ",s)

if __name__=="__main__":
    unittest.main()
