from __future__ import annotations
import importlib.util, json, sys, tempfile, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
B=ROOT/"scripts"/"run_optimus_external_classifier_v072b.py"
BCFG=json.loads((ROOT/"config/research/optimus_external_classifier_v072b.json").read_text(encoding="utf-8"))

def load(path,name):
    spec=importlib.util.spec_from_file_location(name,path)
    m=importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name]=m
    spec.loader.exec_module(m)
    return m

b=load(B,"v072b5")

class PdfAdapterTests(unittest.TestCase):
    def test_adapter_uses_bytes_and_max_pages(self):
        calls={}
        class FakeEvidence:
            @staticmethod
            def extract_pdf_text(pdf_bytes,max_pages):
                calls["type"]=type(pdf_bytes).__name__
                calls["bytes"]=pdf_bytes
                calls["max_pages"]=max_pages
                return ("人形机器人 批量供货",3,None)
        old=b.import_module
        try:
            b.import_module=lambda path,name: FakeEvidence
            with tempfile.TemporaryDirectory() as td:
                p=Path(td)/"x.pdf"; p.write_bytes(b"%PDF-test")
                text=b.pdf_text(p,BCFG)
            self.assertEqual(text,"人形机器人 批量供货")
            self.assertEqual(calls["type"],"bytes")
            self.assertEqual(calls["bytes"],b"%PDF-test")
            self.assertEqual(calls["max_pages"],260)
        finally:
            b.import_module=old

    def test_parse_error_fails_closed(self):
        class FakeEvidence:
            @staticmethod
            def extract_pdf_text(pdf_bytes,max_pages):
                return ("",0,"PYPDF_UNAVAILABLE:test")
        old=b.import_module
        try:
            b.import_module=lambda path,name: FakeEvidence
            with tempfile.TemporaryDirectory() as td:
                p=Path(td)/"x.pdf"; p.write_bytes(b"%PDF-test")
                with self.assertRaises(RuntimeError) as cm:
                    b.pdf_text(p,BCFG)
            self.assertIn("PDF_TEXT_EXTRACTION_FAILED",str(cm.exception))
            self.assertIn("V044_PDF_PARSE_ERROR",str(cm.exception))
        finally:
            b.import_module=old

    def test_empty_text_fails_closed(self):
        class FakeEvidence:
            @staticmethod
            def extract_pdf_text(pdf_bytes,max_pages):
                return ("   ",5,None)
        old=b.import_module
        try:
            b.import_module=lambda path,name: FakeEvidence
            with tempfile.TemporaryDirectory() as td:
                p=Path(td)/"x.pdf"; p.write_bytes(b"%PDF-test")
                with self.assertRaises(RuntimeError) as cm:
                    b.pdf_text(p,BCFG)
            self.assertIn("V044_PDF_TEXT_EMPTY",str(cm.exception))
        finally:
            b.import_module=old

    def test_no_select_pdf_engine_reference_remains(self):
        src=B.read_text(encoding="utf-8")
        self.assertNotIn("select_pdf_engine()",src)

if __name__=="__main__":
    unittest.main()
