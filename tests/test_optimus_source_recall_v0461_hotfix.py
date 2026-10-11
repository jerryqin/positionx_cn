from __future__ import annotations
import importlib.util
import http.client
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_optimus_source_recall_v046.py"
spec = importlib.util.spec_from_file_location("v0461", SCRIPT)
m = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = m
spec.loader.exec_module(m)

class Headers(dict):
    def get(self, key, default=None):
        return super().get(key, default)

class FakeResponse:
    def __init__(self, status, headers, actions):
        self.status = status
        self.headers = Headers(headers)
        self.actions = list(actions)
    def getcode(self):
        return self.status
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self, n=-1):
        if not self.actions:
            return b""
        x = self.actions.pop(0)
        if isinstance(x, BaseException):
            raise x
        return x

class Tests(unittest.TestCase):
    def test_original_core_contract_still_present(self):
        self.assertEqual(m.EXPECTED_V045_SHA256,
                         "10e5a8288177c76e207fa264f74e18c3a878d95cf191585ca8a576643475a691")
        self.assertEqual(len(m.TARGETED_CASES), 2)

    def test_resume_after_incomplete_read(self):
        whole = b"%PDFabcdefgh"
        first = FakeResponse(
            200,
            {"Content-Length": str(len(whole))},
            [http.client.IncompleteRead(partial=whole[:7], expected=len(whole)-7)],
        )
        second = FakeResponse(
            206,
            {
                "Content-Length": str(len(whole)-7),
                "Content-Range": f"bytes 7-{len(whole)-1}/{len(whole)}",
            },
            [whole[7:], b""],
        )
        seen = []
        def fake_urlopen(req, timeout=45):
            seen.append(dict(req.header_items()))
            return [first, second][len(seen)-1]
        with patch.object(m.urllib.request, "urlopen", side_effect=fake_urlopen):
            data, audit = m._fetch_complete_bytes("https://example.test/a.pdf", tries=3)
        self.assertEqual(data, whole)
        self.assertTrue(audit["resume_used"])
        self.assertEqual(audit["expected_total"], len(whole))
        # urllib normalizes header names; inspect values only.
        self.assertTrue(any(v == "bytes=7-" for k, v in seen[1].items()))

    def test_range_ignored_restarts_safely(self):
        whole = b"%PDFabcdefgh"
        first = FakeResponse(
            200,
            {"Content-Length": str(len(whole))},
            [http.client.IncompleteRead(partial=whole[:6], expected=len(whole)-6)],
        )
        second = FakeResponse(
            200,
            {"Content-Length": str(len(whole))},
            [whole, b""],
        )
        with patch.object(m.urllib.request, "urlopen", side_effect=[first, second]):
            data, audit = m._fetch_complete_bytes("https://example.test/a.pdf", tries=3)
        self.assertEqual(data, whole)
        self.assertEqual(audit["expected_total"], len(whole))

    def test_download_pdf_writes_only_complete_pdf(self):
        whole = b"%PDF-1.7\nabc\n%%EOF\n"
        with tempfile.TemporaryDirectory() as td:
            row = {
                "announcement_id": "123",
                "adjunct_url": "https://example.test/123.pdf",
            }
            with patch.object(m, "_fetch_complete_bytes",
                              return_value=(whole, {"resume_used": True, "expected_total": len(whole)})):
                rec = m.download_pdf(row, Path(td))
            p = Path(rec["path"])
            self.assertEqual(p.read_bytes(), whole)
            self.assertEqual(rec["status"], "DOWNLOADED")
            self.assertTrue(rec["transport"]["resume_used"])

if __name__ == "__main__":
    unittest.main()
