"""Independent V0.1 evidence profile/consumer; real execution stays disabled."""

import hashlib
import hmac
import json
import math
import os
import re
import subprocess
import threading
import weakref
from pathlib import Path
from types import SimpleNamespace

from tools.full_qmt_readonly_probe_v01.evidence import (
    CallbackBuffer, Capture, Redactor, RunDeadline, bounded_io, clock,
    new_run_directory, qualified_type, strict_json, verify_directory, PUBLIC_FIELDS, IDENTIFIERS, valid_id,
)
from tools.full_qmt_readonly_probe_v01.normalize import CONTRACT, normalize
from tools.full_qmt_readonly_probe_v01.supervisor import (
    configuration, is_token, safe_type, validate_clock, validate_public,
    validate_callback_raw, validate_process_facts, validate_callback_timeline,
)
from .sdk_loader import ReadOnlyGate, selection_inventory, ContractViolation
from .windows_runtime import HEALTH_REQUIRED, POST_STOP_HEALTH, ORIGINS


VERSION = "0.1.3"
SCOPE = "REAL_FULL_QMT_READ_ONLY"
MODE = "HARNESS_FAKE_ONLY"
CONTEXT_COMMIT = "bab4fc4c259450efe31c83a104ab256c532e3f99"
PROBE_COMMIT = "f9679927afaf535638fc4347ce41ba24c2a592e6"
ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
SOURCES = ("README.md", "supervisor.py", "worker.py", "sdk_loader.py", "real_profile.py",
           "windows_runtime.py", "tests/test_fake_harness.py")
PROBE_SOURCES = ("README.md", "supervisor.py", "worker.py", "evidence.py", "normalize.py", "tests/test_fake_sdk.py")
STREAMS = frozenset({"steps.jsonl", "snapshots.jsonl", "callbacks.jsonl", "callback_attempts.jsonl",
                     "worker_events.jsonl", "process_checks.jsonl", "channel_events.jsonl",
                     "bootstrap_events.jsonl", "import_effects.jsonl", "client_health.jsonl", "normalized.jsonl"})
PAYLOAD = STREAMS | {"manifest.json", "worker_result.json", "sdk_inventory.json", "summary.candidate.json", "closure_state.json"}
FILES = PAYLOAD | {"hashes.json", "COMPLETE", "COMPLETE.tmp", "CALLBACK_FAILED"}
DOMAINS = {"query_asset": "account", "query_positions": "positions", "query_orders": "orders", "query_trades": "trades"}
CHECKS = (*ReadOnlyGate.ORDER, *["schema." + d for d in CONTRACT], "manifest", "source_binding", "inventory",
          "bootstrap", "side_effects", "process_schema", "worker_exit", "raw_evidence", "source_consistency",
          "all_failure_sources", "callback_timeline", "callback_capture", "no_disconnect", "account_binding",
          "deadline", "health", "lifecycle_consistency", "callback_closure")
FAILURE_EVENTS = {"primary_failure", "cleanup_failure", "evidence_failure", "late_callback_failure", "scope_violation", "real_sdk_import_denied"}
CALLBACK_EVENTS = {"connected", "disconnected", "account_status", "asset", "position", "order", "trade",
                   "order_error", "cancel_error", "unexpected_order_async_response", "unexpected_cancel_async_response"}
_PINNED_BLOBS = {}  # Git object IDs are immutable; working bytes are always reread.
_LIVE_CLOSURES = weakref.WeakValueDictionary()
_CLOSURE_REGISTRY_LOCK = threading.RLock()
LATCH_REASONS = {"late_callback", "callback_evidence_error", "closure_conflict"}


class PublicationConflict(ContractViolation):
    def __init__(self, phase, reason):
        super().__init__("Closure publication conflict")
        self.phase, self.reason = phase, reason


class CallbackClosure:
    """Run-owned veto, independent of the serialized report and sealed Journal."""

    def __init__(self, journal):
        self.journal = journal
        self.lock = threading.RLock()
        self.failures = []
        self.worker_closed = self.report_serialized = False
        self.unpersisted_failure = self.publish_denied = False
        self.generation = 0
        self.persisted_snapshot_digest = None
        self.publication_verified = False
        self.publication_generation = self.publication_verdict = None
        with _CLOSURE_REGISTRY_LOCK:
            key = str(journal.directory)
            if key in _LIVE_CLOSURES:
                raise ContractViolation("Closure cannot be reused")
            _LIVE_CLOSURES[key] = self

    def trip(self, reason):
        with self.lock:
            self.generation += 1
            self.publication_verified = False
            self.failures.append(reason if reason in LATCH_REASONS else "closure_conflict")
            if len(self.failures) > 16:
                self.failures = self.failures[:16]
            # A fail-only tombstone may be published after COMPLETE. It never
            # changes the sealed payload, never grants success, and carries no text.
            try:
                self.journal.write_callback_veto({**clock(), "run_id": self.journal.directory.name,
                                                 "reason": self.failures[0]})
            except BaseException:
                # Sticky even if later writes recover. A revoke before COMPLETE
                # exists cannot prevent a future publication by itself.
                self.unpersisted_failure = self.publish_denied = True
                if self.journal.deadline is not None:
                    self.journal.deadline.revoke()

    def close_worker(self, callback_facts):
        with self.lock:
            self.generation += 1
            self.worker_closed = callback_facts.get("closed") is True
            if not self.worker_closed:
                self.trip("closure_conflict")

    def serialized(self):
        with self.lock:
            self.generation += 1
            self.report_serialized = True

    def snapshot(self):
        with self.lock:
            return {"run_id": self.journal.directory.name, "worker_closed": self.worker_closed,
                    "report_serialized": self.report_serialized, "failures": list(self.failures),
                    "unpersisted_failure": self.unpersisted_failure, "publish_denied": self.publish_denied,
                    "generation": self.generation}

    def persist_snapshot(self):
        with self.lock:
            state = self.snapshot()
            try:
                self.journal.write("closure_state.json", state)
                self.persisted_snapshot_digest = digest(state)
            except BaseException:
                self.generation += 1
                self.unpersisted_failure = self.publish_denied = True
                raise

    def publication_state_error(self, journal):
        if (type(self.generation) is not int or self.generation < 0 or any(type(value) is not bool for value in (
                self.unpersisted_failure, self.publish_denied, self.worker_closed, self.report_serialized))):
            return "invalid_live_closure_state"
        if self.unpersisted_failure:
            return "unpersisted_closure_failure"
        if self.publish_denied:
            return "publication_already_denied"
        if (self.failures or not self.worker_closed or not self.report_serialized
                or journal.path("CALLBACK_FAILED").exists()):
            return "live_closure_not_clean"
        if (self.persisted_snapshot_digest is None or journal.read("closure_state.json") != self.snapshot()
                or digest(journal.read("closure_state.json")) != self.persisted_snapshot_digest):
            return "closure_not_persisted"
        return None

    def publication_gate(self, journal, candidate, staged):
        """Caller holds this same lock through atomic COMPLETE creation."""
        self.publication_verified = False
        self.publication_generation = self.publication_verdict = None
        reason = self.publication_state_error(journal)
        if reason is None and (staged.get("completion") != "CANDIDATE" or candidate["status"] not in {"PASS", "FAIL", "NOT_COVERED"}
                               or staged.get("status") != candidate["status"]):
            reason = "staged_decision_conflict"
        if reason is None:
            self.publication_generation = self.generation
            self.publication_verdict = (candidate["status"], staged["status"], staged["completion"])
            self.publication_verified = True
            return None
        self.publish_denied = True
        return reason


