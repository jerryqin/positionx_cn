# Full-QMT Read-Only Probe V0.1.3 closure patch — OFFLINE ONLY

This is an independent supervisor/worker evidence protocol with a fixed fake SDK.
It has no real xtquant loader, QMT/Broker connection, production database/service
imports, submit/cancel calls, automatic retry/reconnect/fallback, or package setup.
Offline PASS never opens the Full-QMT implementation or trading gate.

## Six files

- supervisor.py: isolated child launch, OS output boundary, watchdog, final judgment,
  candidate publication, hash/COMPLETE verification and consumer entry.
- worker.py: facts only; explicit construct/register/start/connect/account/subscribe,
  four queries, bounded observation and stop. No business verdict.
- evidence.py: fixed filenames, strict JSON, timestamps, bounded redaction/capture,
  unique run directory and atomic completion publication.
- normalize.py: pure candidate field contract from the old environment, not a
  verified new Full-QMT schema.
- tests/test_fake_sdk.py: fake SDK and isolated tests, never real SDK imports.
- README.md: this protocol and execution limits.

## Offline test command

From the repository root, after reviewing test side effects:

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tools/full_qmt_readonly_probe_v01/tests -p test_fake_sdk.py -v
```

Tests use TemporaryDirectory with fake data and normal cleanup. They launch only
their own Python fake worker. Fault injection and process-survival simulations do
not control any existing process. No production paths, Registry, Scheduler,
persistent environment, installations or external network services are used.

The supervisor launches Python with -I -S -B, supplies only the approved repository
import root, and installs the fake module's real-SDK import blocker. This skips
worker .pth/sitecustomize startup. The parent interpreter's startup environment
still requires separate review; -B alone is not an import sandbox.
For this standard-library-only child it uses the parent's base Python executable
directly, avoiding Windows venv redirector/worker PID ambiguity. Final facts must
identify that exact Popen PID; this does not choose a real SDK environment.

Optional offline CLI, after approving an existing absolute isolated root:

```powershell
.\.venv\Scripts\python.exe -B -m tools.full_qmt_readonly_probe_v01.supervisor --offline-fake --evidence-root <APPROVED_ABSOLUTE_DIRECTORY>
```

No default evidence root. Production data/userdata paths and link/junction
ancestors are rejected. New directories use random IDs and exclusive creation;
existing evidence is never reused by the entry point. Files reject hardlinks.
The root must be trusted and locally accessible: path checks are not an OS sandbox
and do not defend against a privileged concurrent directory replacement.

## Completion protocol: do not consume candidate PASS

Required payload files are exactly:

- manifest.json
- steps.jsonl
- snapshots.jsonl
- callbacks.jsonl
- callback_attempts.jsonl
- channel_events.jsonl
- worker_events.jsonl
- worker_result.json
- process_checks.jsonl
- normalized.jsonl
- summary.candidate.json

Empty streams are explicitly created; a missing stream is not an empty stream.
Every raw stream record includes run_id, UTC/Asia-Shanghai timestamps and a
monotonic clock. Stream files must end at a complete newline. Duplicate JSON keys,
non-finite constants, bad types, invalid order or unknown steps are rejected.

1. Supervisor freezes and validates manifest version, run/config/source hashes,
   required checks/files, exclusions and candidate schema.
2. Worker records raw, redacted facts. The parent receives its final facts via an
   authenticated bounded pipe packet, independent of evidence-file writes, then
   persists worker_result.json. No account information or channel key is in argv.
3. Supervisor waits for confirmed child exit and both output readers. It recomputes
   callback sequences/outcomes/closure and channel byte/packet/EOF facts from raw
   observations, compares summaries, checks failure sources/durations/schema, and
   writes normalized views. Nonempty raw callback capture_errors directly fail.
4. summary.candidate.json has publication=CANDIDATE and a nested decision. It is
   never a consumable result on its own.
5. hashes.json must contain every required payload file, with byte SHA256.
6. The full consumer verification algorithm runs against the staged package under
   the original run deadline, before a COMPLETE marker can exist. A timing receipt
   binds the verified package to the original start/deadline. A flushed/fsynced
   COMPLETE.tmp is atomically published as COMPLETE last,
   without replacing an existing marker. Windows uses same-directory rename;
   POSIX uses link/no-replace followed by temp unlink. Leftover temp invalidates
   consumption. All subsequent journal writes are refused.
7. Publication checks the deadline again after marker creation, before returning.
   It reads back the actual published package with consume_completed under that
   same run deadline, then checks the final return gate. consume_completed independently
   rechecks the exact artifact set, marker/hash inventory,
   source/manifest/config identity, process facts, candidate decision and normalized
   views and original timing receipt. The publisher returns only the independently
   verified decision; later consumers must use consume_completed. Absent/invalid COMPLETE
   returns FAIL with completion=INCOMPLETE.

Process facts require every decision field, including cleanup_errors and
controller_error. Their exact types and nested shapes are checked before judgment
and again during consumption: missing is not equivalent to an explicit null, and
cleanup_errors must be a list, never a false-ish substitute. A valid schema does
not make a nonempty cleanup/controller error successful.

A sealed FAIL or NOT_COVERED is allowed: COMPLETE proves only complete evidence
publication. Business PASS/FAIL/NOT_COVERED is judged separately from its raw facts.
The marker contains no business verdict. If exit cannot be confirmed, publication
is refused. Hashes are not signatures. Power-loss durability of the filesystem,
controller and directory metadata is not guaranteed by file fsync alone; consumers
must always revalidate. Same-user malicious replacement of the entire evidence
bundle is outside this protocol's authentication guarantees.

## Failures, timeouts and cleanup

Primary SDK error, cleanup error and evidence-write errors are independent facts.
Writing an error record cannot replace the original exception. Bounded in-memory
failure facts travel through the parent channel even when worker file writes fail.
If storage/channel failure prevents durable preservation, the run is INCOMPLETE
or FAIL; details that were never observed are not fabricated.

Failure events, final failure facts and source conflicts all block PASS. A zero
child exit does not imply successful business results. The run monotonic start is
recorded at supervisor function entry, before configuration, directory creation,
manifest hashing or initialization. One deadline covers worker, publication,
hashing, complete consumer verification, marker creation and the final return gate.
Worker receives that same deadline; step watchdog and post-exit elapsed checks
remain additional gates. Finite late operations cannot return PASS. A run-scoped
watchdog revokes a marker if the finalizer is still active at expiry; normal
completion cancels it. Revocation demotes COMPLETE to COMPLETE.tmp, invalidating
the artifact set. Later reinspection uses a fresh bounded read budget without
extending the producer's original deadline or accepting a late timing receipt.

Cleanup terminates only the exact Popen child created here, waits, and checks actual
exit. It never enumerates or controls QMT. Windows kill is not used as a stronger
second layer. Terminate/wait failure blocks business PASS. If exit is subsequently
confirmed, its FAIL evidence can be completely published; an unconfirmed exit
refuses COMPLETE publication. This
is not Windows Job Object containment: abrupt supervisor death, descendants and
unresponsive OS/filesystem operations remain execution-gate concerns. Python
deadline checks and the revocation thread cannot interrupt a kernel I/O operation
or guarantee a wall-clock return during an OS/filesystem stall. Revocation itself
can fail at the OS boundary. Real execution requires an approved external process/
storage containment policy; this is not a hard real-time or power-loss guarantee.

## Privacy, callbacks and coverage

Account/ID/free-text/path values are tokenized by per-run in-memory HMAC; the key
is not persisted. Unreviewed field names and values, including numeric fields
named return, are tokenized by default. Scalar SDK status returns are a separate
context: only connect/subscribe integer statuses stay numeric. Unknown query
scalars are tokenized. Only explicitly known numeric financial/status fields
remain numeric in public objects. Unknown class/type names become type:<token>;
only builtin and explicitly reviewed fake types retain names. Original types and pre-redaction
ID validity are retained: empty/invalid IDs cannot become valid by tokenization.
None/primitive/wrong-class handles are rejected. Constructed account identity and
the actual pre-subscribe binding must match the requested account, independently
of query samples. No missing field becomes zero. On steps/events parse failure,
intact worker_result primary/cleanup/evidence errors are independently recovered.

Python stdout/stderr, formatted traceback and exceptions stay inside the worker
boundary. OS stdout/stderr are captured by parent pipes, never forwarded or saved
as raw logs. Only an authenticated final packet is parsed; unstructured/native
output is counted and blocks PASS. CLI errors do not echo arguments or paths.
The supervisor collects exception type identity from the class module/qualname
only; it never calls str/repr or otherwise formats the exception. Unknown type
identities remain tokenized, including cleanup and supervision failures.
Native SDK writes to files are not sandboxed; real SDK execution remains absent.

Callbacks copy fields and redact inside the callback, never query or transact.
The bounded queue drains at safe points and observation intervals. Atomic closure
marks late callbacks as failure events; the parent waits for process exit before
judgment and hashing. Explicit stop_begin/stop_end monotonic boundaries are
recorded under the same lock as callback sampling and bracket the stop step even
when stop raises. The consumer cross-checks them against steps and callback closure;
a disconnect belongs to shutdown only according to this timeline. Callback/attempt
phase labels are auxiliary: a label contradicting the timeline blocks PASS.
Overflow/capture/late events fail, not silently truncate.
Known callback payloads must satisfy the candidate field contract and account
binding. Order/cancel error callbacks and unexpected async responses fail business
judgment even when the worker's capture summary is otherwise clean.
Late callbacks also emit a fixed, non-sensitive parent-pipe alarm, so a failed
event-file write cannot hide an event arriving after the final facts snapshot.
If both output descriptors reject that alarm, this fake worker exits nonzero;
the parent cannot accept its earlier facts snapshot as success.

Per-file evidence limit: 8 MiB; total limit: 64 MiB; bounded capture rows/fields,
channel packet and failure lists also apply. Local sequence is observation order,
not broker causality. Native descriptor types must be reviewed before real use.

Raw query return and schema coverage are separate. None stays unknown, [] has no
field samples, and missing/wrong fields fail schema. A query may return successfully
while schema fails or is NOT_COVERED. Order/trade emptiness never proves full API
coverage. Absent callback event types are NOT_COVERED independently.

## Remaining 2026-10-19 execution gate — not implemented or authorized

- Real SDK import/installation/version policy, import/native module paths and hashes.
- Full-QMT version/mode, actual userdata/account/session rules and secure real input.
- SDK filesystem/network side effects, approved evidence root and native object review.
- New return, enum, field/time/unit semantics and callback threading/ordering.
- Live stop behavior and QMT client identity/login/function checks before and after.
- Approved policy for lost supervisor/descendants and any unconfirmed child exit.
- Explicit approval for a separate finite reconnect run; no automatic reconnect.

live_validation, qmt_client_health, implementation_gate, submit and cancel remain
NOT_COVERED even after offline PASS. Trading validation requires separate explicit
authorization after read-only review; this implementation cannot perform it.
