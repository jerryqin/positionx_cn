from __future__ import annotations
import importlib.util, json, sys, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"scripts/run_optimus_leisai_canonical_fact_v054_plan.py"
CFG=json.loads((ROOT/"config/research/optimus_leisai_canonical_fact_v054_plan.json").read_text(encoding="utf-8"))

spec=importlib.util.spec_from_file_location("leisai_v054a",SCRIPT)
m=importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name]=m
spec.loader.exec_module(m)

def proposal(tesla):
    return {
        "evidence_id":"EACQ_5afda139f7d4dd9a1f33b7bb",
        "queue_state":"EVIDENCE_PROPOSAL",
        "symbol":"002979.SZ",
        "company":"雷赛智能",
        "component_domain":"control / encoders",
        "evidence_level":6,
        "tesla_link_level":tesla,
        "contradiction_kind":None,
        "contradiction_severity":None,
        "source_type":"PRIMARY_CNINFO_OFFICIAL",
        "source_ref":"http://static.cninfo.com.cn/finalpage/2026-10-09/1225596738.PDF",
        "source_sha256":"abc",
        "event_time":"2026-10-09T00:00:00+08:00",
        "observed_at":"2026-10-10T14:55:45+08:00",
        "parser_version":"OPTIMUS_EVIDENCE_ACQUISITION_V0_4_4",
        "interpretation":"x",
        "acquisition":{"level_rule":"L6: ROBOT_HUMANOID_CAPACITY_BUILD","pdf_path":None,"pdf_sha256":None},
    }

class T0ZeroHotfixTests(unittest.TestCase):
    def test_exact_zero_is_valid_t0(self):
        m.validate_target_proposal(CFG,proposal(0))

    def test_string_zero_is_valid_t0(self):
        m.validate_target_proposal(CFG,proposal("0"))

    def test_none_is_not_valid_t0(self):
        with self.assertRaises(RuntimeError) as cm:
            m.validate_target_proposal(CFG,proposal(None))
        self.assertIn("tesla_link_level",str(cm.exception))

    def test_buggy_truthiness_expression_removed(self):
        s=SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn('q.get("tesla_link_level") or -1',s)

if __name__=="__main__":
    unittest.main()
