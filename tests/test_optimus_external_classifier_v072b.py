from __future__ import annotations
import importlib.util, json, sys, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"scripts"/"run_optimus_external_classifier_v072b.py"
CFG=json.loads((ROOT/"config/research/optimus_external_classifier_v072b.json").read_text(encoding="utf-8"))

spec=importlib.util.spec_from_file_location("v072b",SCRIPT)
m=importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name]=m
spec.loader.exec_module(m)

def cap(lane="THEME_OFFICIAL", event="2026-10-09T12:00:00+08:00", published=None):
    return {
        "capture_identity":"C1",
        "source_id":"SRC_TESLA_CORPORATE_OFFICIAL" if lane=="THEME_OFFICIAL" else "SRC_SEC_GENERIC",
        "source_role":"NAMED_CUSTOMER_OFFICIAL" if lane=="THEME_OFFICIAL" else "EXCHANGE_REGULATORY",
        "lane":lane,
        "entity_name":"Tesla, Inc." if lane=="THEME_OFFICIAL" else "Supplier X",
        "symbol":"TSLA" if lane=="THEME_OFFICIAL" else "SUPP",
        "source_url":"https://www.tesla.com/AI" if lane=="THEME_OFFICIAL" else "https://www.sec.gov/Archives/edgar/x",
        "document_title":"Doc",
        "event_time":event,
        "published_at":published,
        "observed_at":"2026-10-10T09:30:00+08:00",
    }

class ClassifierTests(unittest.TestCase):
    def test_generic_ai_page_is_not_stage2(self):
        text="Tesla Optimus is a general purpose bi-pedal humanoid robot. We are hiring engineers."
        x=m.classify_capture(cap(),text,CFG)
        self.assertEqual(x["queue_state"],"NO_NEW_MATERIAL_EVIDENCE")

    def test_prospective_theme_volume_production_candidate(self):
        text="Tesla Optimus production ramp has begun and volume production is starting."
        x=m.classify_capture(cap(),text,CFG)
        self.assertEqual(x["queue_state"],"STAGE2_GATE_CANDIDATE")

    def test_historical_signal_never_becomes_stage2(self):
        text="First generation production lines for Optimus are being installed in anticipation of volume production."
        x=m.classify_capture(cap(event="2026-01-28T12:00:00+08:00"),text,CFG)
        self.assertEqual(x["queue_state"],"HISTORICAL_BASELINE_ONLY")
        self.assertEqual(x["reason_code"],"NO_HINDSIGHT_PRE_CASE_START_SIGNAL")

    def test_unknown_time_strong_signal_requires_review(self):
        text="Tesla Optimus production ramp and volume production."
        x=m.classify_capture(cap(event=None,published=None),text,CFG)
        self.assertEqual(x["queue_state"],"HUMAN_REVIEW_REQUIRED")

    def test_supplier_commercial_without_tesla_is_proposal(self):
        text="Our humanoid robot actuator entered small-batch delivery and batch supply to customers."
        x=m.classify_capture(cap(lane="SUPPLIER_GLOBAL_OFFICIAL"),text,CFG)
        self.assertEqual(x["queue_state"],"EVIDENCE_PROPOSAL")

    def test_supplier_named_tesla_commercial_is_stage2_candidate(self):
        text="For Tesla Optimus, our humanoid robot actuator entered mass production and batch supply."
        x=m.classify_capture(cap(lane="SUPPLIER_GLOBAL_OFFICIAL"),text,CFG)
        self.assertEqual(x["queue_state"],"STAGE2_GATE_CANDIDATE")

    def test_prospective_contradiction_alert(self):
        text="Tesla confirmed the Optimus production program has been delayed."
        x=m.classify_capture(cap(),text,CFG)
        self.assertEqual(x["queue_state"],"CONTRADICTION_ALERT")

    def test_observed_at_is_not_effective_time(self):
        c=cap(event=None,published=None)
        eff,src=m.effective_time(c)
        self.assertIsNone(eff)
        self.assertEqual(src,"UNKNOWN")

if __name__=="__main__":
    unittest.main()