@bounded_io
def closure_veto(journal, decision):
    """Final decision linearization; a subsequent callback vetoes later readers."""
    with _CLOSURE_REGISTRY_LOCK:
        live = _LIVE_CLOSURES.get(str(journal.directory))
    lock = live.lock if live is not None else threading.RLock()
    with lock:
        failed = journal.path("CALLBACK_FAILED").exists() or (live is not None and (
            bool(live.failures) or live.unpersisted_failure is not False or live.publish_denied is not False
            or live.worker_closed is not True or live.report_serialized is not True))
        if not failed:
            return decision
        return {**decision, "status": "FAIL", "reason": "callback_closure_failure",
                "checks": {**decision.get("checks", {}), "callback_closure": "FAIL", "all_failure_sources": "FAIL"},
                "failures": [*decision.get("failures", []), {"kind": "callback_closure_failure"}]}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()).hexdigest()


class PrivateRedactor(Redactor):
    """Never stringify unknown objects or exceptions, including in the parent."""

    def __init__(self, key):
        self.key = key

    def token(self, value):
        if type(value) in (str, int, bool, float, type(None)):
            payload = [type(value).__name__, value]
        else:
            payload = ["opaque", id(value)]
        return "redacted:" + hmac.new(self.key, json.dumps(payload, allow_nan=False).encode(), hashlib.sha256).hexdigest()

    def error(self, exc, **facts):
        return {"error_type": qualified_type(exc, self), **facts}


class RealCapture(Capture):
    def returned(self, value, handle=False, status_return=False):
        if handle:
            if value is None or type(value) in (str, int, float, bool, list, tuple, dict, set):
                raise ContractViolation("Invalid owned handle")
            return {"raw_type": qualified_type(value, self.redactor), "kind": "handle", "object_ref": "harness-owned"}
        return super().returned(value, handle=False, status_return=status_return)


class CallbackCapture(RealCapture):
    """Offline reviewed payload only: exact SimpleNamespace and primitive fields.

    No dir(), getattr(), properties, native descriptors or unknown formatters.
    Real SDK/native accessors need a separate reviewed capture profile.
    """

    def returned(self, value, handle=False, status_return=False):
        if value is None:
            return {"raw_type": "builtins.NoneType", "kind": "none", "value": None}
        if type(value) is not SimpleNamespace:
            raise ContractViolation("Unreviewed callback payload")
        return {"raw_type": "types.SimpleNamespace", "kind": "object", "items": [self.public(value)]}

    def public(self, value):
        if type(value) is not SimpleNamespace:
            raise ContractViolation("Unreviewed callback payload")
        source = object.__getattribute__(value, "__dict__")
        if type(source) is not dict or len(source) > self.max_fields:
            raise ContractViolation("Invalid callback field storage")
        fields, errors = {}, []
        for name, item in source.copy().items():
            if type(name) is not str:
                raise ContractViolation("Invalid callback field label")
            label = name if name in PUBLIC_FIELDS else "redacted_field_" + self.redactor.token(name)[9:]
            if type(item) not in (str, int, bool, float, type(None)):
                fields[label] = {"raw_type": "type:" + self.redactor.token(item)[9:], "value": self.redactor.token(item)}
                errors.append({"error_type": "builtins.TypeError", "field": label})
                continue
            fields[label] = {"raw_type": qualified_type(item, self.redactor), "value": self.redactor.value(name, item)}
            if name == "account_id" or name in IDENTIFIERS:
                fields[label]["source_valid"] = valid_id(item)
        return {"raw_type": "types.SimpleNamespace", "public_fields": fields, "capture_errors": errors}


