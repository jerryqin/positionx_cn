"""V0.1.3 offline worker: facts only, no real SDK loader or verdicts."""

import base64
import contextlib
import hashlib
import hmac
import io
import os
import sys
import threading
import time

from .evidence import (CallbackBuffer, Capture, Journal, Redactor, RunDeadline,
                       clock, qualified_type, strict_json, valid_id)


REPORT_PREFIX = b"POSITIONX_FACTS_V013 "


def late_alarm():
    # A late callback must remain visible if either output descriptor is broken.
    for descriptor in (2, 1):
        try:
            if os.write(descriptor, b"PROBE_LATE_CALLBACK\n") > 0:
                return
        except OSError:
            pass
    os._exit(94)  # Only this offline worker; parent observes a nonzero exit.


class WorkflowPreconditionError(RuntimeError):
    pass


class OutputDiscard(io.TextIOBase):
    def __init__(self):
        self.characters = 0

    def write(self, text):
        self.characters += len(text)
        return len(text)

    def flush(self):
        pass


class Recorder:
    def __init__(self, journal, capture, callbacks, facts):
        self.journal, self.capture, self.callbacks, self.facts = journal, capture, callbacks, facts
        self.sequence = 0
        self.lock = threading.RLock()

    def evidence_error(self, exc, origin="write"):
        error = self.capture.redactor.error(exc, origin=origin)
        with self.lock:
            self.facts["evidence_failure_count"] += 1
            if len(self.facts["evidence_failures"]) < 16:
                self.facts["evidence_failures"].append(error)
            if self.facts["evidence_failure"] is None:
                self.facts["evidence_failure"] = error
        # Best effort only. Never replace the SDK error or recurse on journal failure.
        try:
            self.journal.append("worker_events.jsonl", {**clock(), "event": "evidence_failure", "facts": error})
        except BaseException:
            pass

    def append(self, name, row):
        try:
            self.journal.append(name, row)
        except BaseException as exc:
            self.evidence_error(exc, origin=name)

    def failure(self, kind, exc):
        error = self.capture.redactor.error(exc)
        if self.facts[kind] is None:
            self.facts[kind] = error
            self.append("worker_events.jsonl", {**clock(), "event": kind, "facts": error})

    def drain(self):
        try:
            self.callbacks.drain()
        except BaseException as exc:
            self.evidence_error(exc, origin="callbacks.jsonl")

    def call(self, name, operation, handle=False, expected_type=None, cleanup=False):
        self.sequence += 1
        step_id = self.sequence
        begin = clock()
        self.append("steps.jsonl", {**begin, "phase": "begin", "step_id": step_id, "name": name})
        start = time.monotonic_ns()
        returned, value, raw, error, error_stage = False, None, None, None, None
        try:
            if not cleanup and self.journal.deadline is not None:
                self.journal.deadline.check()
            value = operation()
        except BaseException as exc:
            elapsed = (time.monotonic_ns() - start) / 1_000_000
            error, error_stage = self.capture.redactor.error(exc), "call"
            self.failure("cleanup_failure" if cleanup else "primary_failure", exc)
            self.append("steps.jsonl", {
                **clock(), "phase": "end", "step_id": step_id, "name": name,
                "returned": False, "raw_type": None, "raw_return": None,
                "elapsed_ms": elapsed, "error": error, "error_stage": error_stage,
            })
            raise  # The original SDK exception, even if all evidence writes failed.
        elapsed = (time.monotonic_ns() - start) / 1_000_000
        returned = True
        try:
            if handle and type(value) is not expected_type:
                raise TypeError("Unexpected handle type")
            raw = self.capture.returned(value, handle=handle, status_return=name in {"connect", "subscribe"})
            if name == "construct_account":
                raw["identity"] = self.capture.public(value)
        except BaseException as exc:
            error, error_stage = self.capture.redactor.error(exc), "capture"
            self.failure("cleanup_failure" if cleanup else "primary_failure", exc)
            self.append("steps.jsonl", {
                **clock(), "phase": "end", "step_id": step_id, "name": name,
                "returned": returned, "raw_type": qualified_type(value, self.capture.redactor), "raw_return": None,
                "elapsed_ms": elapsed, "error": error, "error_stage": error_stage,
            })
            raise
        self.append("steps.jsonl", {
            **clock(), "phase": "end", "step_id": step_id, "name": name,
            "returned": True, "raw_type": raw["raw_type"], "raw_return": raw,
            "elapsed_ms": elapsed, "error": None, "error_stage": None,
        })
        self.drain()
        return value, raw, step_id


def build_callback(base, buffer):
    class Callback(base):
        def on_connected(self):
            buffer.emit("connected")

        def on_disconnected(self):
            buffer.emit("disconnected")

        def on_account_status(self, value):
            buffer.emit("account_status", value)

        def on_stock_asset(self, value):
            buffer.emit("asset", value)

        def on_stock_position(self, value):
            buffer.emit("position", value)

        def on_stock_order(self, value):
            buffer.emit("order", value)

        def on_stock_trade(self, value):
            buffer.emit("trade", value)

        def on_order_error(self, value):
            buffer.emit("order_error", value)

        def on_cancel_error(self, value):
            buffer.emit("cancel_error", value)

        def on_order_stock_async_response(self, value):
            buffer.emit("unexpected_order_async_response", value)

        def on_cancel_order_stock_async_response(self, value):
            buffer.emit("unexpected_cancel_async_response", value)

    return Callback()


