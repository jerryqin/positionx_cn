from __future__ import annotations
import importlib.util, json, sys, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"scripts"/"run_optimus_candidate_discovery_v072c.py"
CFG=json.loads((ROOT/"config/research/optimus_candidate_discovery_v072c.json").read_text(encoding="utf-8"))

spec=importlib.util.spec_from_file_location("v072c8",SCRIPT)
m=importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name]=m
spec.loader.exec_module(m)

class C8PreclassificationRegistrySkipTests(unittest.TestCase):
    def test_discovery_only_symbol_skips_before_parse(self):
        cap={"lane":"NEW_CANDIDATE_DISCOVERY","entity_name":"无锡信捷电气股份有限公司","symbol":"603416.SH"}
        skip,reason=m.preclassification_registry_skip(cap,CFG)
        self.assertTrue(skip)
        self.assertEqual(reason,"SYMBOL_ALREADY_IN_REGISTRY_DISCOVERY_ONLY")

    def test_discovery_only_company_name_skips_without_symbol(self):
        cap={"lane":"NEW_CANDIDATE_DISCOVERY","entity_name":"成都豪能科技股份有限公司","symbol":None}
        skip,reason=m.preclassification_registry_skip(cap,CFG)
        self.assertTrue(skip)
        self.assertEqual(reason,"COMPANY_NAME_ALREADY_IN_REGISTRY_DISCOVERY_ONLY")

    def test_current22_still_skips_before_parse(self):
        cap={"lane":"NEW_CANDIDATE_DISCOVERY","entity_name":"拓普集团股份有限公司","symbol":"601689.SH"}
        skip,reason=m.preclassification_registry_skip(cap,CFG)
        self.assertTrue(skip)
        self.assertEqual(reason,"SYMBOL_ALREADY_IN_CURRENT_22")

    def test_outside_candidate_is_not_prefiltered(self):
        cap={"lane":"NEW_CANDIDATE_DISCOVERY","entity_name":"测试新公司","symbol":"600000.SH"}
        skip,reason=m.preclassification_registry_skip(cap,CFG)
        self.assertFalse(skip)
        self.assertIsNone(reason)

    def test_non_discovery_lane_not_prefiltered_here(self):
        cap={"lane":"THEME_OFFICIAL","entity_name":"无锡信捷电气股份有限公司","symbol":"603416.SH"}
        skip,reason=m.preclassification_registry_skip(cap,CFG)
        self.assertFalse(skip)
        self.assertIsNone(reason)

    def test_scan_calls_prefilter_before_classifier(self):
        text=SCRIPT.read_text(encoding="utf-8")
        st=text.index("def scan_inbox")
        en=text.index("\ndef ",st+5)
        body=text[st:en]
        self.assertLess(body.index("preclassification_registry_skip(cap,cfg)"),
                        body.index("classify_normalized_file(p,bcfg)"))
        self.assertIn("existing_registry_skipped_before_classification",body)

    def test_contract_flags(self):
        c=CFG["candidate_contract"]
        self.assertTrue(c["preclassification_registry_membership_filter"])
        self.assertTrue(c["registry_members_are_skipped_before_content_parse"])
        self.assertTrue(c["unreadable_capture_of_existing_registry_member_does_not_degrade_discovery_scan"])

if __name__=="__main__":
    unittest.main()
