"""Facts-only OS worker. No genuine SDK import path is enabled in V0.2 offline."""

import importlib.abc
import os
from pathlib import Path
import struct
import sys
import threading
import time
from types import SimpleNamespace

# -I -S disables site/.pth/usercustomize/PYTHONPATH before this entry executes.
# Only our explicit source directory is added, never site-packages or SDK roots.
sys.path.insert(0, str(Path(__file__).absolute().parent))
from profile import (CALLBACK_FAILURE_MARKER, CALLS, CONTRACT, SCENARIOS, encode, sha, token)


class SDKDenied(ValueError):
    pass


class CallbackClosure:
    """Sticky failure is independent of report bytes, including after encoding."""
    def __init__(self):
        self.lock = threading.RLock()
        self.closed = False
        self.failed = False
        self.signalled = False

    def fail(self):
        with self.lock:
            self.failed = True
            if not self.signalled:
                try:
                    if os.write(2, CALLBACK_FAILURE_MARKER) != len(CALLBACK_FAILURE_MARKER):
                        raise OSError()
                    self.signalled = True
                except BaseException:
                    # Unable to deliver the independent veto: this owned worker
                    # cannot exit successfully. No SDK/client/process-tree action.
                    os._exit(3)


class NoRealSDK(importlib.abc.MetaPathFinder):
    def __init__(self, failures):
        self.failures = failures

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "xtquant" or fullname.startswith("xtquant."):
            self.failures.append("REAL_SDK_IMPORT_DENIED")
            raise SDKDenied()
        return None


def load_sdk(mode, scenario, account, scope_errors):
    if mode != "FAKE_ONLY":
        scope_errors.append("REAL_EXECUTION_DISABLED")
        raise SDKDenied()
    return FakeSDK(scenario, account)


class FakeSDK:
    """Inert deterministic fixture, not a wrapper around any installed package."""
    def __init__(self, scenario, account):
        self.scenario, self.account, self.callback = scenario, account, None
        self._producer_lock = threading.Lock()
        self._producers = []
        self._callbacks_sealed = False

    def start_callback_producer(self, target):
        """Owned fake fixture producer; not an additional SDK operation."""
        with self._producer_lock:
            if self._callbacks_sealed:
                raise SDKDenied()
            thread = threading.Thread(target=target, daemon=True)
            self._producers.append(thread)
            thread.start()
            return thread

    def seal_and_drain_callbacks(self, deadline_ns):
        with self._producer_lock:
            self._callbacks_sealed = True
            producers = tuple(self._producers)
        for thread in producers:
            remaining = (deadline_ns - time.perf_counter_ns()) / 1e9
            if remaining <= 0:
                raise TimeoutError()
            thread.join(timeout=remaining)
            if thread.is_alive():
                raise TimeoutError()

    def invoke(self, name, callback):
        if name == "construct":
            if self.scenario == "hang":
                time.sleep(30)
            return None if self.scenario == "invalid_handle" else SimpleNamespace(fixture=True)
        if name == "register_callback":
            self.callback = callback
        if name == "connect":
            if self.scenario in ("connect_fail", "primary_cleanup"):
                return -1
            return 0
        if name == "construct_account":
            return SimpleNamespace(account_id="WRONG" if self.scenario == "wrong_account" else self.account)
        if name == "subscribe":
            return -1 if self.scenario == "subscribe_fail" else 0
        if name.startswith("query_"):
            if self.scenario == "query_error":
                raise RuntimeError()
            if self.scenario == "query_none":
                return None
            domain = name.removeprefix("query_")
            if self.scenario == "query_empty" and domain != "asset":
                return []
            if self.scenario == "query_wrong_type":
                return False
            fields = {n: self.account if n == "account_id" else "600000.SH" if rule == "symbol"
                      else "ID001" if rule == "id" else 1 if rule in ("integer", "quantity") else 1.0
                      for n, rule in CONTRACT[domain].items()}
            if self.scenario == "query_missing":
                fields.pop(next(iter(fields)))
            if self.scenario == "query_wrong_field":
                fields["account_id"] = ""
            obj = SimpleNamespace(**fields)
            return obj if domain == "asset" else [obj]
        if name == "bounded_callback_observation":
            if self.scenario == "callback_property":
                class Dangerous:
                    @property
                    def account_id(inner):
                        print(self.account)
                        raise RuntimeError()
                callback("account_status", Dangerous())
            else:
                count = 34 if self.scenario == "callback_overflow" else 1
                for _ in range(count):
                    callback("error" if self.scenario == "callback_error" else "account_status",
                             SimpleNamespace(account_id=self.account, status=1))
            if self.scenario in ("stdout", "stderr", "native_output"):
                target = 2 if self.scenario == "stderr" else 1
                os.write(target, (self.account + " D:\\sensitive\\userdata\n").encode())
        if name == "stop":
            if self.scenario in ("stop_error", "primary_cleanup"):
                raise RuntimeError()
            callback("disconnected", None)
        return None