def run_worker(sdk, directory, config, late_notifier=None, run_timing=None):
    if getattr(sdk, "offline_fake", None) is not True:
        raise ValueError("Only the offline fake SDK is supported")
    deadline = None
    if run_timing is not None:
        deadline = RunDeadline(run_timing["started"], config["total_timeout_seconds"])
        if deadline.timing != run_timing:
            raise ValueError("Run deadline conflict")
    journal, redactor = Journal(directory, deadline), Redactor()
    capture = Capture(redactor)
    callbacks = CallbackBuffer(capture, journal, config["callback_capacity"])
    callbacks.on_late = late_notifier
    facts = {
        "run_id": journal.directory.name, "pid": os.getpid(), "started": clock(),
        "primary_failure": None, "cleanup_failure": None, "evidence_failure": None,
        "evidence_failures": [], "evidence_failure_count": 0,
        "stop_attempted": False, "stop_returned": False,
        "expected_account_token": redactor.token(sdk.fake_account_id), "config": dict(config),
        "run_timing": run_timing,
        "runtime": {
            "python_version": sys.version, "python_executable_token": redactor.token(sys.executable),
            "python_bits": 64 if sys.maxsize > 2**32 else 32,
            "sdk_kind": "offline_fake", "sdk_version": sdk.version,
            "sdk_import_path": None, "native_module_path": None, "native_module_sha256": None,
            "qmt_version": None, "userdata_path": "FAKE_USERDATA_NOT_A_FILESYSTEM_PATH",
            "session_id": config["session_id"],
        },
    }
    recorder = Recorder(journal, capture, callbacks, facts)
    callbacks.on_evidence_error = lambda exc: recorder.evidence_error(exc, origin="callback_capture")
    output = OutputDiscard()
    trader = None

    def construct():
        nonlocal trader
        trader = sdk.Trader("FAKE_USERDATA_NOT_A_FILESYSTEM_PATH", config["session_id"])
        return trader

    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        recorder.append("worker_events.jsonl", {**clock(), "event": "runtime", "facts": facts["runtime"]})
        recorder.append("worker_events.jsonl", {**clock(), "event": "account_requested", "facts": {
            "account_token": facts["expected_account_token"], "source_valid": valid_id(sdk.fake_account_id),
        }})
        try:
            recorder.call("construct", construct, handle=True, expected_type=sdk.TraderType)
            callback = build_callback(sdk.CallbackBase, callbacks)
            recorder.call("register", lambda: trader.register_callback(callback))
            recorder.call("start", trader.start)
            rc, _, _ = recorder.call("connect", trader.connect)
            if type(rc) is not int or rc != 0:
                raise WorkflowPreconditionError("connect did not return integer zero")
            account, _, _ = recorder.call("construct_account", lambda: sdk.Account(
                sdk.fake_account_id, "STOCK"), handle=True, expected_type=sdk.AccountType)
            def subscribe():
                identity = getattr(account, "account_id", None)
                if not valid_id(identity) or type(identity) is not type(sdk.fake_account_id) or identity != sdk.fake_account_id:
                    raise WorkflowPreconditionError("Constructed account identity conflict")
                recorder.append("worker_events.jsonl", {**clock(), "event": "subscribe_binding", "facts": {
                    "identity": capture.public(account),
                }})
                return trader.subscribe(account)
            rc, _, _ = recorder.call("subscribe", subscribe)
            if type(rc) is not int or rc != 0:
                raise WorkflowPreconditionError("subscribe did not return integer zero")
            for domain, operation in (
                ("account", trader.query_stock_asset), ("positions", trader.query_stock_positions),
                ("orders", trader.query_stock_orders), ("trades", trader.query_stock_trades),
            ):
                if facts["evidence_failure_count"]:
                    raise WorkflowPreconditionError("Evidence capture is incomplete")
                _, raw, step_id = recorder.call("query." + domain, lambda operation=operation: operation(account))
                recorder.append("snapshots.jsonl", {**clock(), "domain": domain, "step_id": step_id, "raw": raw})
            deadline = time.monotonic() + config["observation_seconds"]
            while time.monotonic() < deadline:
                if journal.deadline is not None:
                    journal.deadline.check()
                recorder.drain()
                time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        except BaseException as exc:
            recorder.failure("primary_failure", exc)
        finally:
            if type(trader) is sdk.TraderType:
                facts["stop_attempted"] = True
                callbacks.stop_boundary("stop_begin")
                try:
                    recorder.call("stop", trader.stop, cleanup=True)
                    facts["stop_returned"] = True
                except BaseException as exc:
                    recorder.failure("cleanup_failure", exc)
                finally:
                    callbacks.stop_boundary("stop_end")
            try:
                callbacks.close()
            except BaseException as exc:
                recorder.evidence_error(exc, origin="callback_close")
            facts["callbacks"] = callbacks.facts()
            facts["suppressed_output_characters"] = output.characters
            facts["finished"] = clock()
    return facts


def entrypoint():
    """-I -S child entry. No exception/traceback may escape to inherited output."""
    import json
    output = OutputDiscard()
    request, report = None, None
    original_stdout = sys.stdout.buffer
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        try:
            request = strict_json(sys.stdin.buffer.readline(65537).decode("utf-8"))
            from .tests.test_fake_sdk import FakeSDK
            report = run_worker(FakeSDK(request["scenario"]), os.getcwd(), request["config"],
                                late_notifier=late_alarm, run_timing=request["run_timing"])
        except BaseException as exc:
            report = {"bootstrap_failure": Redactor().error(exc)}
    if request is not None:
        try:
            payload = json.dumps(report, ensure_ascii=True, allow_nan=False).encode("utf-8")
            signature = hmac.new(bytes.fromhex(request["channel_key"]), payload, hashlib.sha256).hexdigest().encode("ascii")
            original_stdout.write(REPORT_PREFIX + base64.b64encode(payload) + b" " + signature + b"\n")
            original_stdout.flush()
        except BaseException:
            pass  # Parent treats absent or corrupt final facts as INCOMPLETE, never PASS.
