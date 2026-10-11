import importlib.util
import json
from pathlib import Path
import sys, types, unittest

# stub duckdb for pure function tests
if "duckdb" not in sys.modules:
    d=types.ModuleType("duckdb")
    d.connect=lambda *a,**k: (_ for _ in ()).throw(RuntimeError("duckdb stub"))
    sys.modules["duckdb"]=d

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"scripts"/"run_optimus_candidate_registry_v03.py"
spec=importlib.util.spec_from_file_location("reg",SCRIPT)
reg=importlib.util.module_from_spec(spec); spec.loader.exec_module(reg)
CFG=json.loads((ROOT/"config/research/optimus_candidate_registry_v0_3.json").read_text(encoding="utf-8"))

class CandidateRegistryV03Tests(unittest.TestCase):
    def test_seed_count_is_bootstrap_not_contract(self):
        import pandas as pd
        df=pd.read_csv(ROOT/"config/research/optimus_candidate_seed_v0_2.csv")
        self.assertEqual(len(df),22)
        self.assertFalse(bool(CFG.get("fixed_candidate_count",False)))

    def test_seed_link_mapping_never_infers_explicit(self):
        self.assertEqual(reg.normalize_seed_tesla_link("LEGACY_TESLA_AUTO_RELATION_ROBOT_CUSTOMER_UNNAMED"),1)
        self.assertEqual(reg.normalize_seed_tesla_link("OVERSEAS_HEAD_ROBOT_CUSTOMER_UNNAMED"),2)
        self.assertLess(reg.normalize_seed_tesla_link("WORLD_KNOWN_TECH_CUSTOMER_UNNAMED"),3)

    def test_possible_rule(self):
        x=reg.derive_f_status(max_level=2,max_tesla_link=0,has_primary=True,hard_negative=False,has_component_relevance=True,rules=CFG["rules"])
        self.assertEqual(x,("ACTIVE_CANONICAL","POSSIBLE"))

    def test_probable_requires_explicit_named_link_level(self):
        x=reg.derive_f_status(max_level=6,max_tesla_link=2,has_primary=True,hard_negative=False,has_component_relevance=True,rules=CFG["rules"])
        self.assertEqual(x,("ACTIVE_CANONICAL","POSSIBLE"))
        y=reg.derive_f_status(max_level=4,max_tesla_link=3,has_primary=True,hard_negative=False,has_component_relevance=True,rules=CFG["rules"])
        self.assertEqual(y,("ACTIVE_CANONICAL","PROBABLE"))

    def test_confirmed_is_strict(self):
        x=reg.derive_f_status(max_level=7,max_tesla_link=4,has_primary=False,hard_negative=False,has_component_relevance=True,rules=CFG["rules"])
        self.assertEqual(x,("ACTIVE_CANONICAL","PROBABLE"))
        y=reg.derive_f_status(max_level=7,max_tesla_link=4,has_primary=True,hard_negative=False,has_component_relevance=True,rules=CFG["rules"])
        self.assertEqual(y,("ACTIVE_CANONICAL","CONFIRMED"))

    def test_hard_negative_removes_without_delete_semantics(self):
        x=reg.derive_f_status(max_level=9,max_tesla_link=4,has_primary=True,hard_negative=True,has_component_relevance=True,rules=CFG["rules"])
        self.assertEqual(x,("REMOVED","REMOVED"))

    def test_title_classifier_is_always_provisional(self):
        x=reg.classify_discovery_title("关于获得人形机器人量产订单的公告","人形机器人")
        self.assertEqual(x["review_state"],"PROVISIONAL")
        self.assertGreaterEqual(x["evidence_level"],8)

    def test_explicit_tesla_title_still_provisional(self):
        x=reg.classify_discovery_title("关于特斯拉人形机器人项目定点的公告","特斯拉机器人")
        self.assertEqual(x["tesla_link_level"],3)
        self.assertEqual(x["review_state"],"PROVISIONAL")

    def test_validate_rejects_unapproved_canonical_input(self):
        row={"symbol":"600000.SH","evidence_level":4,"tesla_link_level":3,"review_state":"CANONICAL","source_type":"PRIMARY_REPORT","source_ref":"x"}
        with self.assertRaises(ValueError): reg.validate_evidence_row(row,allow_canonical_input=False)

    def test_stable_evidence_id(self):
        row={"symbol":"600000.SH","company":"X","component_domain":"actuator","evidence_level":4,"evidence_type":"SUPPLIER_NOMINATION","tesla_link_level":3,"source_type":"PRIMARY_REPORT","source_ref":"doc1","observed_at":"2026-10-08T20:00:00+08:00"}
        a=reg.validate_evidence_row(row); b=reg.validate_evidence_row(row)
        self.assertEqual(a["evidence_id"],b["evidence_id"])

    def test_unknown_component_stays_discovery(self):
        x=reg.derive_f_status(max_level=8,max_tesla_link=4,has_primary=True,hard_negative=False,has_component_relevance=False,rules=CFG["rules"])
        self.assertEqual(x[0],"DISCOVERY_ONLY")


class CandidateRegistryV031OpsTests(unittest.TestCase):
    def test_symbol_scope_accepts_sh_sz_a_share(self):
        self.assertEqual(reg.symbol_from_cninfo('600000'), '600000.SH')
        self.assertEqual(reg.symbol_from_cninfo('000001'), '000001.SZ')
        self.assertEqual(reg.symbol_from_cninfo('300001'), '300001.SZ')

    def test_symbol_scope_rejects_bse_and_invalid(self):
        self.assertIsNone(reg.symbol_from_cninfo('830000'))
        self.assertIsNone(reg.symbol_from_cninfo('12345'))
        self.assertIsNone(reg.symbol_from_cninfo('ABCDEF'))

    def test_maintenance_bucket_seed_debt(self):
        row={'lifecycle_state':'ACTIVE_SEED','canonical':False,'freshness_state':'SEED_NEEDS_CANONICAL_EVIDENCE'}
        self.assertEqual(reg.maintenance_bucket_for_candidate(row),'SEED_AUDIT_DEBT')

    def test_maintenance_bucket_stale_canonical(self):
        row={'lifecycle_state':'ACTIVE_CANONICAL','canonical':True,'freshness_state':'STALE_REVIEW'}
        self.assertEqual(reg.maintenance_bucket_for_candidate(row),'STALE_CANONICAL')

if __name__=="__main__": unittest.main()
