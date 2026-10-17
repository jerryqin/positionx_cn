"""V0.1.3 supervisor: raw-fact verification under a single run deadline."""

import argparse
import base64
import builtins
import hashlib
import hmac
import json
import math
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from .evidence import (PAYLOAD_FILES, VERSION, PUBLIC_FIELDS, NUMERIC_FIELDS, REVIEWED_TYPES,
                       Journal, RunDeadline, RunDeadlineExceeded, Redactor, clock,
                       new_run_directory, qualified_type, strict_json)
from .normalize import CONTRACT, normalize, valid as valid_field
from .worker import REPORT_PREFIX


DEFAULT_CONFIG = {"session_id": 1019, "callback_capacity": 64, "observation_seconds": 0.02,
                  "step_timeout_seconds": 2.0, "total_timeout_seconds": 10.0}
SCENARIOS = frozenset({
    "normal", "connect_nonzero", "subscribe_nonzero", "query_none", "query_empty",
    "query_exception", "query_block", "stop_exception", "primary_and_stop_failure",
    "callback_overflow", "callback_capture_failure", "missing_field", "wrong_type",
    "connect_bool", "stop_block", "construct_exception", "start_exception",
    "unexpected_shape", "none_account", "disconnected", "no_callback", "primary_and_stop_block",
    "finite_return", "account_none", "invalid_account", "invalid_trader", "empty_id",
    "unknown_numeric", "stdout_leak", "native_output", "stop_callback", "late_callback", "worker_crash",
    "late_callback_write_failure", "late_channel_failure",
    "return_field", "sensitive_class", "wrong_account", "empty_account_identity",
    "callback_error",
})
LIFECYCLE = ("construct", "register", "start", "connect", "construct_account", "subscribe")
STEP_NAMES = (*LIFECYCLE, *["query." + domain for domain in CONTRACT], "stop")
CALLBACK_EVENTS = ("connected", "disconnected", "account_status", "asset", "position", "order", "trade",
                   "order_error", "cancel_error", "unexpected_order_async_response", "unexpected_cancel_async_response")
SOURCES = ("supervisor.py", "worker.py", "evidence.py", "normalize.py", "README.md", "tests/test_fake_sdk.py")
EXCLUDED = ["real_sdk_import", "broker_connection", "submit", "cancel", "qmt_client_inspection", "reconnect", "fallback"]
HANDLE_TYPES = {"construct": "tools.full_qmt_readonly_probe_v01.tests.test_fake_sdk.FakeTrader",
                "construct_account": "types.SimpleNamespace"}
REQUIRED_CHECKS = (*STEP_NAMES, "evidence_parse", "manifest", "all_failure_sources", "source_consistency",
                   "deadlines", "normal_cleanup", "callback_capture", "no_observed_disconnect", "account_binding",
                   "worker_exit", "process_schema", "callback_timeline",
                   *["schema." + domain for domain in CONTRACT])


def configuration(overrides=None):
    config = dict(DEFAULT_CONFIG)
    if overrides is not None:
        if type(overrides) is not dict or set(overrides) - set(config):
            raise ValueError("Invalid configuration fields")
        config.update(overrides)
    for key in ("session_id", "callback_capacity"):
        if type(config[key]) is not int or not 0 < config[key] <= 2_000_000_000:
            raise ValueError("Positive bounded integer required")
    if config["callback_capacity"] > 1024:
        raise ValueError("Callback capacity too large")
    for key in ("observation_seconds", "step_timeout_seconds", "total_timeout_seconds"):
        value = config[key]
        if (type(value) not in (int, float) or not math.isfinite(value) or value < 0
                or value > 600 or (key != "observation_seconds" and value == 0)):
            raise ValueError("Invalid timeout setting")
    return config


def aggregate(statuses):
    if any(status not in {"PASS", "FAIL", "NOT_COVERED"} for status in statuses) or "FAIL" in statuses:
        return "FAIL"
    return "NOT_COVERED" if not statuses or "NOT_COVERED" in statuses else "PASS"


