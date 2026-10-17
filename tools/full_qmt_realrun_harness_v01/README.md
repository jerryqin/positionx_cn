# Full-QMT Real-Run Harness V0.1 — offline contract implementation

```text
HARNESS_FAKE_ONLY = PASS / 当前离线阶段
REAL_SDK = NOT_COVERED
REAL_FULL_QMT = NOT_COVERED
EXECUTION = OUT_OF_SCOPE
```

Version 0.1.3. The target profile scope is REAL_FULL_QMT_READ_ONLY, but every
currently executable run is explicitly execution_mode=HARNESS_FAKE_ONLY.
A PASS proves only this harness's offline fixture contract. It is not a real
SDK/QMT result and does not authorize QmtBrokerAdapter or any transaction.

## Files and frozen dependency

- supervisor.py: deadline, source/selection preflight, private request, fake worker
  management, source-labelled health facts and final publication/consumption.
- worker.py: facts-only lifecycle, bounded callbacks, separate SDK/cleanup/evidence
  errors. Real SDK imports are blocked for the whole fixture lifecycle.
- sdk_loader.py: explicit selection identities and closed SDK call dispatcher.
- real_profile.py: independent manifest, artifact registry, validators and consumer.
- windows_runtime.py: scripted fake process, opaque ownership capability, private
  in-memory channel, authenticated report and labelled fake health fixtures.
- tests/test_fake_harness.py: independent fixture implementation and adversarial tests.

Context source commit: bab4fc4c259450efe31c83a104ab256c532e3f99.
Probe source commit: f9679927afaf535638fc4347ce41ba24c2a592e6.
The six frozen probe files are never edited or monkeypatched. We reuse reviewed
pure configuration/validation functions, normalization, clocks/deadline, callback
buffer and the facts Recorder. The old fake worker, fake manifest, fake handle
whitelist and full old consumer are not used as the new real profile.

Every run rereads/hashes all seven harness files and six frozen probe files.
Probe and AGENTS.md working bytes must equal actual pinned Git blobs. The living
PROJECT_CONTEXT.md may have the authorized offline update: its current SHA256/blob
and the distinct pinned baseline SHA256/blob are recorded with
binding_state=OFFLINE_CONTEXT_UPDATE. They are never presented as identical.
Consumer rechecks both bindings; this exception does not enable real mode.
Pinned immutable
Git object bytes can be cached; working files are always reread. Git commands
are read-only cat-file operations, never add/commit or production commands.
The uncommitted offline harness uses harness_source_commit=null and
harness_binding_state=UNCOMMITTED_OFFLINE, with actual SHA256 and Git blob IDs.
A future explicit harness commit is accepted only if its actual blobs match all
seven files; a commit-shaped string alone is insufficient. Real runs must require
a reviewed committed harness; real mode is unconditionally disabled here.

## Offline process/import boundary

This stage launches NO SDK worker OS process. FixtureProcess is a synchronous,
scripted model with a simulated PID/handle, not Windows process validation. The
private one-shot request and HMAC report model future private IPC. No account or
key is put in argv, environment or an evidence file. This is not a proof of real
Windows anonymous-pipe inheritance or native stdout/stderr capture.

There is no real loader, subprocess launcher for QMT, real client inspection,
encryption system, credential discovery, SDK installation or network operation.
Neither an authorization boolean nor explicit real paths enable real SDK import.
The supervisor never imports xtquant. Tests install a real-import deny finder
before harness imports; workers guard the entire fake lifecycle and retained
callbacks guard capture even after the report is serialized. Harness denials
produce a persistent real_sdk_import_denied event; tests independently verify
that this guard runs before the outer test guard. This guards normal named
imports, including importlib.import_module, not arbitrary Python dynamic loading:
spec_from_file_location / loader.exec_module are not an OS sandbox boundary.
The harness itself has no such dynamic loading path. Fixtures remain trusted code.

Selection must explicitly supply exactly one Python file, SDK root, module file,
native module file, DLL roots and DLL files. No auto-discovery, PATH selection,
fallback or alternate SDK/Python is attempted. Fixtures are inert files under a
test TemporaryDirectory, not imported executables. Inventory records file IDs,
actual SHA256 and run-scoped path tokens; no exact sensitive paths are persisted.
The fixture inventory explicitly says loading=SIMULATED; it is NOT actual native
module loading evidence. A future real loader must record actual __file__/module
origin, Python/SDK versions, loaded .pyd/DLL identities and hashes after review.

