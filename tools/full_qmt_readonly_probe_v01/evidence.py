"""Standard-library-only evidence capture. Never imports an SDK."""

import hashlib
import builtins
import hmac
import inspect
import json
import math
import os
import queue
import re
import secrets
import stat
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


VERSION = "0.1.3"
PAYLOAD_FILES = frozenset({
    "manifest.json", "steps.jsonl", "snapshots.jsonl", "callbacks.jsonl",
    "worker_result.json", "worker_events.jsonl", "normalized.jsonl", "process_checks.jsonl",
    "summary.candidate.json", "callback_attempts.jsonl", "channel_events.jsonl",
})
FILES = PAYLOAD_FILES | {"hashes.json", "COMPLETE", "COMPLETE.tmp"}
STREAM_FILES = frozenset({"steps.jsonl", "snapshots.jsonl", "callbacks.jsonl",
                          "worker_events.jsonl", "normalized.jsonl", "process_checks.jsonl",
                          "callback_attempts.jsonl", "channel_events.jsonl"})
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
PUBLIC_FIELDS = frozenset({
    "account_id", "account_type", "cash", "frozen_cash", "market_value",
    "total_asset", "stock_code", "volume", "can_use_volume", "open_price",
    "avg_price", "order_id", "order_sysid", "order_type", "order_volume",
    "price", "traded_volume", "order_status", "order_time", "traded_id",
    "traded_price", "traded_amount", "traded_time", "status_msg",
    "strategy_name", "order_remark", "error_id", "error_msg", "status",
})
IDENTIFIERS = frozenset({"order_id", "order_sysid", "traded_id", "trade_id"})
NUMERIC_FIELDS = frozenset({
    "cash", "frozen_cash", "market_value", "total_asset", "volume", "can_use_volume",
    "open_price", "avg_price", "order_type", "order_volume", "price", "traded_volume",
    "order_status", "order_time", "traded_price", "traded_amount", "traded_time",
    "error_id", "status",
})
REVIEWED_TYPES = frozenset({
    "types.SimpleNamespace", "tools.full_qmt_readonly_probe_v01.tests.test_fake_sdk.FakeTrader",
    "tools.full_qmt_readonly_probe_v01.worker.WorkflowPreconditionError",
})


class RunDeadlineExceeded(TimeoutError):
    pass


class RunDeadline:
    """One monotonic budget, including publication and verification, never reset."""

    def __init__(self, started, seconds):
        self.started = started
        self.expires_ns = started["monotonic_ns"] + int(seconds * 1_000_000_000)
        self.journal = None
        self.timer = None
        self.stopped = threading.Event()

    @property
    def timing(self):
        return {"started": self.started, "deadline_ns": self.expires_ns}

    def remaining(self):
        return max(0, (self.expires_ns - time.monotonic_ns()) / 1_000_000_000)

    def revoke(self):
        # A late finalizer must not leave a consumable marker. Only our own run.
        if self.journal is not None:
            marker = self.journal.directory / "COMPLETE"
            pending = self.journal.directory / "COMPLETE.tmp"
            try:
                if marker.exists() and not pending.exists():
                    marker.rename(pending)
            except OSError:
                # Consumers also validate the timing receipt; OS failures are not success.
                pass

    def check(self):
        if time.monotonic_ns() >= self.expires_ns:
            self.revoke()
            raise RunDeadlineExceeded("Run deadline exceeded")

    def arm(self, journal):
        self.journal = journal
        def watch():
            # Windows clocks/timers can have different granularity: never revoke early.
            while not self.stopped.wait(self.remaining()):
                if self.remaining() == 0:
                    self.revoke()
                    return
        self.timer = threading.Thread(target=watch)
        self.timer.daemon = True
        self.timer.start()
        self.check()

    def close(self):
        self.stopped.set()


def bounded_io(method):
    def bounded(self, *args, **kwargs):
        if self.deadline is not None:
            self.deadline.check()
        result = method(self, *args, **kwargs)
        if self.deadline is not None:
            self.deadline.check()
        return result
    return bounded


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("Non-finite JSON constant")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant)


