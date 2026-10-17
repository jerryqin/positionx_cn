"""V0.2 offline profile, source binding, and independent evidence consumer.

No SDK or PositionX runtime imports. This is a read-only migration probe,
not a sandbox for untrusted Python/native code.
"""

import hashlib
import hmac
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time
from functools import lru_cache

VERSION = "0.2.1-offline"
CONTEXT = "8ca8defeb81fb678e5451946c1ce9e01a8ddf5a0"
PROBE = "f9679927afaf535638fc4347ce41ba24c2a592e6"
V01 = "f136674f655c243d7612a6f4c95d1a9a613c5baf"
DIRECTORY = "tools/full_qmt_realrun_v02/"
FILES = ("README.md", "supervisor.py", "worker.py", "profile.py",
         "windows_runtime.py", "tests/test_fake_realrun.py")
CALLS = ("construct", "register_callback", "start", "connect", "construct_account",
         "subscribe", "query_asset", "query_positions", "query_orders", "query_trades",
         "bounded_callback_observation", "stop")
PAYLOAD = ("manifest.json", "facts.json", "process.json", "decision.candidate.json")
PACKAGE = frozenset((*PAYLOAD, "hashes.json", "COMPLETE"))
CALLBACK_FAILURE_MARKER = b"\x00POSITIONX_V02_CALLBACK_FAILURE\x00"
CONTRACT = {
    "asset": {"account_id": "id", "cash": "number", "frozen_cash": "number",
              "market_value": "number", "total_asset": "number"},
    "positions": {"account_id": "id", "stock_code": "symbol", "volume": "quantity",
                  "can_use_volume": "quantity", "open_price": "number",
                  "avg_price": "number", "market_value": "number"},
    "orders": {"account_id": "id", "order_id": "id", "stock_code": "symbol",
               "order_type": "integer", "order_volume": "quantity", "price": "number",
               "traded_volume": "quantity", "order_status": "integer", "order_time": "integer"},
    "trades": {"account_id": "id", "traded_id": "id", "order_id": "id",
               "stock_code": "symbol", "order_type": "integer", "traded_price": "number",
               "traded_volume": "quantity", "traded_amount": "number", "traded_time": "integer"},
}
SCENARIOS = frozenset(("pass", "connect_fail", "subscribe_fail", "query_none", "query_empty",
    "query_wrong_type", "query_missing", "query_wrong_field", "query_error", "stop_error",
    "primary_cleanup", "invalid_handle", "wrong_account", "callback_error", "callback_late",
    "callback_overflow", "callback_property", "callback_after_report", "stdout", "stderr",
    "native_output", "hang", "forbidden", "import_attempt", "unknown_effect", "health_missing"))


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":")).encode("ascii")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def digest(value):
    return sha(encode(value))


def token(key, value):
    # Never format unknown objects, classes, exceptions, getters or descriptors.
    raw = encode([type(value).__name__, value]) if type(value) in (str, int, bool, float, type(None)) else b"opaque"
    return "token:" + hmac.new(key, raw, hashlib.sha256).hexdigest()


def token_valid(value):
    return type(value) is str and re.fullmatch(r"token:[0-9a-f]{64}", value) is not None


def load(path):
    def pairs(items):
        result = {}
        for name, value in items:
            if name in result:
                raise ValueError("duplicate_key")
            result[name] = value
        return result
    if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("artifact_size_or_link")
    return json.loads(path.read_bytes(), object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))


class Deadline:
    def __init__(self, seconds):
        self.started = time.perf_counter_ns()
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 < seconds <= 120:
            raise ValueError("run_timeout")
        self.end = self.started + int(seconds * 1e9)

    def check(self):
        if time.perf_counter_ns() >= self.end:
            raise TimeoutError("run_deadline")

    def remaining(self):
        self.check()
        return (self.end - time.perf_counter_ns()) / 1e9


@lru_cache(maxsize=128)
def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                            timeout=10, check=True)
    return result.stdout


