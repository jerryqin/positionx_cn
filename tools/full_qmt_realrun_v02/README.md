# Full-QMT Real-Run V0.2 — minimal offline implementation

Version: **0.2.1-offline** (B1 closure patch). Six files; no edits or monkeypatches to frozen V0.1.

```ini
V02_FAKE_ONLY = current validation scope
REAL_EXECUTION_ENABLED = false
REAL_SDK = NOT_COVERED
REAL_FULL_QMT = NOT_COVERED
EXECUTION = OUT_OF_SCOPE
```

This version validates the boundary for one future Full-QMT XtQuantTrader read
migration run. It has a real owned subprocess and OS pipes, with an **inert built-in
fake SDK only**. It has no enabled real SDK loader, QMT connection or real-run CLI.
A profile boolean, a real pathname or a supplied authorization cannot enable real
execution. Real loader/accessor/environment approval remains a separate gate;
fake PASS must never be reported as Full-QMT runtime PASS.

## Six files and reuse

- `supervisor.py`: one run deadline, preflight, final judgment and publication.
- `worker.py`: fixture lifecycle/raw facts, closed dispatcher and safe capture.
- `profile.py`: explicit selection/source binding, schema and independent consumer.
- `windows_runtime.py`: owned Popen, private stdin, bounded stdout/stderr OS pipes.
- `tests/test_fake_realrun.py`: isolated fake-only checks, independent of V0.1 tests.
- this README.

V0.1's fail-closed, raw-facts precedence, separate errors, timeline and COMPLETE
contracts are retained in this smaller implementation, not its synchronous fake
process/handle model, manifest or consumer. No frozen modules are imported.
Before report encoding the worker seals callback admission and drains/joins every
registered fake producer within the run deadline. Drain failure refuses PASS.
Any callback after closure, callback error/capture failure/overflow, or report-write
failure sets an independent sticky failure and emits one fixed, non-sensitive
stderr OS-pipe marker. Failure to deliver that marker exits the owned worker
unsuccessfully. Every scenario uses this mechanism; the after-report fixture only
injects a callback and does not choose the safety outcome.

Confirmed child exit and pipe EOF precede publication; no callback runs in the
supervisor. Runtime latches the marker as an exact boolean `callback_failure` in
the hashed process evidence. Supervisor and independent consumer require that
fact to be false and the report's producer closure to be complete before PASS.
A clean encoded report and even exit code zero cannot wash out a delivered veto.
No live/in-memory registry is needed by later consumers. Actual native SDK producer
closure remains unimplemented, rather than inferred from the fake producer model.

## Selection, secrets and source identity

`make_fake_profile` takes explicit repository, Python and inert fixture root.
Fixture root must contain `sdk/package/`, `sdk/dlls/`, `sdk/native.fixture`, and
`userdata/`. Nothing is discovered, installed, downloaded or mixed with another
SDK. Native fixture identity is a file hash, **not** native loading proof.
Offline Python must be this supervisor's explicitly selected interpreter; arbitrary
executables cannot be substituted for the fake worker launcher.
The profile pins:

- probe `f9679927afaf535638fc4347ce41ba24c2a592e6` (six actual Git blobs);
- harness V0.1 `f136674f655c243d7612a6f4c95d1a9a613c5baf` (seven actual blobs);
- context `8ca8defeb81fb678e5451946c1ce9e01a8ddf5a0` (AGENTS/context blobs);
- V0.2: null means explicitly uncommitted offline bytes; a future source commit
  must match all six actual Git blobs. Every run/consumer rereads working SHA256
  and Git blob identity. Strings alone cannot establish identity.

No account is discovered. Caller supplies one account privately; stdin pipe carries
it once to the child, never argv/environment/files/evidence. Public evidence has a
run-scoped HMAC token, not a reusable account hash. Exact private paths and the key
are not written. Known financial fields retain reviewed primitive values; unknown
fields/values/types are tokenized. Exceptions are not formatted or named. Plain
fixture storage is read without properties/descriptors/getters/str/repr. A rejected
object is a capture failure. Callback threads never replace global output streams.

The child starts with `-I -S -B` and only SystemRoot/WINDIR environment entries;
site/.pth/usercustomize and PYTHONPATH are excluded. Only its explicit source
directory is added to sys.path. Its named-import guard denies xtquant and records
a sticky scope error even if the exception is caught. This is not a general
Python dynamic-loader or native sandbox. Supervisor has no SDK import path.

## Process and output boundary

Only the retained Popen object can be terminated; Windows Popen retains its own
process HANDLE. No external PID, name, process tree or QMT client is controlled.
Timeout/overflow uses terminate + bounded wait + actual exit confirmation, with
no stronger-kill claim. Missing exit/pipe EOF refuses PASS. Real creation identity
and Windows private IPC/native inheritance still need the real-run review.

