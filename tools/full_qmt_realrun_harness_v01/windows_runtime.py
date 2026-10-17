"""Offline process/channel/health models. No Win32 process control implementation."""

import hashlib
import contextlib
import hmac
import io
import json
import secrets
import weakref

from tools.full_qmt_readonly_probe_v01.evidence import clock, strict_json
from .sdk_loader import ContractViolation, RealSDKDisabled


_OWNED = weakref.WeakKeyDictionary()
HEALTH_REQUIRED = (("pre", "os", "process_identity"), ("pre", "human", "logged_in"),
                   ("during_lifecycle", "sdk", "account_readable"), ("post", "os", "process_identity"),
                   ("post", "human", "logged_in"))
POST_STOP_HEALTH = ("post_stop", "sdk", "account_readable")
ORIGINS = {"os": "FAKE_OS", "sdk": "FAKE_SDK", "human": "FAKE_HUMAN"}


class PrivatePipe:
    """Bounded one-shot private transport model; never an argv/env/file channel."""

    def __init__(self):
        self.__frame = None
        self.__used = False

    def send(self, value):
        if self.__used or self.__frame is not None:
            raise ContractViolation("Private channel already used")
        payload = json.dumps(value, ensure_ascii=True, allow_nan=False).encode("utf-8")
        if len(payload) > 1024 * 1024:
            raise ContractViolation("Private packet too large")
        self.__frame = payload

    def receive(self):
        if self.__frame is None or self.__used:
            raise ContractViolation("Private packet missing")
        payload, self.__frame, self.__used = self.__frame, None, True
        return strict_json(payload.decode("utf-8"))

    def __repr__(self):
        return "<PrivatePipe>"


class FixtureProcess:
    """Scripted process, no OS child or thread; PID and handle are simulated."""

    def __init__(self, behavior):
        self.behavior = behavior
        self.pid = 6001
        self.argv = ("HARNESS_FAKE_WORKER",)
        self.env = {}
        self.returncode = None
        self.packet = None
        self.terminations = 0
        self.__handle = object()

    def _owned_identity(self):
        return self.__handle

    def start(self, request_pipe, operation, key, after_worker=None, closure=None):
        if self not in _OWNED or _OWNED[self] is not self.__handle:
            raise ContractViolation("Unowned worker")
        if self.behavior in {"worker_timeout", "survivor"}:
            return
        if self.behavior == "worker_crash":
            self.returncode = 17
            return
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            report = operation(request_pipe.receive(), self.pid)
            if after_worker is not None:
                after_worker()
        payload = json.dumps(report, sort_keys=True, ensure_ascii=True, allow_nan=False).encode("utf-8")
        if closure is None:
            raise ContractViolation("Independent closure required")
        closure.serialized()
        self.packet = (payload, hmac.new(key, payload, hashlib.sha256).hexdigest())
        self.returncode = 0

    def poll(self):
        return self.returncode

    def _terminate_owned(self):
        self.terminations += 1
        if self.behavior != "survivor":
            self.returncode = -1


def create_fixture_process(behavior="normal", mode="fixture"):
    if mode != "fixture":
        raise RealSDKDisabled("Real process launch is not implemented")
    if behavior not in {"normal", "worker_timeout", "survivor", "worker_crash"}:
        raise ContractViolation("Unknown process fixture")
    process = FixtureProcess(behavior)
    _OWNED[process] = process._owned_identity()
    return process


def cleanup_owned(process):
    if type(process) is not FixtureProcess or _OWNED.get(process) is not process._owned_identity():
        raise ContractViolation("No owned fixture handle; never target an arbitrary PID")
    facts = {"exited": False, "exitcode": None, "forced_termination": False, "cleanup_errors": []}
    if process.poll() is None:
        facts["forced_termination"] = True
        process._terminate_owned()
    facts["exited"] = process.poll() is not None
    facts["exitcode"] = process.returncode if facts["exited"] else None
    if not facts["exited"]:
        facts["cleanup_errors"].append({"stage": "wait", "error_type": "builtins.TimeoutError"})
    return facts


def receive_report(process, key):
    if process.packet is None:
        return {"report_missing": True}, []
    payload, signature = process.packet
    if (len(payload) > 1024 * 1024 or not hmac.compare_digest(
            signature, hmac.new(key, payload, hashlib.sha256).hexdigest())):
        raise ContractViolation("Unauthenticated facts packet")
    report = strict_json(payload.decode("utf-8"))
    rows = [{**clock(), "sequence": 1, "event": "report", "authenticated": True,
             "report_sha256": hashlib.sha256(payload).hexdigest()}]
    rows.extend({**clock(), "sequence": i, "event": "closed", "stream": stream}
                for i, stream in enumerate(("stdout", "stderr"), 2))
    return report, rows


def fixture_health(phase, source, status, client_token):
    if (phase not in {"pre", "post", "during_lifecycle", "post_stop"} or source not in ORIGINS
            or status not in {"PASS", "FAIL", "NOT_COVERED"}
            or (source == "sdk") != (phase in {"during_lifecycle", "post_stop"})
            or (phase == "post_stop" and status != "NOT_COVERED")):
        raise ContractViolation("Invalid health fixture")
    check = {"os": "process_identity", "human": "logged_in", "sdk": "account_readable"}[source]
    return {**clock(), "phase": phase, "source": source, "check": check, "status": status,
            "origin": ORIGINS[source], "simulation": True, "client_token": client_token}


def channel_key():
    return secrets.token_bytes(32)
