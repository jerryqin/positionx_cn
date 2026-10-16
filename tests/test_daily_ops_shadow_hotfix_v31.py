from __future__ import annotations
import hashlib, importlib.util, json, tempfile, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def load(name,rel):
    sp=importlib.util.spec_from_file_location(name,ROOT/rel);m=importlib.util.module_from_spec(sp);sp.loader.exec_module(m);return m
acc=load("optimus_accept_v31","scripts/run_optimus_v045_first_scheduled_acceptance.py")
wd=load("daily_watch_v31","scripts/run_daily_ops_watchdog_v1.py")
class ShadowOpsHotfixV31Tests(unittest.TestCase):
    def test_frozen_v071_hash_restored(self):
        b=(ROOT/"scripts/run_optimus_prospective_intake_v071.py").read_bytes()
        self.assertEqual(hashlib.sha256(b).hexdigest(),"0e0a707030b960f7f1e5530577cf1449071c29b0023b11fc13977d4b0d4d45df")
    def test_acceptance_reads_raw_queue_contract_fields(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"proposal_queue.jsonl"
            p.write_text(json.dumps({"evidence_id":"E1","parser_version":"OPTIMUS_EVIDENCE_ACQUISITION_V0_4_5","component_domain":"X","queue_state":"EVIDENCE_PROPOSAL"})+"\n",encoding="utf-8")
            got=acc.acceptance_proposals({"queue_file":str(p),"proposals":[{"evidence_id":"E1"}]})
            self.assertEqual(got[0]["parser_version"],"OPTIMUS_EVIDENCE_ACQUISITION_V0_4_5")
            self.assertEqual(got[0]["component_domain"],"X")
    def test_morning_watchdog_does_not_prejudge_0920_g4(self):
        self.assertNotIn("G4_OPEN_CHECK",wd.WATCHDOG_GROUPS["morning"])
        self.assertIn("G4_OPEN_CHECK",wd.WATCHDOG_GROUPS["eod"])
if __name__=="__main__": unittest.main()
