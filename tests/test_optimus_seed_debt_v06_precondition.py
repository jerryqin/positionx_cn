from __future__ import annotations
import importlib.util, sys, unittest
from pathlib import Path

SCRIPT=Path(__file__).resolve().parents[1]/"scripts"/"run_optimus_seed_debt_v06_precondition.py"
spec=importlib.util.spec_from_file_location("pre",SCRIPT)
m=importlib.util.module_from_spec(spec); assert spec and spec.loader
sys.modules[spec.name]=m; spec.loader.exec_module(m)

class Tests(unittest.TestCase):
    def test_seed_scope(self):
        self.assertEqual(m.EXPECTED_SEED_SYMBOLS,{"601100.SH","603009.SH","603119.SH","601689.SH","600699.SH"})
    def test_registry_status_eval_pass(self):
        x={"status":"PASS_OPTIMUS_CANDIDATE_REGISTRY_V0_3_STATUS","candidate_count":22,
           "registry_sha256":m.EXPECTED_REGISTRY_DATA_SHA256,
           "lifecycle_counts":{"ACTIVE_CANONICAL":17,"ACTIVE_SEED":5},
           "maintenance":{"provisional_evidence":0,"seed_audit_debt":5,"stale_canonical":0,"discovery_only":0}}
        self.assertEqual(m.evaluate_registry_status(x),[])
    def test_maintenance_scope_drift(self):
        x={"status":"PASS_OPTIMUS_MAINTENANCE_QUEUE_V0_3_1",
           "summary":{"provisional_evidence":0,"seed_audit_debt":5,"stale_canonical":0,"discovery_only":0},
           "seed_audit_debt":[{"symbol":"601100.SH","lifecycle_state":"ACTIVE_SEED","state_basis":"SEED_V0_2_BOOTSTRAP_UNVERIFIED"}]}
        self.assertTrue(any(e["code"]=="SEED_DEBT_SCOPE_DRIFT" for e in m.evaluate_maintenance(x)))
    def test_canon_status_batch_guard(self):
        x={"status":"PASS_OPTIMUS_CANONICALIZATION_V0_5_1_STATUS",
           "latest_batch":{"batch_id":m.EXPECTED_LATEST_BATCH_ID,"result_manifest_sha256":m.EXPECTED_LATEST_BATCH_RESULT_SHA256},
           "canonical_symbol_count":17,"fact_count":18,
           "maintenance":{"provisional_evidence":0,"seed_audit_debt":5,"stale_canonical":0,"discovery_only":0}}
        self.assertEqual(m.evaluate_canon_status(x),[])
    def test_preview_expected_hashes(self):
        self.assertEqual(m.EXPECTED_PLAN_SHA256,"2c164036dd7dd44ded7211bf6ec33e9e69628e9ca8aa1fe88cfa195144d5a9a5")
        self.assertEqual(m.EXPECTED_PREVIEW_PAYLOAD_SHA256,"1f890d7ea1ed12cf49919b937b8d502f4be60be73644d925bdd7e5e93e43a007")
if __name__=="__main__":
    unittest.main()