class Dispatcher:
    def __init__(self, sdk, failures):
        self.sdk, self.failures, self.index = sdk, failures, 0
        self.owner = threading.get_ident()
        self.stopped = False

    def call(self, name, callback):
        allowed = (threading.get_ident() == self.owner and not self.stopped
                   and name in CALLS and (name == CALLS[self.index] or name == "stop"))
        if not allowed:
            self.failures.append("SDK_SCOPE_VIOLATION")
            raise SDKDenied()
        if name == "stop":
            self.stopped = True
        else:
            self.index += 1
        return self.sdk.invoke(name, callback)


def capture(value, contract, key):
    """Only exact plain fixture storage; no arbitrary getattr/dir/formatting."""
    if type(value) is not SimpleNamespace:
        return {"fields": {}, "capture_errors": ["UNREVIEWED_OBJECT"]}
    fields = object.__getattribute__(value, "__dict__").copy()
    result, errors = {}, []
    for name, raw in fields.items():
        if type(name) is not str:
            errors.append("INVALID_FIELD_NAME")
            continue
        rule = contract.get(name)
        safe_name = name if rule else token(key, name)
        if rule == "id":
            valid = (type(raw) is str and bool(raw.strip()) and len(raw) <= 256) or (type(raw) is int and raw > 0)
            if not valid:
                errors.append("INVALID_ID")
            result[safe_name] = token(key, raw)
        elif rule in ("integer", "quantity") and type(raw) is int:
            result[safe_name] = raw
        elif rule == "number" and type(raw) in (int, float):
            # Nonfinite numbers never enter JSON or silently become valid.
            import math
            if math.isfinite(raw):
                result[safe_name] = raw
            else:
                result[safe_name] = token(key, "nonfinite")
                errors.append("INVALID_NUMBER")
        elif rule == "symbol" and type(raw) is str:
            import re
            if re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", raw):
                result[safe_name] = raw
            else:
                result[safe_name] = token(key, raw)
                errors.append("INVALID_SYMBOL")
        else:
            result[safe_name] = token(key, raw)
            if rule is not None or type(raw) not in (str, int, bool, float, type(None)):
                errors.append("UNREVIEWED_FIELD_VALUE")
    return {"fields": result, "capture_errors": errors}


def snapshot(domain, value, key):
    if value is None:
        return {"kind": "none", "items": []}
    if domain == "asset":
        return {"kind": "object", "items": [capture(value, CONTRACT[domain], key)]}
    if type(value) is not list:
        return {"kind": "invalid", "items": []}
    return {"kind": "collection", "items": [capture(v, CONTRACT[domain], key) for v in value]}


