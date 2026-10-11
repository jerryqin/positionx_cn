from __future__ import annotations
import importlib.util, json, sys, tempfile, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"scripts"/"run_optimus_external_inbox_v072a.py"
CFG=json.loads((ROOT/"config/research/optimus_external_inbox_v072a.json").read_text(encoding="utf-8"))
REG=json.loads((ROOT/"config/research/optimus_external_source_registry_v072a.json").read_text(encoding="utf-8"))

spec=importlib.util.spec_from_file_location("v072a",SCRIPT)
m=importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name]=m
spec.loader.exec_module(m)

class ExternalInboxTests(unittest.TestCase):
    def base(self,raw):
        return {
            "source_id":"SRC_TESLA_CORPORATE_OFFICIAL",
            "lane":"THEME_OFFICIAL",
            "entity_name":"Tesla, Inc.",
            "symbol":"TSLA",
            "source_url":"https://www.tesla.com/AI",
            "document_title":"AI & Robotics",
            "observed_at":"2026-10-10T09:30:00+08:00",
            "event_time":None,
            "raw_file_path":str(raw),
        }

    def test_registered_source_count(self):
        self.assertEqual(len(REG["entries"]),8)

    def test_tesla_subdomain_host_allowed(self):
        e=m.source_entry(REG,"SRC_TESLA_CORPORATE_OFFICIAL")
        self.assertTrue(m.host_allowed(e,"https://www.tesla.com/AI"))
        self.assertFalse(m.host_allowed(e,"https://tesla.example.com/AI"))

    def test_sec_tesla_cik_constraint(self):
        e=m.source_entry(REG,"SRC_SEC_TESLA_EDGAR")
        good=m.canonical_url("https://www.sec.gov/Archives/edgar/data/1318605/abc.htm")
        bad=m.canonical_url("https://www.sec.gov/Archives/edgar/data/9999999/abc.htm")
        req=e["required_url_substrings_any"]
        self.assertTrue(any(x in good for x in req))
        self.assertFalse(any(x in bad for x in req))

    def test_unknown_source_requires_registration(self):
        with self.assertRaises(RuntimeError):
            m.source_entry(REG,"SRC_UNKNOWN")

    def test_event_time_null_is_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            raw=Path(td)/"x.html"; raw.write_text("tesla",encoding="utf-8")
            n=m.normalize_capture(self.base(raw),CFG,REG,check_raw=True)
            self.assertIsNone(n["event_time"])
            self.assertEqual(n["inference_state"],"NOT_PERFORMED")

    def test_naive_observed_at_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            raw=Path(td)/"x.html"; raw.write_text("tesla",encoding="utf-8")
            x=self.base(raw); x["observed_at"]="2026-10-10T09:30:00"
            with self.assertRaises(RuntimeError):
                m.normalize_capture(x,CFG,REG,check_raw=True)

    def test_inference_fields_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            raw=Path(td)/"x.html"; raw.write_text("tesla",encoding="utf-8")
            x=self.base(raw); x["evidence_level"]=9
            with self.assertRaises(RuntimeError):
                m.normalize_capture(x,CFG,REG,check_raw=True)

    def test_capture_identity_is_stable_for_same_source_url_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            raw=Path(td)/"x.html"; raw.write_text("tesla",encoding="utf-8")
            a=self.base(raw); b=self.base(raw)
            a["observed_at"]="2026-10-10T09:30:00+08:00"
            b["observed_at"]="2026-10-11T09:30:00+08:00"
            na=m.normalize_capture(a,CFG,REG,check_raw=True)
            nb=m.normalize_capture(b,CFG,REG,check_raw=True)
            self.assertEqual(na["capture_identity"],nb["capture_identity"])

    def test_cninfo_external_duplicate_lane_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            raw=Path(td)/"x.pdf"; raw.write_bytes(b"x")
            x=self.base(raw)
            x.update({
                "source_id":"SRC_CNINFO_EXISTING_LANE",
                "lane":"SUPPLIER_GLOBAL_OFFICIAL",
                "entity_name":"Example A Share",
                "source_url":"https://static.cninfo.com.cn/finalpage/x.pdf",
            })
            with self.assertRaises(RuntimeError):
                m.normalize_capture(x,CFG,REG,check_raw=True)

if __name__=="__main__":
    unittest.main()