def valid_id(value):
    return ((type(value) is str and bool(value.strip()) and len(value) <= 256)
            or (type(value) is int and value > 0))


def clock():
    now = datetime.now(timezone.utc)
    return {
        "timestamp_utc": now.isoformat(timespec="milliseconds"),
        "timestamp_cn": now.astimezone(timezone(timedelta(hours=8))).isoformat(
            timespec="milliseconds"),
        "monotonic_ns": time.monotonic_ns(),
    }


def qualified_type(value, redactor=None):
    cls = type(value)
    name = f"{cls.__module__}.{cls.__qualname__}"
    if cls is type(None) or getattr(builtins, cls.__qualname__, None) is cls or name in REVIEWED_TYPES:
        return name
    if redactor is None:
        raise TypeError("Unknown type requires a redaction boundary")
    return "type:" + redactor.token(name)[9:]


def verify_directory(directory):
    path = Path(directory)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("Evidence root must be an existing absolute directory")
    for part in (path, *path.parents):
        info = part.lstat()
        if part.is_symlink() or getattr(info, "st_file_attributes", 0) & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ValueError("Evidence path cannot traverse a link or junction")
    resolved = path.resolve(strict=True)
    lowered = str(resolved).replace("/", "\\").lower()
    if (lowered == "d:\\positionx_data" or lowered.startswith("d:\\positionx_data\\")
            or any(p.lower() in {"userdata", "userdata_mini"} for p in resolved.parts)
            or (Path(__file__).resolve().parents[2] / "data") in (resolved, *resolved.parents)):
        raise ValueError("Production evidence destinations are prohibited")
    return resolved


def new_run_directory(approved_root):
    root = verify_directory(approved_root)
    run = root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_")
                  + secrets.token_hex(8))
    run.mkdir()
    return run


class Journal:
    """Only fixed, flat filenames in a prevalidated isolated run directory."""

    def __init__(self, directory, deadline=None):
        self.directory = verify_directory(directory)
        self.lock = threading.RLock()
        self.deadline = deadline

    def path(self, name):
        if name not in FILES:
            raise ValueError("Unapproved evidence filename")
        verify_directory(self.directory)
        path = self.directory / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError("Unsafe evidence file")
        if path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("Evidence file is a reparse point")
        if path.exists() and path.stat().st_nlink != 1:
            raise ValueError("Evidence hardlinks are prohibited")
        return path

    def _writable(self, additional):
        if self.path("COMPLETE").exists():
            raise ValueError("Completed evidence is sealed")
        total = sum(p.stat().st_size for p in self.directory.iterdir() if p.is_file())
        if total + additional > MAX_TOTAL_BYTES:
            raise ValueError("Evidence byte budget exceeded")

    @bounded_io
    def initialize_streams(self):
        for name in sorted(STREAM_FILES):
            self._writable(0)
            with self.path(name).open("x", encoding="utf-8") as stream:
                stream.flush()
                os.fsync(stream.fileno())

    @bounded_io
    def append(self, name, value):
        if name not in STREAM_FILES:
            raise ValueError("Not an evidence stream")
        value = {**value, "run_id": self.directory.name}
        payload = json.dumps(value, ensure_ascii=True, allow_nan=False)
        size = len(payload.encode("utf-8")) + 1
        self._writable(size)
        if self.path(name).stat().st_size + size > MAX_FILE_BYTES:
            raise ValueError("Evidence file budget exceeded")
        with self.lock, self.path(name).open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(payload + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    @bounded_io
    def write(self, name, value):
        payload = json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False)
        self._writable(len(payload.encode("utf-8")) + 1)
        if len(payload.encode("utf-8")) + 1 > MAX_FILE_BYTES:
            raise ValueError("Evidence file budget exceeded")
        with self.lock, self.path(name).open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    @bounded_io
    def text(self, name):
        path = self.path(name)
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("Evidence file budget exceeded")
        return path.read_text(encoding="utf-8")

    def read(self, name):
        return strict_json(self.text(name))

    @bounded_io
    def rows(self, name, running=False):
        path = self.path(name)
        if not path.exists():
            if running:
                return []
            raise ValueError("Required evidence stream missing")
        text = self.text(name)
        lines = text.splitlines(keepends=True)
        if running and lines and not lines[-1].endswith("\n"):
            lines.pop()
        elif lines and not lines[-1].endswith("\n"):
            raise ValueError("Unterminated evidence stream")
        return [strict_json(line) for line in lines]

    @bounded_io
    def hashes(self):
        return {name: self.digest(name) for name in sorted(PAYLOAD_FILES)}

    @bounded_io
    def digest(self, name):
        # Enforce the same size/link checks on reads and hashing.
        path = self.path(name)
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("Evidence file budget exceeded")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @bounded_io
    def complete(self, marker):
        self.write("COMPLETE.tmp", marker)
        source, target = self.path("COMPLETE.tmp"), self.path("COMPLETE")
        if os.name == "nt":
            os.rename(source, target)  # Windows: atomic, fails if target exists.
        else:
            os.link(source, target)  # Atomic no-replace publication on POSIX.
            source.unlink()


