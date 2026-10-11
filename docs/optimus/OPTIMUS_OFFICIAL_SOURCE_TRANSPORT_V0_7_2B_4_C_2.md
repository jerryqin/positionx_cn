# V0.7.2B.4 / C.2 — Official Source Transport Hardening

## Observed real failure

The three-company official pilot produced:

```text
record 1  SSEInfo roadshow   HTTP success, raw.html = 2996 bytes
          classifier = NO_NEW_MATERIAL_EVIDENCE, tags = []

record 2  SSE official PDF   ConnectionResetError / WinError 10054

record 3  SSEInfo roadshow   HTTP success, raw.html = 2996 bytes
          classifier = NO_NEW_MATERIAL_EVIDENCE, tags = []
```

The two identical 2996-byte HTML files are a dynamic-site shell, not the rendered
official Q&A content. Therefore those zero-signal classifications are transport
artifacts, not research conclusions.

## B.4 transport rule

For `SRC_SSE_OFFICIAL`:

```text
network reset / timeout
→ WAITING_MANUAL_CAPTURE

roadshow.sseinfo.com tiny dynamic shell
→ WAITING_MANUAL_CAPTURE

never:
transport failure / SPA shell
→ NO_NEW_MATERIAL_EVIDENCE
```

The classifier itself is unchanged.

## C.2 dependency rule

V0.7.2C originally froze the exact *latest B manifest*. That is too strict for a
live pipeline: a legitimate new B queue would look like dependency drift.

C.2 now freezes:

```text
B script hash
B config hash
```

and requires:

```text
latest B error_count = 0
```

but allows:

```text
latest B queue_count > 0
latest B manifest hash to evolve
```

This is operational hardening, not weaker evidence governance.

## Manual recovery for the current pilot

Use a normal browser.

For the two SSEInfo pages, wait until the official Q&A content is visibly rendered,
then use **Print -> Save as PDF**:

```text
D:\positionx_data\incoming\optimus_discovery\603416_xinjie_20260916.pdf
D:\positionx_data\incoming\optimus_discovery\600353_xuguang_20261008.pdf
```

For 豪能股份, open the exact official SSE PDF URL and save the original PDF:

```text
D:\positionx_data\incoming\optimus_discovery\603809_haoneng_20250913.pdf
```

Then run the three one-record manual captures. Each still goes through V0.7.2A
Source Registry + immutable External Inbox before B.4 classification.

No Registry, Canonicalization, or Theme Stage mutation is performed.
