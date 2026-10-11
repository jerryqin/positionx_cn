import importlib.util
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
P = ROOT / "scripts" / "run_optimus_canonicalization_v05.py"
spec = importlib.util.spec_from_file_location("optimus_canon_v05_testmod", P)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

PLAN_PATH = ROOT / "config" / "research" / "optimus_canonicalization_seed_batch_v0_5_20261009.json"
PLAN = json.loads(PLAN_PATH.read_text(encoding="utf-8"))


class CanonicalizationV05PureTests(unittest.TestCase):
    def test_plan_schema_passes(self):
        x = mod.validate_plan_schema(PLAN)
        self.assertTrue(x["ok"], x["errors"])

    def test_fact_count(self):
        self.assertEqual(len(PLAN["facts"]), 12)

    def test_canonical_symbol_count(self):
        self.assertEqual(len({x["symbol"] for x in PLAN["facts"]}), 11)

    def test_plan_covers_21_v044_rows(self):
        ids = mod.plan_covered_evidence_ids(PLAN)
        self.assertEqual(len(ids), 21)
        self.assertEqual(len(set(ids)), 21)

    def test_two_reject_only_rows(self):
        self.assertEqual(len(PLAN["reject_only"]), 2)

    def test_joyson_is_rejected_not_fact(self):
        self.assertFalse(any(x["symbol"] == "600699.SH" for x in PLAN["facts"]))
        self.assertTrue(any(x["symbol"] == "600699.SH" for x in PLAN["reject_only"]))

    def test_orbbec_anchor_is_t0(self):
        f = next(x for x in PLAN["facts"] if x["symbol"] == "688322.SH")
        self.assertEqual(f["canonical_tesla_link_level"], 0)
        self.assertEqual(f["anchor_evidence_id"], "EACQ_b85617109bff45fb4c126dd6")

    def test_jlmag_has_two_distinct_facts(self):
        facts = [x for x in PLAN["facts"] if x["symbol"] == "300748.SZ"]
        self.assertEqual(len(facts), 2)
        self.assertEqual(sorted(x["canonical_level"] for x in facts), [4, 9])
        self.assertEqual(sorted(x["canonical_tesla_link_level"] for x in facts), [0, 2])

    def test_sanhua_three_sources_one_fact(self):
        f = next(x for x in PLAN["facts"] if x["symbol"] == "002050.SZ")
        self.assertEqual(1 + len(f["support_evidence_ids"]), 3)
        self.assertEqual(f["canonical_level"], 9)

    def test_lens_three_sources_one_fact(self):
        f = next(x for x in PLAN["facts"] if x["symbol"] == "300433.SZ")
        self.assertEqual(1 + len(f["support_evidence_ids"]), 3)

    def test_leader_harmonic_financing_row_rejected(self):
        self.assertTrue(any(x["evidence_id"] == "EACQ_1eddfecbec2d002da36e2373" for x in PLAN["reject_only"]))

    def test_fact_ids_stable(self):
        f = PLAN["facts"][0]
        self.assertEqual(mod.fact_id_for(f), mod.fact_id_for(f))

    def test_fact_ids_unique(self):
        ids = [mod.fact_id_for(x) for x in PLAN["facts"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_duplicate_source_detection(self):
        bad = json.loads(json.dumps(PLAN))
        bad["facts"][1]["support_evidence_ids"].append(bad["facts"][0]["anchor_evidence_id"])
        x = mod.validate_plan_schema(bad)
        self.assertFalse(x["ok"])
        self.assertTrue(any("used more than once" in e for e in x["errors"]))

    def test_anchor_cannot_be_support(self):
        bad = json.loads(json.dumps(PLAN))
        bad["facts"][0]["support_evidence_ids"].append(bad["facts"][0]["anchor_evidence_id"])
        x = mod.validate_plan_schema(bad)
        self.assertFalse(x["ok"])
        self.assertTrue(any("anchor must not also be support" in e for e in x["errors"]))

    def test_level_bounds(self):
        bad = json.loads(json.dumps(PLAN))
        bad["facts"][0]["canonical_level"] = 10
        self.assertFalse(mod.validate_plan_schema(bad)["ok"])

    def test_tesla_bounds(self):
        bad = json.loads(json.dumps(PLAN))
        bad["facts"][0]["canonical_tesla_link_level"] = 5
        self.assertFalse(mod.validate_plan_schema(bad)["ok"])

    def test_no_direct_named_tesla_fact(self):
        self.assertFalse(any(int(x["canonical_tesla_link_level"]) >= 3 for x in PLAN["facts"]))

    def test_only_one_t2_fact(self):
        self.assertEqual(sum(int(x["canonical_tesla_link_level"]) == 2 for x in PLAN["facts"]), 1)

    def test_batch_delivery_levels(self):
        keys = {x["fact_key"]: x for x in PLAN["facts"]}
        self.assertEqual(keys["SANHUA_2026H1_ROBOT_ACTUATOR_BATCH_DELIVERY"]["canonical_level"], 9)
        self.assertEqual(keys["JIANGSU_LEILI_2026H1_ROBOT_PARTS_REVENUE"]["canonical_level"], 9)

    def test_low_level_double_ring(self):
        f = next(x for x in PLAN["facts"] if x["symbol"] == "002472.SZ")
        self.assertEqual(f["canonical_level"], 1)

    def test_expected_pre_state(self):
        self.assertEqual(PLAN["expected_pre_state"]["effective_v044_provisional_count"], 21)
        self.assertEqual(PLAN["expected_pre_state"]["seed_audit_debt"], 22)

    def test_materialized_fact_id_stable(self):
        fid = mod.fact_id_for(PLAN["facts"][0])
        a = mod._fact_materialized_evidence_id("CB_TEST", fid)
        b = mod._fact_materialized_evidence_id("CB_TEST", fid)
        self.assertEqual(a, b)

    def test_materialized_fact_uses_curated_component_domain(self):
        f = dict(PLAN["facts"][4])
        f["fact_id"] = mod.fact_id_for(f)
        f["canonicalized_at"] = "2026-10-09T11:06:16+08:00"
        anchor = {
            "evidence_id": f["anchor_evidence_id"],
            "company": f["company"],
            "source_type": "CNINFO_PRIMARY_PDF",
            "source_ref": "https://example.test/a.pdf",
            "source_sha256": "a"*64,
            "event_time": "2026-08-24T00:00:00+08:00",
            "component_domain": "UNKNOWN_PENDING_REVIEW",
            "contradiction_kind": None,
        }
        e = mod.build_materialized_fact_evidence(batch_id="CB_TEST", fact_row=f, anchor_row=anchor)
        self.assertEqual(e["component_domain"], f["component_domain"])
        self.assertNotEqual(e["component_domain"], anchor["component_domain"])

    def test_materialized_fact_does_not_inherit_anchor_contradiction(self):
        f = dict(next(x for x in PLAN["facts"] if x["symbol"] == "002979.SZ"))
        f["fact_id"] = mod.fact_id_for(f)
        f["canonicalized_at"] = "2026-10-09T11:06:16+08:00"
        anchor = {
            "evidence_id": f["anchor_evidence_id"],
            "company": f["company"],
            "source_type": "CNINFO_PRIMARY_PDF",
            "source_ref": "https://example.test/leisai.pdf",
            "source_sha256": "b"*64,
            "event_time": "2026-08-28T00:00:00+08:00",
            "contradiction_kind": "NAMED_TESLA_RUMOR_DENIAL",
            "contradiction_severity": "HARD",
        }
        e = mod.build_materialized_fact_evidence(batch_id="CB_TEST", fact_row=f, anchor_row=anchor)
        self.assertIsNone(e["contradiction_kind"])
        self.assertIsNone(e["contradiction_severity"])
        self.assertEqual(e["evidence_level"], 9)
        self.assertEqual(e["tesla_link_level"], 0)

    def test_materialized_fact_preserves_primary_source_provenance(self):
        f = dict(PLAN["facts"][0])
        f["fact_id"] = mod.fact_id_for(f)
        f["canonicalized_at"] = "2026-10-09T11:06:16+08:00"
        anchor = {
            "evidence_id": f["anchor_evidence_id"],
            "company": f["company"],
            "source_type": "CNINFO_PRIMARY_PDF",
            "source_ref": "https://example.test/sanhua.pdf",
            "source_sha256": "c"*64,
            "event_time": "2026-08-27T00:00:00+08:00",
        }
        e = mod.build_materialized_fact_evidence(batch_id="CB_TEST", fact_row=f, anchor_row=anchor)
        self.assertEqual(e["source_ref"], anchor["source_ref"])
        self.assertEqual(e["source_sha256"], anchor["source_sha256"])
        self.assertEqual(e["source_type"], "CNINFO_PRIMARY_PDF")
        self.assertEqual(e["review_state"], "CANONICAL")

    def test_materialized_fact_uses_fact_level_not_anchor_level(self):
        f = dict(PLAN["facts"][0])
        f["fact_id"] = mod.fact_id_for(f)
        f["canonicalized_at"] = "2026-10-09T11:06:16+08:00"
        anchor = {
            "evidence_id": f["anchor_evidence_id"],
            "company": f["company"],
            "source_type": "CNINFO_PRIMARY_PDF",
            "source_ref": "https://example.test/sanhua.pdf",
            "source_sha256": "d"*64,
            "event_time": None,
            "evidence_level": 1,
            "tesla_link_level": 4,
        }
        e = mod.build_materialized_fact_evidence(batch_id="CB_TEST", fact_row=f, anchor_row=anchor)
        self.assertEqual(e["evidence_level"], f["canonical_level"])
        self.assertEqual(e["tesla_link_level"], f["canonical_tesla_link_level"])

    def test_materialization_policy_version(self):
        self.assertEqual(mod.FACT_MATERIALIZATION_PARSER, "OPTIMUS_CANONICALIZATION_V0_5_1_FACT_MATERIALIZATION")


if __name__ == "__main__":
    unittest.main()

BATCH2_PATH = ROOT / "config" / "research" / "optimus_canonicalization_seed_batch2_v0_5_2_20261009.json"
BATCH2 = json.loads(BATCH2_PATH.read_text(encoding="utf-8"))

class CanonicalizationV052Batch2Tests(unittest.TestCase):
    def test_v045_evidence_script_hash_is_supported(self):
        self.assertIn("10e5a8288177c76e207fa264f74e18c3a878d95cf191585ca8a576643475a691", mod.SUPPORTED_EVIDENCE_SCRIPT_SHA256)

    def test_batch2_schema_passes(self):
        x = mod.validate_plan_schema(BATCH2)
        self.assertTrue(x["ok"], x["errors"])

    def test_batch2_covers_exactly_two_rows(self):
        ids = mod.plan_covered_evidence_ids(BATCH2)
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), 2)

    def test_batch2_has_two_symbols(self):
        self.assertEqual({x["symbol"] for x in BATCH2["facts"]}, {"300115.SZ", "688160.SH"})

    def test_everwin_is_l2_t2_not_named_tesla(self):
        f = next(x for x in BATCH2["facts"] if x["symbol"] == "300115.SZ")
        self.assertEqual(f["canonical_level"], 2)
        self.assertEqual(f["canonical_tesla_link_level"], 2)
        self.assertIn("unnamed", f["decision_rationale"].lower())

    def test_kinco_is_l2_t0(self):
        f = next(x for x in BATCH2["facts"] if x["symbol"] == "688160.SH")
        self.assertEqual(f["canonical_level"], 2)
        self.assertEqual(f["canonical_tesla_link_level"], 0)

    def test_batch2_expected_pre_state(self):
        self.assertEqual(BATCH2["expected_pre_state"]["effective_v044_provisional_count"], 2)
        self.assertEqual(BATCH2["expected_pre_state"]["seed_audit_debt"], 11)
