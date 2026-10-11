# V0.7.2B.5 / C.3 — Frozen V0.4.4 PDF Adapter Hotfix

## Root cause

All three browser-saved PDFs reached V0.7.2A ingest, then failed at B classification:

```text
PDF_TEXT_EXTRACTION_FAILED
AttributeError:
module 'positionx_v044_pdf_for_v072b'
has no attribute 'select_pdf_engine'
```

The frozen evidence source does not expose `select_pdf_engine`.

Its actual API is:

```python
extract_pdf_text(pdf_bytes: bytes, max_pages: int)
-> (text, pages, parse_error)
```

B.5 now calls exactly that frozen interface.

## Fail-closed behavior

```text
parse_error != None
→ FAIL PDF extraction

text empty after parsing
→ FAIL PDF extraction

never:
empty/failed PDF parse
→ NO_NEW_MATERIAL_EVIDENCE
```

## Existing raw captures

The three failed `manual-capture` commands already pass through V0.7.2A ingest before
classification. Re-running the same manual-capture commands after this hotfix is safe:
V0.7.2A should return `ALREADY_INGESTED` for identical source URL + raw bytes and B.5
will continue to classification.

No re-download or re-save is required unless the local PDF files themselves changed.

## Install

```powershell
python .\install_optimus_pdf_adapter_v072b5_c3.py `
  --project-root D:\positionx_cn
```

## Validate

```powershell
cd D:\positionx_cn

python -m unittest tests.test_optimus_pdf_adapter_v072b5 -v
python -m unittest tests.test_optimus_official_source_transport_v072b4_c2 -v
python -m unittest tests.test_optimus_external_classifier_v072b3_chinese -v
python -m unittest tests.test_optimus_candidate_discovery_v072c -v

python scripts\run_optimus_external_classifier_v072b.py check
```

Then rerun the same three one-record manual captures.
