from __future__ import annotations
import importlib.util, json, sys, tempfile, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
B=ROOT/"scripts"/"run_optimus_external_classifier_v072b.py"
C=ROOT/"scripts"/"run_optimus_candidate_discovery_v072c.py"
V07=ROOT/"config/research/optimus_prospective_monitor_v07.json"
V071=ROOT/"config/research/optimus_prospective_intake_v071.json"
CCFG=json.loads((ROOT/"config/research/optimus_candidate_discovery_v072c.json").read_text(encoding="utf-8"))

def load(path,name):
    spec=importlib.util.spec_from_file_location(name,path)
    m=importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name]=m
    spec.loader.exec_module(m)
    return m

b=load(B,"v072b8")
c=load(C,"v072c7")

class PostExpansionRebaseTests(unittest.TestCase):
    def test_registry24_but_current22_universe_stays_frozen(self):
        v=json.loads(V07.read_text(encoding="utf-8"))
        self.assertEqual(v["baseline"]["candidate_count"],24)
        self.assertEqual(v["baseline"]["active_canonical"],22)
        self.assertEqual(v["baseline"]["discovery_only"],2)
        self.assertEqual(v["baseline"]["provisional_evidence"],2)
        self.assertEqual(len(v["universe"]),22)
        syms={x["symbol"] for x in v["universe"]}
        self.assertNotIn("603416.SH",syms)
        self.assertNotIn("603809.SH",syms)

    def test_v071_scope_is_still_exact_current22(self):
        v=json.loads(V071.read_text(encoding="utf-8"))
        self.assertEqual(v["scope"]["monitored_symbol_count"],22)
        self.assertTrue(v["scope"]["discovery_only_excluded"])
        self.assertEqual(set(v["scope"]["discovery_only_symbols"]),{"603416.SH","603809.SH"})

    def test_c7_excludes_existing_discovery_only_symbol(self):
        member,why=c.current_member("无锡信捷电气股份有限公司","603416.SH",CCFG)
        self.assertTrue(member)
        self.assertEqual(why,"SYMBOL_ALREADY_IN_REGISTRY_DISCOVERY_ONLY")
        member,why=c.current_member("成都豪能科技股份有限公司","603809.SH",CCFG)
        self.assertTrue(member)
        self.assertEqual(why,"SYMBOL_ALREADY_IN_REGISTRY_DISCOVERY_ONLY")

    def test_c7_excludes_existing_discovery_only_company_name_without_symbol(self):
        member,why=c.current_member("无锡信捷电气股份有限公司",None,CCFG)
        self.assertTrue(member)
        self.assertEqual(why,"COMPANY_NAME_ALREADY_IN_REGISTRY_DISCOVERY_ONLY")

    def test_c7_still_excludes_current22_with_old_reason(self):
        member,why=c.current_member("拓普集团股份有限公司","601689.SH",CCFG)
        self.assertTrue(member)
        self.assertEqual(why,"SYMBOL_ALREADY_IN_CURRENT_22")

class DedupeTests(unittest.TestCase):
    def write(self,root,sub,name,rows):
        p=root/sub/name
        p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text("".join(json.dumps(x,ensure_ascii=False)+"\n" for x in rows),encoding="utf-8")
        return p

    def test_no_new_success_is_deduped_from_legacy_audit(self):
        with tempfile.TemporaryDirectory() as td:
            r=Path(td)
            self.write(r,"r1","audit.jsonl",[
                {"status":"CLASSIFIED","capture_identity":"NO_NEW_1","queue_state":"NO_NEW_MATERIAL_EVIDENCE"}
            ])
            self.assertIn("NO_NEW_1",b.prior_classified_ids(r))

    def test_queued_success_is_deduped(self):
        with tempfile.TemporaryDirectory() as td:
            r=Path(td)
            self.write(r,"r1","processed_capture_identities.jsonl",[
                {"status":"CLASSIFIED_SUCCESS","capture_identity":"Q1","queue_state":"EVIDENCE_PROPOSAL"}
            ])
            self.assertIn("Q1",b.prior_classified_ids(r))

    def test_error_is_not_deduped(self):
        with tempfile.TemporaryDirectory() as td:
            r=Path(td)
            self.write(r,"r1","audit.jsonl",[
                {"status":"ERROR","capture_identity":"ERR1","error":"x"}
            ])
            self.assertNotIn("ERR1",b.prior_classified_ids(r))

    def test_manual_waiting_is_not_deduped(self):
        with tempfile.TemporaryDirectory() as td:
            r=Path(td)
            self.write(r,"r1","audit.jsonl",[
                {"status":"WAITING_MANUAL_CAPTURE","capture_identity":"WAIT1"}
            ])
            self.assertNotIn("WAIT1",b.prior_classified_ids(r))

    def test_write_run_ledgers_no_new_success(self):
        with tempfile.TemporaryDirectory() as td:
            cfg={"output_root":td}
            out=b.write_classification_run(
                cfg,"CAPTURE_TEST",[],
                [{"status":"CLASSIFIED","capture_identity":"N1","queue_state":"NO_NEW_MATERIAL_EVIDENCE"}],
                1
            )
            p=Path(out["output"])/"processed_capture_identities.jsonl"
            rows=[json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]["capture_identity"],"N1")
            self.assertEqual(rows[0]["status"],"CLASSIFIED_SUCCESS")

    def test_reclassify_explicitly_bypasses_routine_dedupe(self):
        src=B.read_text(encoding="utf-8")
        start=src.index("def reclassify_latest")
        end=src.index("\ndef status(",start)
        body=src[start:end]
        self.assertNotIn("prior_classified_ids(",body)
        self.assertIn('"reclassification":True',body)

if __name__=="__main__":
    unittest.main()
