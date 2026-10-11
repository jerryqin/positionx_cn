from __future__ import annotations
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_optimus_classifier_bridge_v046.py"
spec = importlib.util.spec_from_file_location("bridge_v046", SCRIPT)
m = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = m
spec.loader.exec_module(m)

class Tests(unittest.TestCase):
    def test_frozen_classifier_sha(self):
        p = Path(__file__).resolve().parents[1] / "scripts" / "run_optimus_evidence_acquisition_v04.py"
        self.assertTrue(p.exists(), p)
        self.assertEqual(m.sha256_file(p), m.FROZEN_CLASSIFIER_SHA256)

    def test_normalized_to_cninfo(self):
        x = m.normalized_to_cninfo({
            "announcement_id": "1225521853",
            "sec_code": "601689",
            "sec_name": "拓普集团",
            "org_id": "9900023677",
            "announcement_title": "2026年半年度报告",
            "announcement_time": 1787846400000,
            "storage_time": 1787846400000,
            "adjunct_url": "finalpage/2026-08-28/1225521853.PDF",
        })
        self.assertEqual(x["announcementId"], "1225521853")
        self.assertEqual(x["secCode"], "601689")
        self.assertEqual(x["secName"], "拓普集团")
        self.assertEqual(x["orgId"], "9900023677")

    def test_manifest_policy_validation(self):
        manifest = {
            "status": "PASS_OPTIMUS_SOURCE_RECALL_V0_4_6_BACKFILL",
            "source_recall_quality": "CANDIDATE_STOCK_SCOPED_COMPLETE",
            "transport_quality": "COMPLETE",
            "classifier_quality": "FROZEN_NOT_INVOKED",
            "candidate_count": 1,
            "candidate_results": [
                {"symbol":"601689.SH","all_daily_integrity_pass":True}
            ],
            "announcement_count": 1,
            "pdf_download_count": 1,
            "pdf_downloads": [
                {"announcement_id":"1","status":"DOWNLOADED","sha256":"x"}
            ],
            "safety": {
                "classifier_invoked": False,
                "db_opened": False,
                "registry_mutation": False,
                "canonicalization_mutation": False,
                "broker": False,
                "orders": False,
            },
        }
        got = m.validate_source_manifest(manifest, Path("."), check_files=False)
        self.assertEqual(got["announcement_count"], 1)

    def test_manifest_rejects_integrity_failure(self):
        manifest = {
            "status": "PASS_OPTIMUS_SOURCE_RECALL_V0_4_6_BACKFILL",
            "source_recall_quality": "CANDIDATE_STOCK_SCOPED_COMPLETE",
            "transport_quality": "COMPLETE",
            "classifier_quality": "FROZEN_NOT_INVOKED",
            "candidate_count": 1,
            "candidate_results": [
                {"symbol":"601689.SH","all_daily_integrity_pass":False}
            ],
            "announcement_count": 0,
            "pdf_download_count": 0,
            "pdf_downloads": [],
            "safety": {},
        }
        with self.assertRaises(RuntimeError):
            m.validate_source_manifest(manifest, Path("."), check_files=False)

    def test_source_file_validation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pdfdir = root / "601689_SH" / "pdfs"
            pdfdir.mkdir(parents=True)
            pdf = pdfdir / "A1.pdf"
            pdf.write_bytes(b"%PDF-test")
            h = m.sha256_file(pdf)
            (root/"announcements_all.jsonl").write_text(json.dumps({
                "announcement_id":"A1",
                "symbol":"601689.SH",
                "sec_code":"601689",
            }, ensure_ascii=False)+"\n", encoding="utf-8")
            manifest = {
                "status": "PASS_OPTIMUS_SOURCE_RECALL_V0_4_6_BACKFILL",
                "source_recall_quality": "CANDIDATE_STOCK_SCOPED_COMPLETE",
                "transport_quality": "COMPLETE",
                "classifier_quality": "FROZEN_NOT_INVOKED",
                "candidate_count": 1,
                "candidate_results": [
                    {"symbol":"601689.SH","all_daily_integrity_pass":True}
                ],
                "announcement_count": 1,
                "pdf_download_count": 1,
                "pdf_downloads": [
                    {"announcement_id":"A1","status":"DOWNLOADED","sha256":h}
                ],
                "safety": {},
            }
            got = m.validate_source_manifest(manifest, root, check_files=True)
            self.assertEqual(len(got["rows"]), 1)

if __name__ == "__main__":
    unittest.main()
