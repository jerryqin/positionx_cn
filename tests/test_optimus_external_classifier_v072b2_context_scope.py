from __future__ import annotations
import importlib.util, json, sys, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"scripts"/"run_optimus_external_classifier_v072b.py"
CFG=json.loads((ROOT/"config/research/optimus_external_classifier_v072b.json").read_text(encoding="utf-8"))

spec=importlib.util.spec_from_file_location("v072b2",SCRIPT)
m=importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name]=m
spec.loader.exec_module(m)

def cap(event="2026-10-09T12:00:00+08:00"):
    return {
        "capture_identity":"C1",
        "source_id":"SRC_TESLA_CORPORATE_OFFICIAL",
        "source_role":"NAMED_CUSTOMER_OFFICIAL",
        "lane":"THEME_OFFICIAL",
        "entity_name":"Tesla, Inc.",
        "symbol":"TSLA",
        "source_url":"https://www.tesla.com/AI",
        "document_title":"AI & Robotics",
        "event_time":event,
        "published_at":None,
        "observed_at":"2026-10-10T09:30:00+08:00",
    }

class ContextScopeTests(unittest.TestCase):
    def test_exact_tesla_ai_cross_section_false_positive_is_rejected(self):
        text="""AI & Robotics
Tesla Optimus
Create a general purpose, bi-pedal, autonomous humanoid robot capable of performing unsafe, repetitive or boring tasks.
FSD Chip
Build AI inference chips to run our Full Self-Driving software.
Finally, validate the silicon chip and bring it to mass production in our vehicles.
Neural Networks
Apply cutting-edge research."""
        x=m.classify_capture(cap(event=None),text,CFG)
        self.assertEqual(x["queue_state"],"NO_NEW_MATERIAL_EVIDENCE")
        self.assertEqual(x["reason_code"],"CROSS_SECTION_SIGNAL_REJECTED")
        self.assertIn("MASS_PRODUCTION",x["global_signal_tags"])
        self.assertNotIn("MASS_PRODUCTION",x["theme_context_signal_tags"])
        self.assertIn("MASS_PRODUCTION",x["cross_section_signal_tags"])

    def test_same_sentence_optimus_mass_production_is_signal(self):
        text="Tesla Optimus production ramp has begun and volume production is starting."
        x=m.classify_capture(cap(),text,CFG)
        self.assertEqual(x["queue_state"],"STAGE2_GATE_CANDIDATE")
        self.assertIn("MASS_PRODUCTION",x["theme_context_signal_tags"])

    def test_short_optimus_heading_carries_to_next_block_only(self):
        text="""Tesla Optimus
Volume production is starting and the production ramp is underway.
FSD Chip
Mass production in our vehicles."""
        x=m.classify_capture(cap(),text,CFG)
        self.assertEqual(x["queue_state"],"STAGE2_GATE_CANDIDATE")
        self.assertIn("MASS_PRODUCTION",x["theme_context_signal_tags"])

    def test_heading_does_not_carry_across_unrelated_section(self):
        text="""Tesla Optimus
Create a general purpose humanoid robot.
FSD Chip
Bring the silicon chip to mass production in our vehicles."""
        x=m.classify_capture(cap(event=None),text,CFG)
        self.assertEqual(x["queue_state"],"NO_NEW_MATERIAL_EVIDENCE")
        self.assertEqual(x["reason_code"],"CROSS_SECTION_SIGNAL_REJECTED")

    def test_supplier_robot_context_still_proposes(self):
        c=cap()
        c.update({
            "source_id":"SRC_SEC_GENERIC",
            "source_role":"EXCHANGE_REGULATORY",
            "lane":"SUPPLIER_GLOBAL_OFFICIAL",
            "entity_name":"Supplier X",
            "symbol":"SUPP",
            "source_url":"https://www.sec.gov/Archives/edgar/x"
        })
        text="Our humanoid robot actuator entered small-batch delivery and batch supply to customers."
        x=m.classify_capture(c,text,CFG)
        self.assertEqual(x["queue_state"],"EVIDENCE_PROPOSAL")

    def test_global_unrelated_mass_production_does_not_make_supplier_proposal(self):
        c=cap()
        c.update({
            "source_id":"SRC_SEC_GENERIC",
            "source_role":"EXCHANGE_REGULATORY",
            "lane":"SUPPLIER_GLOBAL_OFFICIAL",
            "entity_name":"Supplier X",
            "symbol":"SUPP",
            "source_url":"https://www.sec.gov/Archives/edgar/x"
        })
        text="""Robotics
We research humanoid robots.
Automotive
Our automotive inverter entered mass production."""
        x=m.classify_capture(c,text,CFG)
        self.assertEqual(x["queue_state"],"NO_NEW_MATERIAL_EVIDENCE")
        self.assertEqual(x["reason_code"],"CROSS_SECTION_SIGNAL_REJECTED")

if __name__=="__main__":
    unittest.main()
