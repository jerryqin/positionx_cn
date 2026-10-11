# PositionX Optimus Source Recall V0.4.6.1 — PDF transport hotfix

## Trigger

The 180-day five-company rebuild stopped while downloading CNINFO PDF
`1225126973` with:

`IncompleteRead(130459 bytes read, 14894 more expected)`

This is a PDF transport interruption, not a source-recall, classifier, Registry,
or canonicalization failure.

## Change

Only `download_pdf` transport is hardened:

- keep partial bytes returned by `IncompleteRead`;
- resume from the exact next byte using HTTP `Range`;
- validate `Content-Range`;
- if Range is ignored and HTTP 200 is returned, restart safely from byte zero;
- validate total byte count when `Content-Length` / `Content-Range` provides it;
- write the final `.pdf` atomically only after a complete `%PDF` object exists;
- record segment/resume audit metadata in the download record.

No classifier logic is changed.

## Frozen boundaries

- V0.4.4 classifier: unchanged / not invoked by source rebuild
- V0.4.5 acquisition script lineage SHA: `025eb2c6e3904a87586a868d87fa1920d919953e25516bff85765985a2fbe622`
- Registry mutation: none
- Canonicalization mutation: none
- Broker/order/production DB: none

## Install

```powershell
python .\install_optimus_source_recall_v0461.py --project-root D:\positionx_cn
```

The installer only accepts the exact V0.4.6 script SHA generated in Phase A:

`025eb2c6e3904a87586a868d87fa1920d919953e25516bff85765985a2fbe622`

or an already-installed hotfix SHA:

`28d4155dae1e307b7b61f7f9dfbff75feaa4aa3f24bc81e65e32ea10ebb859a0`

Anything else fails closed.

## Validate

```powershell
cd D:\positionx_cn
python -m unittest tests.test_optimus_source_recall_v0461_hotfix -v
python scripts\run_optimus_source_recall_v046.py check
```

## Rerun

The failed capture is partial and must not be promoted. Rerun the same full
five-company rebuild command. A new immutable capture directory will be created.
