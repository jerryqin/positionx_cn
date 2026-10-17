"""Harness fixture worker: facts only. No real import or connection entry point."""

import contextlib
import io
import time

from tools.full_qmt_readonly_probe_v01.evidence import RunDeadline, CallbackBuffer, clock, qualified_type, valid_id
from tools.full_qmt_readonly_probe_v01.worker import Recorder
from .real_profile import Journal, PrivateRedactor, RealCapture, CallbackCapture, CALLBACK_EVENTS, DOMAINS, MODE, digest
from .sdk_loader import load_fixture, ReadOnlyGate, ContractViolation, selection_inventory, deny_real_imports


class Discard(io.TextIOBase):
    def __init__(self):
        self.characters = 0

    def write(self, value):
        self.characters += len(value)
        return len(value)

    def flush(self):
        pass


def run_fixture_worker(request, pid, factory, closure):
    if request["mode"] != "fixture":
        raise ContractViolation("Real worker is disabled")
    config, timing = request["config"], request["run_timing"]
    deadline = RunDeadline(timing["started"], config["total_timeout_seconds"])
    if timing != deadline.timing:
        raise ContractViolation("Worker deadline conflict")
    journal = Journal(request["directory"], deadline)
    redactor = PrivateRedactor(bytes.fromhex(request["key"]))
    capture = RealCapture(redactor)
    callbacks = CallbackBuffer(CallbackCapture(redactor), journal, config["callback_capacity"])
    facts = {"run_id": journal.directory.name, "pid": pid, "config": config, "started": clock(),
             "execution_mode": MODE, "process_model": "FAKE_PROCESS", "primary_failure": None,
             "cleanup_failure": None, "evidence_failure": None, "evidence_failures": [], "evidence_failure_count": 0,
             "stop_attempted": False, "stop_returned": False,
             "expected_account_token": redactor.token(request["account_id"]), "inventory": {"unavailable": True}}
    recorder = Recorder(journal, capture, callbacks, facts)
    def callback_error(exc):
        closure.trip("callback_evidence_error")
        recorder.evidence_error(exc, origin="callback_capture")
    callbacks.on_evidence_error = callback_error
    callbacks.on_late = lambda: closure.trip("late_callback")
    gate = sdk = None
    output = Discard()
    def import_denied():
        recorder.append("worker_events.jsonl", {**clock(), "event": "real_sdk_import_denied",
                                               "facts": {"reason": "offline_real_sdk_guard"}})
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output), deny_real_imports(import_denied):
        bootstrap_start = clock()
        recorder.append("bootstrap_events.jsonl", {**bootstrap_start, "name": "load_fixture", "phase": "begin"})
        bootstrap_done = False
        try:
            if type(request["account_id"]) is not str or not valid_id(request["account_id"]):
                raise ContractViolation("Invalid private account")
            deadline.check()
            manifest = journal.read("manifest.json")
            selected = selection_inventory(request["selection"], redactor, deadline)
            if selected != manifest["sdk_selection"]:
                raise ContractViolation("Worker selection binding conflict")
            boundary = {"stdout_captured": True, "stderr_captured": True,
                        "evidence_ready": all(journal.path(name).exists() for name in (
                            "bootstrap_events.jsonl", "steps.jsonl", "worker_events.jsonl")), "deadline": deadline}
            sdk = load_fixture("fixture", request["selection"], boundary, factory)
            if sdk.version != "HARNESS_FIXTURE_V01":
                raise ContractViolation("Fixture version conflict")
            facts["inventory"] = {"selection": selected, "sdk_kind": "HARNESS_FIXTURE", "loading": "SIMULATED",
                                  "version": sdk.version, "handle_types": {
                                      "construct": "type:" + redactor.token(sdk.TraderType.__module__ + "." + sdk.TraderType.__qualname__)[9:],
                                      "construct_account": "type:" + redactor.token(sdk.AccountType.__module__ + "." + sdk.AccountType.__qualname__)[9:]}}
            recorder.append("bootstrap_events.jsonl", {**clock(), "name": "load_fixture", "phase": "end", "error": None,
                            "inventory_digest": digest(facts["inventory"]),
                            "elapsed_ms": (time.monotonic_ns() - bootstrap_start["monotonic_ns"]) / 1_000_000,
                            "raw_type": qualified_type(sdk, redactor), "raw_return": "FIXTURE_LOADED"})
            bootstrap_done = True
            for effect in sdk.effects:
                label = effect if type(effect) is str and effect in {
                    "fixture_import", "fixture_file_read", "network", "file_write", "userdata_write", "unknown"} else redactor.token(effect)
                recorder.append("import_effects.jsonl", {**clock(), "effect": label, "origin": "FIXTURE_OBSERVATION"})
            def violation(name):
                recorder.append("worker_events.jsonl", {**clock(), "event": "scope_violation", "facts": {
                    "command_token": redactor.token(name)}})
            gate = ReadOnlyGate(sdk, request["account_id"], config["session_id"], violation)

            class Callback:
                def emit(self, name, value=None):
                    # No global stream redirection on callback threads. Capture
                    # accepts reviewed passive storage and never invokes getters.
                    try:
                        if type(name) is not str or name not in CALLBACK_EVENTS:
                            raise ContractViolation("Unreviewed callback event")
                        with deny_real_imports(import_denied):
                            return gate.callback_call(lambda: callbacks.emit(name, value))
                    except BaseException as exc:
                        callback_error(exc)

                def on_connected(self):
                    self.emit("connected")

                def on_disconnected(self):
                    self.emit("disconnected")

                def on_account_status(self, value):
                    self.emit("account_status", value)

                def on_stock_asset(self, value):
                    self.emit("asset", value)

                def on_stock_position(self, value):
                    self.emit("position", value)

                def on_stock_order(self, value):
                    self.emit("order", value)

                def on_stock_trade(self, value):
                    self.emit("trade", value)

                def on_order_error(self, value):
                    self.emit("order_error", value)

                def on_cancel_error(self, value):
                    self.emit("cancel_error", value)

                def on_order_stock_async_response(self, value):
                    self.emit("unexpected_order_async_response", value)

                def on_cancel_order_stock_async_response(self, value):
                    self.emit("unexpected_cancel_async_response", value)

            gate.callback = Callback()
            def observe():
                until = time.monotonic() + config["observation_seconds"]
                while time.monotonic() < until:
                    deadline.check()
                    recorder.drain()
                    time.sleep(min(0.005, max(0, until - time.monotonic())))
                recorder.drain()
            gate.observe = observe
            recorder.append("worker_events.jsonl", {**clock(), "event": "account_requested", "facts": {
                "account_token": facts["expected_account_token"], "source_valid": True}})
            for name in ReadOnlyGate.ORDER[:-1]:
                expected_type = {"construct": sdk.TraderType, "construct_account": sdk.AccountType}.get(name)
                if name == "subscribe":
                    recorder.append("worker_events.jsonl", {**clock(), "event": "subscribe_binding", "facts": {
                        "identity": capture.public(gate.account)}})
                value, raw, step = recorder.call(name, lambda name=name: gate.invoke(name),
                                                handle=expected_type is not None, expected_type=expected_type)
                if name in {"connect", "subscribe"} and (type(value) is not int or value != 0):
                    raise ContractViolation("Connection/subscription did not return integer zero")
                if name in DOMAINS:
                    recorder.append("snapshots.jsonl", {**clock(), "domain": DOMAINS[name], "raw": raw, "step_id": step})
                if facts["evidence_failure_count"]:
                    raise ContractViolation("Evidence is incomplete")
        except BaseException as exc:
            recorder.failure("primary_failure", exc)
            if not bootstrap_done:
                recorder.append("bootstrap_events.jsonl", {**clock(), "name": "load_fixture", "phase": "end",
                                "error": redactor.error(exc), "inventory_digest": None})
        finally:
            if gate is not None and type(gate.trader) is sdk.TraderType:
                facts["stop_attempted"] = True
                callbacks.stop_boundary("stop_begin")
                try:
                    recorder.call("stop", lambda: gate.invoke("stop"), cleanup=True)
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
            closure.close_worker(facts["callbacks"])
            try:
                journal.write("sdk_inventory.json", facts["inventory"])
            except BaseException as exc:
                recorder.evidence_error(exc, origin="sdk_inventory")
            facts["suppressed_output_characters"] = output.characters
            facts["finished"] = clock()
    return facts