def perform(request):
    key = bytes.fromhex(request["key"])
    account = request["account"]
    facts = {"steps": [], "snapshots": {}, "callbacks": [], "callback_errors": [],
             "primary_error": None, "cleanup_error": None, "evidence_errors": [], "scope_errors": [],
             "effects": [{"kind": "fixture_only", "simulation": True}], "health": [], "identity": {},
             "callback_closed_ns": 0,
             "callback_closure": {"sealed": False, "producers_drained": False}}
    guard = NoRealSDK(facts["scope_errors"])
    sys.meta_path.insert(0, guard)
    closure = CallbackClosure()

    def callback(kind, payload):
        with closure.lock:
            at = time.perf_counter_ns()
            if closure.closed:
                closure.fail()
                facts["callback_errors"].append("LATE_CALLBACK")
            if kind == "error":
                closure.fail()
            if len(facts["callbacks"]) >= 32:
                closure.fail()
                facts["callback_errors"].append("CALLBACK_OVERFLOW")
                return
            try:
                captured = {"fields": {}, "capture_errors": []} if payload is None else capture(
                    payload, {"account_id": "id", "status": "integer"}, key)
                facts["callbacks"].append({"kind": kind, "at_ns": at, **captured})
                if captured["capture_errors"]:
                    closure.fail()
                    facts["callback_errors"].append("CALLBACK_CAPTURE_ERROR")
            except BaseException:
                closure.fail()
                facts["callback_errors"].append("CALLBACK_CAPTURE_ERROR")

    sdk, dispatch = None, None
    try:
        if request["scenario"] not in SCENARIOS or request["account_type"] != "STOCK":
            raise SDKDenied()
        sdk = load_sdk(request["mode"], request["scenario"], account, facts["scope_errors"])
        dispatch = Dispatcher(sdk, facts["scope_errors"])
        facts["identity"] = {"python_hash": sha(Path(sys.executable).read_bytes()),
            "python_version": list(sys.version_info[:3]), "pointer_bits": struct.calcsize("P") * 8,
            "account_token": token(key, account), "binding_ok": True, "sdk_loading": "FAKE_FIXTURE"}
        if request["scenario"] == "import_attempt":
            try:
                __import__("xtquant")
            except SDKDenied:
                pass  # Denial survives exception handling as an independent fact.
        if request["scenario"] == "forbidden":
            try:
                dispatch.call("submit", callback)
            except SDKDenied:
                pass
        if request["scenario"] == "unknown_effect":
            facts["effects"].append({"kind": "unknown", "simulation": True})
        for name in CALLS[:-1]:
            row = {"step": name, "begin_ns": time.perf_counter_ns(), "end_ns": 0, "return": None, "error": None}
            facts["steps"].append(row)
            try:
                if time.perf_counter_ns() >= request["deadline_ns"]:
                    raise TimeoutError()
                raw = dispatch.call(name, callback)
                if name == "construct":
                    row["return"] = type(raw) is SimpleNamespace
                    if row["return"] is not True:
                        raise SDKDenied()
                elif name == "construct_account":
                    bound = type(raw) is SimpleNamespace and object.__getattribute__(raw, "__dict__").get("account_id") == account
                    row["return"] = bound
                    facts["identity"]["binding_ok"] = bound
                    if not bound:
                        raise SDKDenied()
                elif name in ("connect", "subscribe"):
                    row["return"] = raw if type(raw) is int else token(key, raw)
                    if type(raw) is not int or raw != 0:
                        raise SDKDenied()
                elif name.startswith("query_"):
                    domain = name.removeprefix("query_")
                    facts["snapshots"][domain] = snapshot(domain, raw, key)
                elif name == "bounded_callback_observation":
                    time.sleep(request["observation_seconds"])
            except BaseException:
                row["error"] = "SDK_ERROR"
                facts["primary_error"] = {"step": name, "code": "SDK_ERROR"}
                break
            finally:
                row["end_ns"] = time.perf_counter_ns()
    except BaseException:
        if facts["primary_error"] is None:
            facts["primary_error"] = {"step": "construct", "code": "SDK_ERROR"}
    finally:
        if dispatch is not None:
            row = {"step": "stop", "begin_ns": time.perf_counter_ns(), "end_ns": 0, "return": None, "error": None}
            facts["steps"].append(row)
            try:
                dispatch.call("stop", callback)
            except BaseException:
                row["error"] = "SDK_ERROR"
                facts["cleanup_error"] = {"step": "stop", "code": "SDK_ERROR"}
            finally:
                row["end_ns"] = time.perf_counter_ns()
        with closure.lock:
            closure.closed = True
            facts["callback_closed_ns"] = time.perf_counter_ns()
            facts["callback_closure"]["sealed"] = True
        try:
            if sdk is not None:
                sdk.seal_and_drain_callbacks(request["deadline_ns"])
            facts["callback_closure"]["producers_drained"] = True
        except BaseException:
            closure.fail()
            facts["callback_errors"].append("PRODUCER_DRAIN_FAILURE")
        if request["scenario"] == "callback_late":
            callback("account_status", SimpleNamespace(account_id=account))
        facts["health"] = [{"check": check, "simulation": True, "result": "PASS"}
                           for check in ("pre/os", "post/os", "pre/manual", "post/manual")]
        if request["scenario"] == "health_missing":
            facts["health"].pop()
    return facts, callback, closure


def main():
    # No traceback or exception string is ever emitted by the worker.
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            return 2
        import json
        request = json.loads(raw)
        facts, callback, closure = perform(request)
        path = Path(request["directory"]) / "facts.json"
        try:
            with path.open("xb") as stream:
                stream.write(encode(facts))
        except BaseException:
            closure.fail()
            return 2
        if request["scenario"] == "callback_after_report":
            callback("account_status", SimpleNamespace(account_id=request["account"]))
        # The fixture above only injects an event. Every scenario shares the veto;
        # the OS marker survives even a subsequently forced zero exit code.
        return 3 if closure.failed else 0
    except BaseException:
        return 2


if __name__ == "__main__":
    sys.exit(main())