Before the fake factory runs the worker establishes deadline, evidence streams,
and Python stdout/stderr suppression. These model the future boundary; actual
OS stdout/stderr pipes, SDK import effects and native observations remain gates.
Unknown import/file/network/userdata effect facts cannot be treated as approved.
Only the two fixed fixture effect kinds are accepted; fixture declarations of
"approved" are not used as authorization. Output/exception/free-text values are
not persisted raw. Parent error collection never formats exceptions. Unknown
objects are tokenized without str/repr. Financial/status fields retain only the
reviewed primitive contract. This is trusted fixture execution, not a sandbox
against arbitrary malicious Python/vendor/native code.

## Lifecycle and outcome contract

The dispatcher accepts only, and in order:

```text
construct -> register_callback -> start -> connect -> construct_account
-> subscribe -> query_asset -> query_positions -> query_orders -> query_trades
-> bounded_callback_observation -> stop
```

Cleanup stop may follow an earlier failure, but never repeats or reconnects.
Unknown commands, wrong order and callback/thread SDK calls record a persistent
scope violation. No submit/cancel/replace/modify/account discovery/download,
fallback or QMT/client control is implemented. The fixture dispatcher uses
FIXTURE_USERDATA only; it cannot take a real userdata path or connect a real SDK.
Future Full-QMT path/account/session policy must be reviewed separately.

Handle types are checked before capture and independently matched to this
profile's fixture inventory; they are not disguised as frozen fake types.
Constructed and subscribed account identity and captured account tokens must
match the private request. None is unknown; [] proves a returned collection but
has no schema samples. Missing/wrong fields fail schema; never use result or [].
Callbacks copy/redact facts only and use synchronized stop_begin/stop_end and
closure boundaries. Consumer checks raw attempts, phase/timestamp consistency,
overflow/capture/late facts and summaries; error callbacks block PASS. A separate
run-owned CallbackClosure remains live after report serialization. Late callbacks
and callback capture/evidence errors latch a veto before attempting normal writes.
closure_state.json is a required hashed snapshot; consumer additionally checks the
live latch and a fixed fail-only CALLBACK_FAILED tombstone. The tombstone can be
created exclusively after COMPLETE without modifying sealed payload/hashes. Its
presence (even damaged) can only refuse success. Failure to persist it permanently
sets unpersisted_failure and publish_denied; later successful writes cannot reset
either flag. Revocation of an existing marker is additional protection, not the
publication gate. Under the run lock, publication requires a clean live latch,
an exactly matching successfully persisted closure snapshot, no unpersisted
failure, and a verified staged decision matching the candidate. A PASS candidate
requires staged PASS. The hashed closure snapshot includes a monotonic generation;
the publication gate binds that generation and the permitted staged verdict.
COMPLETE publication is provisional: under the same closure lock, state is checked
before atomic rename and again immediately after it returns. Both checks require
unchanged generation, clean sticky flags, matching persisted closure state and
the permitted verdict. Same-thread callback reentry inside rename therefore
cannot rely on the earlier staged/pre-rename check. A conflict sets publish denial,
invalidates an already-created COMPLETE to COMPLETE.tmp while still holding the
lock, and returns explicit pre_rename_closure_conflict or
post_rename_closure_conflict diagnostics. Valid payload/hash bytes cannot turn
that invalidated package into PASS after the live registry is discarded.
A persisted business FAIL/NOT_COVERED with a clean closure
can still be published as a complete FAIL/NOT_COVERED package. A reentrant closure
failure may overlap creation of a provisional marker, but cannot leave a
consumable COMPLETE after the publication transaction returns successfully.
No child-exit trick is used.
The lock-protected final veto check is the decision linearization point. Later
callbacks cannot retract a returned value. Subsequent consumers reject a persisted
veto or an invalidated marker; current-process consumers additionally check the
live latch. Failed OS marker invalidation remains a real-run limitation below.
Raw stop begin/end, return/error and worker attempted/returned/cleanup facts are
cross-checked independently by this harness; conflicts are lifecycle_source_conflict.

Callback threads never redirect or replace sys.stdout/sys.stderr. Offline callback
payloads are limited to None or exact types.SimpleNamespace with primitive fields
in its reviewed plain dict storage. No dir(), property, descriptor, unknown getter
or formatter is executed. Unknown payload types and object-valued fields fail
capture; unknown primitive fields are tokenized. Queries retain their separate
worker-only capture profile. Real SDK/native accessor coverage is NOT_COVERED and
must receive its own reviewed capture policy; native output isolation remains a
real-run gate. Concurrent callbacks still use the buffer's fact lock, not a global
output-redirection lock. The callbacks do not depend on output suppression for
safety and do not restore a stream object saved by another callback thread.

