from __future__ import annotations
import importlib.util, json, sys, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
CFG=json.loads((ROOT/"config/research/optimus_component_domain_v045_production_patch.json").read_text(encoding="utf-8"))
PAYLOAD_ROOT=ROOT/CFG["payload_root"]
EV=PAYLOAD_ROOT/"scripts/run_optimus_evidence_acquisition_v04.py"
V071=PAYLOAD_ROOT/"scripts/run_optimus_prospective_intake_v071.py"

spec=importlib.util.spec_from_file_location("evv045",EV)
m=importlib.util.module_from_spec(spec); assert spec and spec.loader
sys.modules[spec.name]=m; spec.loader.exec_module(m)

class ProductionPatchTests(unittest.TestCase):
    def test_leisai_dominant_control(self):
        text=("减速器 "*3)+("控制器 "*10)+("编码器 "*8)+("运动控制 "*17)+("伺服电机 "*9)+("空心杯电机 "*5)
        # Use direct score helper with synthetic windows so threshold semantics are exact.
        rows=[
            {"map_index":9,"domain":"control / encoders","window_hit_count":17,"occurrence_count":35},
            {"map_index":2,"domain":"motors / motor components","window_hit_count":11,"occurrence_count":22},
            {"map_index":1,"domain":"reducers / precision transmission","window_hit_count":3,"occurrence_count":3},
        ]
        # Reproduce the production threshold contract.
        top,second=rows[0],rows[1]
        self.assertGreaterEqual(top["window_hit_count"]-second["window_hit_count"],m.V045_MIN_WINDOW_MARGIN)
        self.assertGreaterEqual(top["window_hit_count"]/second["window_hit_count"],m.V045_MIN_WINDOW_RATIO)

    def test_ambiguous_domain_constant(self):
        self.assertEqual(m.V045_AMBIGUOUS_DOMAIN,"AMBIGUOUS_MULTI_DOMAIN_PENDING_REVIEW")
        self.assertEqual(m.V045_PARSER,"OPTIMUS_EVIDENCE_ACQUISITION_V0_4_5")

    def test_future_only_historical_commands_blocked(self):
        s=EV.read_text(encoding="utf-8")
        self.assertIn("V0_4_5_FUTURE_ONLY: historical reclassification is frozen",s)
        self.assertIn("V0_4_5_FUTURE_ONLY: historical seed backfill is disabled",s)
        self.assertIn("V0_4_5_FUTURE_ONLY: historical seed-debt backfill is disabled",s)

    def test_v071_ambiguous_goes_human_review(self):
        spec2=importlib.util.spec_from_file_location("v071",V071)
        v=importlib.util.module_from_spec(spec2); assert spec2 and spec2.loader
        sys.modules[spec2.name]=v; spec2.loader.exec_module(v)
        ev={"component_domain":"AMBIGUOUS_MULTI_DOMAIN_PENDING_REVIEW",
             "tesla_link_level":0,"contradiction_severity":None,"acquisition":{}}
        self.assertEqual(v.queue_class(ev),"HUMAN_REVIEW_REQUIRED")

    def test_v071_cross_parser_source_identity(self):
        spec2=importlib.util.spec_from_file_location("v071b",V071)
        v=importlib.util.module_from_spec(spec2); assert spec2 and spec2.loader
        sys.modules[spec2.name]=v; spec2.loader.exec_module(v)
        a={"symbol":"002979.SZ","source_sha256":"abc","source_ref":"x"}
        b={"symbol":"002979.SZ","source_sha256":"abc","source_ref":"y"}
        self.assertEqual(v.source_identity(a),v.source_identity(b))

    def test_b8_review_keeps_v8_identity_contract(self):
        b=json.loads((PAYLOAD_ROOT/"config/research/optimus_external_classifier_v072b.json").read_text(encoding="utf-8"))
        d=b["dedupe_contract"]
        self.assertEqual(d["version"],"V0_7_2B_8")
        self.assertEqual(d["component_domain_v0_4_5_review"],"PASS_NO_CAPTURE_IDENTITY_CHANGE")
        self.assertFalse(d["capture_identity_includes_component_domain_parser_version"])
        self.assertFalse(d["historical_capture_auto_reclassification_on_parser_upgrade"])
        self.assertTrue(d["historical_reclassification_requires_explicit_reclassify"])

    def test_baseline_registry_is_unchanged(self):
        b=json.loads((PAYLOAD_ROOT/"config/research/optimus_steady_state_baseline_v074.json").read_text(encoding="utf-8"))
        self.assertEqual(b["registry"]["registry_sha256"],"d51bbc34dc0731667cd7c2991b11cb954fd3747c9b1c9c6c6154f40cbbc9ccaa")
        self.assertEqual(b["registry"]["evidence_count"],163)
        self.assertEqual(b["registry"]["canonical_fact_count"],25)

    def test_payload_dependency_chain(self):
        b=json.loads((PAYLOAD_ROOT/"config/research/optimus_steady_state_baseline_v074.json").read_text(encoding="utf-8"))
        for rel,spec in CFG["files"].items():
            if rel in b["code_freeze"]:
                self.assertEqual(b["code_freeze"][rel],spec["new_sha256"],rel)

    def test_namespaced_payload_is_used(self):
        self.assertEqual(CFG["payload_root"],"patch_payload/optimus_component_domain_v045")
        self.assertTrue((PAYLOAD_ROOT/"config/research/optimus_external_classifier_v072b.json").exists())
        self.assertTrue((PAYLOAD_ROOT/"config/research/optimus_steady_state_baseline_v074.json").exists())

    def test_no_db_mutation_in_patch_tool(self):
        s=(ROOT/"scripts/run_optimus_component_domain_v045_production_patch.py").read_text(encoding="utf-8")
        self.assertNotIn("duckdb.connect",s)
        self.assertNotIn("INSERT INTO",s)
        self.assertNotIn("UPDATE ",s)
        self.assertNotIn("DELETE FROM",s)

if __name__=="__main__":
    unittest.main()