def blob(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def source_inventory(repo, v02_commit=None):
    result = {}
    groups = ((PROBE, "tools/full_qmt_readonly_probe_v01/"),
              (V01, "tools/full_qmt_realrun_harness_v01/"))
    paths = [(CONTEXT, "AGENTS.md"), (CONTEXT, "docs/PROJECT_CONTEXT.md")]
    for commit, directory in groups:
        names = git(repo, "ls-tree", "-r", "--name-only", commit, "--", directory).decode().splitlines()
        if len(names) != (6 if commit == PROBE else 7):
            raise ValueError("frozen_file_inventory")
        paths.extend((commit, name) for name in names)
    paths.extend((v02_commit, DIRECTORY + name) for name in FILES)
    for commit, name in paths:
        path = Path(repo) / name
        reject_links(path)
        data = path.read_bytes()
        if commit is not None and data != git(repo, "cat-file", "blob", commit + ":" + name):
            raise ValueError("source_blob_mismatch")
        result[name] = {"commit": commit, "sha256": sha(data), "blob": blob(data)}
    return result


def reject_links(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.exists():
            st = part.lstat()
            if part.is_symlink() or getattr(st, "st_file_attributes", 0) & 0x400:
                raise ValueError("link_or_reparse")
    if path.is_file() and path.stat().st_nlink != 1:
        raise ValueError("hardlink")


def make_fake_profile(repo, fixture_root, python, v02_commit=None):
    """Explicit inert fixture selection; never search for SDKs or accounts."""
    root = Path(fixture_root).absolute()
    selected = {"python": str(Path(python).absolute()), "sdk_root": str(root / "sdk"),
                "package_root": str(root / "sdk/package"), "native": str(root / "sdk/native.fixture"),
                "dll_roots": [str(root / "sdk/dlls")], "userdata": str(root / "userdata")}
    files = ("python", "native")
    return {"version": VERSION, "execution_mode": "FAKE_ONLY", "scope": "FULL_QMT_READ_ONLY",
            "context_source_commit": CONTEXT, "probe_source_commit": PROBE,
            "harness_v01_source_commit": V01, "harness_v02_source_commit": v02_commit,
            "sources": source_inventory(repo, v02_commit), "selection": selected,
            "selection_hashes": {name: sha(Path(selected[name]).read_bytes()) for name in files},
            "account_type": "STOCK", "session_id": 101901, "run_timeout": 15.0,
            "observation_seconds": 0.01, "max_output_bytes": 16384,
            "required_health": ["pre/os", "post/os", "pre/manual", "post/manual"],
            "sdk_health": "NOT_COVERED", "real_authorization": None}


def validate_profile(profile, repo):
    template_keys = {"version", "execution_mode", "scope", "context_source_commit", "probe_source_commit",
        "harness_v01_source_commit", "harness_v02_source_commit", "sources", "selection", "selection_hashes",
        "account_type", "session_id", "run_timeout", "observation_seconds", "max_output_bytes",
        "required_health", "sdk_health", "real_authorization"}
    if type(profile) is not dict or set(profile) != template_keys:
        raise ValueError("profile_schema")
    if profile["execution_mode"] != "FAKE_ONLY" or profile["real_authorization"] is not None:
        raise ValueError("real_execution_disabled")
    if (profile["version"], profile["scope"], profile["context_source_commit"],
        profile["probe_source_commit"], profile["harness_v01_source_commit"]) != (
            VERSION, "FULL_QMT_READ_ONLY", CONTEXT, PROBE, V01):
        raise ValueError("profile_binding")
    commit = profile["harness_v02_source_commit"]
    if commit is not None and (type(commit) is not str or not re.fullmatch("[0-9a-f]{40}", commit)):
        raise ValueError("v02_source_commit")
    if source_inventory(repo, commit) != profile["sources"]:
        raise ValueError("source_binding")
    selected = profile["selection"]
    if type(selected) is not dict or set(selected) != {"python", "sdk_root", "package_root", "native", "dll_roots", "userdata"}:
        raise ValueError("selection_schema")
    for name in ("python", "sdk_root", "package_root", "native", "userdata"):
        if type(selected[name]) is not str or not Path(selected[name]).is_absolute():
            raise ValueError("explicit_selection_required")
        reject_links(selected[name])
    # Offline tests may create only this interpreter's inert worker, never an
    # arbitrary executable disguised as a Python selection.
    if Path(selected["python"]).resolve() != Path(sys.executable).resolve():
        raise ValueError("offline_python_identity")
    for name in ("sdk_root", "package_root", "userdata"):
        if not Path(selected[name]).is_dir():
            raise ValueError("selection_directory")
    if type(selected["dll_roots"]) is not list or len(selected["dll_roots"]) != 1:
        raise ValueError("dll_roots")
    reject_links(selected["dll_roots"][0])
    if not Path(selected["dll_roots"][0]).is_dir():
        raise ValueError("dll_directory")
    sdk = Path(selected["sdk_root"])
    for name in ("package_root", "native"):
        if not Path(selected[name]).is_relative_to(sdk):
            raise ValueError("sdk_mixed_roots")
    if not Path(selected["dll_roots"][0]).is_relative_to(sdk):
        raise ValueError("dll_mixed_roots")
    if set(profile["selection_hashes"]) != {"python", "native"} or any(
        sha(Path(selected[n]).read_bytes()) != profile["selection_hashes"][n] for n in ("python", "native")):
        raise ValueError("selection_identity")
    if profile["account_type"] != "STOCK" or type(profile["session_id"]) is not int or not 0 < profile["session_id"] < 2**31:
        raise ValueError("account_or_session")
    Deadline(profile["run_timeout"])
    if type(profile["observation_seconds"]) not in (int, float) or not 0 <= profile["observation_seconds"] <= 1:
        raise ValueError("observation_window")
    if type(profile["max_output_bytes"]) is not int or not 1 <= profile["max_output_bytes"] <= 65536:
        raise ValueError("output_limit")
    if profile["required_health"] != ["pre/os", "post/os", "pre/manual", "post/manual"] or profile["sdk_health"] != "NOT_COVERED":
        raise ValueError("health_requirements")


def public_profile(profile, key):
    result = {k: v for k, v in profile.items() if k != "selection"}
    result["selection"] = {k: [token(key, p) for p in v] if type(v) is list else token(key, v)
                           for k, v in profile["selection"].items()}
    return result


def decision(facts, process, manifest):
    """Recompute from raw records. Report/candidate success is never authoritative."""
    failures, uncovered = [], []
    normalized = {}
    try:
        if set(process) != {"exit_confirmed", "returncode", "timed_out", "cleanup_errors", "controller_error",
                            "stdin_complete", "stdout", "stderr", "elapsed_ns", "callback_failure"}:
            raise ValueError("process_schema")
        for key in ("exit_confirmed", "timed_out", "stdin_complete", "callback_failure"):
            if type(process[key]) is not bool:
                raise ValueError("process_schema")
        if (type(process["returncode"]) is not int or type(process["elapsed_ns"]) is not int
            or process["elapsed_ns"] < 0 or type(process["cleanup_errors"]) is not list
            or any(x != "WORKER_EXIT_UNCONFIRMED" for x in process["cleanup_errors"])
            or process["controller_error"] not in (None, "CONTROLLER_ERROR")):
            raise ValueError("process_schema")
        if (not process["exit_confirmed"] or process["returncode"] != 0 or process["timed_out"]
            or process["cleanup_errors"] or process["controller_error"] is not None or not process["stdin_complete"]):
            failures.append("process_failure")
        if process["elapsed_ns"] >= manifest["deadline_ns"] - manifest["started_ns"]:
            failures.append("process_elapsed_timeout")
        if process["callback_failure"] is True:
            failures.append("callback_closure_failure")
        for channel in ("stdout", "stderr"):
            row = process[channel]
            if type(row) is not dict or set(row) != {"bytes", "eof", "error", "overflow"}:
                raise ValueError("channel_schema")
            if (type(row["bytes"]) is not int or row["bytes"] < 0 or type(row["eof"]) is not bool
                or type(row["overflow"]) is not bool or row["error"] not in (None, "PIPE_ERROR")):
                raise ValueError("channel_schema")
            if row["bytes"] != 0 or not row["eof"] or row["overflow"] or row["error"] is not None:
                failures.append("output_boundary")
        if set(facts) != {"steps", "snapshots", "callbacks", "callback_errors", "primary_error", "cleanup_error",
                          "evidence_errors", "scope_errors", "effects", "health", "identity", "callback_closed_ns",
                          "callback_closure"}:
            raise ValueError("facts_schema")
        for key in ("steps", "callbacks", "callback_errors", "evidence_errors", "scope_errors", "effects", "health"):
            if type(facts[key]) is not list:
                raise ValueError("facts_schema")
        for key in ("primary_error", "cleanup_error"):
            row = facts[key]
            if row is not None:
                if type(row) is not dict or set(row) != {"step", "code"} or row["step"] not in CALLS or row["code"] != "SDK_ERROR":
                    raise ValueError("error_schema")
                failures.append(key)
        if facts["callback_errors"] or facts["evidence_errors"] or facts["scope_errors"]:
            failures.append("raw_failure")
        closure = facts["callback_closure"]
        if (type(closure) is not dict or set(closure) != {"sealed", "producers_drained"}
            or any(type(closure[name]) is not bool for name in closure)):
            raise ValueError("callback_closure_schema")
        if closure["sealed"] is not True or closure["producers_drained"] is not True:
            failures.append("callback_closure_failure")
        identity = facts["identity"]
        if (type(identity) is not dict or set(identity) != {"python_hash", "python_version", "pointer_bits", "account_token", "binding_ok", "sdk_loading"}
            or identity["python_hash"] != manifest["profile"]["selection_hashes"]["python"]
            or identity["account_token"] != manifest["account_token"] or identity["binding_ok"] is not True
            or identity["sdk_loading"] != "FAKE_FIXTURE" or type(identity["pointer_bits"]) is not int
            or identity["pointer_bits"] not in (32, 64)
            or type(identity["python_version"]) is not list or len(identity["python_version"]) != 3
            or any(type(v) is not int for v in identity["python_version"])):
            failures.append("identity_failure")
        seen, last, ends = [], -1, {}
        for row in facts["steps"]:
            if (type(row) is not dict or set(row) != {"step", "begin_ns", "end_ns", "return", "error"}
                or row["step"] not in CALLS or type(row["begin_ns"]) is not int
                or type(row["end_ns"]) is not int or not last <= row["begin_ns"] <= row["end_ns"]
                or not manifest["started_ns"] <= row["begin_ns"] <= row["end_ns"] < manifest["deadline_ns"]
                or row["error"] not in (None, "SDK_ERROR")):
                raise ValueError("step_schema_or_timeline")
            name = row["step"]
            if name in seen:
                raise ValueError("duplicate_step")
            seen.append(name)
            ends[name] = row
            last = row["end_ns"]
            if row["error"] is not None:
                failures.append("step_error")
            if name in ("connect", "subscribe") and (type(row["return"]) is not int or row["return"] != 0):
                failures.append("interface_return")
            if name in ("construct", "construct_account") and row["return"] is not True:
                failures.append("invalid_handle")
            if name not in ("construct", "construct_account", "connect", "subscribe") and row["return"] is not None:
                failures.append("step_return_type")
        if seen != list(CALLS):
            failures.append("lifecycle_order_or_missing")
        stop = ends.get("stop")
        close = facts["callback_closed_ns"]
        if (type(close) is not int or stop is None or close < stop["end_ns"]
            or not manifest["started_ns"] <= close < manifest["deadline_ns"]):
            failures.append("closure_timeline")
        for event in facts["callbacks"]:
            if (type(event) is not dict or set(event) != {"kind", "at_ns", "fields", "capture_errors"}
                or event["kind"] not in ("account_status", "order", "trade", "disconnected", "error")
                or type(event["at_ns"]) is not int or type(event["fields"]) is not dict
                or type(event["capture_errors"]) is not list):
                raise ValueError("callback_schema")
            if event["capture_errors"] or event["kind"] == "error":
                failures.append("callback_failure")
            if not manifest["started_ns"] <= event["at_ns"] < manifest["deadline_ns"]:
                failures.append("callback_timeline")
            if type(close) is int and event["at_ns"] >= close:
                failures.append("late_callback")
            if event["kind"] == "disconnected" and (stop is None or not stop["begin_ns"] <= event["at_ns"] <= stop["end_ns"]):
                failures.append("disconnect_outside_stop")
            if "account_id" in event["fields"] and event["fields"]["account_id"] != manifest["account_token"]:
                failures.append("callback_account_binding")
        if facts["effects"] != [{"kind": "fixture_only", "simulation": True}]:
            failures.append("unknown_side_effect")
        expected_health = manifest["profile"]["required_health"]
        if len(facts["health"]) != len(expected_health):
            failures.append("health_evidence")
        for check, row in zip(expected_health, facts["health"]):
            if (type(row) is not dict or row != {"check": check, "simulation": True, "result": "PASS"}
                or row["simulation"] is not True):
                failures.append("health_evidence")
        snapshots = facts["snapshots"]
        if type(snapshots) is not dict or set(snapshots) != set(CONTRACT):
            raise ValueError("snapshot_inventory")
        for domain, contract in CONTRACT.items():
            snap = snapshots[domain]
            if type(snap) is not dict or set(snap) != {"kind", "items"} or type(snap["items"]) is not list:
                raise ValueError("snapshot_schema")
            result = {"raw_query": "PASS", "schema": "PASS", "items": snap["items"]}
            if snap["kind"] == "none" and snap["items"] == []:
                result.update(raw_query="NOT_COVERED", schema="NOT_COVERED")
                uncovered.append(domain + ":none")
            elif snap["kind"] != ("object" if domain == "asset" else "collection"):
                failures.append("query_shape")
                result.update(raw_query="FAIL", schema="FAIL")
            elif not snap["items"]:
                result["schema"] = "NOT_COVERED"
                uncovered.append(domain + ":no_samples")
            if domain == "asset" and snap["kind"] == "object" and len(snap["items"]) != 1:
                failures.append("asset_cardinality")
            for item in snap["items"]:
                if type(item) is not dict or set(item) != {"fields", "capture_errors"} or type(item["fields"]) is not dict or type(item["capture_errors"]) is not list:
                    raise ValueError("item_schema")
                fields = item["fields"]
                good = not item["capture_errors"] and set(contract).issubset(fields)
                for name, rule in contract.items():
                    value = fields.get(name)
                    if rule == "id":
                        good &= token_valid(value)
                    elif rule == "symbol":
                        good &= type(value) is str and re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", value) is not None
                    elif rule in ("quantity", "integer"):
                        good &= type(value) is int and (rule != "quantity" or value >= 0)
                    else:
                        good &= type(value) in (int, float) and math.isfinite(value)
                if fields.get("account_id") != manifest["account_token"]:
                    good = False
                if not good:
                    failures.append("query_schema_or_identity")
                    result["schema"] = "FAIL"
            normalized[domain] = result
    except (KeyError, ValueError, TypeError, AttributeError):
        failures.append("facts_or_process_schema")
    return {"overall": "FAIL" if failures else "NOT_COVERED" if uncovered else "PASS",
            "failures": sorted(set(failures)), "not_covered": sorted(set(uncovered)),
            "normalized": normalized, "scope": "FAKE_ONLY", "REAL_SDK": "NOT_COVERED",
            "REAL_FULL_QMT": "NOT_COVERED", "EXECUTION": "OUT_OF_SCOPE"}


def consume(directory, expected_manifest, deadline=None):
    """No COMPLETE => no consumable result. Valid COMPLETE does NOT imply PASS."""
    limit = deadline or Deadline(10)
    try:
        limit.check()
        root = Path(directory)
        reject_links(root)
        if set(p.name for p in root.iterdir()) != PACKAGE:
            raise ValueError("artifact_inventory")
        for name in PACKAGE:
            reject_links(root / name)
        hashes = load(root / "hashes.json")
        if type(hashes) is not dict or set(hashes) != set(PAYLOAD):
            raise ValueError("hash_inventory")
        for name in PAYLOAD:
            limit.check()
            if sha((root / name).read_bytes()) != hashes[name]:
                raise ValueError("hash_mismatch")
        marker = load(root / "COMPLETE")
        if (type(marker) is not dict or set(marker) != {"run_id", "hashes_sha256", "deadline_ns", "published_ns"}
            or marker["run_id"] != expected_manifest["run_id"]
            or marker["hashes_sha256"] != sha((root / "hashes.json").read_bytes())
            or type(marker["deadline_ns"]) is not int or type(marker["published_ns"]) is not int
            or marker["deadline_ns"] != expected_manifest["deadline_ns"]
            or not expected_manifest["started_ns"] <= marker["published_ns"] < marker["deadline_ns"]):
            raise ValueError("completion_binding")
        manifest = load(root / "manifest.json")
        if manifest != expected_manifest:
            raise ValueError("manifest_binding")
        # Commit strings alone are not a source identity, including offline files.
        repo = Path(__file__).absolute().parents[2]
        if source_inventory(repo, manifest["profile"]["harness_v02_source_commit"]) != manifest["profile"]["sources"]:
            raise ValueError("source_binding")
        result = decision(load(root / "facts.json"), load(root / "process.json"), manifest)
        if load(root / "decision.candidate.json") != result:
            raise ValueError("candidate_source_conflict")
        limit.check()
        return dict(result, completion="COMPLETE")
    except (OSError, ValueError, TypeError, KeyError, TimeoutError):
        return {"overall": "FAIL", "completion": "INCOMPLETE", "failures": ["evidence_invalid"]}