def build_manifest(directory, config, scenario, run_timing=None):
    root = Path(__file__).resolve().parent
    return {"schema_version": VERSION, "scope": "OFFLINE_FAKE_ONLY", "run_id": directory.name,
            "config": config, "scenario": scenario, "run_timing": run_timing,
            "required_checks": list(REQUIRED_CHECKS),
            "required_files": sorted(PAYLOAD_FILES), "field_contract": CONTRACT, "excluded": EXCLUDED,
            "source_sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in SOURCES}}


def validate_manifest(journal, expected=None):
    manifest = journal.read("manifest.json")
    if type(manifest) is not dict:
        raise ValueError("Manifest must be an object")
    config = configuration(manifest["config"])
    if manifest["scenario"] not in SCENARIOS:
        raise ValueError("Unexpected scenario")
    timing = manifest["run_timing"]
    start = validate_clock(timing["started"])
    if (type(timing["deadline_ns"]) is not int or timing["deadline_ns"] !=
            start + int(config["total_timeout_seconds"] * 1_000_000_000)):
        raise ValueError("Run deadline binding conflict")
    canonical = build_manifest(journal.directory, config, manifest["scenario"], timing)
    if manifest != canonical or (expected is not None and manifest != expected):
        raise ValueError("Manifest conflict")
    return manifest


def validate_clock(row, run_id=None):
    if type(row) is not dict or (run_id is not None and row["run_id"] != run_id):
        raise ValueError("Invalid evidence identity")
    mono = row["monotonic_ns"]
    utc, cn = datetime.fromisoformat(row["timestamp_utc"]), datetime.fromisoformat(row["timestamp_cn"])
    if (type(mono) is not int or mono <= 0 or utc.utcoffset() is None or cn.utcoffset() is None
            or utc.utcoffset().total_seconds() != 0 or cn.utcoffset().total_seconds() != 28800 or utc != cn):
        raise ValueError("Invalid timestamp")
    return mono


def is_token(value, prefix="redacted:"):
    return (type(value) is str and value.startswith(prefix) and len(value) == len(prefix) + 64
            and all(c in "0123456789abcdef" for c in value[len(prefix):]))


def safe_type(name):
    return (type(name) is str and (name in REVIEWED_TYPES or is_token(name, "type:")
            or name in {"builtins." + item for item in
                        ("NoneType", "str", "int", "float", "bool", "list", "tuple", "dict", "set")}))


def safe_error_type(exc):
    # Do not format an unknown exception, even to discard its message afterward.
    return qualified_type(exc, Redactor())


def safe_error_identity(name):
    if is_token(name, "type:") or name == "tools.full_qmt_readonly_probe_v01.worker.WorkflowPreconditionError":
        return True
    if type(name) is not str or not name.startswith("builtins."):
        return False
    cls = getattr(builtins, name[len("builtins."):], None)
    return isinstance(cls, type) and issubclass(cls, BaseException)


def validate_process_facts(facts):
    required = {"started", "finished", "pid", "timeout", "controller_error", "exited", "exitcode",
                "forced_termination", "cleanup_errors", "channel_ok", "output_counts"}
    envelope = {"run_id", "timestamp_utc", "timestamp_cn", "monotonic_ns"}
    if type(facts) is not dict or set(facts) not in (required, required | envelope):
        raise ValueError("Invalid process facts fields")
    if envelope <= set(facts):
        validate_clock(facts)
        if type(facts["run_id"]) is not str or not facts["run_id"]:
            raise ValueError("Invalid process run identity")
    if validate_clock(facts["finished"]) < validate_clock(facts["started"]):
        raise ValueError("Invalid process interval")
    if facts["pid"] is not None and (type(facts["pid"]) is not int or facts["pid"] <= 0):
        raise ValueError("Invalid process PID")
    if any(type(facts[key]) is not bool for key in ("exited", "forced_termination", "channel_ok")):
        raise ValueError("Invalid process boolean")
    if ((facts["exited"] and (type(facts["exitcode"]) is not int or facts["pid"] is None))
            or (not facts["exited"] and facts["exitcode"] is not None)):
        raise ValueError("Invalid process exit facts")
    counts = facts["output_counts"]
    if (type(counts) is not dict or set(counts) != {"noise_bytes", "stderr_bytes", "invalid_packets"}
            or any(type(value) is not int or value < 0 for value in counts.values())):
        raise ValueError("Invalid output counts")
    errors = facts["cleanup_errors"]
    if type(errors) is not list:
        raise ValueError("Invalid cleanup errors")
    for error in errors:
        if (type(error) is not dict or set(error) != {"error_type", "stage"}
                or not safe_error_identity(error["error_type"]) or type(error["stage"]) is not str
                or error["stage"] not in {"terminate", "wait", "poll"}):
            # A failed launch has no observed exception type in cleanup itself.
            if error != {"stage": "launch"}:
                raise ValueError("Invalid cleanup error")
    error = facts["controller_error"]
    if error is not None and (type(error) is not dict or set(error) != {"error_type", "reason"}
                              or not safe_error_identity(error["error_type"]) or error["reason"] != "supervision_failed"):
        raise ValueError("Invalid controller error")
    timeout = facts["timeout"]
    if timeout is not None:
        clock_fields = {"timestamp_utc", "timestamp_cn", "monotonic_ns"}
        if (type(timeout) is not dict or type(timeout.get("kind")) is not str
                or timeout["kind"] not in {"total", "step"}):
            raise ValueError("Invalid timeout")
        fields = clock_fields | {"kind"} | ({"name", "returned"} if timeout["kind"] == "step" else set())
        if set(timeout) != fields:
            raise ValueError("Invalid timeout fields")
        validate_clock(timeout)
        if timeout["kind"] == "step" and (timeout["name"] not in STEP_NAMES or timeout["returned"] is not False):
            raise ValueError("Invalid step timeout")


def validate_callback_timeline(steps, events, callbacks, attempts, worker):
    begins = [row for row in events if row["event"] == "stop_begin"]
    ends = [row for row in events if row["event"] == "stop_end"]
    closures = [row for row in events if row["event"] == "callback_closed"]
    stop_steps = [row for row in steps if row["name"] == "stop"]
    if (type(worker["stop_attempted"]) is not bool or type(worker["stop_returned"]) is not bool
            or len(closures) != 1):
        raise ValueError("Invalid callback lifecycle")
    closed = closures[0]["monotonic_ns"]
    stop_begin = None
    if worker["stop_attempted"]:
        if (len(begins) != 1 or len(ends) != 1 or len(stop_steps) != 2
                or [row["phase"] for row in stop_steps] != ["begin", "end"]
                or begins[0]["facts"] != {} or ends[0]["facts"] != {}):
            raise ValueError("Missing stop boundaries")
        stop_begin, stop_end = begins[0]["monotonic_ns"], ends[0]["monotonic_ns"]
        if not (stop_begin <= stop_steps[0]["monotonic_ns"] <= stop_steps[1]["monotonic_ns"]
                <= stop_end <= closed <= worker["finished"]["monotonic_ns"]):
            raise ValueError("Stop boundary ordering conflict")
        if any(row["monotonic_ns"] > stop_begin for row in steps if row["name"] != "stop"):
            raise ValueError("Stop began before active lifecycle completed")
    elif begins or ends or stop_steps or worker["stop_returned"]:
        raise ValueError("Unexpected stop boundaries")
    for row in (*callbacks, *attempts):
        mono = row["monotonic_ns"]
        expected = "stopping" if stop_begin is not None and mono >= stop_begin else "running"
        if row["phase"] != expected or mono > closed:
            raise ValueError("Callback phase/timeline conflict")
    return stop_begin


def validate_public(item):
    if (not safe_type(item["raw_type"]) or type(item["public_fields"]) is not dict
            or type(item["capture_errors"]) is not list):
        raise ValueError("Invalid public object")
    primitives = {"builtins.str": str, "builtins.int": int, "builtins.float": float,
                  "builtins.bool": bool, "builtins.NoneType": type(None)}
    for name, field in item["public_fields"].items():
        if name not in PUBLIC_FIELDS and not is_token(name, "redacted_field_"):
            raise ValueError("Unreviewed field label")
        if type(field) is not dict or not safe_type(field["raw_type"]):
            raise ValueError("Invalid field")
        raw_type, value = field["raw_type"], field["value"]
        if value is not None and not is_token(value):
            if (raw_type not in primitives or type(value) is not primitives[raw_type]
                    or (name not in NUMERIC_FIELDS and name != "stock_code")):
                raise ValueError("Unredacted/invalid public value")
            if name == "stock_code":
                import re
                if type(value) is not str or re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", value) is None:
                    raise ValueError("Invalid symbol")
        if type(value) is float and not math.isfinite(value):
            raise ValueError("Nonfinite public field")
        if "source_valid" in field and type(field["source_valid"]) is not bool:
            raise ValueError("Invalid pre-redaction validity")


def validate_raw(raw):
    if type(raw) is not dict or not safe_type(raw["raw_type"]):
        raise ValueError("Invalid raw type")
    kind = raw["kind"]
    if kind == "none":
        if raw["raw_type"] != "builtins.NoneType" or raw["value"] is not None:
            raise ValueError("Invalid None record")
    elif kind == "handle":
        if raw["raw_type"] not in HANDLE_TYPES.values() or raw["object_ref"] != "probe-owned":
            raise ValueError("Invalid handle record")
    elif kind == "scalar":
        value = raw["value"]
        if type(value) not in (str, int, bool, float):
            raise ValueError("Invalid scalar")
        if type(value) is float and not math.isfinite(value):
            raise ValueError("Nonfinite scalar")
        if not is_token(value) and raw["raw_type"] != "builtins." + type(value).__name__:
            raise ValueError("Scalar type mismatch")
    elif kind in {"object", "collection"}:
        if type(raw["items"]) is not list or (kind == "object" and len(raw["items"]) != 1):
            raise ValueError("Invalid item container")
        if kind == "collection" and raw["raw_type"] not in {"builtins.list", "builtins.tuple"}:
            raise ValueError("Invalid collection type")
        for item in raw["items"]:
            validate_public(item)
    else:
        raise ValueError("Unknown raw shape")


def validate_callback_raw(event, raw, expected_account):
    validate_raw(raw)
    if any(item["capture_errors"] for item in raw.get("items", [])):
        raise ValueError("Raw callback capture failure")
    if event in {"connected", "disconnected"}:
        if raw["kind"] != "none":
            raise ValueError("Unexpected no-argument callback payload")
        return
    if raw["kind"] != "object":
        raise ValueError("Missing structured callback payload")
    fields = raw["items"][0]["public_fields"]
    account = fields.get("account_id", {})
    if (account.get("source_valid") is not True or account.get("value") != expected_account
            or account.get("raw_type") not in {"builtins.str", "builtins.int"}):
        raise ValueError("Invalid callback account binding")
    if event == "account_status":
        status = fields.get("status", {})
        if status.get("raw_type") != "builtins.int" or type(status.get("value")) is not int:
            raise ValueError("Missing/invalid account status")
    domain = {"asset": "account", "position": "positions", "order": "orders", "trade": "trades"}.get(event)
    if domain is not None:
        if any(name not in fields or not valid_field(fields[name], rule)
               for name, rule in CONTRACT[domain].items()):
            raise ValueError("Incomplete callback field contract")


def report_digest(report):
    return hashlib.sha256(json.dumps(report, sort_keys=True, ensure_ascii=True,
                                     allow_nan=False).encode("utf-8")).hexdigest()


def channel_facts(journal, worker, process_facts):
    rows = journal.rows("channel_events.jsonl")
    counts = {"noise_bytes": 0, "stderr_bytes": 0, "invalid_packets": 0}
    reports, closures, error = 0, set(), False
    previous_mono = process_facts["started"]["monotonic_ns"]
    for index, row in enumerate(rows, 1):
        mono = validate_clock(row, journal.directory.name)
        if not previous_mono <= mono <= process_facts["finished"]["monotonic_ns"]:
            raise ValueError("Channel timestamp conflict")
        previous_mono = mono
        if row["sequence"] != index or type(row["sequence"]) is not int:
            raise ValueError("Channel order conflict")
        event = row["event"]
        if event in counts:
            stream = "stderr" if event == "stderr_bytes" else "stdout"
            if stream in closures:
                raise ValueError("Channel observation after EOF")
            if type(row["count"]) is not int or row["count"] <= 0:
                raise ValueError("Invalid channel count")
            counts[event] += row["count"]
        elif event == "report":
            if (row["authenticated"] is not True or "stdout" in closures
                    or row["report_sha256"] != report_digest(worker)):
                raise ValueError("Unauthenticated report")
            reports += 1
        elif event == "closed":
            if row["stream"] not in {"stdout", "stderr"} or row["stream"] in closures:
                raise ValueError("Invalid channel closure")
            closures.add(row["stream"])
        elif event == "reader_error":
            if row["stream"] not in {"stdout", "stderr"} or row["stream"] in closures:
                raise ValueError("Invalid reader failure order")
            error = True
        else:
            raise ValueError("Unknown channel event")
    return counts, reports == 1 and len(closures) == 2 and not error and not any(counts.values())


def judge(journal, process_facts, expected_manifest=None):
    """Candidate only. Call consume_completed to obtain a consumable result."""
    checks = {name: "FAIL" for name in REQUIRED_CHECKS}
    domains, failures, callbacks = {}, [], []
    worker, evidence_error = None, None
    try:
        manifest = validate_manifest(journal, expected_manifest)
        checks["manifest"] = "PASS"
        config, run_id = manifest["config"], manifest["run_id"]
        worker = journal.read("worker_result.json")
        validate_process_facts(process_facts)
        checks["process_schema"] = "PASS"
        if (type(worker) is not dict or worker["run_id"] != run_id or worker["pid"] != process_facts["pid"]
                or worker["config"] != config or worker["runtime"]["sdk_kind"] != "offline_fake"
                or worker["run_timing"] != manifest["run_timing"]):
            raise ValueError("Worker identity/config conflict")
        validate_clock(worker["started"])
        validate_clock(worker["finished"])
        steps = journal.rows("steps.jsonl")
        snapshots = journal.rows("snapshots.jsonl")
        callbacks = journal.rows("callbacks.jsonl")
        events = journal.rows("worker_events.jsonl")
        attempts = journal.rows("callback_attempts.jsonl")
        for row in (*steps, *snapshots, *callbacks, *events, *attempts):
            validate_clock(row, run_id)
        run_start = validate_clock(manifest["run_timing"]["started"])
        if any(not run_start <= row["monotonic_ns"] <= process_facts["finished"]["monotonic_ns"]
               for row in (*steps, *snapshots, *callbacks, *events, *attempts)):
            raise ValueError("Evidence outside process/run interval")
        ends, pending, last_index, previous_mono = {}, None, -1, 0
        deadlines_ok = True
        for row in steps:
            mono = row["monotonic_ns"]
            if mono < previous_mono or type(row["step_id"]) is not int or row["name"] not in STEP_NAMES:
                raise ValueError("Step ordering/type violation")
            previous_mono = mono
            if row["phase"] == "begin":
                index = STEP_NAMES.index(row["name"])
                if pending is not None or index <= last_index:
                    raise ValueError("Step order violation")
                last_index, pending = index, row
            elif row["phase"] == "end":
                if pending is None or (row["step_id"], row["name"]) != (pending["step_id"], pending["name"]):
                    raise ValueError("Unpaired step")
                elapsed = row["elapsed_ms"]
                if (type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0
                        or type(row["returned"]) is not bool or "error" not in row):
                    raise ValueError("Invalid step result")
                wall_ms = (mono - pending["monotonic_ns"]) / 1_000_000
                if elapsed > wall_ms + 1:
                    raise ValueError("Contradictory durations")
                deadlines_ok &= max(wall_ms, elapsed) <= config["step_timeout_seconds"] * 1000
                if row["error"] is None:
                    if row["returned"] is not True or row["raw_type"] != row["raw_return"]["raw_type"]:
                        raise ValueError("Invalid successful return")
                    validate_raw(row["raw_return"])
                ends[row["name"]], pending = row, None
            else:
                raise ValueError("Unknown step phase")
        if pending is not None:
            raise ValueError("Incomplete step")
        for name in (*LIFECYCLE, "stop"):
            row = ends.get(name)
            ok = row is not None and row["error"] is None and row["returned"] is True
            if ok:
                raw = row["raw_return"]
                if name in {"connect", "subscribe"}:
                    ok = raw["raw_type"] == "builtins.int" and type(raw["value"]) is int and raw["value"] == 0
                elif name in HANDLE_TYPES:
                    ok = raw["kind"] == "handle" and raw["raw_type"] == HANDLE_TYPES[name]
                    if ok and name == "construct_account":
                        validate_public(raw["identity"])
                        ok = not raw["identity"]["capture_errors"]
                else:
                    ok = raw["kind"] == "none"
            checks[name] = "PASS" if ok else "FAIL"
        seen, tokens = set(), []
        for snapshot in snapshots:
            domain = snapshot["domain"]
            if domain not in CONTRACT or domain in seen:
                raise ValueError("Unexpected/duplicate domain")
            seen.add(domain)
            row = ends.get("query." + domain)
            if (row is None or row["error"] is not None or row["step_id"] != snapshot["step_id"]
                    or row["raw_return"] != snapshot["raw"]):
                raise ValueError("Snapshot conflict")
            validate_raw(snapshot["raw"])
            domains[domain] = normalize(domain, snapshot["raw"])
            checks["query." + domain] = domains[domain]["raw_query"]
            checks["schema." + domain] = domains[domain]["schema_coverage"]
            for item in snapshot["raw"].get("items", []):
                tokens.append(item["public_fields"].get("account_id", {}).get("value"))
        requested = [row["facts"] for row in events if row["event"] == "account_requested"]
        subscribed = [row["facts"]["identity"] for row in events if row["event"] == "subscribe_binding"]
        binding_ok = len(requested) == len(subscribed) == 1
        if binding_ok:
            expected = requested[0]["account_token"]
            binding_ok &= is_token(expected) and requested[0]["source_valid"] is True
            binding_ok &= worker["expected_account_token"] == expected
            constructed = ends["construct_account"]["raw_return"]["identity"]
            for identity in (constructed, subscribed[0]):
                validate_public(identity)
                field = identity["public_fields"].get("account_id", {})
                binding_ok &= (not identity["capture_errors"] and field.get("source_valid") is True
                               and field.get("raw_type") in {"builtins.str", "builtins.int"}
                               and field.get("value") == expected)
            binding_ok &= all(token == expected for token in tokens)
        checks["account_binding"] = ("FAIL" if not binding_ok else "PASS" if tokens else "NOT_COVERED")
        stop_begin = validate_callback_timeline(steps, events, callbacks, attempts, worker)
        checks["callback_timeline"] = "PASS"
        cb = worker["callbacks"]
        for key in ("observed_count", "overflow_count", "capture_failure_count", "late_count"):
            if type(cb[key]) is not int or cb[key] < 0:
                raise ValueError("Invalid callback counters")
        captured = []
        outcome_counts = {"overflow": 0, "capture_failure": 0, "late": 0}
        previous_mono = 0
        for index, attempt in enumerate(attempts, 1):
            if (type(attempt["sequence"]) is not int or attempt["sequence"] != index
                    or attempt["event"] not in CALLBACK_EVENTS or attempt["phase"] not in {"running", "stopping"}
                    or attempt["monotonic_ns"] < previous_mono):
                raise ValueError("Invalid callback attempt sequence")
            previous_mono = attempt["monotonic_ns"]
            if attempt["outcome"] == "captured":
                if attempt["error"] is not None:
                    raise ValueError("Captured callback has error")
                validate_callback_raw(attempt["event"], attempt["raw"], worker["expected_account_token"])
                captured.append(attempt)
            elif attempt["outcome"] in outcome_counts:
                outcome_counts[attempt["outcome"]] += 1
            else:
                raise ValueError("Unknown callback outcome")
        if len(captured) != len(callbacks):
            raise ValueError("Missing callback raw records")
        previous = 0
        for callback, attempt in zip(callbacks, captured):
            if (type(callback["sequence"]) is not int or callback["sequence"] <= previous
                    or type(callback["thread_id"]) is not int or callback["thread_id"] <= 0
                    or callback["event"] not in CALLBACK_EVENTS or callback["phase"] not in {"running", "stopping"}):
                raise ValueError("Invalid callback row")
            validate_callback_raw(callback["event"], callback["raw"], worker["expected_account_token"])
            if callback["event"] in {"order_error", "cancel_error", "unexpected_order_async_response",
                                     "unexpected_cancel_async_response"}:
                failures.append({"kind": "callback_failure", "evidence": callback})
            if any(callback[key] != attempt[key] for key in ("sequence", "event", "phase", "raw")):
                raise ValueError("Callback raw/attempt conflict")
            if callback["monotonic_ns"] > attempt["monotonic_ns"]:
                raise ValueError("Callback/attempt timestamp conflict")
            previous = callback["sequence"]
        closures = [row for row in events if row["event"] == "callback_closed"]
        raw_closed = (len(closures) == 1 and type(closures[0]["facts"]["last_sequence"]) is int
                      and closures[0]["facts"]["last_sequence"] == len(attempts)
                      and all(row["monotonic_ns"] <= closures[0]["monotonic_ns"] for row in attempts))
        summary_matches = (cb["observed_count"] == len(attempts)
                           and cb["overflow_count"] == outcome_counts["overflow"]
                           and cb["capture_failure_count"] == outcome_counts["capture_failure"]
                           and cb["late_count"] == outcome_counts["late"] and not cb["diagnostics"])
        checks["callback_capture"] = ("PASS" if raw_closed and summary_matches and cb["closed"] is True
                                      and not any(outcome_counts.values()) else "FAIL")
        checks["no_observed_disconnect"] = ("FAIL" if any(row["event"] == "disconnected"
                                                        and (stop_begin is None or row["monotonic_ns"] < stop_begin)
                                                        for row in callbacks) else "PASS")
        event_failures, consistent = [], True
        runtime_events = [row for row in events if row["event"] == "runtime"]
        consistent &= len(runtime_events) == 1 and runtime_events[0]["facts"] == worker["runtime"]
        for event in events:
            kind = event["event"]
            if kind not in {"runtime", "account_requested", "subscribe_binding", "callback_closed", "stop_begin", "stop_end",
                            "primary_failure", "cleanup_failure", "evidence_failure", "late_callback_failure"}:
                raise ValueError("Unknown worker event")
            if kind in {"primary_failure", "cleanup_failure", "evidence_failure", "late_callback_failure"}:
                event_failures.append({"kind": kind, "evidence": event["facts"]})
        for kind in ("primary_failure", "cleanup_failure"):
            value = worker[kind]
            matching = [row["facts"] for row in events if row["event"] == kind]
            consistent &= matching == ([] if value is None else [value])
            if value is not None:
                failures.append({"kind": kind, "evidence": value})
        if type(worker["evidence_failure_count"]) is not int or type(worker["evidence_failures"]) is not list:
            raise ValueError("Invalid evidence failures")
        if worker["evidence_failure"] is not None or worker["evidence_failure_count"] != 0 or worker["evidence_failures"]:
            failures.append({"kind": "evidence_failure", "evidence": worker["evidence_failures"]})
        failures.extend(event_failures)
        checks["source_consistency"] = "PASS" if consistent else "FAIL"
        checks["all_failure_sources"] = "FAIL" if failures else "PASS"
        checks["normal_cleanup"] = "PASS" if worker["stop_attempted"] is True and worker["stop_returned"] is True else "FAIL"
        counts, channel_ok = channel_facts(journal, worker, process_facts)
        if counts != process_facts["output_counts"] or channel_ok is not process_facts["channel_ok"]:
            raise ValueError("Channel facts/summary conflict")
        total_ns = validate_clock(process_facts["finished"]) - validate_clock(process_facts["started"])
        deadlines_ok &= 0 <= total_ns <= config["total_timeout_seconds"] * 1_000_000_000
        deadlines_ok &= worker["finished"]["monotonic_ns"] >= worker["started"]["monotonic_ns"]
        checks["deadlines"] = "PASS" if deadlines_ok and process_facts["timeout"] is None else "FAIL"
        checks["evidence_parse"] = "PASS"
    except (Exception,) as exc:
        evidence_error = {"error_type": safe_error_type(exc), "reason": "missing_invalid_or_conflicting_evidence"}
        checks["evidence_parse"] = "FAIL"
    # This gate is independent of whether any worker files parsed successfully.
    ok_exit = (checks["process_schema"] == "PASS" and process_facts["exited"] is True
               and process_facts["exitcode"] == 0 and process_facts["timeout"] is None
               and process_facts["forced_termination"] is False and process_facts["cleanup_errors"] == []
               and process_facts["controller_error"] is None and process_facts["channel_ok"] is True)
    checks["worker_exit"] = "PASS" if ok_exit else "FAIL"
    # Recover already persisted errors when a crash prevented the final report.
    if checks["evidence_parse"] == "FAIL":
        # Independent recovery: malformed steps/events must not hide intact final facts.
        if type(worker) is dict:
            for kind in ("primary_failure", "cleanup_failure"):
                if worker.get(kind) is not None:
                    failures.append({"kind": kind, "evidence": worker[kind], "origin": "worker_result"})
            if (worker.get("evidence_failure") is not None or worker.get("evidence_failures")
                    or worker.get("evidence_failure_count", 0)):
                failures.append({"kind": "evidence_failure", "evidence": worker.get("evidence_failures", []),
                                 "origin": "worker_result"})
        try:
            for row in journal.rows("worker_events.jsonl"):
                if row.get("event") in {"primary_failure", "cleanup_failure", "evidence_failure", "late_callback_failure"}:
                    failures.append({"kind": row["event"], "evidence": row["facts"]})
        except Exception:
            pass
    if checks["process_schema"] == "PASS" and process_facts["timeout"] is not None:
        failures.append({"kind": "supervisor_timeout", "evidence": process_facts["timeout"]})
        if process_facts["timeout"].get("name") == "stop":
            failures.append({"kind": "cleanup_failure", "evidence": {"origin": "supervisor", "reason": "stop_did_not_return"}})
    return {"scope": "OFFLINE_FAKE_ONLY", "status": aggregate(list(checks.values())), "checks": checks,
            "domains": domains, "failures": failures, "evidence_error": evidence_error,
            "callback_event_coverage": {event: "PASS" if any(type(row) is dict and row.get("event") == event
                                                             for row in callbacks) else "NOT_COVERED" for event in CALLBACK_EVENTS},
            "live_validation": "NOT_COVERED", "qmt_client_health": "NOT_COVERED",
            "implementation_gate": "NOT_COVERED", "submit": "NOT_COVERED", "cancel": "NOT_COVERED"}


def incomplete(reason, candidate=None):
    return {"scope": "OFFLINE_FAKE_ONLY", "status": "FAIL", "completion": "INCOMPLETE",
            "reason": reason, "candidate": candidate, "live_validation": "NOT_COVERED"}


def _verify_package(journal, expected_manifest=None, staging_marker=None):
    try:
        if journal.deadline is not None:
            journal.deadline.check()
        actual = {p.name for p in journal.directory.iterdir()}
        expected_files = PAYLOAD_FILES | {"hashes.json"} | ({"COMPLETE"} if staging_marker is None else set())
        if actual != expected_files:
            raise ValueError("Incomplete or extra artifact set")
        marker = journal.read("COMPLETE") if staging_marker is None else staging_marker
        hashes = journal.read("hashes.json")
        base = {key: marker[key] for key in ("schema_version", "run_id", "hashes_sha256")}
        if (base != {"schema_version": VERSION, "run_id": journal.directory.name,
                     "hashes_sha256": journal.digest("hashes.json")}
                or hashes["run_id"] != journal.directory.name or hashes["algorithm"] != "sha256"
                or set(hashes["files"]) != PAYLOAD_FILES or hashes["files"] != journal.hashes()):
            raise ValueError("Invalid completion/hash inventory")
        manifest = validate_manifest(journal, expected_manifest)
        if staging_marker is None:
            if set(marker) != set(base) | {"run_timing", "verification_finished"}:
                raise ValueError("Invalid completion receipt")
            verified = validate_clock(marker["verification_finished"])
            timing = manifest["run_timing"]
            if (marker["run_timing"] != timing or not
                    timing["started"]["monotonic_ns"] <= verified < timing["deadline_ns"]):
                raise ValueError("Late or conflicting completion receipt")
        candidate = journal.read("summary.candidate.json")
        process_rows = journal.rows("process_checks.jsonl")
        if len(process_rows) != 1:
            raise ValueError("Invalid process evidence")
        validate_process_facts(process_rows[0])
        if process_rows[0]["exited"] is not True:
            raise ValueError("Exit unconfirmed")
        validate_clock(process_rows[0], journal.directory.name)
        decision = judge(journal, process_rows[0], manifest)
        normalized = [{key: value for key, value in row.items() if key != "run_id"}
                      for row in journal.rows("normalized.jsonl")]
        if any(row["run_id"] != journal.directory.name for row in journal.rows("normalized.jsonl")):
            raise ValueError("Normalized evidence identity conflict")
        if (candidate["publication"] != "CANDIDATE" or candidate["manifest_sha256"] != journal.digest("manifest.json")
                or candidate["decision"] != decision or normalized != list(decision["domains"].values())):
            raise ValueError("Candidate decision mismatch")
        if journal.deadline is not None:
            journal.deadline.check()
        return {**decision, "process": process_rows[0],
                "completion": "COMPLETE" if staging_marker is None else "CANDIDATE"}
    except Exception:
        return incomplete("completion_validation_failed")


def consume_completed(journal, expected_manifest=None):
    # Reinspection gets its own bounded budget; it must not extend producer timing.
    prior = journal.deadline
    local = prior or RunDeadline(clock(), DEFAULT_CONFIG["total_timeout_seconds"])
    journal.deadline = local
    try:
        return _verify_package(journal, expected_manifest)
    finally:
        journal.deadline = prior


def publish(journal, decision, manifest):
    try:
        if journal.deadline is not None:
            journal.deadline.check()
        if journal.rows("process_checks.jsonl")[0]["exited"] is not True:
            return incomplete("worker_exit_unconfirmed", decision)
        for domain in decision["domains"].values():
            journal.append("normalized.jsonl", domain)
        journal.write("summary.candidate.json", {"publication": "CANDIDATE", "decision": decision,
                                               "manifest_sha256": journal.digest("manifest.json")})
        journal.write("hashes.json", {"run_id": journal.directory.name, "algorithm": "sha256", "files": journal.hashes()})
        marker = {"schema_version": VERSION, "run_id": journal.directory.name,
                  "hashes_sha256": journal.digest("hashes.json")}
        # Full consumer verification runs BEFORE the final atomic marker publication.
        verified = _verify_package(journal, manifest, staging_marker=marker)
        if verified["completion"] != "CANDIDATE":
            return incomplete("prepublication_verification_failed", decision)
        marker.update(run_timing=manifest["run_timing"], verification_finished=clock())
        if journal.deadline is not None:
            journal.deadline.check()
        journal.complete(marker)
        if journal.deadline is not None:
            journal.deadline.check()
        # Read back the actual published marker/package too. Staged verification
        # cannot prove that the final marker write was complete and uncorrupted.
        result = consume_completed(journal, manifest)
        if journal.deadline is not None:
            journal.deadline.check()
        return result
    except Exception:
        return incomplete("publication_failed", decision)


def cleanup_worker(process, wait_seconds=2.0):
    facts = {"forced_termination": False, "exited": False, "exitcode": None, "cleanup_errors": []}
    try:
        if process.poll() is None:
            facts["forced_termination"] = True
            try:
                process.terminate()  # One operation; Windows kill is the same operation.
            except Exception as exc:
                facts["cleanup_errors"].append({"error_type": safe_error_type(exc), "stage": "terminate"})
        try:
            process.wait(timeout=wait_seconds)
        except Exception as exc:
            facts["cleanup_errors"].append({"error_type": safe_error_type(exc), "stage": "wait"})
        facts["exited"] = process.poll() is not None
        facts["exitcode"] = process.returncode if facts["exited"] else None
    except Exception as exc:
        facts["cleanup_errors"].append({"error_type": safe_error_type(exc), "stage": "poll"})
    return facts


def _channel_event(channel, event, **facts):
    with channel["lock"]:
        if len(channel["events"]) >= 4096:
            channel["reader_error"] = True
            return
        channel["events"].append({**clock(), "sequence": len(channel["events"]) + 1,
                                  "event": event, **facts})


def _read_channel(stream, stdout, channel, key):
    pending = b""
    try:
        while True:
            chunk = stream.read1(4096)
            if not chunk:
                break
            if not stdout:
                channel["stderr_bytes"] += len(chunk)
                _channel_event(channel, "stderr_bytes", count=len(chunk))
                continue  # Never forward or persist untrusted bytes.
            pending += chunk
            if len(pending) > 1024 * 1024:
                channel["noise_bytes"] += len(pending)
                _channel_event(channel, "noise_bytes", count=len(pending))
                pending = b""
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                if not line.startswith(REPORT_PREFIX):
                    channel["noise_bytes"] += len(line) + 1
                    _channel_event(channel, "noise_bytes", count=len(line) + 1)
                    continue
                try:
                    encoded, signature = line[len(REPORT_PREFIX):].rsplit(b" ", 1)
                    payload = base64.b64decode(encoded, validate=True)
                    if not hmac.compare_digest(signature, hmac.new(key, payload, hashlib.sha256).hexdigest().encode("ascii")):
                        raise ValueError("Invalid facts signature")
                    if channel["report"] is not None:
                        raise ValueError("Duplicate facts report")
                    channel["report"] = strict_json(payload.decode("utf-8"))
                    _channel_event(channel, "report", authenticated=True, report_sha256=report_digest(channel["report"]))
                except Exception:
                    channel["invalid_packets"] += 1
                    _channel_event(channel, "invalid_packets", count=1)
        if pending:
            channel["noise_bytes"] += len(pending)
            _channel_event(channel, "noise_bytes", count=len(pending))
    except Exception:
        channel["reader_error"] = True
        _channel_event(channel, "reader_error", stream="stdout" if stdout else "stderr")
    finally:
        _channel_event(channel, "closed", stream="stdout" if stdout else "stderr")


def run_offline_probe(approved_evidence_root, scenario="normal", overrides=None):
    started = clock()  # Earliest entry boundary, before config/path/source/file work.
    if scenario not in SCENARIOS:
        raise ValueError("Only fixed offline fake scenarios are accepted")
    config = configuration(overrides)
    deadline = RunDeadline(started, config["total_timeout_seconds"])
    directory = None
    try:
        result = _run_offline_probe(approved_evidence_root, scenario, config, deadline)
        deadline.check()
        return result
    except RunDeadlineExceeded:
        deadline.revoke()
        directory = deadline.journal.directory if deadline.journal is not None else None
        return directory, incomplete("run_deadline_exceeded")
    finally:
        deadline.close()


def _run_offline_probe(approved_evidence_root, scenario, config, deadline):
    deadline.check()
    directory = new_run_directory(approved_evidence_root)
    journal = Journal(directory, deadline)
    deadline.arm(journal)
    manifest = build_manifest(directory, config, scenario, deadline.timing)
    deadline.check()
    journal.write("manifest.json", manifest)
    journal.initialize_streams()
    key = secrets.token_bytes(32)
    channel = {"report": None, "noise_bytes": 0, "stderr_bytes": 0, "invalid_packets": 0,
               "reader_error": False, "events": [], "lock": threading.Lock()}
    process, readers = None, []
    process_facts = {"started": clock(), "pid": None, "timeout": None, "controller_error": None}
    try:
        bootstrap = ("import sys; sys.path.insert(0, sys.argv[1]); "
                     "from tools.full_qmt_readonly_probe_v01.worker import entrypoint; entrypoint()")
        # Windows venv python.exe may be a redirector with a different child PID.
        # Use the same interpreter's base executable directly for this stdlib-only worker.
        interpreter = Path(getattr(sys, "_base_executable", sys.executable)).resolve(strict=True)
        process = subprocess.Popen([str(interpreter), "-I", "-S", "-B", "-c", bootstrap,
                                    str(Path(__file__).resolve().parents[2])], cwd=directory,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        process_facts["pid"] = process.pid
        for stream, stdout in ((process.stdout, True), (process.stderr, False)):
            thread = threading.Thread(target=_read_channel, args=(stream, stdout, channel, key), daemon=True)
            thread.start()
            readers.append(thread)
        request = {"scenario": scenario, "config": config, "channel_key": key.hex(), "run_timing": deadline.timing}
        process.stdin.write(json.dumps(request).encode("utf-8") + b"\n")
        process.stdin.close()
        while True:
            elapsed = (time.monotonic_ns() - process_facts["started"]["monotonic_ns"]) / 1_000_000_000
            if deadline.remaining() == 0:
                process_facts["timeout"] = {"kind": "total", **clock()}
                break
            rows = journal.rows("steps.jsonl", running=True)
            pending = {}
            for row in rows:
                if row["phase"] == "begin":
                    pending[row["step_id"]] = row
                else:
                    pending.pop(row["step_id"], None)
            if pending:
                active = next(iter(pending.values()))
                if (time.monotonic_ns() - active["monotonic_ns"]) / 1_000_000_000 > config["step_timeout_seconds"]:
                    process_facts["timeout"] = {"kind": "step", "name": active["name"], "returned": False, **clock()}
                    break
            if process.poll() is not None:
                break
            time.sleep(0.01)
    except Exception as exc:
        process_facts["controller_error"] = {"error_type": safe_error_type(exc), "reason": "supervision_failed"}
    finally:
        cleanup = cleanup_worker(process, wait_seconds=min(2.0, deadline.remaining())) if process is not None else {
            "exited": False, "exitcode": None, "forced_termination": False, "cleanup_errors": [{"stage": "launch"}]}
        process_facts.update(cleanup)
        if process_facts["exited"]:
            for thread in readers:
                thread.join(timeout=min(2.0, deadline.remaining()))
            if process is not None:
                for stream in (process.stdout, process.stderr):
                    if not any(thread.is_alive() for thread in readers):
                        stream.close()
        process_facts["finished"] = clock()
        process_facts["channel_ok"] = (channel["report"] is not None and channel["noise_bytes"] == 0
                                       and channel["stderr_bytes"] == 0 and channel["invalid_packets"] == 0
                                       and not channel["reader_error"] and not any(t.is_alive() for t in readers))
        process_facts["output_counts"] = {key: channel[key] for key in ("noise_bytes", "stderr_bytes", "invalid_packets")}
    try:
        deadline.check()
        for event in channel["events"]:
            journal.append("channel_events.jsonl", event)
        journal.append("process_checks.jsonl", {**clock(), **process_facts})
        journal.write("worker_result.json", channel["report"] if channel["report"] is not None else {"report_missing": True})
        decision = judge(journal, process_facts, manifest)
        if not process_facts["exited"]:
            return directory, incomplete("worker_exit_unconfirmed", decision)
        result = publish(journal, decision, manifest)
        deadline.check()
        return directory, result
    except RunDeadlineExceeded:
        deadline.revoke()
        return directory, incomplete("run_deadline_exceeded")
    except Exception:
        return directory, incomplete("supervisor_evidence_write_failed")


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("Invalid arguments")


def main():
    try:
        parser = SafeParser(description="V0.1.3 OFFLINE fake SDK probe ONLY")
        parser.add_argument("--offline-fake", action="store_true", required=True)
        parser.add_argument("--evidence-root", required=True)
        parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="normal")
        args = parser.parse_args()
        _, result = run_offline_probe(args.evidence_root, args.scenario)
        print(result["status"] + " scope=OFFLINE_FAKE_ONLY completion=" + result["completion"])
        return {"PASS": 0, "FAIL": 1, "NOT_COVERED": 2}[result["status"]]
    except BaseException:
        print("FAIL scope=OFFLINE_FAKE_ONLY completion=INCOMPLETE")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