Stdout/stderr pipes exist before Python startup, drained concurrently with bounded
byte counters. The one fixed failure marker is recognized across read boundaries;
other raw bytes are discarded, never printed, hashed publicly or saved.
Any output, overflow, read error or missing EOF fails the run. This basic boundary
also captures `os.write(1/2, ...)`; it is not Python redirect_stdout. Console handles
opened separately/native logging/file writes are not contained. Cleanup is bounded
even after deadline expiry but cannot restore PASS. Subprocess environment changes
are child-local; no persistent environment/system modifications occur.

## Fixed lifecycle and decisions

```text
construct -> register_callback -> start -> connect -> construct_account
-> subscribe -> query_asset -> query_positions -> query_orders -> query_trades
-> bounded_callback_observation -> stop
```

Early failure may proceed directly to one cleanup stop. Every other out-of-order,
unknown, repeated, or callback-thread SDK invocation latches scope failure.
No submit/cancel/replace/modify/account discovery/reconnect/fallback/download or
client start/stop/restart API is implemented. Account construction validates
identity, not just handle type. Connect/subscribe require exact integer zero in
the fixture contract; this is not a verified new-SDK return contract.

Consumer independently derives decisions from process facts, raw steps, snapshots,
callbacks and separated primary/cleanup/evidence/scope errors. No worker PASS flag
is trusted. Stop timeline determines disconnected shutdown; late/error/overflow/
capture failures reject PASS. No sample is manufactured by trading.

- PASS: every declared offline required check and all four query schemas covered.
- FAIL: explicit interface, identity, schema, scope, process, output, timeout,
  cleanup, evidence, health or source failure.
- NOT_COVERED: no explicit failure but None or empty query samples leave required
  coverage unknown. `None` is not query success; `[]` is a returned collection with
  no schema samples. Missing/wrong fields/types are FAIL.

All health facts here are explicitly simulated pre/post OS/manual fixtures.
SDK post-stop health is NOT_COVERED, never inferred from a pre-stop asset query.
Order/trade callbacks are optional samples, absence is not proof of coverage.
Real required health/callback/schema checks must be frozen before real execution;
cannot lower requirements afterwards or let manual evidence impersonate SDK facts.

## Evidence and deadline

Each new unique run contains exactly:
`manifest.json`, `facts.json`, `process.json`, `decision.candidate.json`,
`hashes.json`, `COMPLETE`. No run directory reuse/overwrite. Facts preserve raw
return shape, reviewed fields, monotonic steps/callbacks and separate error slots;
normalized views are independently derived in candidate/consumer.

Hashes cover the full fixed payload inventory. COMPLETE is atomically renamed
last and binds run, hashes and timing. It means complete publication, **not business
success**: complete FAIL/NOT_COVERED packages are valid. Missing/extra/temp/partial/
bad-schema artifacts, source mismatch, candidate conflict or incomplete hash lists
cannot be consumed as PASS. Independent consumption requires the caller's expected
manifest; hashes are integrity checks, not signatures against a privileged attacker.

One high-resolution monotonic (`perf_counter_ns`) deadline starts at supervisor entry and covers preflight, worker,
output drain, publish, hashes, COMPLETE and final consumption, with before/after
checks. Finite operations finishing after expiry invalidate completion; uninterruptible
OS I/O is not promised to finish on time. OS invalidation/durability failures are
limitations below, not silently accepted runtime success.

## Offline test (review side effects first)

```powershell
.\.venv\Scripts\python.exe -I -S -B -m unittest discover -s tools/full_qmt_realrun_v02/tests -p test_fake_realrun.py -v
```

Tests use stdlib only, auto-cleaned TemporaryDirectory fixtures, owned fake worker
processes and read-only Git inspection. No SDK import, network, QMT process control,
production state, package install, Registry/Scheduler edits, staging or commit.

## REAL_RUN_LIMITATION / remaining gate

Not proven here: actual Python/xtquant/native ABI and dependency startup under -S;
two distinct SDK installations must not be mixed; actual module/DLL origin/hash;
Full-QMT userdata/session and connect/subscribe semantics; native fields/accessors,
callback/stop behavior; real HANDLE/IPC policy; SDK import/network/log/userdata/temp
effects; native output bypasses; client pre/post health. Side effects must be
explicitly approved, denied or unresolved before the real loader is enabled.
No generic native sandbox, complete named-object observation, arbitrary loader
containment, secure memory erasure, crashdump safety, force-kill/power-loss or
durability guarantee is claimed. These are not reasons to grow the offline framework.

10/19 requires explicit environment/SDK/paths/account/session/evidence/output/IPC/
write allowances, health requirements, deadlines and owned-worker termination
authorization. First run is one lifecycle, no reconnect; first failure seals its
evidence, no live patch-and-continue. Source changes require a separately reviewed
version/commit. Adapter work waits for real read evidence; execution needs separate
authorization. This implementation is submitted for one independent offline review.