def _git(args, deadline):
    deadline.check()
    result = subprocess.run(["git", "-C", str(ROOT), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=max(0.001, deadline.remaining()), check=False)
    deadline.check()
    if result.returncode or len(result.stdout) > 2 * 1024 * 1024:
        raise ContractViolation("Git source binding unavailable")
    return result.stdout


def blob_id(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def source_bindings(deadline, harness_commit=None):
    if harness_commit is not None and (type(harness_commit) is not str or not re.fullmatch("[0-9a-f]{40}", harness_commit)):
        raise ContractViolation("Invalid harness commit")
    groups = (("context", CONTEXT_COMMIT, ROOT, ("AGENTS.md", "docs/PROJECT_CONTEXT.md")),
              ("probe", PROBE_COMMIT, ROOT / "tools/full_qmt_readonly_probe_v01", PROBE_SOURCES),
              ("harness", harness_commit, HERE, SOURCES))
    bindings = {}
    for role, commit, directory, names in groups:
        entries = {}
        for name in names:
            deadline.check()
            path = directory / name
            if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400 or path.stat().st_nlink != 1:
                raise ContractViolation("Linked source rejected")
            data = path.read_bytes()
            relative = path.relative_to(ROOT).as_posix()
            entry = {"sha256": hashlib.sha256(data).hexdigest(), "git_blob": blob_id(data)}
            if commit is not None:
                cache_key = (commit, relative)
                if cache_key not in _PINNED_BLOBS:
                    _PINNED_BLOBS[cache_key] = _git(["cat-file", "blob", commit + ":" + relative], deadline)
                pinned = _PINNED_BLOBS[cache_key]
                if data != pinned:
                    if role != "context" or name != "docs/PROJECT_CONTEXT.md":
                        raise ContractViolation("Working source differs from pinned Git blob")
                    # The authorized living context update is not the historical
                    # baseline. Bind both sets of bytes, explicitly offline only.
                    entry.update(binding_state="OFFLINE_CONTEXT_UPDATE",
                                 baseline_sha256=hashlib.sha256(pinned).hexdigest(),
                                 baseline_git_blob=blob_id(pinned))
            entries[name] = entry
        bindings[role] = entries
    return bindings


def manifest_for(journal, config, selection, redactor, deadline, harness_commit=None):
    return {"schema_version": VERSION, "scope": SCOPE, "execution_mode": MODE, "run_id": journal.directory.name,
            "context_source_commit": CONTEXT_COMMIT, "probe_source_commit": PROBE_COMMIT,
            "harness_source_commit": harness_commit,
            "harness_binding_state": "UNCOMMITTED_OFFLINE" if harness_commit is None else "COMMIT_BOUND",
            "source_bindings": source_bindings(deadline, harness_commit), "config": configuration(config),
            "run_timing": deadline.timing, "required_checks": list(CHECKS), "required_files": sorted(PAYLOAD),
            "health_required": [list(row) for row in HEALTH_REQUIRED], "sdk_selection": selection_inventory(selection, redactor, deadline),
            "callback_closure_policy": {"snapshot": "closure_state.json", "fail_only_marker": "CALLBACK_FAILED",
                                        "final_check": "run_owned_lock", "immutable_payload": True,
                                        "unpersisted_failure": "PERMANENT_PUBLISH_DENIED",
                                        "publication_gate": "locked_live_and_persisted_state",
                                        "publication_transaction": "generation_checked_before_and_after_atomic_publish"},
            "callback_capture_policy": "EXACT_SIMPLE_NAMESPACE_PRIMITIVES_NO_GETTERS",
            "field_contract": CONTRACT, "sdk_calls": list(ReadOnlyGate.ORDER),
            "effect_policy": ["fixture_file_read", "fixture_import"], "real_execution_enabled": False}


def validate_manifest(journal, selection, deadline):
    manifest = journal.read("manifest.json")
    config = configuration(manifest["config"])
    started = validate_clock(manifest["run_timing"]["started"])
    if (type(manifest["run_timing"]["deadline_ns"]) is not int or manifest["run_timing"]["deadline_ns"] !=
            started + int(config["total_timeout_seconds"] * 1_000_000_000)):
        raise ContractViolation("Deadline binding conflict")
    expected = manifest_for(journal, config, selection, PrivateRedactor(b"validation-only"), deadline,
                            manifest["harness_source_commit"])
    expected["run_timing"] = manifest["run_timing"]
    # Tokens are deliberately not reversible/persistently keyed. Verify their
    # shape and independently recheck every explicit file's identity/hash.
    expected_inventory = expected["sdk_selection"]
    recorded = manifest["sdk_selection"]
    if set(recorded) != {"files", "roots"}:
        raise ContractViolation("Invalid selection inventory")
    for group in ("files", "roots"):
        if set(recorded[group]) != set(expected_inventory[group]):
            raise ContractViolation("Selection inventory incomplete")
        for role, value in recorded[group].items():
            if not is_token(value["path_token"]):
                raise ContractViolation("Unredacted selection path")
            expected_inventory[group][role]["path_token"] = value["path_token"]
    if recorded != expected_inventory:
        raise ContractViolation("Selected file identity/hash conflict")
    expected["sdk_selection"] = recorded
    if manifest != expected:
        raise ContractViolation("Manifest/source/policy conflict")
    return manifest


class Journal:
    """Own fixed registry; never modifies the frozen Journal's constants."""

    def __init__(self, directory, deadline=None):
        self.directory, self.deadline = verify_directory(directory), deadline
        self.lock = threading.RLock()

    def path(self, name):
        if name not in FILES:
            raise ContractViolation("Unknown artifact")
        verify_directory(self.directory)
        path = self.directory / name
        if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_nlink != 1
                or getattr(path.lstat(), "st_file_attributes", 0) & 0x400)):
            raise ContractViolation("Unsafe artifact")
        return path

    def _write_bytes(self, name, payload, append=False):
        with self.lock:
            if self.path("COMPLETE").exists():
                raise ContractViolation("Evidence already sealed")
            path = self.path(name)
            if append and not path.exists():
                raise ContractViolation("Required stream missing")
            size = path.stat().st_size if path.exists() else 0
            if size + len(payload) > 8 * 1024 * 1024 or sum(p.stat().st_size for p in self.directory.iterdir()) + len(payload) > 64 * 1024 * 1024:
                raise ContractViolation("Evidence budget exceeded")
            with path.open("ab" if append else "xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())

    @bounded_io
    def initialize(self):
        for name in sorted(STREAMS):
            self._write_bytes(name, b"")

    @bounded_io
    def append(self, name, row):
        if name not in STREAMS:
            raise ContractViolation("Not a stream")
        self._write_bytes(name, (json.dumps({**row, "run_id": self.directory.name}, ensure_ascii=True, allow_nan=False) + "\n").encode(), True)

    @bounded_io
    def write(self, name, value):
        if name in STREAMS:
            raise ContractViolation("Stream must be appended")
        self._write_bytes(name, (json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n").encode())

    @bounded_io
    def write_callback_veto(self, row):
        # Only this fail-only artifact bypasses the normal publication seal.
        # Exclusive creation: no overwrite, no payload/hash mutation.
        with self.lock:
            path = self.path("CALLBACK_FAILED")
            if not path.exists():
                with path.open("xb") as stream:
                    stream.write((json.dumps(row, sort_keys=True, allow_nan=False) + "\n").encode())
                    stream.flush()
                    os.fsync(stream.fileno())

    @bounded_io
    def data(self, name):
        path = self.path(name)
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ContractViolation("Artifact too large")
        return path.read_bytes()

    def text(self, name):
        return self.data(name).decode("utf-8")

    def read(self, name):
        return strict_json(self.text(name))

    def rows(self, name):
        text = self.text(name)
        if text and not text.endswith("\n"):
            raise ContractViolation("Truncated JSONL")
        rows = [strict_json(line) for line in text.splitlines()]
        for row in rows:
            validate_clock(row, self.directory.name)
        return rows

    @bounded_io
    def hashes(self):
        return {name: hashlib.sha256(self.data(name)).hexdigest() for name in sorted(PAYLOAD)}

    @bounded_io
    def complete(self, marker):
        with _CLOSURE_REGISTRY_LOCK:
            live = _LIVE_CLOSURES.get(str(self.directory))
        if live is None:
            raise ContractViolation("Live publication authority missing")
        with live.lock:
            generation, verdict = live.publication_generation, live.publication_verdict
            def require_clean(phase):
                if type(generation) is not int or type(live.generation) is not int:
                    reason = "invalid_publication_generation"
                elif live.generation != generation or live.publication_generation != generation:
                    reason = "closure_generation_changed"
                elif live.publication_verified is not True:
                    reason = "publication_authority_revoked"
                else:
                    reason = live.publication_state_error(self)
                if reason is None and (verdict not in (("PASS", "PASS", "CANDIDATE"),
                        ("FAIL", "FAIL", "CANDIDATE"), ("NOT_COVERED", "NOT_COVERED", "CANDIDATE"))
                        or live.publication_verdict != verdict
                        or self.read("summary.candidate.json")["decision"]["status"] != verdict[0]):
                    reason = "staged_decision_conflict"
                if reason is not None:
                    live.publish_denied = True
                    live.publication_verified = False
                    # Still inside the publication lock. A failure before the
                    # rename could not revoke a marker that did not yet exist.
                    try:
                        if self.deadline is not None:
                            self.deadline.revoke()
                        elif self.path("COMPLETE").exists():
                            self.path("COMPLETE").rename(self.path("COMPLETE.tmp"))
                    finally:
                        raise PublicationConflict(phase, reason)
            require_clean("pre_rename_closure_conflict")
            self.write("COMPLETE.tmp", marker)
            require_clean("pre_rename_closure_conflict")
            source, target = self.path("COMPLETE.tmp"), self.path("COMPLETE")
            if os.name == "nt":
                os.rename(source, target)
            else:
                os.link(source, target)
                source.unlink()
            # Atomic publication itself may reenter before creating the marker.
            # Success is provisional until this lock-held postcondition passes.
            require_clean("post_rename_closure_conflict")


def raw_valid(raw):
    if type(raw) is not dict or not safe_type(raw["raw_type"]):
        raise ContractViolation("Invalid raw type")
    if raw["kind"] == "handle":
        if not is_token(raw["raw_type"], "type:") or raw["object_ref"] != "harness-owned":
            raise ContractViolation("Invalid real-profile handle")
    elif raw["kind"] == "none":
        if raw["raw_type"] != "builtins.NoneType" or raw["value"] is not None:
            raise ContractViolation("Invalid None")
    elif raw["kind"] == "scalar":
        if not (is_token(raw["value"]) or (type(raw["value"]) is int and raw["raw_type"] == "builtins.int")):
            raise ContractViolation("Invalid scalar")
    elif raw["kind"] in {"object", "collection"}:
        if type(raw["items"]) is not list or (raw["kind"] == "object" and len(raw["items"]) != 1):
            raise ContractViolation("Invalid items")
        if raw["kind"] == "collection" and raw["raw_type"] not in {"builtins.list", "builtins.tuple"}:
            raise ContractViolation("Invalid collection")
        for item in raw["items"]:
            validate_public(item)
    else:
        raise ContractViolation("Unknown raw shape")


def _channel(journal, worker, process):
    rows = journal.rows("channel_events.jsonl")
    counts = {"noise_bytes": 0, "stderr_bytes": 0, "invalid_packets": 0}
    reports, closed, failed, previous = 0, set(), False, process["started"]["monotonic_ns"]
    for index, row in enumerate(rows, 1):
        if (type(row["sequence"]) is not int or row["sequence"] != index
                or not previous <= row["monotonic_ns"] <= process["finished"]["monotonic_ns"]):
            raise ContractViolation("Channel sequence/time conflict")
        previous = row["monotonic_ns"]
        event = row["event"]
        if event == "report":
            if row["authenticated"] is not True or row["report_sha256"] != digest(worker) or "stdout" in closed:
                raise ContractViolation("Report binding conflict")
            reports += 1
        elif event == "closed":
            if row["stream"] not in {"stdout", "stderr"} or row["stream"] in closed:
                raise ContractViolation("Channel closure conflict")
            closed.add(row["stream"])
        elif event in counts:
            if type(row["count"]) is not int or row["count"] <= 0:
                raise ContractViolation("Channel count invalid")
            counts[event] += row["count"]
        elif event == "reader_error":
            failed = True
        else:
            raise ContractViolation("Unknown channel fact")
    ok = reports == 1 and closed == {"stdout", "stderr"} and not failed and not any(counts.values())
    if counts != process["output_counts"] or ok is not process["channel_ok"]:
        raise ContractViolation("Channel summary conflict")
    return ok


def lifecycle_consistency(steps, events, worker):
    """Independent harness contract; do not change the frozen validator."""
    attempted, returned = worker["stop_attempted"], worker["stop_returned"]
    if type(attempted) is not bool or type(returned) is not bool:
        return False
    stop = [row for row in steps if row["name"] == "stop"]
    boundaries = [row for row in events if row["event"] in {"stop_begin", "stop_end"}]
    if not attempted:
        return not returned and not stop and not boundaries
    if (len(stop) != 2 or [row["phase"] for row in stop] != ["begin", "end"]
            or [row["event"] for row in boundaries] != ["stop_begin", "stop_end"]):
        return False
    end = stop[1]
    if type(end["returned"]) is not bool:
        return False
    if returned is not (end["returned"] is True and end["error"] is None):
        return False
    # A capture failure may follow an SDK return; it is still not a completed
    # recorder call. For a normal call exception returned must be false.
    if end["error"] is not None:
        return (end.get("error_stage") in {"call", "capture"}
                and (end["error_stage"] != "call" or end["returned"] is False)
                and worker["cleanup_failure"] == end["error"])
    return end["returned"] is True and worker["cleanup_failure"] is None


def validate_closure_snapshot(journal):
    state = journal.read("closure_state.json")
    if (type(state) is not dict or set(state) != {"run_id", "worker_closed", "report_serialized", "failures", "unpersisted_failure", "publish_denied", "generation"}
            or state["run_id"] != journal.directory.name
            or type(state["generation"]) is not int or state["generation"] < 0
            or any(type(state[k]) is not bool for k in ("worker_closed", "report_serialized", "unpersisted_failure", "publish_denied"))
            or type(state["failures"]) is not list
            or any(type(reason) is not str or reason not in LATCH_REASONS for reason in state["failures"])):
        raise ContractViolation("Invalid closure state")
    return (state["worker_closed"] is True and state["report_serialized"] is True and state["failures"] == []
            and state["unpersisted_failure"] is False and state["publish_denied"] is False)


def judge(journal, selection, deadline):
    checks = dict.fromkeys(CHECKS, "FAIL")
    domains, failures, worker, error = {}, [], None, None
    try:
        worker = journal.read("worker_result.json")
        events = journal.rows("worker_events.jsonl")
        failures.extend({"kind": row["event"], "facts": row["facts"]} for row in events if row["event"] in FAILURE_EVENTS)
        manifest = validate_manifest(journal, selection, deadline)
        checks["manifest"] = checks["source_binding"] = "PASS"
        config = manifest["config"]
        process_rows = journal.rows("process_checks.jsonl")
        if len(process_rows) != 1:
            raise ContractViolation("Process facts missing")
        process = process_rows[0]
        validate_process_facts(process)
        checks["process_schema"] = "PASS"
        checks["worker_exit"] = "PASS" if (process["exited"] is True and process["exitcode"] == 0
            and process["forced_termination"] is False and process["cleanup_errors"] == []
            and process["controller_error"] is None and process["timeout"] is None and process["channel_ok"] is True) else "FAIL"
        if (type(worker) is not dict or worker["run_id"] != journal.directory.name
                or type(worker["pid"]) is not int or worker["pid"] != process["pid"] or worker["config"] != config
                or worker["process_model"] != "FAKE_PROCESS" or worker["execution_mode"] != MODE):
            raise ContractViolation("Worker identity/config conflict")
        validate_clock(worker["started"])
        validate_clock(worker["finished"])
        inventory = journal.read("sdk_inventory.json")
        if (inventory != worker["inventory"] or inventory["selection"] != manifest["sdk_selection"]
                or inventory["sdk_kind"] != "HARNESS_FIXTURE" or inventory["loading"] != "SIMULATED"
                or inventory["version"] != "HARNESS_FIXTURE_V01"
                or set(inventory["handle_types"]) != {"construct", "construct_account"}
                or any(not is_token(t, "type:") for t in inventory["handle_types"].values())):
            raise ContractViolation("SDK inventory conflict")
        checks["inventory"] = "PASS"
        bootstrap = journal.rows("bootstrap_events.jsonl")
        if (len(bootstrap) != 2 or [row["phase"] for row in bootstrap] != ["begin", "end"]
                or any(row["name"] != "load_fixture" for row in bootstrap)
                or bootstrap[0]["monotonic_ns"] > bootstrap[1]["monotonic_ns"]
                or bootstrap[1]["error"] is not None or bootstrap[1]["inventory_digest"] != digest(inventory)):
            raise ContractViolation("Bootstrap incomplete")
        elapsed = bootstrap[1]["elapsed_ms"]
        wall = (bootstrap[1]["monotonic_ns"] - bootstrap[0]["monotonic_ns"]) / 1_000_000
        if (type(elapsed) not in (int, float) or not math.isfinite(elapsed) or not 0 <= elapsed <= wall + 1
                or not safe_type(bootstrap[1]["raw_type"]) or bootstrap[1]["raw_return"] != "FIXTURE_LOADED"):
            raise ContractViolation("Invalid bootstrap return/duration")
        checks["bootstrap"] = "PASS" if max(elapsed, wall) <= config["step_timeout_seconds"] * 1000 else "FAIL"
        effects = journal.rows("import_effects.jsonl")
        checks["side_effects"] = "PASS" if effects and all(
            set(row) == {"run_id", "timestamp_utc", "timestamp_cn", "monotonic_ns", "effect", "origin"}
            and row["effect"] in manifest["effect_policy"] and row["origin"] == "FIXTURE_OBSERVATION" for row in effects) else "FAIL"
        steps = journal.rows("steps.jsonl")
        lifecycle_ok = lifecycle_consistency(steps, events, worker)
        checks["lifecycle_consistency"] = "PASS" if lifecycle_ok else "FAIL"
        if not lifecycle_ok:
            failures.append({"kind": "lifecycle_source_conflict"})
        checks["callback_closure"] = "PASS" if validate_closure_snapshot(journal) else "FAIL"
        if checks["callback_closure"] != "PASS":
            failures.append({"kind": "callback_closure_failure"})
        snapshots = journal.rows("snapshots.jsonl")
        callbacks = journal.rows("callbacks.jsonl")
        attempts = journal.rows("callback_attempts.jsonl")
        start, finish = manifest["run_timing"]["started"]["monotonic_ns"], process["finished"]["monotonic_ns"]
        if any(not start <= row["monotonic_ns"] <= finish for row in (*steps, *snapshots, *events, *callbacks, *attempts, *bootstrap, *effects)):
            raise ContractViolation("Fact outside run interval")
        ends, pending, previous, last = {}, None, 0, -1
        durations_ok = True
        for row in steps:
            name, mono = row["name"], row["monotonic_ns"]
            if name not in ReadOnlyGate.ORDER or mono < previous or type(row["step_id"]) is not int:
                raise ContractViolation("Invalid step")
            previous = mono
            if row["phase"] == "begin":
                index = ReadOnlyGate.ORDER.index(name)
                if pending is not None or index <= last:
                    raise ContractViolation("Step order conflict")
                pending, last = row, index
            elif row["phase"] == "end":
                if pending is None or (pending["name"], pending["step_id"]) != (name, row["step_id"]):
                    raise ContractViolation("Unpaired step")
                elapsed = row["elapsed_ms"]
                wall = (mono - pending["monotonic_ns"]) / 1_000_000
                if (type(elapsed) not in (int, float) or not math.isfinite(elapsed) or not 0 <= elapsed <= wall + 1
                        or type(row["returned"]) is not bool):
                    raise ContractViolation("Invalid duration/return")
                durations_ok &= max(elapsed, wall) <= config["step_timeout_seconds"] * 1000
                if row["error"] is None:
                    if row["returned"] is not True or row["raw_type"] != row["raw_return"]["raw_type"]:
                        raise ContractViolation("Invalid return fact")
                    raw_valid(row["raw_return"])
                ends[name], pending = row, None
            else:
                raise ContractViolation("Invalid step phase")
        if pending is not None:
            raise ContractViolation("Incomplete step")
        for name in ReadOnlyGate.ORDER:
            row = ends.get(name)
            ok = row is not None and row["error"] is None and row["returned"] is True
            if ok:
                raw = row["raw_return"]
                if name in {"connect", "subscribe"}:
                    ok = raw["kind"] == "scalar" and raw["raw_type"] == "builtins.int" and type(raw["value"]) is int and raw["value"] == 0
                elif name in inventory["handle_types"]:
                    ok = raw["kind"] == "handle" and raw["raw_type"] == inventory["handle_types"][name]
                elif name not in DOMAINS:
                    ok = raw["kind"] == "none"
            checks[name] = "PASS" if ok else "FAIL"
        tokens, seen = [], set()
        for snapshot in snapshots:
            domain = snapshot["domain"]
            name = next((key for key, value in DOMAINS.items() if value == domain), None)
            if name is None or domain in seen:
                raise ContractViolation("Unknown/duplicate snapshot")
            seen.add(domain)
            row = ends[name]
            if row["error"] is not None or snapshot["step_id"] != row["step_id"] or snapshot["raw"] != row["raw_return"]:
                raise ContractViolation("Snapshot/step conflict")
            raw_valid(snapshot["raw"])
            domains[domain] = normalize(domain, snapshot["raw"])
            checks[name] = domains[domain]["raw_query"]
            checks["schema." + domain] = domains[domain]["schema_coverage"]
            tokens.extend(item["public_fields"].get("account_id", {}).get("value") for item in snapshot["raw"].get("items", []))
        requested = [row["facts"] for row in events if row["event"] == "account_requested"]
        subscribed = [row["facts"] for row in events if row["event"] == "subscribe_binding"]
        expected = worker["expected_account_token"]
        bound = len(requested) == len(subscribed) == 1 and is_token(expected)
        if bound:
            bound &= requested[0] == {"account_token": expected, "source_valid": True}
            identities = (ends["construct_account"]["raw_return"]["identity"], subscribed[0]["identity"])
            for item in identities:
                validate_public(item)
                account = item["public_fields"]["account_id"]
                bound &= (not item["capture_errors"] and account["source_valid"] is True and account["value"] == expected
                          and account["raw_type"] == "builtins.str")
            bound &= all(token == expected for token in tokens)
        checks["account_binding"] = "PASS" if bound else "FAIL"
        try:
            stop_begin = validate_callback_timeline(steps, events, callbacks, attempts, worker)
        except Exception:
            checks["lifecycle_consistency"] = checks["source_consistency"] = "FAIL"
            if not any(row["kind"] == "lifecycle_source_conflict" for row in failures):
                failures.append({"kind": "lifecycle_source_conflict"})
            raise
        checks["callback_timeline"] = "PASS"
        captured, outcomes, previous = [], {"overflow": 0, "capture_failure": 0, "late": 0}, 0
        for index, row in enumerate(attempts, 1):
            if (type(row["sequence"]) is not int or row["sequence"] != index or row["monotonic_ns"] < previous
                    or row["event"] not in CALLBACK_EVENTS):
                raise ContractViolation("Callback sequence conflict")
            previous = row["monotonic_ns"]
            if row["outcome"] == "captured":
                if row["error"] is not None:
                    raise ContractViolation("Callback error conflict")
                validate_callback_raw(row["event"], row["raw"], expected)
                captured.append(row)
            elif row["outcome"] in outcomes:
                outcomes[row["outcome"]] += 1
            else:
                raise ContractViolation("Unknown callback outcome")
        if len(captured) != len(callbacks):
            raise ContractViolation("Callback missing")
        for callback, attempt in zip(callbacks, captured):
            if callback["event"] not in CALLBACK_EVENTS or type(callback["thread_id"]) is not int or callback["thread_id"] <= 0:
                raise ContractViolation("Invalid callback event/thread")
            validate_callback_raw(callback["event"], callback["raw"], expected)
            if (any(callback[k] != attempt[k] for k in ("sequence", "event", "raw", "phase"))
                    or callback["monotonic_ns"] > attempt["monotonic_ns"]):
                raise ContractViolation("Callback/attempt conflict")
            if callback["event"] in {"order_error", "cancel_error", "unexpected_order_async_response", "unexpected_cancel_async_response"}:
                failures.append({"kind": "callback_failure", "facts": callback})
        cb = worker["callbacks"]
        for key in ("observed_count", "overflow_count", "capture_failure_count", "late_count"):
            if type(cb[key]) is not int or cb[key] < 0:
                raise ContractViolation("Callback summary type conflict")
        closures = [row for row in events if row["event"] == "callback_closed"]
        capture_ok = (cb["closed"] is True and type(cb["diagnostics"]) is list and cb["diagnostics"] == []
                      and cb["observed_count"] == len(attempts) and len(closures) == 1
                      and type(closures[0]["facts"]["last_sequence"]) is int and closures[0]["facts"]["last_sequence"] == len(attempts)
                      and all(cb[name + "_count"] == value for name, value in outcomes.items()) and not any(outcomes.values()))
        checks["callback_capture"] = "PASS" if capture_ok else "FAIL"
        checks["no_disconnect"] = "FAIL" if any(row["event"] == "disconnected" and (
            stop_begin is None or row["monotonic_ns"] < stop_begin) for row in callbacks) else "PASS"
        consistent = True
        for kind in ("primary_failure", "cleanup_failure"):
            observed = [row["facts"] for row in events if row["event"] == kind]
            consistent &= observed == ([] if worker[kind] is None else [worker[kind]])
            if worker[kind] is not None:
                failures.append({"kind": kind, "facts": worker[kind]})
        if (type(worker["evidence_failure_count"]) is not int or type(worker["evidence_failures"]) is not list
                or worker["evidence_failure_count"] < 0):
            raise ContractViolation("Invalid evidence failure summary")
        if worker["evidence_failure"] is not None or worker["evidence_failure_count"] != 0 or worker["evidence_failures"]:
            failures.append({"kind": "evidence_failure", "facts": worker["evidence_failures"]})
        allowed_events = FAILURE_EVENTS | {"account_requested", "subscribe_binding", "stop_begin", "stop_end", "callback_closed"}
        if any(row["event"] not in allowed_events for row in events):
            raise ContractViolation("Unknown worker event")
        checks["source_consistency"] = "PASS" if consistent and lifecycle_ok and _channel(journal, worker, process) else "FAIL"
        health = journal.rows("client_health.jsonl")
        health_statuses, keys = [], set()
        client_tokens = set()
        for row in health:
            key = (row["phase"], row["source"], row["check"])
            if (key not in (*HEALTH_REQUIRED, POST_STOP_HEALTH) or key in keys or row["simulation"] is not True
                    or row["origin"] != ORIGINS[row["source"]] or row["status"] not in {"PASS", "FAIL", "NOT_COVERED"}
                    or not is_token(row["client_token"])):
                raise ContractViolation("Health source/identity conflict")
            if not start <= row["monotonic_ns"] <= manifest["run_timing"]["deadline_ns"]:
                raise ContractViolation("Health outside deadline")
            if row["phase"] == "pre" and row["monotonic_ns"] > worker["started"]["monotonic_ns"]:
                raise ContractViolation("Late pre-health")
            if row["phase"] in {"post", "post_stop"} and row["monotonic_ns"] < process["finished"]["monotonic_ns"]:
                raise ContractViolation("Early post-health")
            if row["phase"] == "during_lifecycle":
                expected_status = domains.get("account", {}).get("raw_query", "NOT_COVERED")
                account = next((snap for snap in snapshots if snap["domain"] == "account"), None)
                if (row["status"] != expected_status or account is None or row.get("basis") != "query_asset_snapshot"
                        or type(row.get("basis_step_id")) is not int or row["basis_step_id"] != account["step_id"]
                        or any(row[k] != account[k] for k in ("monotonic_ns", "timestamp_utc", "timestamp_cn"))):
                    raise ContractViolation("SDK health/raw query conflict")
            if row["phase"] == "post_stop" and (row["status"] != "NOT_COVERED" or row.get("basis") != "not_performed"):
                raise ContractViolation("Post-stop SDK health not observed")
            keys.add(key)
            client_tokens.add(row["client_token"])
            if key in HEALTH_REQUIRED:
                health_statuses.append(row["status"])
        if keys != set(HEALTH_REQUIRED) | {POST_STOP_HEALTH} or len(client_tokens) != 1:
            raise ContractViolation("Health incomplete")
        checks["health"] = aggregate(health_statuses)
        checks["deadline"] = "PASS" if (durations_ok and process["timeout"] is None and
            start <= process["started"]["monotonic_ns"] <= worker["started"]["monotonic_ns"] <= worker["finished"]["monotonic_ns"]
            <= finish < manifest["run_timing"]["deadline_ns"]) else "FAIL"
        checks["raw_evidence"] = "PASS"
    except Exception as exc:
        error = {"reason": "invalid_raw_evidence"}  # No exception formatting or unknown class disclosure.
        if type(worker) is dict:
            for name in ("primary_failure", "cleanup_failure", "evidence_failure"):
                if worker.get(name) is not None:
                    failures.append({"kind": name, "facts": worker[name], "origin": "worker_result"})
    checks["all_failure_sources"] = "FAIL" if failures else "PASS"
    return {"status": "FAIL" if failures else aggregate(list(checks.values())), "checks": checks,
            "domains": domains, "failures": failures, "error": error, "scope": SCOPE, "execution_mode": MODE,
            "POST_STOP_SDK": "NOT_COVERED",
            "REAL_SDK": "NOT_COVERED", "REAL_FULL_QMT": "NOT_COVERED", "EXECUTION": "OUT_OF_SCOPE"}


def aggregate(statuses):
    if not statuses or any(value not in {"PASS", "FAIL", "NOT_COVERED"} for value in statuses) or "FAIL" in statuses:
        return "FAIL"
    return "NOT_COVERED" if "NOT_COVERED" in statuses else "PASS"


def incomplete(reason):
    return {"status": "FAIL", "completion": "INCOMPLETE", "reason": reason, "scope": SCOPE,
            "execution_mode": MODE, "REAL_SDK": "NOT_COVERED", "REAL_FULL_QMT": "NOT_COVERED", "EXECUTION": "OUT_OF_SCOPE"}


def verify_package(journal, selection, deadline, staged=None):
    try:
        deadline.check()
        expected = PAYLOAD | {"hashes.json"} | ({"COMPLETE"} if staged is None else set())
        if journal.path("CALLBACK_FAILED").exists():
            expected |= {"CALLBACK_FAILED"}
        if {p.name for p in journal.directory.iterdir()} != expected:
            raise ContractViolation("Incomplete artifact inventory")
        for name in STREAMS:
            journal.rows(name)  # Parse/identity completeness, independent of a FAIL candidate.
        for name in PAYLOAD - STREAMS:
            if type(journal.read(name)) is not dict:
                raise ContractViolation("Required JSON object missing")
        hashes = journal.read("hashes.json")
        if set(hashes) != {"run_id", "algorithm", "files"} or hashes != {
                "run_id": journal.directory.name, "algorithm": "sha256", "files": journal.hashes()}:
            raise ContractViolation("Hash inventory conflict")
        marker = journal.read("COMPLETE") if staged is None else staged
        manifest = validate_manifest(journal, selection, deadline)
        if (set(marker) != {"schema_version", "run_id", "hashes_sha256", "run_timing", "verification_finished"}
                or marker["schema_version"] != VERSION or marker["run_id"] != journal.directory.name
                or marker["hashes_sha256"] != hashlib.sha256(journal.text("hashes.json").encode()).hexdigest()
                or marker["run_timing"] != manifest["run_timing"]):
            raise ContractViolation("Completion binding conflict")
        verified = validate_clock(marker["verification_finished"])
        if not manifest["run_timing"]["started"]["monotonic_ns"] <= verified < manifest["run_timing"]["deadline_ns"]:
            raise ContractViolation("Late completion")
        process = journal.rows("process_checks.jsonl")
        if len(process) != 1 or process[0]["exited"] is not True:
            raise ContractViolation("Worker exit unconfirmed")
        validate_process_facts(process[0])
        decision = judge(journal, selection, deadline)
        candidate = journal.read("summary.candidate.json")
        normalized = [{k: v for k, v in row.items() if k not in {"run_id", "timestamp_utc", "timestamp_cn", "monotonic_ns"}}
                      for row in journal.rows("normalized.jsonl")]
        if (candidate != {"publication": "CANDIDATE", "manifest_digest": digest(manifest), "decision": decision}
                or normalized != list(decision["domains"].values())):
            raise ContractViolation("Candidate/raw decision conflict")
        deadline.check()
        return closure_veto(journal, {**decision, "completion": "CANDIDATE" if staged is not None else "COMPLETE"})
    except Exception:
        return incomplete("package_validation_failed")


def consume(directory, selection, deadline=None):
    local = deadline or RunDeadline(clock(), 30.0)
    journal = Journal(directory, local)
    return verify_package(journal, selection, local)


def publish(journal, selection, deadline):
    try:
        deadline.check()
        decision = judge(journal, selection, deadline)
        process = journal.rows("process_checks.jsonl")
        if len(process) != 1 or process[0]["exited"] is not True:
            return incomplete("worker_exit_unconfirmed")
        for domain in decision["domains"].values():
            journal.append("normalized.jsonl", {**clock(), **domain})
        manifest = journal.read("manifest.json")
        journal.write("summary.candidate.json", {"publication": "CANDIDATE", "manifest_digest": digest(manifest), "decision": decision})
        journal.write("hashes.json", {"run_id": journal.directory.name, "algorithm": "sha256", "files": journal.hashes()})
        marker = {"schema_version": VERSION, "run_id": journal.directory.name,
                  "hashes_sha256": hashlib.sha256(journal.text("hashes.json").encode()).hexdigest(),
                  "run_timing": manifest["run_timing"], "verification_finished": clock()}
        staged = verify_package(journal, selection, deadline, staged=marker)
        with _CLOSURE_REGISTRY_LOCK:
            live = _LIVE_CLOSURES.get(str(journal.directory))
        if live is None:
            return incomplete("live_closure_missing")
        # The lock excludes other callback threads; complete() also checks for
        # same-thread reentry before and after the atomic publication operation.
        # Persisted business FAIL/NOT_COVERED packages remain valid packages;
        # a PASS candidate must also be staged PASS. A live closure failure
        # always denies publication, irrespective of the candidate's verdict.
        with live.lock:
            reason = live.publication_gate(journal, decision, staged)
            if reason is not None:
                return {**incomplete("closure_publish_denied"), "publication_failure": reason}
            marker["verification_finished"] = clock()
            deadline.check()
            journal.complete(marker)
        deadline.check()
        result = consume(journal.directory, selection, deadline)
        deadline.check()
        return result
    except PublicationConflict as exc:
        deadline.revoke()
        return {**incomplete("publication_conflict"), "publication_failure": exc.phase, "closure_failure": exc.reason}
    except Exception:
        with _CLOSURE_REGISTRY_LOCK:
            live = _LIVE_CLOSURES.get(str(journal.directory))
        if live is not None:
            with live.lock:
                live.publish_denied = True
        deadline.revoke()
        return incomplete("publication_failed")
