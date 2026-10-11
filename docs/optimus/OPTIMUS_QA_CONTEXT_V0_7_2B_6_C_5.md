# V0.7.2B.6 / C.5 — Q&A Local Context Hotfix

## Why this is needed

The 603809 official SSE PDF is machine-readable, but its page layout hard-wraps one
semantic answer across many physical lines. B.2/B.3/B.5 treated each line as a hard
context boundary, which produced a false negative:

```text
line 24: ...人形机器人...
line 25: ...验证与批量应用...
line 26: ...部分产品已实现批量供货
```

Those lines are one company answer, not unrelated document sections.

B.6 keeps the strict same-block rule and adds one narrow exception:

```text
explicit 答：/回复： block
+
robot/Tesla context
+
milestone within ±180 characters
```

It stops at the next numbered question. It does not join arbitrary PDF paragraphs.

## 603416

Do not OCR the image-only browser print.

Use the static official SSE post-event announcement instead:

```text
https://static.sse.com.cn/disclosure/listedinfo/announcement/c/new/2026-09-17/603416_20260917_NI0X.pdf
```

A one-record manifest is included:

```text
examples\discovery_manual_603416_static_sse.json
```

Download/save that official PDF and feed it to `manual-capture`.

## 600353

The browser-rendered PDF is image-only (`V044_PDF_TEXT_EMPTY pages=7`).
Do not OCR it in this phase. Keep the candidate pending until a machine-readable official
post-event announcement becomes available, or until a separately governed rendered-text
capture path is added.

## C.4

Candidate Discovery logic is unchanged. Only the frozen B dependency hashes are rebased
to B.6.

## C.5 immutable failed-capture handling

Earlier manual attempts already placed some source captures into the immutable External
Inbox before classification failed. C must not delete those captures, and it must not
abort all independent candidates because one old capture is unreadable.

C.5 therefore records:

```text
WAITING_CAPTURE_REPAIR
```

for that capture, creates **no discovery inference** from it, continues scanning other
captures, and returns an explicit partial scan:

```text
PARTIAL_OPTIMUS_CANDIDATE_DISCOVERY_SCAN_V0_7_2C_WAITING_CAPTURE_REPAIR
```

This allows valid 603416/603809 proposals to coexist with 600353 still pending repair.