class Redactor:
    """Per-run correlation, no persisted key and no unfiltered error text."""

    def __init__(self):
        self.key = secrets.token_bytes(32)

    def token(self, value):
        cls = type(value)
        payload = (f"{cls.__module__}.{cls.__qualname__}:" + str(value)).encode("utf-8")
        return "redacted:" + hmac.new(self.key, payload, hashlib.sha256).hexdigest()

    def error(self, exc, **facts):
        try:
            message = str(exc)
        except BaseException:
            message = "unprintable_exception"
        name = qualified_type(exc, self)
        return {"error_type": name, "message_token": self.token(message), **facts}

    def value(self, name, value):
        if value is None:
            return value
        if name in NUMERIC_FIELDS and type(value) in (bool, int, float):
            if type(value) is float and not math.isfinite(value):
                raise ValueError("Non-finite public field")
            return value
        if (name == "stock_code" and type(value) is str
                and re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", value)):
            return value
        return self.token(value)


class Capture:
    def __init__(self, redactor, max_items=1000, max_fields=128):
        self.redactor = redactor
        self.max_items = max_items
        self.max_fields = max_fields

    def public(self, obj):
        fields, errors = {}, []
        names = [name for name in dir(obj) if not name.startswith("_")]
        if len(names) > self.max_fields:
            raise ValueError("Public field limit exceeded; capture incomplete")
        for name in names:
            label = name if name in PUBLIC_FIELDS else "redacted_field_" + self.redactor.token(name)[9:]
            try:
                descriptor = inspect.getattr_static(obj, name)
                if callable(descriptor):
                    continue
                # Do not execute unknown descriptors. Native accessors need review.
                if hasattr(descriptor, "__get__") and name not in PUBLIC_FIELDS:
                    raise TypeError("Unreviewed public descriptor")
                value = getattr(obj, name)
                if callable(value):
                    continue
                fields[label] = {"raw_type": qualified_type(value, self.redactor),
                                "value": self.redactor.value(name, value)}
                if name == "account_id" or name in IDENTIFIERS:
                    fields[label]["source_valid"] = valid_id(value)
            except Exception as exc:
                errors.append(self.redactor.error(exc, field=label))
        return {"raw_type": qualified_type(obj, self.redactor), "public_fields": fields,
                "capture_errors": errors}

    def returned(self, value, handle=False, status_return=False):
        result = {"raw_type": qualified_type(value, self.redactor)}
        if handle:
            if value is None or isinstance(value, (str, int, float, bool, list, tuple, dict, set)):
                raise TypeError("Invalid handle")
            return {**result, "kind": "handle", "object_ref": "probe-owned"}
        if value is None:
            return {**result, "kind": "none", "value": None}
        if type(value) in (int, bool, float, str):
            safe = value if status_return and type(value) is int else self.redactor.token(value)
            return {**result, "kind": "scalar", "value": safe}
        if type(value) in (list, tuple):
            if len(value) > self.max_items:
                raise ValueError("Row limit exceeded; no silent truncation")
            return {**result, "kind": "collection", "items": [self.public(v) for v in value]}
        return {**result, "kind": "object", "items": [self.public(value)]}


class CallbackBuffer:
    """Copy inside callback; drain at worker safe points. No SDK calls here."""

    def __init__(self, capture, journal, capacity=64):
        if type(capacity) is not int or capacity < 1:
            raise ValueError("Callback capacity must be positive")
        self.capture = capture
        self.journal = journal
        self.queue = queue.Queue(maxsize=capacity)
        self.lock = threading.Lock()
        self.sequence = 0
        self.overflow_count = 0
        self.capture_failure_count = 0
        self.diagnostics = []
        self.closed = False
        self.phase = "running"
        self.late_count = 0
        self.on_evidence_error = None
        self.on_late = None

    def stop_boundary(self, event):
        # Serialize the boundary with callback timestamps/phase sampling. Evidence
        # failure must remain visible without preventing the caller's SDK stop.
        if event not in {"stop_begin", "stop_end"}:
            raise ValueError("Unknown stop boundary")
        with self.lock:
            boundary = clock()
            if event == "stop_begin":
                self.phase = "stopping"
            try:
                self.journal.append("worker_events.jsonl", {**boundary, "event": event, "facts": {}})
            except BaseException as exc:
                if self.on_evidence_error is None:
                    raise
                self.on_evidence_error(exc)

    def emit(self, event, value=None):
        with self.lock:
            self.sequence += 1
            if self.closed:
                self.late_count += 1
                if self.on_late is not None:
                    # Independent of journal health and of the already sent facts packet.
                    self.on_late()
            raw, error, outcome = None, None, "captured"
            try:
                raw = self.capture.returned(value)
                items = raw.get("items", [])
                if any(item["capture_errors"] for item in items):
                    raise ValueError("Callback public field capture incomplete")
                row = {**clock(), "sequence": self.sequence, "event": event,
                       "thread_id": threading.get_ident(), "raw": raw, "phase": self.phase}
                if self.closed:
                    outcome = "late"
                    self.journal.append("worker_events.jsonl", {
                        **clock(), "event": "late_callback_failure", "facts": row,
                    })
                else:
                    self.queue.put_nowait(row)
            except queue.Full:
                self.overflow_count += 1
                outcome = "overflow"
            except Exception as exc:
                self.capture_failure_count += 1
                outcome = "capture_failure"
                error = self.capture.redactor.error(exc)
                if len(self.diagnostics) < 16:
                    self.diagnostics.append(self.capture.redactor.error(exc, event=event))
                if self.on_evidence_error is not None:
                    self.on_evidence_error(exc)
            finally:
                try:
                    self.journal.append("callback_attempts.jsonl", {
                        **clock(), "sequence": self.sequence, "event": event, "phase": self.phase,
                        "outcome": outcome, "raw": raw, "error": error,
                    })
                except BaseException as exc:
                    if self.on_evidence_error is not None:
                        self.on_evidence_error(exc)

    def drain(self):
        while True:
            try:
                row = self.queue.get_nowait()
            except queue.Empty:
                return
            self.journal.append("callbacks.jsonl", row)

    def facts(self):
        with self.lock:
            return {"observed_count": self.sequence,
                    "overflow_count": self.overflow_count,
                    "capture_failure_count": self.capture_failure_count,
                    "late_count": self.late_count, "closed": self.closed,
                    "diagnostics": list(self.diagnostics)}

    def close(self):
        with self.lock:
            self.closed = True
            self.drain()
            self.journal.append("worker_events.jsonl", {
                **clock(), "event": "callback_closed", "facts": {"last_sequence": self.sequence},
            })
