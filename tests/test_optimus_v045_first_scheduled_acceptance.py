from __future__ import annotations
import importlib.util, json, sys, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"scripts/run_optimus_v045_first_scheduled_acceptance.py"
CFG=json.loads((ROOT/"config/research/optimus_v045_first_scheduled_acceptance.json").read_text(encoding="utf-8"))
spec=importlib.util.spec_from_file_location("accept",SCRIPT)
m=importlib.util.module_from_spec(spec); assert spec and spec.loader
sys.modules[spec.name]=m; spec.loader.exec_module(m)

class AcceptanceTests(unittest.TestCase):
    def test_contract_locked(self):
        f=CFG["frozen_contract"]
        self.assertEqual(f["baseline_sha256"],"7ed350965ac08df8398aa2972b31d875dc8576e6ece08d16f79f4ad3473ba343")
        self.assertEqual(f["parser_version"],"OPTIMUS_EVIDENCE_ACQUISITION_V0_4_5")

    def test_scheduler_hash_locked(self):
        self.assertEqual(CFG["dependencies"]["scheduler_script"]["sha256"],
                         "eedd75b1073d8db55bc2cf9a66595216c43e59baf807e7f61c916689eabddbcd")

    def test_mutation_guard_helper(self):
        self.assertTrue(m.mutation_flags_false({
            "registry_mutation":False,"canonicalization_mutation":False,
            "theme_stage_mutation":False,"broker":False,"orders":False,"production_db":False
        }))
        self.assertFalse(m.mutation_flags_false({"registry_mutation":True}))

    def test_read_only_tool(self):
        s=SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("write_text(",s)
        self.assertNotIn("duckdb",s)
        self.assertNotIn("INSERT INTO",s)
        self.assertNotIn("UPDATE ",s)
        self.assertNotIn("DELETE FROM",s)

if __name__=="__main__":
    unittest.main()