Health requirements are fixed before execution. OS, SDK and human observations
have distinct source/check/origin tuples. All current observations additionally
say simulation=true and FAKE_OS/FAKE_SDK/FAKE_HUMAN; none claims live health.
SDK account readability is during_lifecycle/sdk/account_readable, bound to the
asset snapshot's original timestamp and step ID. It is never post-stop evidence.
post_stop/sdk/account_readable is explicitly simulated NOT_COVERED/not_performed;
it is a mandatory disclosure, excluded from the offline required-health PASS
aggregation. POST_STOP_SDK=NOT_COVERED remains in the result even for fixture PASS.
Real post-stop health requirements must be reviewed/frozen before real execution.
OS/human fixtures cannot stand in for SDK facts or for each other.

Outcome rules are fixed in code:

- PASS: every declared required check meets its requirement.
- FAIL: explicit interface, scope, identity, schema, timeout, cleanup, source,
  evidence conflict or health failure.
- NOT_COVERED: no explicit failure, but a required capability has no sample or
  observation (e.g. None/empty-query schema or explicit unknown health).

No transaction may be used to generate schema/callback samples. Missing a
declared check or corrupting the required inventory is invalid evidence, not PASS.
Absent callback types remain unobserved; capturing a connected/account-status
fixture does not prove order/trade/error callbacks or native causality.

## Evidence and deadline

The fixed payload set is manifest.json, steps.jsonl, snapshots.jsonl,
callbacks.jsonl, callback_attempts.jsonl, worker_events.jsonl, worker_result.json,
process_checks.jsonl, channel_events.jsonl, bootstrap_events.jsonl,
sdk_inventory.json, import_effects.jsonl, client_health.jsonl, normalized.jsonl,
summary.candidate.json and closure_state.json. hashes.json lists every payload's
actual-byte SHA256. CALLBACK_FAILED is a separate, fail-only post-seal tombstone,
not mutable successful evidence and not part of the immutable hash inventory.
COMPLETE is published atomically last, without overwrite, after staged consumer
verification. Final consumer revalidates actual published bytes and raw facts.
COMPLETE is package completion only; a valid complete FAIL or NOT_COVERED is allowed.
Exact artifact and hash inventories, manifest/source/selection bindings and
candidate/raw/normalized equality are required. No marker means INCOMPLETE/FAIL.
Do not consume candidate PASS. Later consume requires the caller's explicit
selection to independently recheck SDK fixture file identities/hashes; no path
map or HMAC key is recovered from public evidence.

A single monotonic deadline starts at supervisor entry, covers preflight/load,
lifecycle, health, publish/hash/COMPLETE and final consume. Both before/after I/O
checks and marker revocation apply. Simulated worker hangs are scripted, not
uninterruptible threads. Finite late operations cannot become PASS. Owned cleanup
accepts only the harness-created fixture capability; it never targets arbitrary
PIDs, process names, trees or QMT. Unknown exit refuses COMPLETE. A later consumer
has a bounded read budget but validates the producer's original timing receipt.
Only the fixed flat artifact names are writable. Evidence roots reject production
paths, links/junctions and hardlinked files. Limits are 8 MiB per file, 64 MiB total.
Hashes are not signatures; privileged concurrent replacement, OS stalls, crash,
power loss and failed marker revocation are not solved by this offline model.

## Test command (only after side-effect review)

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tools/full_qmt_realrun_harness_v01/tests -p test_fake_harness.py -v
```

Tests use disposable fake SDK files/evidence in TemporaryDirectory, fake process
and health fixtures, and read-only Git source verification. They never import
real xtquant, create real broker workers, access production databases, install
packages, edit Registry/Scheduler/system config or stage/commit. Their result is
independent of the frozen probe's historical 108/108 tests.

## REAL_RUN_GATE_ONLY (not implemented or authorized)

- Approved real Python/SDK/dependency/ABI and actual module/DLL loading identity.
- Owned Windows worker HANDLE/PID/creation-time proof, private OS IPC and native
  output capture before imports; no name/tree/QMT termination.
- Approved import/file/network/IPC/userdata/log effects and observation coverage.
- Secure real account input, Full-QMT userdata/session and real type/descriptor,
  enum/field/time/quantity contracts; no silent conversion to obtain PASS.
- Real OS, SDK and human client-health attestations before/after stop.
- Process/storage containment, OS blocking, strong supervisor death and durability.
- Independent real-run harness review/commit and explicit finite-run authorization.

Frozen V0.1.3 remains untouched. Failed first real evidence must be sealed; no live
patch-and-continue in the same run. Source changes require a new reviewed/tested
version and commit. No QmtBrokerAdapter implementation before the real read gate.
