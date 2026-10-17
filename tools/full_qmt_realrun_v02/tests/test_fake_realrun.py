"""Offline only: stdlib, inert files, owned fake children, temporary evidence.

No production imports/config/state. The outer SDK guard is a last-resort alarm;
the worker guard has a separately asserted denial fact.
"""

import copy
import importlib.abc
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

REPO = Path(__file__).absolute().parents[3]
sys.path.insert(0, str(REPO))


class DenySDK(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "xtquant" or fullname.startswith("xtquant."):
            raise AssertionError("test_must_not_import_real_sdk")
        return None


sys.meta_path.insert(0, DenySDK())
from tools.full_qmt_realrun_v02 import profile as p
from tools.full_qmt_realrun_v02 import supervisor as s
from tools.full_qmt_realrun_v02 import windows_runtime as runtime
from tools.full_qmt_realrun_v02 import worker as w

ACCOUNT = "987654321_SECRET_ACCOUNT"


class FakeRealRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory(prefix="positionx_v02_sdk_")
        root = Path(cls.fixture.name)
        for name in ("sdk/package", "sdk/dlls", "userdata"):
            (root / name).mkdir(parents=True)
        (root / "sdk/native.fixture").write_bytes(b"INERT_FAKE_NOT_A_NATIVE_MODULE")
        cls.base = p.make_fake_profile(REPO, root, sys.executable)

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="positionx_v02_evidence_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = copy.deepcopy(self.base)

    def run_fixture(self, scenario="pass"):
        return s.run_fake(self.profile, ACCOUNT, self.root, scenario)

    def write_barrier_fixture(self, force_zero_exit=False, write_error=False):
        """Instrument only a fake child's write boundary; leave verdicts intact."""
        script = f'''
import json
from pathlib import Path
import sys
import threading
sys.path.insert(0, {str(REPO)!r})
from tools.full_qmt_realrun_v02 import worker as w
original_perform, original_open = w.perform, Path.open
retained = {{}}
def perform(request):
    if request["scenario"] != "pass":
        raise RuntimeError()
    result = original_perform(request)
    retained["callback"], retained["closure"] = result[1], result[2]
    return result
class WriteBarrier:
    def __init__(self, stream):
        self.stream = stream
    def __enter__(self):
        self.stream.__enter__()
        return self
    def __exit__(self, *args):
        return self.stream.__exit__(*args)
    def write(self, payload):
        clean = json.loads(payload)
        if clean["callback_errors"] != [] or clean["callback_closure"] != {{"sealed": True, "producers_drained": True}}:
            raise RuntimeError()
        if {write_error!r}:
            self.stream.write(payload)
            raise OSError()
        fired = threading.Event()
        def late():
            retained["callback"]("error", None)
            fired.set()
        thread = threading.Thread(target=late)
        thread.start()
        thread.join(timeout=2)
        if thread.is_alive() or not fired.is_set() or not retained["closure"].failed:
            raise RuntimeError()
        if json.loads(payload)["callback_errors"] != []:
            raise RuntimeError()
        return self.stream.write(payload)
def open_file(path, *args, **kwargs):
    stream = original_open(path, *args, **kwargs)
    return WriteBarrier(stream) if path.name == "facts.json" and args == ("xb",) else stream
w.perform, Path.open = perform, open_file
exit_code = w.main()
sys.exit(0 if {force_zero_exit!r} else exit_code)
'''
        original = runtime.subprocess.Popen
        launches = []
        def launch(argv, *args, **kwargs):
            if argv[-1] == str(REPO / p.DIRECTORY / "worker.py"):
                argv = [*argv[:4], "-c", script]
                launches.append(True)
            return original(argv, *args, **kwargs)
        with patch.object(runtime.subprocess, "Popen", launch):
            run = self.run_fixture("pass")
        self.assertEqual(launches, [True])
        return run

    def assert_failure(self, run, reason):
        self.assertEqual(run["decision"]["overall"], "FAIL")
        self.assertIn(reason, run["decision"]["failures"])

    def reseal(self, run, change):
        """Keep bytes/digests/candidate valid; target raw-fact defenses directly."""
        root, manifest = run["directory"], run["manifest"]
        facts, proc = p.load(root / "facts.json"), p.load(root / "process.json")
        change(facts, proc)
        (root / "facts.json").write_bytes(p.encode(facts))
        (root / "process.json").write_bytes(p.encode(proc))
        result = p.decision(facts, proc, manifest)
        (root / "decision.candidate.json").write_bytes(p.encode(result))
        hashes = {name: p.sha((root / name).read_bytes()) for name in p.PAYLOAD}
        (root / "hashes.json").write_bytes(p.encode(hashes))
        marker = p.load(root / "COMPLETE")
        marker["hashes_sha256"] = p.sha((root / "hashes.json").read_bytes())
        (root / "COMPLETE").write_bytes(p.encode(marker))
        verified = p.consume(root, manifest)
        self.assertEqual(verified["completion"], "COMPLETE")
        self.assertNotIn("evidence_invalid", verified["failures"])
        return verified

    def test_pass_is_offline_only(self):
        run = self.run_fixture()
        self.assertEqual(run["decision"]["overall"], "PASS")
        self.assertEqual(run["decision"]["completion"], "COMPLETE")
        self.assertEqual(run["decision"]["REAL_SDK"], "NOT_COVERED")
        self.assertEqual(run["decision"]["EXECUTION"], "OUT_OF_SCOPE")
        self.assertIsNone(run["manifest"]["profile"]["harness_v02_source_commit"])
        self.assertEqual(set(x.name for x in run["directory"].iterdir()), p.PACKAGE)

    def test_default_closed_real_profile(self):
        self.profile["execution_mode"] = "REAL_FULL_QMT"
        with patch.object(s, "start_fake_worker") as launch:
            self.assert_failure(self.run_fixture(), "supervisor_or_deadline_failure")
            launch.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_authorization_boolean_cannot_enable_real(self):
        self.profile["real_authorization"] = True
        self.assert_failure(self.run_fixture(), "supervisor_or_deadline_failure")

    def test_worker_loader_real_mode_rejected(self):
        failures = []
        with self.assertRaises(w.SDKDenied):
            w.load_sdk("REAL_FULL_QMT", "pass", ACCOUNT, failures)
        self.assertEqual(failures, ["REAL_EXECUTION_DISABLED"])

    def test_worker_import_guard_latches_despite_caught_error(self):
        run = self.run_fixture("import_attempt")
        self.assert_failure(run, "raw_failure")
        self.assertIn("REAL_SDK_IMPORT_DENIED", p.load(run["directory"] / "facts.json")["scope_errors"])

    def test_source_string_mismatch(self):
        self.profile["context_source_commit"] = "0" * 40
        self.assert_failure(self.run_fixture(), "supervisor_or_deadline_failure")

    def test_actual_source_hash_mismatch(self):
        key = p.DIRECTORY + "worker.py"
        self.profile["sources"][key]["sha256"] = "0" * 64
        self.assert_failure(self.run_fixture(), "supervisor_or_deadline_failure")

    def test_fake_committed_v02_identity_rejected(self):
        self.profile["harness_v02_source_commit"] = p.CONTEXT
        self.assert_failure(self.run_fixture(), "supervisor_or_deadline_failure")

    def test_native_identity_mismatch(self):
        self.profile["selection_hashes"]["native"] = "0" * 64
        self.assert_failure(self.run_fixture(), "supervisor_or_deadline_failure")

    def test_explicit_selection_no_fallback(self):
        self.profile["selection"]["python"] = "python"
        with patch.object(s, "start_fake_worker") as launch:
            self.assert_failure(self.run_fixture(), "supervisor_or_deadline_failure")
            launch.assert_not_called()

    def test_sdk_root_mixing_rejected(self):
        self.profile["selection"]["package_root"] = str(self.root)
        self.assert_failure(self.run_fixture(), "supervisor_or_deadline_failure")

    def test_private_account_argv_env_evidence(self):
        captured = []
        original = runtime.OwnedWorker.__init__
        def observe(child, *args, **kwargs):
            original(child, *args, **kwargs)
            captured.append((child.argv, child.environment))
        with patch.object(runtime.OwnedWorker, "__init__", observe):
            run = self.run_fixture()
        self.assertEqual(run["decision"]["overall"], "PASS")
        self.assertEqual(len(captured), 1)
        self.assertNotIn(ACCOUNT, json.dumps(captured))
        self.assertNotIn("key", captured[0][1])
        for path in run["directory"].iterdir():
            data = path.read_bytes()
            self.assertNotIn(ACCOUNT.encode(), data)
            self.assertNotIn(str(Path(self.fixture.name)).encode(), data)

    def test_account_tokens_are_run_scoped(self):
        first, second = self.run_fixture(), self.run_fixture()
        self.assertNotEqual(first["manifest"]["account_token"], second["manifest"]["account_token"])
        self.assertNotEqual(first["directory"], second["directory"])

    def test_invalid_account_input(self):
        for value in (None, "", "  ", 123, False):
            with self.subTest(value=value):
                run = s.run_fake(self.profile, value, self.root)
                self.assert_failure(run, "supervisor_or_deadline_failure")

    def test_unknown_fields_and_types_never_formatted(self):
        class Dangerous:
            def __str__(self):
                raise AssertionError("must_not_format")
            __repr__ = __str__
        value = w.SimpleNamespace(**{"return": 987654321, "unknown": Dangerous()})
        captured = w.capture(value, {}, b"k" * 32)
        self.assertNotIn(b"987654321", p.encode(captured))
        self.assertNotIn(b"Dangerous", p.encode(captured))
        self.assertIn("UNREVIEWED_FIELD_VALUE", captured["capture_errors"])

    def test_forbidden_call_is_sticky_failure(self):
        run = self.run_fixture("forbidden")
        self.assert_failure(run, "raw_failure")
        self.assertIn("SDK_SCOPE_VIOLATION", p.load(run["directory"] / "facts.json")["scope_errors"])

    def test_dispatcher_has_no_transaction_or_retry_path(self):
        class SDK:
            def invoke(self, *args):
                self.fail = True
        sdk, errors = SDK(), []
        dispatch = w.Dispatcher(sdk, errors)
        for call in ("submit", "cancel", "replace", "modify", "reconnect", "fallback", "download", "account_discovery", "client_restart"):
            with self.assertRaises(w.SDKDenied):
                dispatch.call(call, None)
        self.assertFalse(hasattr(sdk, "fail"))
        self.assertEqual(len(errors), 9)

    def test_interface_and_schema_failures(self):
        scenarios = {"connect_fail": "interface_return", "subscribe_fail": "interface_return",
                     "query_wrong_type": "query_shape", "query_missing": "query_schema_or_identity",
                     "query_wrong_field": "query_schema_or_identity", "query_error": "primary_error",
                     "invalid_handle": "invalid_handle", "wrong_account": "identity_failure"}
        for scenario, reason in scenarios.items():
            with self.subTest(scenario=scenario):
                self.assert_failure(self.run_fixture(scenario), reason)

    def test_none_is_not_empty_query_success(self):
        run = self.run_fixture("query_none")
        self.assertEqual(run["decision"]["overall"], "NOT_COVERED")
        self.assertEqual(run["decision"]["normalized"]["orders"]["raw_query"], "NOT_COVERED")
        self.assertEqual(run["decision"]["completion"], "COMPLETE")

    def test_empty_collection_query_success_schema_not_covered(self):
        run = self.run_fixture("query_empty")
        self.assertEqual(run["decision"]["overall"], "NOT_COVERED")
        self.assertEqual(run["decision"]["normalized"]["orders"]["raw_query"], "PASS")
        self.assertEqual(run["decision"]["normalized"]["orders"]["schema"], "NOT_COVERED")

    def test_primary_and_cleanup_preserved(self):
        run = self.run_fixture("primary_cleanup")
        self.assert_failure(run, "primary_error")
        self.assert_failure(run, "cleanup_error")
        facts = p.load(run["directory"] / "facts.json")
        self.assertEqual(facts["primary_error"]["step"], "connect")
        self.assertEqual(facts["cleanup_error"]["step"], "stop")

    def test_callback_failures_are_raw_not_summary(self):
        for scenario in ("callback_error", "callback_late", "callback_overflow", "callback_property"):
            with self.subTest(scenario=scenario):
                run = self.run_fixture(scenario)
                self.assertEqual(run["decision"]["overall"], "FAIL")
                self.assertTrue(set(run["decision"]["failures"]) & {"callback_failure", "raw_failure"})

    def test_callback_after_report_child_exit_blocks_success(self):
        run = self.run_fixture("callback_after_report")
        self.assert_failure(run, "process_failure")
        self.assert_failure(run, "callback_closure_failure")
        self.assertEqual(p.load(run["directory"] / "process.json")["returncode"], 3)
        self.assertEqual(p.consume(run["directory"], run["manifest"])["overall"], "FAIL")

    def assert_barrier_veto(self, run, returncode):
        self.assert_failure(run, "callback_closure_failure")
        self.assertEqual(run["decision"]["completion"], "COMPLETE")
        root = run["directory"]
        facts, process = p.load(root / "facts.json"), p.load(root / "process.json")
        self.assertEqual(facts["callback_errors"], [])  # Encoded snapshot stayed clean.
        self.assertFalse(any(row["kind"] == "error" for row in facts["callbacks"]))
        self.assertEqual(facts["callback_closure"], {"sealed": True, "producers_drained": True})
        self.assertIs(process["callback_failure"], True)
        self.assertEqual(process["returncode"], returncode)
        hashes = p.load(root / "hashes.json")
        self.assertEqual(set(hashes), set(p.PAYLOAD))
        for name in p.PAYLOAD:
            self.assertEqual(hashes[name], p.sha((root / name).read_bytes()))
        # A fresh disk-derived manifest and consumer need no retained worker/latch.
        independent = p.consume(root, p.load(root / "manifest.json"))
        self.assertEqual(independent["completion"], "COMPLETE")
        self.assertEqual(independent["overall"], "FAIL")
        self.assertIn("callback_closure_failure", independent["failures"])
        self.assertNotIn("evidence_invalid", independent["failures"])
        # A completely new OS process has no worker or supervisor live state.
        script = f'''
import json
from pathlib import Path
import sys
sys.path.insert(0, {str(REPO)!r})
from tools.full_qmt_realrun_v02 import profile as p
root = Path(sys.argv[1])
result = p.consume(root, p.load(root / "manifest.json"))
print(json.dumps({{key: result[key] for key in ("overall", "completion", "failures")}}))
'''
        # Preserve the parent's read-only Git/sandbox ownership context. -I -S
        # still excludes site/.pth/PYTHONPATH; no private account is passed.
        self.assertTrue(all(ACCOUNT not in value for value in runtime.os.environ.values()))
        fresh = runtime.subprocess.run([sys.executable, "-I", "-S", "-B", "-c", script, str(root)],
            capture_output=True, timeout=15, check=True)
        self.assertEqual(fresh.stderr, b"")
        independent = json.loads(fresh.stdout)
        self.assertEqual(independent["completion"], "COMPLETE")
        self.assertEqual(independent["overall"], "FAIL")
        self.assertIn("callback_closure_failure", independent["failures"])
        self.assertNotIn("evidence_invalid", independent["failures"])

    def test_normal_pass_write_barrier_late_callback_veto(self):
        self.assert_barrier_veto(self.write_barrier_fixture(), 3)

    def test_normal_pass_write_barrier_zero_exit_cannot_wash_veto(self):
        run = self.write_barrier_fixture(force_zero_exit=True)
        self.assert_barrier_veto(run, 0)
        self.assertNotIn("process_failure", run["decision"]["failures"])

    def test_report_write_error_uses_same_sticky_veto(self):
        self.assert_barrier_veto(self.write_barrier_fixture(write_error=True), 2)

    def test_callback_marker_split_across_os_reads(self):
        marker = p.CALLBACK_FAILURE_MARKER
        child = runtime.OwnedWorker.__new__(runtime.OwnedWorker)
        child._callback_failure, child._overflow = threading.Event(), threading.Event()
        class Pipe:
            def __init__(self):
                self.parts = iter((marker[:3], marker[3:-2], marker[-2:], b""))
            def read(self, size):
                return next(self.parts)
            def close(self):
                pass
        facts = {"bytes": 0, "eof": False, "error": None, "overflow": False}
        child._drain(Pipe(), facts, 1024, "stderr")
        self.assertTrue(child._callback_failure.is_set())
        self.assertEqual(facts, {"bytes": len(marker), "eof": True, "error": None, "overflow": False})

    def test_known_callback_producers_sealed_and_joined(self):
        sdk = w.FakeSDK("pass", ACCOUNT)
        started, release, ended = threading.Event(), threading.Event(), threading.Event()
        def producer():
            started.set()
            if release.wait(timeout=1):
                ended.set()
        thread = sdk.start_callback_producer(producer)
        self.assertTrue(started.wait(timeout=1))
        timer = threading.Timer(0.02, release.set)
        timer.start()
        try:
            sdk.seal_and_drain_callbacks(time.perf_counter_ns() + 1_000_000_000)
            self.assertTrue(ended.is_set())
            self.assertFalse(thread.is_alive())
            with self.assertRaises(w.SDKDenied):
                sdk.start_callback_producer(lambda: None)
        finally:
            release.set()
            thread.join(timeout=1)
            timer.join(timeout=1)

    def test_producer_drain_failure_uses_independent_signal(self):
        class SDK(w.FakeSDK):
            def seal_and_drain_callbacks(self, deadline_ns):
                raise RuntimeError()
        request = {"key": (b"k" * 32).hex(), "account": ACCOUNT, "account_type": "STOCK",
                   "mode": "FAKE_ONLY", "scenario": "pass", "observation_seconds": 0,
                   "deadline_ns": time.perf_counter_ns() + 1_000_000_000}
        with patch.object(w, "FakeSDK", SDK), patch.object(sys, "meta_path", list(sys.meta_path)), \
             patch.object(w.os, "write", return_value=len(p.CALLBACK_FAILURE_MARKER)) as signal:
            facts, callback, closure = w.perform(request)
        self.assertIs(facts["callback_closure"]["producers_drained"], False)
        self.assertIn("PRODUCER_DRAIN_FAILURE", facts["callback_errors"])
        self.assertTrue(closure.failed)
        signal.assert_called_once_with(2, p.CALLBACK_FAILURE_MARKER)

    def test_callback_signal_delivery_failure_forbids_successful_exit(self):
        closure = w.CallbackClosure()
        class ForcedExit(BaseException):
            pass
        with patch.object(w.os, "write", side_effect=OSError()), \
             patch.object(w.os, "_exit", side_effect=ForcedExit()) as terminate:
            with self.assertRaises(ForcedExit):
                closure.fail()
        self.assertTrue(closure.failed)
        self.assertFalse(closure.signalled)
        terminate.assert_called_once_with(3)

    def test_callback_closure_and_sticky_process_fact_are_required(self):
        for field, value in (("callback_failure", None), ("callback_failure", "false"),
                             ("callback_closure", None), ("callback_closure", False)):
            with self.subTest(field=field, value=value):
                run = self.run_fixture()
                def change(facts, proc):
                    if field == "callback_failure":
                        proc[field] = value
                    else:
                        facts[field] = value
                result = self.reseal(run, change)
                self.assertIn("facts_or_process_schema", result["failures"])

    def test_callback_timeline_independent_recompute(self):
        run = self.run_fixture()
        def change(facts, proc):
            event = next(e for e in facts["callbacks"] if e["kind"] == "disconnected")
            event["at_ns"] = facts["steps"][3]["begin_ns"]
        result = self.reseal(run, change)
        self.assertIn("disconnect_outside_stop", result["failures"])

    def test_callback_outside_run_timeline(self):
        run = self.run_fixture()
        def change(facts, proc):
            facts["callbacks"][0]["at_ns"] = -1
        result = self.reseal(run, change)
        self.assertIn("callback_timeline", result["failures"])

    def test_post_exit_elapsed_timeout_recomputed(self):
        run = self.run_fixture()
        def change(facts, proc):
            proc["elapsed_ns"] = run["manifest"]["deadline_ns"] - run["manifest"]["started_ns"] + 1
        result = self.reseal(run, change)
        self.assertIn("process_elapsed_timeout", result["failures"])

    def test_stdout_and_native_stderr_discarded_not_leaked(self):
        out_before, err_before = sys.stdout, sys.stderr
        for scenario in ("stdout", "stderr", "native_output"):
            with self.subTest(scenario=scenario):
                run = self.run_fixture(scenario)
                self.assert_failure(run, "output_boundary")
                for path in run["directory"].iterdir():
                    self.assertNotIn(ACCOUNT.encode(), path.read_bytes())
                    self.assertNotIn(b"sensitive", path.read_bytes())
        self.assertIs(sys.stdout, out_before)
        self.assertIs(sys.stderr, err_before)

    def test_output_overflow_terminates_owned_child(self):
        self.profile["max_output_bytes"] = 1
        run = self.run_fixture("native_output")
        self.assert_failure(run, "output_boundary")
        proc = p.load(run["directory"] / "process.json")
        self.assertTrue(proc["exit_confirmed"])
        self.assertTrue(proc["stdout"]["overflow"])

    def test_worker_timeout_no_complete_and_exit_confirmed(self):
        self.profile["run_timeout"] = 0.6
        exits = []
        original = runtime.OwnedWorker.terminate_and_wait
        def cleanup(child, seconds):
            result = original(child, seconds)
            exits.append((result, child._process.poll()))
            return result
        with patch.object(runtime.OwnedWorker, "terminate_and_wait", cleanup):
            run = self.run_fixture("hang")
        self.assert_failure(run, "supervisor_or_deadline_failure")
        self.assertTrue(exits)
        self.assertTrue(all(ok and code is not None for ok, code in exits))
        self.assertFalse((run["directory"] / "COMPLETE").exists())

    def test_unknown_exit_cannot_publish(self):
        run = self.run_fixture()
        facts = p.load(run["directory"] / "facts.json")
        proc = p.load(run["directory"] / "process.json")
        proc["exit_confirmed"] = False
        fresh = self.root / "unknown_exit"
        fresh.mkdir()
        (fresh / "facts.json").write_bytes(p.encode(facts))
        with self.assertRaisesRegex(ValueError, "worker_exit_unconfirmed"):
            s.publish(fresh, run["manifest"], facts, proc, p.Deadline(5))
        self.assertFalse((fresh / "COMPLETE").exists())

    def test_health_evidence_cannot_impersonate_sdk(self):
        run = self.run_fixture()
        def change(facts, proc):
            facts["health"][0] = {"check": "pre/sdk", "simulation": False, "result": "PASS"}
        result = self.reseal(run, change)
        self.assertIn("health_evidence", result["failures"])

    def test_required_health_cannot_be_lowered(self):
        self.profile["required_health"] = []
        self.assert_failure(self.run_fixture(), "supervisor_or_deadline_failure")

    def test_unknown_effect_is_not_approved(self):
        self.assert_failure(self.run_fixture("unknown_effect"), "unknown_side_effect")

    def test_complete_can_mean_business_fail(self):
        run = self.run_fixture("stop_error")
        self.assert_failure(run, "cleanup_error")
        independent = p.consume(run["directory"], run["manifest"])
        self.assertEqual(independent["completion"], "COMPLETE")
        self.assertEqual(independent["overall"], "FAIL")

    def test_process_facts_strict_missing_and_types(self):
        for value in ("MISSING", None, False, "false"):
            with self.subTest(value=value):
                run = self.run_fixture()
                def change(facts, proc):
                    if value == "MISSING":
                        proc.pop("cleanup_errors")
                    else:
                        proc["cleanup_errors"] = value
                result = self.reseal(run, change)
                self.assertIn("facts_or_process_schema", result["failures"])

    def test_raw_stderr_cannot_be_hidden_by_candidate(self):
        run = self.run_fixture()
        def change(facts, proc):
            proc["stderr"]["bytes"] = 1
        result = self.reseal(run, change)
        self.assertIn("output_boundary", result["failures"])

    def test_step_order_stop_conflict_rejected_with_valid_hashes(self):
        run = self.run_fixture()
        def change(facts, proc):
            facts["steps"][-1]["end_ns"] = 0
        result = self.reseal(run, change)
        self.assertIn("facts_or_process_schema", result["failures"])

    def test_raw_error_and_candidate_success_conflict(self):
        run = self.run_fixture()
        root = run["directory"]
        facts = p.load(root / "facts.json")
        facts["callback_errors"].append("CAPTURE_ERROR")
        (root / "facts.json").write_bytes(p.encode(facts))
        hashes = p.load(root / "hashes.json")
        hashes["facts.json"] = p.sha((root / "facts.json").read_bytes())
        (root / "hashes.json").write_bytes(p.encode(hashes))
        marker = p.load(root / "COMPLETE")
        marker["hashes_sha256"] = p.sha((root / "hashes.json").read_bytes())
        (root / "COMPLETE").write_bytes(p.encode(marker))
        self.assertEqual(p.decision(facts, p.load(root / "process.json"), run["manifest"])["overall"], "FAIL")
        self.assertEqual(p.consume(root, run["manifest"])["overall"], "FAIL")

    def test_missing_required_artifact(self):
        run = self.run_fixture()
        (run["directory"] / "facts.json").unlink()
        self.assertEqual(p.consume(run["directory"], run["manifest"])["completion"], "INCOMPLETE")

    def test_hash_inventory_missing_entry(self):
        run = self.run_fixture()
        root = run["directory"]
        hashes = p.load(root / "hashes.json")
        hashes.pop("facts.json")
        (root / "hashes.json").write_bytes(p.encode(hashes))
        marker = p.load(root / "COMPLETE")
        marker["hashes_sha256"] = p.sha((root / "hashes.json").read_bytes())
        (root / "COMPLETE").write_bytes(p.encode(marker))
        self.assertEqual(p.consume(root, run["manifest"])["overall"], "FAIL")

    def test_extra_temp_and_truncated_file_rejected(self):
        for mode in ("temp", "truncated", "duplicate_json"):
            with self.subTest(mode=mode):
                run = self.run_fixture()
                root = run["directory"]
                if mode == "temp":
                    (root / "COMPLETE.tmp").write_bytes(b"stale")
                elif mode == "truncated":
                    (root / "facts.json").write_bytes(b'{"steps":')
                else:
                    (root / "hashes.json").write_bytes(b'{"facts.json":"a","facts.json":"b"}')
                self.assertEqual(p.consume(root, run["manifest"])["overall"], "FAIL")

    def test_missing_complete_not_consumed(self):
        run = self.run_fixture()
        (run["directory"] / "COMPLETE").unlink()
        self.assertEqual(p.consume(run["directory"], run["manifest"])["completion"], "INCOMPLETE")

    def test_publish_io_failure_no_complete(self):
        original = s.write
        def fail(directory, name, value, deadline):
            if name == "hashes.json":
                raise OSError()
            original(directory, name, value, deadline)
        with patch.object(s, "write", fail):
            run = self.run_fixture()
        self.assert_failure(run, "supervisor_or_deadline_failure")
        self.assertFalse((run["directory"] / "COMPLETE").exists())
        self.assertTrue((run["directory"] / "decision.candidate.json").exists())

    def test_publish_finishes_after_deadline_no_complete(self):
        original = s.write
        def expire(directory, name, value, deadline):
            if name == "hashes.json":
                deadline.end = time.perf_counter_ns() - 1
            original(directory, name, value, deadline)
        with patch.object(s, "write", expire):
            run = self.run_fixture()
        self.assert_failure(run, "supervisor_or_deadline_failure")
        self.assertFalse((run["directory"] / "COMPLETE").exists())

    def test_rename_return_after_deadline_invalidates_complete(self):
        original = s.os.rename
        created = []
        def rename(source, dest):
            original(source, dest)
            created.append(Path(dest).exists())
            # Manifest's fixed deadline is close enough via test profile.
            time.sleep(0.7)
        self.profile["run_timeout"] = 0.6
        with patch.object(s.os, "rename", rename):
            run = self.run_fixture()
        self.assert_failure(run, "supervisor_or_deadline_failure")
        self.assertTrue(created and all(created))
        self.assertFalse((run["directory"] / "COMPLETE").exists())

    def test_unknown_exception_never_formatted(self):
        class SecretError(Exception):
            def __str__(self):
                print(ACCOUNT, file=sys.stderr)
                raise AssertionError()
            __repr__ = __str__
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "stdout", out), patch.object(sys, "stderr", err), patch.object(s, "start_fake_worker", side_effect=SecretError()):
            run = self.run_fixture()
        self.assert_failure(run, "supervisor_or_deadline_failure")
        self.assertEqual(out.getvalue() + err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
