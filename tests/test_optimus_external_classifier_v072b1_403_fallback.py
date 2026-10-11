from __future__ import annotations
import importlib.util, json, sys, tempfile, unittest
from pathlib import Path
from urllib.error import HTTPError

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"scripts"/"run_optimus_external_classifier_v072b.py"
CFG=json.loads((ROOT/"config/research/optimus_external_classifier_v072b.json").read_text(encoding="utf-8"))

spec=importlib.util.spec_from_file_location("v072b1",SCRIPT)
m=importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name]=m
spec.loader.exec_module(m)

class TransportFallbackTests(unittest.TestCase):
    def test_tesla_corporate_transport_policy_is_manual_on_403(self):
        self.assertEqual(
            m.transport_mode("SRC_TESLA_CORPORATE_OFFICIAL",CFG),
            "MANUAL_BROWSER_FALLBACK_ON_HTTP_403"
        )

    def test_http_403_is_not_no_evidence(self):
        self.assertTrue(CFG["transport_contract"]["http_403_is_not_no_evidence"])
        self.assertTrue(CFG["transport_contract"]["http_403_is_not_classifier_result"])

    def test_media_type_from_path(self):
        self.assertEqual(m.media_type_from_path(Path("x.html")),"text/html")
        self.assertEqual(m.media_type_from_path(Path("x.pdf")),"application/pdf")

    def test_manual_request_preserves_event_time_null(self):
        rec={
            "source_id":"SRC_TESLA_CORPORATE_OFFICIAL",
            "lane":"THEME_OFFICIAL",
            "entity_name":"Tesla, Inc.",
            "symbol":"TSLA",
            "source_url":"https://www.tesla.com/AI",
            "document_title":"AI & Robotics | Tesla",
            "published_at":None,
            "event_time":None,
        }
        err=HTTPError(rec["source_url"],403,"Forbidden",hdrs=None,fp=None)
        with tempfile.TemporaryDirectory() as td:
            p=m.write_manual_capture_request(Path(td),rec,err,CFG)
            x=json.loads(p.read_text(encoding="utf-8"))
            self.assertIsNone(x["event_time"])
            self.assertIsNone(x["published_at"])
            self.assertEqual(x["http_status"],403)
            self.assertTrue(x["event_time_backfill_forbidden"])

    def test_classification_contract_unchanged_generic_page_no_signal(self):
        cap={
            "capture_identity":"C1",
            "source_id":"SRC_TESLA_CORPORATE_OFFICIAL",
            "source_role":"NAMED_CUSTOMER_OFFICIAL",
            "lane":"THEME_OFFICIAL",
            "entity_name":"Tesla, Inc.",
            "symbol":"TSLA",
            "source_url":"https://www.tesla.com/AI",
            "document_title":"AI & Robotics",
            "event_time":None,
            "published_at":None,
            "observed_at":"2026-10-10T09:30:00+08:00",
        }
        text="Tesla Optimus is a general purpose bi-pedal humanoid robot. We are hiring engineers."
        x=m.classify_capture(cap,text,CFG)
        self.assertEqual(x["queue_state"],"NO_NEW_MATERIAL_EVIDENCE")

    def test_no_hindsight_contract_unchanged(self):
        cap={
            "capture_identity":"C1",
            "source_id":"SRC_TESLA_CORPORATE_OFFICIAL",
            "source_role":"NAMED_CUSTOMER_OFFICIAL",
            "lane":"THEME_OFFICIAL",
            "entity_name":"Tesla, Inc.",
            "symbol":"TSLA",
            "source_url":"https://www.tesla.com/AI",
            "document_title":"AI & Robotics",
            "event_time":"2026-01-28T12:00:00+08:00",
            "published_at":None,
            "observed_at":"2026-10-10T09:30:00+08:00",
        }
        text="Optimus production ramp and volume production."
        x=m.classify_capture(cap,text,CFG)
        self.assertEqual(x["queue_state"],"HISTORICAL_BASELINE_ONLY")

if __name__=="__main__":
    unittest.main()
