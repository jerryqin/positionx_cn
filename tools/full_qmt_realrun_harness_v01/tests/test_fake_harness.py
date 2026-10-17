"""Independent fixtures; temp files and read-only Git only. No real SDK imports."""

import contextlib
import hashlib
import importlib.abc
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


class DenySDK(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "xtquant" or fullname.startswith("xtquant."):
            raise ImportError("Real SDK forbidden in harness tests")
        return None


if any(name == "xtquant" or name.startswith("xtquant.") for name in sys.modules):
    raise RuntimeError("Offline test interpreter already loaded SDK")
sys.meta_path.insert(0, DenySDK())

from tools.full_qmt_realrun_harness_v01 import real_profile as profile
from tools.full_qmt_realrun_harness_v01 import sdk_loader as loader
from tools.full_qmt_realrun_harness_v01 import supervisor, windows_runtime as runtime
from tools.full_qmt_readonly_probe_v01.evidence import clock, RunDeadline


ACCOUNT = "HARNESS_PRIVATE_FAKE_ACCOUNT_246813579"


class FixtureAccount:
    def __init__(self, account_id, account_type):
        self.account_id, self.account_type = account_id, account_type


class FixtureSDK:
    fixture_kind = "HARNESS_FIXTURE"
    version = "HARNESS_FIXTURE_V01"
    AccountType = FixtureAccount

    def __init__(self, scenario="normal"):
        self.scenario = scenario
        self.effects = ["fixture_import", "fixture_file_read"]
        if scenario == "effect_network":
            self.effects.append("network")
        if scenario == "effect_unknown":
            self.effects.append(ACCOUNT)
        self.calls, self.trader = [], None

    @property
    def TraderType(self):
        return FixtureTrader

    def Trader(self, userdata, session):
        if userdata != "FIXTURE_USERDATA" or type(session) is not int:
            raise AssertionError("Only fixture inputs are allowed")
        self.calls.append("construct")
        if self.scenario == "invalid_handle":
            return None
        self.trader = FixtureTrader(self)
        return self.trader

    def Account(self, account, kind):
        self.calls.append("construct_account")
        return FixtureAccount("WRONG_FAKE_ACCOUNT" if self.scenario == "wrong_account" else account, kind)


class FixtureTrader:
    def __init__(self, sdk):
        self._sdk, self._callback = sdk, None

    def register_callback(self, callback):
        self._sdk.calls.append("register_callback")
        self._callback = callback

    def start(self):
        self._sdk.calls.append("start")

    def connect(self):
        self._sdk.calls.append("connect")
        self._callback.on_connected()
        self._callback.on_account_status(SimpleNamespace(account_id=ACCOUNT, status=0))
        if self._sdk.scenario == "disconnect":
            self._callback.on_disconnected()
        if self._sdk.scenario == "callback_error":
            self._callback.on_order_error(SimpleNamespace(account_id=ACCOUNT, error_id=5, error_msg=ACCOUNT))
        if self._sdk.scenario == "callback_overflow":
            for _ in range(20):
                self._callback.on_stock_order(self.order())
        if self._sdk.scenario == "capture_error":
            class Broken:
                @property
                def status(self):
                    raise ValueError(ACCOUNT)
            self._callback.on_account_status(Broken())
        if self._sdk.scenario in {"connect_nonzero", "primary_and_stop_error"}:
            return -1
        return False if self._sdk.scenario == "connect_bool" else 0

    def subscribe(self, account):
        self._sdk.calls.append("subscribe")
        if self._sdk.scenario == "mutated_account":
            account.account_id = "WRONG_AFTER_SUBSCRIBE"
        return -1 if self._sdk.scenario == "subscribe_nonzero" else 0

    @staticmethod
    def asset():
        return SimpleNamespace(account_id=ACCOUNT, cash=123.0, frozen_cash=0.0, market_value=45.0, total_asset=168.0)

    @staticmethod
    def position():
        return SimpleNamespace(account_id=ACCOUNT, stock_code="600000.SH", volume=200, can_use_volume=100,
                               open_price=1.0, avg_price=2.0, market_value=45.0)

    @staticmethod
    def order():
        return SimpleNamespace(account_id=ACCOUNT, order_id=9981, stock_code="600000.SH", order_type=23,
                               order_volume=100, price=2.0, traded_volume=100, order_status=56, order_time=123456)

    @staticmethod
    def trade():
        return SimpleNamespace(account_id=ACCOUNT, order_id=9981, traded_id="PRIVATE_FAKE_TRADE", stock_code="600000.SH",
                               order_type=23, traded_price=2.0, traded_volume=100, traded_amount=200.0, traded_time=123456)

    def query_asset(self, account):
        self._sdk.calls.append("query_asset")
        scenario = self._sdk.scenario
        if scenario == "query_none":
            return None
        if scenario == "slow_query":
            time.sleep(0.04)
        if scenario == "query_error":
            raise ValueError(ACCOUNT)
        if scenario == "stdout":
            print(ACCOUNT)
            print("PRIVATE_FAKE_PATH", file=sys.stderr)
        value = self.asset()
        if scenario == "wrong_field":
            value.cash = True
        if scenario == "missing_field":
            del value.cash
        if scenario == "empty_id":
            value.account_id = ""
        if scenario == "unknown_fields":
            setattr(value, "return", 246813579)
            value.investor_id = 246813579
        return value

    def query_positions(self, account):
        self._sdk.calls.append("query_positions")
        return None if self._sdk.scenario == "query_none" else [self.position()]

    def query_orders(self, account):
        self._sdk.calls.append("query_orders")
        if self._sdk.scenario == "query_none":
            return None
        return [] if self._sdk.scenario == "query_empty" else [self.order()]

    def query_trades(self, account):
        self._sdk.calls.append("query_trades")
        if self._sdk.scenario == "query_none":
            return None
        return [] if self._sdk.scenario == "query_empty" else [self.trade()]

    def stop(self):
        self._sdk.calls.append("stop")
        if self._sdk.scenario == "stop_disconnect":
            self._callback.on_disconnected()
        if self._sdk.scenario in {"stop_error", "primary_and_stop_error"}:
            raise RuntimeError(ACCOUNT)


def selection_fixture(root):
    root = Path(root)
    sdk = root / "fixture_sdk"
    dlls = sdk / "fixture_dlls"
    dlls.mkdir(parents=True)
    module, native, dll = sdk / "module.fixture", sdk / "native.fixture", dlls / "library.fixture"
    for path in (module, native, dll):
        path.write_bytes(b"HARNESS INERT FIXTURE; NEVER IMPORT OR LOAD")
    return {"python": sys.executable, "sdk_root": str(sdk), "module": str(module),
            "native_module": str(native), "dll_roots": [str(dlls)], "dll_files": [str(dll)]}


class HarnessTests(unittest.TestCase):
    def setup_run(self, root, scenario="normal", **options):
        selection = selection_fixture(root)
        sdk = FixtureSDK(scenario)
        overrides = {"observation_seconds": 0.0, **options.pop("overrides", {})}
        directory, result = supervisor.run_fixture(root, selection, ACCOUNT, lambda: sdk, overrides=overrides, **options)
        return profile.Journal(directory), result, selection, sdk

    def healthy(self, root):
        journal, result, selection, sdk = self.setup_run(root)
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["completion"], "COMPLETE", result)
        return journal, result, selection, sdk

    def rows_write(self, journal, name, rows):
        journal.path(name).write_bytes(b"".join((json.dumps(row, allow_nan=False) + "\n").encode() for row in rows))

    def json_write(self, journal, name, value):
        journal.path(name).write_bytes((json.dumps(value, ensure_ascii=True, allow_nan=False) + "\n").encode())

    def rehash(self, journal):
        hashes = {"run_id": journal.directory.name, "algorithm": "sha256", "files": {
            name: hashlib.sha256(journal.path(name).read_bytes()).hexdigest() for name in profile.PAYLOAD}}
        self.json_write(journal, "hashes.json", hashes)
        marker = journal.read("COMPLETE")
        marker["hashes_sha256"] = hashlib.sha256(journal.path("hashes.json").read_bytes()).hexdigest()
        self.json_write(journal, "COMPLETE", marker)

    def decision(self, journal, selection):
        return profile.judge(journal, selection, RunDeadline(clock(), 30.0))

    def bind_report(self, journal, worker):
        self.json_write(journal, "worker_result.json", worker)
        rows = journal.rows("channel_events.jsonl")
        for row in rows:
            if row["event"] == "report":
                row["report_sha256"] = hashlib.sha256(json.dumps(worker, sort_keys=True, ensure_ascii=True,
                                                                allow_nan=False).encode()).hexdigest()
        self.rows_write(journal, "channel_events.jsonl", rows)

    def test_normal_independent_profile_and_all_calls(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, result, selection, sdk = self.healthy(root)
            self.assertEqual(result["execution_mode"], "HARNESS_FAKE_ONLY")
            self.assertEqual(result["scope"], "REAL_FULL_QMT_READ_ONLY")
            for key in ("REAL_SDK", "REAL_FULL_QMT"):
                self.assertEqual(result[key], "NOT_COVERED")
            self.assertEqual(result["EXECUTION"], "OUT_OF_SCOPE")
            self.assertFalse(hasattr(sdk, "offline_fake"))
            self.assertEqual(sdk.calls, [n for n in loader.ReadOnlyGate.ORDER if n != "bounded_callback_observation"])
            self.assertEqual(set(result["checks"]), set(profile.CHECKS))
            self.assertTrue(all(value == "PASS" for value in result["checks"].values()))
            self.assertEqual(profile.consume(journal.directory, selection)["status"], "PASS")

    def test_manifest_pins_and_uncommitted_state_are_honest(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, _, _ = self.healthy(root)
            manifest = journal.read("manifest.json")
            self.assertEqual(manifest["context_source_commit"], "bab4fc4c259450efe31c83a104ab256c532e3f99")
            self.assertEqual(manifest["probe_source_commit"], "f9679927afaf535638fc4347ce41ba24c2a592e6")
            self.assertIsNone(manifest["harness_source_commit"])
            self.assertEqual(manifest["harness_binding_state"], "UNCOMMITTED_OFFLINE")
            self.assertFalse(manifest["real_execution_enabled"])
            for group, count in (("context", 2), ("probe", 6), ("harness", 7)):
                self.assertEqual(len(manifest["source_bindings"][group]), count)
                self.assertTrue(all(len(value["sha256"]) == 64 and len(value["git_blob"]) == 40
                                    for value in manifest["source_bindings"][group].values()))

    def test_real_mode_refused_before_factory_or_directory(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            selection = selection_fixture(root)
            factory = mock.Mock(side_effect=AssertionError("Factory must not execute"))
            with self.assertRaises(loader.RealSDKDisabled):
                supervisor.run_fixture(root, selection, ACCOUNT, factory, mode="real")
            factory.assert_not_called()
            self.assertEqual({p.name for p in Path(root).iterdir()}, {"fixture_sdk"})

    def test_loader_real_mode_refuses_even_explicit_selection(self):
        factory = mock.Mock()
        with self.assertRaises(loader.RealSDKDisabled):
            loader.load_fixture("real", {}, {}, factory)
        factory.assert_not_called()

    def test_worker_lifecycle_blocks_attempted_real_import(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            selection = selection_fixture(root)
            def factory():
                __import__("xtquant")
            _, result = supervisor.run_fixture(root, selection, ACCOUNT, factory)
            self.assertEqual(result["status"], "FAIL")
            self.assertFalse(any(n == "xtquant" or n.startswith("xtquant.") for n in sys.modules))

    def test_import_boundary_required_before_factory(self):
        factory = mock.Mock()
        with self.assertRaises(loader.ContractViolation):
            loader.load_fixture("fixture", {}, {}, factory)
        factory.assert_not_called()

    def test_explicit_selection_refuses_missing_native_and_fallback(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            selection = selection_fixture(root)
            for key in ("python", "sdk_root", "module", "native_module", "dll_roots", "dll_files"):
                changed = dict(selection)
                del changed[key]
                with self.subTest(key=key), self.assertRaises(loader.ContractViolation):
                    loader.explicit_selection(changed)
            for extra in ("fallback", "python_candidates", "sdk_roots", "auto_discover"):
                with self.subTest(extra=extra), self.assertRaises(loader.ContractViolation):
                    loader.explicit_selection({**selection, extra: True})

    def test_missing_file_does_not_choose_another_sdk(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            selection = selection_fixture(root)
            selection["native_module"] += ".missing"
            with self.assertRaises(loader.ContractViolation):
                loader.explicit_selection(selection)

    def test_module_outside_sdk_root_rejected(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            selection = selection_fixture(root)
            selection["module"] = sys.executable
            with self.assertRaises(loader.ContractViolation):
                loader.explicit_selection(selection)

    def test_account_private_in_argv_environment_and_every_artifact(self):
        original, processes = runtime.create_fixture_process, []
        def audited(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process
        stdout, stderr = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            with mock.patch.object(supervisor, "create_fixture_process", side_effect=audited), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                journal, _, _, _ = self.setup_run(root, "stdout")
            text = "\n".join(path.read_text(encoding="utf-8") for path in journal.directory.iterdir())
            self.assertNotIn(ACCOUNT, text + stdout.getvalue() + stderr.getvalue())
            self.assertNotIn("PRIVATE_FAKE_PATH", text + stdout.getvalue() + stderr.getvalue())
            self.assertNotIn(ACCOUNT, json.dumps([processes[0].argv, processes[0].env]))
            self.assertEqual(processes[0].env, {})
            self.assertNotIn(str(Path(root)), text)

    def test_private_pipe_is_one_shot_and_not_repr_leak(self):
        pipe = runtime.PrivatePipe()
        pipe.send({"account": ACCOUNT})
        self.assertNotIn(ACCOUNT, repr(pipe))
        self.assertEqual(pipe.receive(), {"account": ACCOUNT})
        with self.assertRaises(loader.ContractViolation):
            pipe.receive()
        with self.assertRaises(loader.ContractViolation):
            pipe.send({})

    def test_unknown_fields_and_return_numeric_are_private(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, result, _, _ = self.setup_run(root, "unknown_fields")
            text = "\n".join(path.read_text(encoding="utf-8") for path in journal.directory.iterdir())
            self.assertEqual(result["status"], "PASS")
            for secret in ("246813579", "investor_id", '"return"'):
                self.assertNotIn(secret, text)

    def test_unknown_object_and_exception_never_stringified(self):
        calls = []
        class PRIVATE_EXCEPTION(Exception):
            def __str__(self):
                calls.append("str")
                print(ACCOUNT)
                return ACCOUNT
            def __repr__(self):
                calls.append("repr")
                return ACCOUNT
        redactor = profile.PrivateRedactor(b"one-test-only")
        value = PRIVATE_EXCEPTION(ACCOUNT)
        error = redactor.error(value)
        token = redactor.token(value)
        self.assertEqual(calls, [])
        self.assertNotIn("PRIVATE_EXCEPTION", json.dumps(error))
        self.assertNotIn(ACCOUNT, json.dumps(error) + token)

    def test_source_hash_and_blob_mismatch_rejected(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, selection, _ = self.healthy(root)
            original = journal.read("manifest.json")
            for field in ("sha256", "git_blob"):
                changed = json.loads(json.dumps(original))
                changed["source_bindings"]["probe"]["worker.py"][field] = "0" * (64 if field == "sha256" else 40)
                self.json_write(journal, "manifest.json", changed)
                with self.subTest(field=field), self.assertRaises(loader.ContractViolation):
                    profile.validate_manifest(journal, selection, RunDeadline(clock(), 30))

    def test_commit_string_cannot_claim_harness_binding(self):
        with self.assertRaises(loader.ContractViolation):
            profile.source_bindings(RunDeadline(clock(), 30), profile.CONTEXT_COMMIT)

    def test_selected_sdk_bytes_changed_rejected(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, selection, _ = self.healthy(root)
            Path(selection["native_module"]).write_bytes(b"CHANGED INERT FIXTURE")
            self.assertEqual(profile.consume(journal.directory, selection)["completion"], "INCOMPLETE")

    def test_selected_python_identity_mismatch_rejected(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, selection, _ = self.healthy(root)
            selection["python"] = selection["module"]
            self.assertEqual(profile.consume(journal.directory, selection)["status"], "FAIL")

    def test_unapproved_commands_persist_scope_violation(self):
        violations = []
        gate = loader.ReadOnlyGate(FixtureSDK(), ACCOUNT, 1019, violations.append)
        for name in ("submit", "cancel", "replace", "modify", "account_discovery", "reconnect", "download", "fallback", "restart_client"):
            with self.subTest(name=name), self.assertRaises(loader.ContractViolation):
                gate.invoke(name)
        self.assertEqual(len(violations), 9)

    def test_wrong_order_and_repeated_stop_rejected(self):
        violations = []
        gate = loader.ReadOnlyGate(FixtureSDK(), ACCOUNT, 1019, violations.append)
        with self.assertRaises(loader.ContractViolation):
            gate.invoke("connect")
        gate.invoke("construct")
        gate.invoke("stop")
        with self.assertRaises(loader.ContractViolation):
            gate.invoke("stop")
        self.assertEqual(violations, ["connect", "stop"])

    def test_callback_cannot_make_even_next_allowed_sdk_call(self):
        violations = []
        gate = loader.ReadOnlyGate(FixtureSDK(), ACCOUNT, 1019, violations.append)
        gate.invoke("construct")
        gate.invoke("register_callback")
        with self.assertRaises(loader.ContractViolation):
            gate.callback_call(lambda: gate.invoke("start"))
        gate.invoke("start")
        self.assertEqual(violations, ["start"])

    def test_scope_failure_event_cannot_be_overridden_by_clean_summary(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, selection, _ = self.healthy(root)
            rows = journal.rows("worker_events.jsonl")
            stamp = {k: rows[-1][k] for k in ("run_id", "timestamp_utc", "timestamp_cn", "monotonic_ns")}
            rows.append({**stamp, "event": "scope_violation", "facts": {"command_token": "redacted:" + "a" * 64}})
            self.rows_write(journal, "worker_events.jsonl", rows)
            decision = self.decision(journal, selection)
            self.assertEqual(decision["status"], "FAIL")
            self.assertEqual(decision["checks"]["all_failure_sources"], "FAIL")
            self.rehash(journal)
            self.assertEqual(profile.consume(journal.directory, selection)["status"], "FAIL")

    def test_none_is_not_empty_and_is_not_covered(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, result, _, _ = self.setup_run(root, "query_none")
            self.assertEqual(result["status"], "NOT_COVERED", result)
            self.assertEqual(result["completion"], "COMPLETE", result)
            self.assertTrue(all(row["raw"]["kind"] == "none" for row in journal.rows("snapshots.jsonl")))
            self.assertTrue(all(row["raw_query"] == row["schema_coverage"] == "NOT_COVERED" for row in result["domains"].values()))

    def test_empty_orders_trades_raw_success_without_schema_pass(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            _, result, _, _ = self.setup_run(root, "query_empty")
            self.assertEqual(result["status"], "NOT_COVERED", result)
            for domain in ("orders", "trades"):
                self.assertEqual(result["domains"][domain]["raw_query"], "PASS")
                self.assertEqual(result["domains"][domain]["schema_coverage"], "NOT_COVERED")

    def test_wrong_field_type_fails_schema(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            _, result, _, _ = self.setup_run(root, "wrong_field")
            self.assertEqual(result["status"], "FAIL", result)
            self.assertEqual(result["domains"]["account"]["schema_coverage"], "FAIL")

    def test_missing_field_fails_schema(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            _, result, _, _ = self.setup_run(root, "missing_field")
            self.assertEqual(result["status"], "FAIL", result)
            self.assertIn("cash", result["domains"]["account"]["items"][0]["missing_fields"])

    def test_empty_identity_and_wrong_account_do_not_pass(self):
        for scenario in ("wrong_account", "empty_id", "invalid_handle"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
                _, result, _, _ = self.setup_run(root, scenario)
                self.assertEqual(result["status"], "FAIL", result)

    def test_connect_bool_nonzero_and_subscribe_nonzero_fail(self):
        for scenario, check in (("connect_bool", "connect"), ("connect_nonzero", "connect"), ("subscribe_nonzero", "subscribe")):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
                _, result, _, _ = self.setup_run(root, scenario)
                self.assertEqual(result["status"], "FAIL", result)
                self.assertEqual(result["checks"][check], "FAIL")

    def test_shutdown_disconnect_timeline_passes(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            _, result, _, _ = self.setup_run(root, "stop_disconnect")
            self.assertEqual(result["status"], "PASS", result)
            self.assertEqual(result["checks"]["callback_timeline"], "PASS")

    def test_running_disconnect_fails(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            _, result, _, _ = self.setup_run(root, "disconnect")
            self.assertEqual(result["checks"]["no_disconnect"], "FAIL")

    def test_forged_stopping_phase_during_connect_rejected(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, selection, _ = self.healthy(root)
            for name in ("callbacks.jsonl", "callback_attempts.jsonl"):
                rows = journal.rows(name)
                rows[0].update(event="disconnected", phase="stopping")
                self.rows_write(journal, name, rows)
            decision = self.decision(journal, selection)
            self.assertEqual(decision["checks"]["callback_timeline"], "FAIL")
            self.rehash(journal)
            self.assertEqual(profile.consume(journal.directory, selection)["status"], "FAIL")

    def test_missing_stop_boundary_rejected(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, selection, _ = self.healthy(root)
            self.rows_write(journal, "worker_events.jsonl", [r for r in journal.rows("worker_events.jsonl") if r["event"] != "stop_begin"])
            self.assertEqual(self.decision(journal, selection)["checks"]["callback_timeline"], "FAIL")

    def test_callback_overflow_and_capture_error_fail(self):
        for scenario in ("callback_overflow", "capture_error", "callback_error"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
                _, result, _, _ = self.setup_run(root, scenario, overrides={"callback_capacity": 2})
                self.assertEqual(result["status"], "FAIL", result)

    def test_late_callback_after_worker_snapshot_fails(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            selection = selection_fixture(root)
            sdk = FixtureSDK()
            directory, result = supervisor.run_fixture(root, selection, ACCOUNT, lambda: sdk,
                after_worker=lambda: sdk.trader._callback.on_disconnected())
            self.assertEqual(result["status"], "FAIL", result)
            self.assertTrue(any(row["event"] == "late_callback_failure" for row in profile.Journal(directory).rows("worker_events.jsonl")))

    def test_primary_and_cleanup_errors_separate(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, result, _, _ = self.setup_run(root, "primary_and_stop_error")
            facts = journal.read("worker_result.json")
            self.assertIsNotNone(facts["primary_failure"])
            self.assertIsNotNone(facts["cleanup_failure"])
            self.assertEqual(result["status"], "FAIL")
            self.assertTrue({"primary_failure", "cleanup_failure"} <= {r["kind"] for r in result["failures"]})

    def test_evidence_write_error_does_not_replace_primary_or_stop_error(self):
        original = profile.Journal.append
        def fail_record(journal, name, row):
            if name == "steps.jsonl" and row.get("name") == "connect" and row.get("phase") == "end":
                raise OSError(ACCOUNT)
            return original(journal, name, row)
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            with mock.patch.object(profile.Journal, "append", new=fail_record):
                journal, result, _, _ = self.setup_run(root, "primary_and_stop_error")
            facts = journal.read("worker_result.json")
            self.assertIsNotNone(facts["primary_failure"])
            self.assertIsNotNone(facts["cleanup_failure"])
            self.assertGreater(facts["evidence_failure_count"], 0)
            self.assertEqual(result["status"], "FAIL")

    def test_worker_timeout_cannot_publish_pass(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, result, _, _ = self.setup_run(root, process_behavior="worker_timeout", overrides={"total_timeout_seconds": 0.1})
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["completion"], "INCOMPLETE")
            self.assertFalse(journal.path("COMPLETE").exists())

    def test_unconfirmed_exit_refuses_complete(self):
        process = runtime.create_fixture_process("survivor")
        facts = runtime.cleanup_owned(process)
        self.assertFalse(facts["exited"])
        self.assertEqual(process.terminations, 1)
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, result, _, _ = self.setup_run(root, process_behavior="survivor", overrides={"total_timeout_seconds": 0.1})
            self.assertEqual(result["status"], "FAIL")
            self.assertFalse(journal.path("COMPLETE").exists())

    def test_cannot_control_unowned_fake_or_arbitrary_pid(self):
        for process in (runtime.FixtureProcess("normal"), SimpleNamespace(pid=os.getpid()), None):
            with self.subTest(kind=type(process).__name__), self.assertRaises(loader.ContractViolation):
                runtime.cleanup_owned(process)

    def test_real_process_launcher_disabled(self):
        with self.assertRaises(loader.RealSDKDisabled):
            runtime.create_fixture_process(mode="real")

    def test_worker_crash_is_not_pass(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            _, result, _, _ = self.setup_run(root, process_behavior="worker_crash")
            self.assertEqual(result["status"], "FAIL")

    def test_finite_slow_call_fails_post_exit_duration_check(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            _, result, _, _ = self.setup_run(root, "slow_query", overrides={"step_timeout_seconds": 0.02})
            self.assertEqual(result["status"], "FAIL", result)
            self.assertEqual(result["checks"]["deadline"], "FAIL")

    def test_publish_hash_stage_inside_run_deadline(self):
        original, reached = profile.Journal.hashes, []
        def delayed(journal):
            if not reached:
                reached.append(True)
                self.assertGreater(journal.deadline.remaining(), 0)
                time.sleep(journal.deadline.remaining() + 0.05)
            return original(journal)
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            with mock.patch.object(profile.Journal, "hashes", new=delayed):
                journal, result, selection, _ = self.setup_run(root, overrides={"total_timeout_seconds": 3.0})
            self.assertTrue(reached)
            self.assertEqual(result["completion"], "INCOMPLETE")
            self.assertFalse(journal.path("COMPLETE").exists())
            self.assertEqual(profile.consume(journal.directory, selection)["status"], "FAIL")

    def test_late_complete_is_revoked(self):
        original = profile.Journal.complete
        def delayed(journal, marker):
            original(journal, marker)
            time.sleep(1.1)
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            with mock.patch.object(profile.Journal, "complete", new=delayed):
                journal, result, selection, _ = self.setup_run(root, overrides={"total_timeout_seconds": 1.0})
            self.assertEqual(result["status"], "FAIL")
            self.assertFalse(journal.path("COMPLETE").exists())
            self.assertEqual(profile.consume(journal.directory, selection)["status"], "FAIL")

    def test_health_sources_are_explicit_simulations(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, _, _ = self.healthy(root)
            rows = journal.rows("client_health.jsonl")
            self.assertEqual({r["source"] for r in rows}, {"os", "sdk", "human"})
            self.assertTrue(all(r["simulation"] is True for r in rows))
            self.assertTrue(all(r["origin"] == runtime.ORIGINS[r["source"]] for r in rows))

    def test_human_cannot_be_relabelled_as_os_or_sdk(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, selection, _ = self.healthy(root)
            rows = journal.rows("client_health.jsonl")
            rows[1]["source"] = "os"
            self.rows_write(journal, "client_health.jsonl", rows)
            decision = self.decision(journal, selection)
            self.assertEqual(decision["checks"]["health"], "FAIL")
            self.rehash(journal)
            self.assertEqual(profile.consume(journal.directory, selection)["status"], "FAIL")

    def test_health_failure_and_unknown_are_distinct(self):
        for status in ("FAIL", "NOT_COVERED"):
            with self.subTest(status=status), tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
                _, result, _, _ = self.setup_run(root, health_status=status)
                self.assertEqual(result["status"], status, result)

    def test_required_health_policy_cannot_be_lowered(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, selection, _ = self.healthy(root)
            manifest = journal.read("manifest.json")
            manifest["health_required"] = []
            self.json_write(journal, "manifest.json", manifest)
            self.assertEqual(self.decision(journal, selection)["checks"]["manifest"], "FAIL")

    def test_complete_valid_business_fail(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, result, selection, _ = self.setup_run(root, "stop_error")
            self.assertEqual(result["completion"], "COMPLETE", result)
            self.assertEqual(result["status"], "FAIL")
            self.assertNotIn("status", journal.read("COMPLETE"))
            self.assertEqual(profile.consume(journal.directory, selection)["status"], "FAIL")

    def test_complete_valid_business_not_covered(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, result, selection, _ = self.setup_run(root, "query_empty")
            self.assertEqual(result["completion"], "COMPLETE", result)
            self.assertEqual(profile.consume(journal.directory, selection)["status"], "NOT_COVERED")

    def test_required_check_missing_cannot_pass(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, selection, _ = self.healthy(root)
            manifest = journal.read("manifest.json")
            manifest["required_checks"].remove("callback_timeline")
            self.json_write(journal, "manifest.json", manifest)
            self.rehash(journal)
            self.assertEqual(profile.consume(journal.directory, selection)["status"], "FAIL")

    def test_missing_every_required_file_refuses_consumption(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, selection, _ = self.healthy(root)
            for name in profile.PAYLOAD | {"hashes.json", "COMPLETE"}:
                with self.subTest(name=name):
                    path, data = journal.path(name), journal.path(name).read_bytes()
                    path.unlink()
                    self.assertEqual(profile.consume(journal.directory, selection)["completion"], "INCOMPLETE")
                    path.write_bytes(data)

    def test_hash_inventory_missing_item_with_rebound_marker(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, selection, _ = self.healthy(root)
            hashes = journal.read("hashes.json")
            del hashes["files"]["sdk_inventory.json"]
            self.json_write(journal, "hashes.json", hashes)
            marker = journal.read("COMPLETE")
            marker["hashes_sha256"] = hashlib.sha256(journal.path("hashes.json").read_bytes()).hexdigest()
            self.json_write(journal, "COMPLETE", marker)
            self.assertEqual(profile.consume(journal.directory, selection)["completion"], "INCOMPLETE")

    def test_artifact_inventory_has_independent_literal_oracle(self):
        self.assertEqual(profile.PAYLOAD, {"manifest.json", "steps.jsonl", "snapshots.jsonl", "callbacks.jsonl",
            "callback_attempts.jsonl", "worker_events.jsonl", "worker_result.json", "process_checks.jsonl",
            "channel_events.jsonl", "bootstrap_events.jsonl", "sdk_inventory.json", "import_effects.jsonl",
            "client_health.jsonl", "normalized.jsonl", "summary.candidate.json", "closure_state.json"})

    def test_hashes_are_actual_bytes_not_newline_normalized_text(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, selection, _ = self.healthy(root)
            path = journal.path("steps.jsonl")
            path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
            self.assertEqual(profile.consume(journal.directory, selection)["completion"], "INCOMPLETE")

    def test_truncated_jsonl_rejected_even_with_fresh_hashes(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, selection, _ = self.healthy(root)
            path = journal.path("steps.jsonl")
            path.write_bytes(path.read_bytes().rstrip(b"\n"))
            self.rehash(journal)
            self.assertEqual(profile.consume(journal.directory, selection)["completion"], "INCOMPLETE")

    def test_unknown_artifact_prevents_consumption(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, selection, _ = self.healthy(root)
            (journal.directory / "unexpected.txt").write_text("fixture", encoding="utf-8")
            self.assertEqual(profile.consume(journal.directory, selection)["completion"], "INCOMPLETE")

    def test_completion_seal_refuses_writes(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, _, _ = self.healthy(root)
            with self.assertRaises(loader.ContractViolation):
                journal.append("steps.jsonl", {**clock()})
            with self.assertRaises(loader.ContractViolation):
                journal.write("../outside", {})

    def test_unknown_effects_are_not_safe(self):
        for scenario in ("effect_network", "effect_unknown"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
                _, result, _, _ = self.setup_run(root, scenario)
                self.assertEqual(result["status"], "FAIL", result)
                self.assertEqual(result["checks"]["side_effects"], "FAIL")

    def test_unapproved_file_userdata_effects_do_not_inherit_approval(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, selection, _ = self.healthy(root)
            rows = journal.rows("import_effects.jsonl")
            rows[0]["effect"] = "userdata_write"
            rows[0]["approved"] = True
            self.rows_write(journal, "import_effects.jsonl", rows)
            self.assertEqual(self.decision(journal, selection)["checks"]["side_effects"], "FAIL")

    def test_clean_summary_cannot_hide_callback_raw_capture_error(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, selection, _ = self.healthy(root)
            callbacks, attempts = journal.rows("callbacks.jsonl"), journal.rows("callback_attempts.jsonl")
            callbacks[1]["raw"]["items"][0]["capture_errors"] = [{"error_type": "builtins.ValueError"}]
            attempts[1]["raw"] = callbacks[1]["raw"]
            self.rows_write(journal, "callbacks.jsonl", callbacks)
            self.rows_write(journal, "callback_attempts.jsonl", attempts)
            self.assertEqual(self.decision(journal, selection)["status"], "FAIL")
            self.rehash(journal)
            self.assertEqual(profile.consume(journal.directory, selection)["status"], "FAIL")

    def test_worker_failure_without_event_is_source_conflict_not_digest_failure(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, selection, _ = self.healthy(root)
            worker = journal.read("worker_result.json")
            worker["cleanup_failure"] = {"error_type": "builtins.RuntimeError"}
            self.bind_report(journal, worker)
            result = self.decision(journal, selection)
            self.assertEqual(result["checks"]["source_consistency"], "FAIL")
            self.assertEqual(result["checks"]["raw_evidence"], "PASS")
            self.assertTrue(any(r["kind"] == "cleanup_failure" for r in result["failures"]))

    def test_missing_process_fields_cannot_pass(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, selection, _ = self.healthy(root)
            rows = journal.rows("process_checks.jsonl")
            del rows[0]["cleanup_errors"]
            self.rows_write(journal, "process_checks.jsonl", rows)
            self.assertEqual(self.decision(journal, selection)["checks"]["process_schema"], "FAIL")
            self.rehash(journal)
            self.assertEqual(profile.consume(journal.directory, selection)["completion"], "INCOMPLETE")

    def test_process_wrong_truthiness_type_cannot_pass(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, selection, _ = self.healthy(root)
            rows = journal.rows("process_checks.jsonl")
            rows[0]["cleanup_errors"] = False
            self.rows_write(journal, "process_checks.jsonl", rows)
            self.assertEqual(self.decision(journal, selection)["checks"]["process_schema"], "FAIL")

    def test_real_sdk_still_not_imported(self):
        self.assertFalse(any(name == "xtquant" or name.startswith("xtquant.") for name in sys.modules))

    def test_unknown_callback_event_cannot_pass_with_valid_payload_and_hashes(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, selection, _ = self.healthy(root)
            for name in ("callbacks.jsonl", "callback_attempts.jsonl"):
                rows = journal.rows(name)
                rows[1]["event"] = "unreviewed_event"
                self.rows_write(journal, name, rows)
            self.assertEqual(self.decision(journal, selection)["status"], "FAIL")
            self.rehash(journal)
            self.assertEqual(profile.consume(journal.directory, selection)["status"], "FAIL")

    def test_mutated_account_cannot_reach_query(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, result, _, sdk = self.setup_run(root, "mutated_account")
            self.assertEqual(result["status"], "FAIL")
            self.assertNotIn("query_asset", sdk.calls)
            self.assertTrue(any(row["event"] == "scope_violation" for row in journal.rows("worker_events.jsonl")))

    def test_bootstrap_wrong_duration_type_cannot_be_ignored(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, selection, _ = self.healthy(root)
            rows = journal.rows("bootstrap_events.jsonl")
            rows[1]["elapsed_ms"] = False
            self.rows_write(journal, "bootstrap_events.jsonl", rows)
            self.assertEqual(self.decision(journal, selection)["checks"]["bootstrap"], "FAIL")
            self.rehash(journal)
            self.assertEqual(profile.consume(journal.directory, selection)["status"], "FAIL")

    def test_slow_factory_import_model_has_step_deadline(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            selection = selection_fixture(root)
            def factory():
                time.sleep(0.04)
                return FixtureSDK()
            _, result = supervisor.run_fixture(root, selection, ACCOUNT, factory,
                overrides={"step_timeout_seconds": 0.02, "observation_seconds": 0})
            self.assertEqual(result["status"], "FAIL", result)
            self.assertEqual(result["checks"]["bootstrap"], "FAIL")

    def test_post_health_is_inside_original_run_deadline(self):
        original, reached = supervisor.fixture_health, []
        def delayed(phase, *args):
            if phase == "post" and not reached:
                reached.append(True)
                time.sleep(1.1)
            return original(phase, *args)
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            with mock.patch.object(supervisor, "fixture_health", new=delayed):
                journal, result, _, _ = self.setup_run(root, overrides={"total_timeout_seconds": 1.0})
            self.assertTrue(reached)
            self.assertEqual(result["status"], "FAIL")
            self.assertFalse(journal.path("COMPLETE").exists())

    def test_final_consume_does_not_get_new_producer_budget(self):
        original, reached = profile.consume, []
        def delayed(directory, selection, deadline=None):
            result = original(directory, selection, deadline)
            self.assertEqual(result["status"], "PASS")
            self.assertGreater(deadline.remaining(), 0)
            reached.append(True)
            time.sleep(deadline.remaining() + 0.05)
            return result
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            with mock.patch.object(profile, "consume", new=delayed):
                journal, result, _, _ = self.setup_run(root, overrides={"total_timeout_seconds": 3.0})
            self.assertTrue(reached)
            self.assertEqual(result["status"], "FAIL")
            self.assertFalse(journal.path("COMPLETE").exists())

    def test_raw_stderr_vs_clean_channel_summary_is_conflict(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, selection, _ = self.healthy(root)
            rows = journal.rows("channel_events.jsonl")
            row = {k: v for k, v in rows[0].items() if k not in {"authenticated", "report_sha256"}}
            row.update(event="stderr_bytes", count=5)
            rows.insert(1, row)
            for index, entry in enumerate(rows, 1):
                entry["sequence"] = index
            self.rows_write(journal, "channel_events.jsonl", rows)
            decision = self.decision(journal, selection)
            self.assertEqual(decision["status"], "FAIL")
            self.assertEqual(decision["checks"]["source_consistency"], "FAIL")

    def test_invalid_json_payload_is_incomplete_not_complete_fail(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, selection, _ = self.healthy(root)
            journal.path("worker_result.json").write_bytes(b"{broken\n")
            self.rehash(journal)
            self.assertEqual(profile.consume(journal.directory, selection)["completion"], "INCOMPLETE")

    def assert_payload_binding(self, journal, *, complete=True):
        """Independent literal byte checks; no verdict helper as the oracle."""
        self.assertEqual(set(journal.read("hashes.json")["files"]), set(profile.PAYLOAD))
        for name, value in journal.read("hashes.json")["files"].items():
            self.assertEqual(hashlib.sha256(journal.path(name).read_bytes()).hexdigest(), value)
        if complete:
            self.assertEqual(journal.read("COMPLETE")["hashes_sha256"],
                             hashlib.sha256(journal.path("hashes.json").read_bytes()).hexdigest())
        packet = next(row for row in journal.rows("channel_events.jsonl") if row["event"] == "report")
        self.assertEqual(packet["report_sha256"], hashlib.sha256(json.dumps(journal.read("worker_result.json"),
            sort_keys=True, ensure_ascii=True, allow_nan=False).encode()).hexdigest())

    def reseal_decision(self, journal, selection):
        decision = self.decision(journal, selection)
        self.json_write(journal, "summary.candidate.json", {"publication": "CANDIDATE",
            "manifest_digest": profile.digest(journal.read("manifest.json")), "decision": decision})
        self.rehash(journal)
        self.assert_payload_binding(journal)
        return decision

    def test_callback_after_report_serialization_is_independently_latched(self):
        original = runtime.FixtureProcess.start
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            sdk = FixtureSDK()
            def late(process, *args, **kwargs):
                original(process, *args, **kwargs)
                self.assertIsNotNone(process.packet)
                sdk.trader._callback.on_disconnected()
            with mock.patch.object(runtime.FixtureProcess, "start", new=late):
                directory, result = supervisor.run_fixture(root, selection_fixture(root), ACCOUNT, lambda: sdk)
            journal = profile.Journal(directory)
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["reason"], "callback_closure_failure")
            self.assertEqual(result["completion"], "INCOMPLETE")
            self.assertEqual(result["publication_failure"], "live_closure_not_clean")
            self.assertFalse(journal.path("COMPLETE").exists())
            self.assertEqual(journal.read("worker_result.json")["callbacks"]["late_count"], 0)
            self.assert_payload_binding(journal, complete=False)

    def complete_callback_case(self, root, *, capture_failure=False, break_veto=False, boundary="complete"):
        sdk, rejected, payloads = FixtureSDK(), [], []
        original_complete, original_append, original_consume, original_veto = (
            profile.Journal.complete, profile.Journal.append, profile.consume, profile.closure_veto)
        def emit(journal):
            payloads.append({name: journal.path(name).read_bytes() for name in profile.PAYLOAD | {"hashes.json", "COMPLETE"}})
            if capture_failure:
                class Broken:
                    @property
                    def status(self):
                        raise ValueError(ACCOUNT)
                sdk.trader._callback.on_account_status(Broken())
            else:
                sdk.trader._callback.on_disconnected()
        def sealed_append(journal, name, row):
            try:
                return original_append(journal, name, row)
            except loader.ContractViolation:
                if journal.path("COMPLETE").exists():
                    rejected.append(name)
                raise
        def late_complete(journal, marker):
            original_complete(journal, marker)
            if boundary == "complete":
                emit(journal)
        def late_consume(directory, selection, deadline=None):
            if boundary == "consume":
                emit(profile.Journal(directory))
            return original_consume(directory, selection, deadline)
        def last_veto(journal, decision):
            if boundary == "last_veto" and decision.get("completion") == "COMPLETE" and not payloads:
                emit(journal)
            return original_veto(journal, decision)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(profile.Journal, "complete", new=late_complete))
            stack.enter_context(mock.patch.object(profile.Journal, "append", new=sealed_append))
            stack.enter_context(mock.patch.object(profile, "consume", new=late_consume))
            stack.enter_context(mock.patch.object(profile, "closure_veto", new=last_veto))
            if break_veto:
                stack.enter_context(mock.patch.object(profile.Journal, "write_callback_veto", side_effect=OSError("fixture")))
            selection = selection_fixture(root)
            directory, result = supervisor.run_fixture(root, selection, ACCOUNT, lambda: sdk)
        journal = profile.Journal(directory)
        # Keep the live latch reachable for this fixture's follow-up assertions.
        journal._fixture_closure = profile._LIVE_CLOSURES[str(directory)]
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["reason"], "callback_closure_failure")
        if not break_veto:
            self.assertEqual(result["completion"], "COMPLETE")
            self.assertTrue(rejected)
            self.assertEqual(payloads[0], {name: journal.path(name).read_bytes() for name in payloads[0]})
            self.assert_payload_binding(journal)
            self.assertEqual(original_consume(directory, selection)["reason"], "callback_closure_failure")
        else:
            self.assertFalse(journal.path("COMPLETE").exists())
            self.assertEqual(original_consume(directory, selection)["status"], "FAIL")
        return journal, selection

    def test_callback_after_complete_before_final_consume_vetoes_valid_package(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            self.complete_callback_case(root)

    def test_sealed_journal_callback_evidence_failure_has_independent_veto(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _ = self.complete_callback_case(root)
            live = profile._LIVE_CLOSURES[str(journal.directory)]
            self.assertIn("callback_evidence_error", live.failures)
            self.assertEqual(journal.read("worker_result.json")["evidence_failure_count"], 0)

    def test_callback_capture_failure_after_complete_vetoes_valid_package(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _ = self.complete_callback_case(root, capture_failure=True)
            self.assertIn("callback_evidence_error", profile._LIVE_CLOSURES[str(journal.directory)].failures)

    def test_callback_at_final_consumer_entry_vetoes_valid_package(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            self.complete_callback_case(root, boundary="consume")

    def test_callback_at_last_consumer_veto_check_keeps_hashes_valid(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            self.complete_callback_case(root, boundary="last_veto")

    def test_final_supervisor_veto_does_not_escape_total_deadline(self):
        original, reached = supervisor.closure_veto, []
        def slow(journal, decision):
            result = original(journal, decision)
            reached.append(True)
            time.sleep(journal.deadline.remaining() + 0.05)
            return result
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            with mock.patch.object(supervisor, "closure_veto", new=slow):
                journal, result, selection, _ = self.setup_run(root, overrides={"total_timeout_seconds": 3.0})
            self.assertTrue(reached)
            self.assertEqual(result["status"], "FAIL")
            self.assertFalse(journal.path("COMPLETE").exists())
            self.assertEqual(profile.consume(journal.directory, selection)["status"], "FAIL")

    def test_retained_callback_descriptor_is_not_executed_after_worker_return(self):
        hits, original_guard, original_complete = [], loader.DenyRealSDK.find_spec, profile.Journal.complete
        def guard(instance, fullname, *args, **kwargs):
            if fullname == "xtquant":
                hits.append(fullname)
            return original_guard(instance, fullname, *args, **kwargs)
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            sdk = FixtureSDK()
            class CallbackValue:
                @property
                def status(self):
                    __import__("xtquant")
            def late(journal, marker):
                original_complete(journal, marker)
                sdk.trader._callback.on_account_status(CallbackValue())
            with mock.patch.object(profile.Journal, "complete", new=late), mock.patch.object(loader.DenyRealSDK, "find_spec", new=guard):
                directory, result = supervisor.run_fixture(root, selection_fixture(root), ACCOUNT, lambda: sdk)
            self.assertEqual(hits, [])  # The unreviewed descriptor is never invoked.
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["reason"], "callback_closure_failure")
            self.assert_payload_binding(profile.Journal(directory))

    def test_retained_callback_capture_cannot_leak_parent_stdout_stderr(self):
        original, output, errors = profile.Journal.complete, io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            sdk = FixtureSDK()
            class CallbackValue:
                @property
                def status(self):
                    print(ACCOUNT)
                    print("PRIVATE_LATE_CALLBACK_PATH", file=sys.stderr)
                    raise ValueError(ACCOUNT)
            def late(journal, marker):
                original(journal, marker)
                sdk.trader._callback.on_account_status(CallbackValue())
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), mock.patch.object(
                    profile.Journal, "complete", new=late):
                directory, result = supervisor.run_fixture(root, selection_fixture(root), ACCOUNT, lambda: sdk)
            journal = profile.Journal(directory)
            self.assertEqual(result["reason"], "callback_closure_failure")
            self.assertEqual(result["status"], "FAIL")
            text = output.getvalue() + errors.getvalue() + "".join(p.read_text(encoding="utf-8") for p in directory.iterdir())
            self.assertNotIn(ACCOUNT, text)
            self.assertNotIn("PRIVATE_LATE_CALLBACK_PATH", text)
            self.assert_payload_binding(journal)

    def test_latch_persist_failure_revokes_marker_and_never_passes(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            self.complete_callback_case(root, break_veto=True)

    def test_persisted_callback_veto_survives_without_live_registry(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, selection = self.complete_callback_case(root)
            live = profile._LIVE_CLOSURES.pop(str(journal.directory))
            try:
                result = profile.consume(journal.directory, selection)
                self.assertEqual(result["status"], "FAIL")
                self.assertEqual(result["reason"], "callback_closure_failure")
                self.assertEqual(result["completion"], "COMPLETE")
            finally:
                profile._LIVE_CLOSURES[str(journal.directory)] = live

    def test_missing_closure_snapshot_cannot_consume(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, selection, _ = self.healthy(root)
            journal.path("closure_state.json").unlink()
            self.assertEqual(profile.consume(journal.directory, selection)["completion"], "INCOMPLETE")

    def test_wrong_closure_snapshot_types_fail_closed_with_fresh_hashes(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, selection, _ = self.healthy(root)
            state = journal.read("closure_state.json")
            state["report_serialized"] = 1
            self.json_write(journal, "closure_state.json", state)
            self.reseal_decision(journal, selection)
            self.assertEqual(profile.consume(journal.directory, selection)["status"], "FAIL")

    def stop_conflict_case(self, root, mutate, scenario="normal"):
        journal, _, selection, _ = self.setup_run(root, scenario)
        worker, steps, events = journal.read("worker_result.json"), journal.rows("steps.jsonl"), journal.rows("worker_events.jsonl")
        mutate(worker, steps, events)
        self.bind_report(journal, worker)
        self.rows_write(journal, "steps.jsonl", steps)
        self.rows_write(journal, "worker_events.jsonl", events)
        decision = self.reseal_decision(journal, selection)
        self.assertEqual(decision["checks"]["lifecycle_consistency"], "FAIL")
        self.assertIn({"kind": "lifecycle_source_conflict"}, decision["failures"])
        result = profile.consume(journal.directory, selection)
        self.assertEqual(result["completion"], "COMPLETE", result)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn({"kind": "lifecycle_source_conflict"}, result["failures"])
        return decision

    def test_raw_stop_return_true_summary_false_is_lifecycle_conflict(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            result = self.stop_conflict_case(root, lambda w, s, e: w.update(stop_returned=False))
            self.assertEqual(result["checks"]["raw_evidence"], "PASS")
            self.assertEqual(result["checks"]["source_consistency"], "FAIL")

    def test_raw_stop_failure_summary_true_is_lifecycle_conflict(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            self.stop_conflict_case(root, lambda w, s, e: w.update(stop_returned=True), "stop_error")

    def test_stop_attempted_false_with_actual_stop_is_lifecycle_conflict(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            self.stop_conflict_case(root, lambda w, s, e: w.update(stop_attempted=False))

    def test_stop_attempted_true_missing_actual_stop_is_lifecycle_conflict(self):
        def missing(worker, steps, events):
            steps[:] = [r for r in steps if r["name"] != "stop"]
            events[:] = [r for r in events if r["event"] not in {"stop_begin", "stop_end"}]
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            self.stop_conflict_case(root, missing)

    def test_stop_error_and_cleanup_summary_mismatch_is_lifecycle_conflict(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            self.stop_conflict_case(root, lambda w, s, e: w.update(cleanup_failure=None), "stop_error")

    def test_stop_boundary_time_conflict_has_target_reason(self):
        def reorder(worker, steps, events):
            begin = next(r for r in events if r["event"] == "stop_begin")
            begin["monotonic_ns"] = next(r for r in steps if r["name"] == "connect")["monotonic_ns"]
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            self.stop_conflict_case(root, reorder)

    def test_harness_import_guard_independently_fires_before_outer_guard(self):
        own, outer = [], []
        own_original, outer_original = loader.DenyRealSDK.find_spec, DenySDK.find_spec
        def own_guard(guard, fullname, *args, **kwargs):
            if fullname == "xtquant":
                own.append(fullname)
            return own_original(guard, fullname, *args, **kwargs)
        def outer_guard(guard, fullname, *args, **kwargs):
            if fullname == "xtquant":
                outer.append(fullname)
            return outer_original(guard, fullname, *args, **kwargs)
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            with mock.patch.object(loader.DenyRealSDK, "find_spec", new=own_guard), mock.patch.object(DenySDK, "find_spec", new=outer_guard):
                directory, result = supervisor.run_fixture(root, selection_fixture(root), ACCOUNT, lambda: __import__("xtquant"))
            self.assertEqual(own, ["xtquant"])
            self.assertEqual(outer, [])
            self.assertEqual(result["status"], "FAIL")
            self.assertTrue(any(r["event"] == "real_sdk_import_denied" and r["facts"] == {
                "reason": "offline_real_sdk_guard"} for r in profile.Journal(directory).rows("worker_events.jsonl")))

    def test_swallowed_import_denial_still_has_persistent_failure_event(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            def factory():
                try:
                    __import__("xtquant")
                except loader.RealSDKDisabled:
                    pass
                return FixtureSDK()
            directory, result = supervisor.run_fixture(root, selection_fixture(root), ACCOUNT, factory)
            self.assertEqual(result["status"], "FAIL")
            self.assertIn("real_sdk_import_denied", {r["kind"] for r in result["failures"]})
            self.assert_payload_binding(profile.Journal(directory))

    def test_during_health_is_bound_to_original_query_not_post_stop(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, result, _, _ = self.healthy(root)
            health = journal.rows("client_health.jsonl")
            during = next(r for r in health if r["phase"] == "during_lifecycle")
            asset = next(r for r in journal.rows("snapshots.jsonl") if r["domain"] == "account")
            stop = next(r for r in journal.rows("worker_events.jsonl") if r["event"] == "stop_begin")
            self.assertEqual(during["monotonic_ns"], asset["monotonic_ns"])
            self.assertEqual(during["basis_step_id"], asset["step_id"])
            self.assertLess(during["monotonic_ns"], stop["monotonic_ns"])
            post = next(r for r in health if r["phase"] == "post_stop")
            self.assertEqual(post["status"], "NOT_COVERED")
            self.assertEqual(post["basis"], "not_performed")
            self.assertIs(post["simulation"], True)
            self.assertEqual(result["POST_STOP_SDK"], "NOT_COVERED")

    def test_post_stop_sdk_pass_claim_is_rejected_with_valid_hashes(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, selection, _ = self.healthy(root)
            rows = journal.rows("client_health.jsonl")
            next(r for r in rows if r["phase"] == "post_stop")["status"] = "PASS"
            self.rows_write(journal, "client_health.jsonl", rows)
            decision = self.reseal_decision(journal, selection)
            self.assertEqual(decision["checks"]["health"], "FAIL")
            self.assertEqual(profile.consume(journal.directory, selection)["status"], "FAIL")

    def test_during_health_timestamp_cannot_be_relabelled_as_post(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, selection, _ = self.healthy(root)
            rows = journal.rows("client_health.jsonl")
            next(r for r in rows if r["phase"] == "during_lifecycle")["phase"] = "post"
            self.rows_write(journal, "client_health.jsonl", rows)
            self.reseal_decision(journal, selection)
            self.assertEqual(profile.consume(journal.directory, selection)["checks"]["health"], "FAIL")

    def test_context_update_binds_current_bytes_and_distinct_baseline(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, _, _ = self.healthy(root)
            entry = journal.read("manifest.json")["source_bindings"]["context"]["docs/PROJECT_CONTEXT.md"]
            self.assertEqual(entry["binding_state"], "OFFLINE_CONTEXT_UPDATE")
            self.assertEqual(entry["sha256"], hashlib.sha256((profile.ROOT / "docs/PROJECT_CONTEXT.md").read_bytes()).hexdigest())
            baseline = profile._git(["cat-file", "blob", profile.CONTEXT_COMMIT + ":docs/PROJECT_CONTEXT.md"], RunDeadline(clock(), 30))
            self.assertEqual(entry["baseline_sha256"], hashlib.sha256(baseline).hexdigest())
            self.assertEqual(entry["baseline_git_blob"], hashlib.sha1(b"blob " + str(len(baseline)).encode() + b"\0" + baseline).hexdigest())

    def precomplete_failure_case(self, root, *, veto_fails=True):
        sdk, injected, before, stages = FixtureSDK(), [], {}, []
        original_hashes, original_append, original_gate = profile.Journal.hashes, profile.Journal.append, profile.CallbackClosure.publication_gate
        inside = []
        def broken_callback_write(journal, name, row):
            if inside and name in {"worker_events.jsonl", "callback_attempts.jsonl", "callbacks.jsonl"}:
                raise OSError("isolated callback evidence fault")
            return original_append(journal, name, row)
        def before_hashes(journal):
            if not injected:
                injected.append(True)
                self.assertFalse(journal.path("COMPLETE").exists())
                self.assertEqual(journal.read("closure_state.json")["failures"], [])
                before.update({name: journal.path(name).read_bytes() for name in profile.PAYLOAD})
                inside.append(True)
                try:
                    sdk.trader._callback.on_disconnected()
                finally:
                    inside.clear()
            return original_hashes(journal)
        def observe_gate(live, journal, candidate, staged):
            stages.append(staged)
            return original_gate(live, journal, candidate, staged)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(profile.Journal, "append", new=broken_callback_write))
            stack.enter_context(mock.patch.object(profile.Journal, "hashes", new=before_hashes))
            stack.enter_context(mock.patch.object(profile.CallbackClosure, "publication_gate", new=observe_gate))
            if veto_fails:
                stack.enter_context(mock.patch.object(profile.Journal, "write_callback_veto", side_effect=OSError("isolated veto fault")))
            selection = selection_fixture(root)
            directory, result = supervisor.run_fixture(root, selection, ACCOUNT, lambda: sdk)
        journal = profile.Journal(directory)
        live = profile._LIVE_CLOSURES[str(directory)]
        self.assertTrue(injected)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["completion"], "INCOMPLETE")
        self.assertEqual(result["publication_failure"], "unpersisted_closure_failure" if veto_fails else "live_closure_not_clean")
        self.assertIs(live.publish_denied, True)
        self.assertIs(live.unpersisted_failure, veto_fails)
        self.assertFalse(journal.path("COMPLETE").exists())
        self.assertFalse(journal.path("COMPLETE.tmp").exists())
        self.assertEqual(before, {name: journal.path(name).read_bytes() for name in profile.PAYLOAD})
        self.assert_payload_binding(journal, complete=False)
        self.assertEqual(journal.read("summary.candidate.json")["decision"]["status"], "PASS")
        self.assertEqual(stages[0]["completion"], "CANDIDATE")
        self.assertEqual(stages[0]["status"], "FAIL")
        self.assertEqual(stages[0]["reason"], "callback_closure_failure")
        self.assertEqual(stages[0]["checks"]["raw_evidence"], "PASS")
        self.assertEqual(stages[0]["checks"]["source_consistency"], "PASS")
        self.assertEqual(stages[0]["checks"]["all_failure_sources"], "FAIL")
        return journal, selection, live

    def test_precomplete_double_write_failure_never_creates_complete(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, selection, _ = self.precomplete_failure_case(root)
            self.assertFalse(journal.path("CALLBACK_FAILED").exists())
            self.assertEqual(profile.consume(journal.directory, selection)["status"], "FAIL")

    def test_precomplete_double_write_failure_cannot_pass_fresh_consumer(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, selection, live = self.precomplete_failure_case(root)
            profile._LIVE_CLOSURES.pop(str(journal.directory))
            try:
                result = profile.consume(journal.directory, selection)
                self.assertEqual(result["status"], "FAIL")
                self.assertEqual(result["completion"], "INCOMPLETE")
                self.assertFalse(journal.path("COMPLETE").exists())
            finally:
                profile._LIVE_CLOSURES[str(journal.directory)] = live

    def test_unpersisted_failure_cannot_recover_publish_after_writes_recover(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, live = self.precomplete_failure_case(root)
            live.trip("late_callback")  # Writes now work, but fatal state must stay sticky.
            self.assertTrue(journal.path("CALLBACK_FAILED").exists())
            self.assertIs(live.unpersisted_failure, True)
            self.assertIs(live.publish_denied, True)
            with live.lock:
                self.assertEqual(live.publication_gate(journal, {"status": "PASS"}, {
                    "status": "PASS", "completion": "CANDIDATE"}), "unpersisted_closure_failure")
            self.assertFalse(journal.path("COMPLETE").exists())

    def test_precomplete_persisted_callback_veto_also_denies_publication(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, selection, _ = self.precomplete_failure_case(root, veto_fails=False)
            self.assertTrue(journal.path("CALLBACK_FAILED").exists())
            self.assertEqual(profile.consume(journal.directory, selection)["status"], "FAIL")

    def test_publish_gate_rejects_staged_fail_for_pass_candidate(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, _, sdk = self.healthy(root)
            live = profile._LIVE_CLOSURES[str(journal.directory)]
            with live.lock:
                self.assertEqual(live.publication_gate(journal, {"status": "PASS"}, {
                    "status": "FAIL", "completion": "CANDIDATE"}), "staged_decision_conflict")
                self.assertIs(live.publish_denied, True)
                self.assertEqual(live.publication_gate(journal, {"status": "PASS"}, {
                    "status": "PASS", "completion": "CANDIDATE"}), "publication_already_denied")

    def test_publish_gate_requires_successfully_persisted_snapshot(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, _, _, sdk = self.healthy(root)
            live = profile._LIVE_CLOSURES[str(journal.directory)]
            live.persisted_snapshot_digest = None
            with live.lock:
                self.assertEqual(live.publication_gate(journal, {"status": "PASS"}, {
                    "status": "PASS", "completion": "CANDIDATE"}), "closure_not_persisted")

    def test_snapshot_write_failure_denies_complete(self):
        original = profile.Journal.write
        def fail_snapshot(journal, name, value):
            if name == "closure_state.json":
                raise OSError("isolated closure snapshot fault")
            return original(journal, name, value)
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            with mock.patch.object(profile.Journal, "write", new=fail_snapshot):
                journal, result, selection, _ = self.setup_run(root)
            self.assertEqual(result["status"], "FAIL")
            self.assertFalse(journal.path("COMPLETE").exists())
            self.assertEqual(profile.consume(journal.directory, selection)["completion"], "INCOMPLETE")

    def atomic_publication_failure_case(self, root, boundary):
        sdk, fired, sealed_payload = FixtureSDK(), [], {}
        original_complete, original_write, original_append = profile.Journal.complete, profile.Journal.write, profile.Journal.append
        active = []
        def trip(journal):
            fired.append(True)
            self.assertFalse(journal.path("COMPLETE").exists())
            sealed_payload.update({name: journal.path(name).read_bytes() for name in profile.PAYLOAD | {"hashes.json"}})
            active.append(True)
            try:
                sdk.trader._callback.on_disconnected()
            finally:
                active.clear()
        def broken_callback_write(journal, name, row):
            if active and name in {"worker_events.jsonl", "callback_attempts.jsonl"}:
                raise OSError("isolated callback fault")
            return original_append(journal, name, row)
        def reentrant_complete(journal, marker):
            if boundary == "entry":
                trip(journal)
            return original_complete(journal, marker)
        def reentrant_temp(journal, name, value):
            result = original_write(journal, name, value)
            if name == "COMPLETE.tmp" and boundary == "temp":
                trip(journal)
            return result
        with mock.patch.object(profile.Journal, "complete", new=reentrant_complete), mock.patch.object(
                profile.Journal, "write", new=reentrant_temp), mock.patch.object(
                profile.Journal, "append", new=broken_callback_write), mock.patch.object(
                profile.Journal, "write_callback_veto", side_effect=OSError("isolated veto fault")):
            selection = selection_fixture(root)
            directory, result = supervisor.run_fixture(root, selection, ACCOUNT, lambda: sdk)
        journal = profile.Journal(directory)
        self.assertEqual(fired, [True])
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["publication_failure"], "pre_rename_closure_conflict")
        self.assertEqual(result["closure_failure"], "closure_generation_changed")
        self.assertFalse(journal.path("COMPLETE").exists())
        self.assertEqual(sealed_payload, {name: journal.path(name).read_bytes() for name in sealed_payload})
        self.assert_payload_binding(journal, complete=False)
        live = profile._LIVE_CLOSURES.pop(str(directory))
        try:
            self.assertIs(live.unpersisted_failure, True)
            self.assertIs(live.publish_denied, True)
            self.assertIs(live.publication_verified, False)
            self.assertGreater(live.generation, live.publication_generation)
            self.assertEqual(profile.consume(directory, selection)["status"], "FAIL")
        finally:
            profile._LIVE_CLOSURES[str(directory)] = live

    def test_reentrant_callback_at_complete_entry_cannot_bypass_publish_gate(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            self.atomic_publication_failure_case(root, "entry")

    def test_reentrant_callback_during_complete_temp_write_cannot_publish(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            self.atomic_publication_failure_case(root, "temp")

    def rename_window_case(self, root, fault):
        sdk, prechecks, renamed, sealed = FixtureSDK(), [], [], {}
        active = []
        operation = "rename" if os.name == "nt" else "link"
        original_publish = getattr(profile.os, operation)
        original_append, original_state = profile.Journal.append, profile.CallbackClosure.publication_state_error
        def observe_clean_state(live, journal):
            reason = original_state(live, journal)
            if reason is None and journal.path("COMPLETE.tmp").exists() and not journal.path("COMPLETE").exists():
                prechecks.append(live.generation)
            return reason
        def broken_callback_write(journal, name, row):
            if active and name in {"worker_events.jsonl", "callback_attempts.jsonl", "callbacks.jsonl"}:
                raise OSError("isolated callback evidence fault")
            return original_append(journal, name, row)
        def reentrant_publish(source, target, *args, **kwargs):
            if Path(source).name != "COMPLETE.tmp" or Path(target).name != "COMPLETE":
                return original_publish(source, target, *args, **kwargs)
            journal = profile.Journal(Path(target).parent)
            live = profile._LIVE_CLOSURES[str(journal.directory)]
            self.assertTrue(prechecks, "The final pre-rename clean check must have passed")
            self.assertIs(live.publication_verified, True)
            self.assertEqual(live.publication_verdict, ("PASS", "PASS", "CANDIDATE"))
            self.assertEqual(prechecks[-1], live.publication_generation)
            self.assertEqual(journal.read("summary.candidate.json")["decision"]["status"], "PASS")
            sealed.update({name: journal.path(name).read_bytes() for name in profile.PAYLOAD | {"hashes.json"}})
            if fault == "callback":
                active.append(True)
                try:
                    sdk.trader._callback.on_disconnected()
                finally:
                    active.clear()
                self.assertIs(live.unpersisted_failure, True)
                self.assertIs(live.publish_denied, True)
                self.assertFalse(journal.path("CALLBACK_FAILED").exists())
            elif fault == "generation":
                live.generation += 1  # Prove the epoch check independently of sticky flags.
                self.assertIs(live.unpersisted_failure, False)
                self.assertIs(live.publish_denied, False)
                self.assertIs(live.publication_verified, True)
            else:
                live.publication_verdict = ("FAIL", "FAIL", "CANDIDATE")
            result = original_publish(source, target, *args, **kwargs)
            self.assertTrue(journal.path("COMPLETE").exists(), "Atomic publication actually succeeded")
            renamed.append(journal.path("COMPLETE").read_bytes())
            return result
        with mock.patch.object(profile.os, operation, new=reentrant_publish), mock.patch.object(
                profile.Journal, "append", new=broken_callback_write), mock.patch.object(
                profile.CallbackClosure, "publication_state_error", new=observe_clean_state), mock.patch.object(
                profile.Journal, "write_callback_veto", side_effect=OSError("isolated veto persistence fault")):
            selection = selection_fixture(root)
            directory, result = supervisor.run_fixture(root, selection, ACCOUNT, lambda: sdk)
        journal = profile.Journal(directory)
        self.assertEqual(len(renamed), 1)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["completion"], "INCOMPLETE")
        self.assertEqual(result["publication_failure"], "post_rename_closure_conflict")
        self.assertEqual(result["closure_failure"], "staged_decision_conflict" if fault == "verdict" else "closure_generation_changed")
        self.assertFalse(journal.path("COMPLETE").exists())
        self.assertEqual(journal.path("COMPLETE.tmp").read_bytes(), renamed[0])
        self.assertFalse(journal.path("CALLBACK_FAILED").exists())
        self.assertEqual(sealed, {name: journal.path(name).read_bytes() for name in sealed})
        self.assert_payload_binding(journal, complete=False)
        self.assertEqual(json.loads(renamed[0])["hashes_sha256"], hashlib.sha256(journal.path("hashes.json").read_bytes()).hexdigest())
        self.assertEqual(journal.read("summary.candidate.json")["decision"]["status"], "PASS")
        live = profile._LIVE_CLOSURES.pop(str(directory))
        try:
            self.assertIs(live.publish_denied, True)
            self.assertIs(live.publication_verified, False)
            self.assertIs(live.unpersisted_failure, fault == "callback")
            # With no live latch, raw business facts remain independently PASS:
            # rejection must come from the invalidated publication, not bad hashes/schema.
            self.assertEqual(profile.judge(journal, selection, RunDeadline(clock(), 30.0))["status"], "PASS")
            fresh = profile.consume(directory, selection)
            self.assertEqual(fresh["status"], "FAIL")
            self.assertEqual(fresh["completion"], "INCOMPLETE")
            self.assertEqual(fresh["reason"], "package_validation_failed")
        finally:
            profile._LIVE_CLOSURES[str(directory)] = live

    def test_rename_entry_callback_double_write_failure_invalidates_complete_for_fresh_consumer(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            self.rename_window_case(root, "callback")

    def test_post_rename_generation_change_alone_denies_publication(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            self.rename_window_case(root, "generation")

    def test_post_rename_staged_verdict_change_denies_publication(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            self.rename_window_case(root, "verdict")

    def test_publish_gate_rejects_truthy_or_null_live_flags(self):
        for value in (None, "false"):
            with self.subTest(value=value), tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
                journal, _, _, sdk = self.healthy(root)
                live = profile._LIVE_CLOSURES[str(journal.directory)]
                live.unpersisted_failure = value
                with live.lock:
                    self.assertEqual(live.publication_gate(journal, {"status": "PASS"}, {
                        "status": "PASS", "completion": "CANDIDATE"}), "invalid_live_closure_state")
                self.assertEqual(profile.closure_veto(journal, {"status": "PASS"})["status"], "FAIL")

    def test_closure_veto_updates_all_failure_sources_diagnostic(self):
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            journal, selection = self.complete_callback_case(root)
            result = profile.consume(journal.directory, selection)
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["checks"]["all_failure_sources"], "FAIL")
            self.assertEqual(result["checks"]["callback_closure"], "FAIL")
            self.assertIn({"kind": "callback_closure_failure"}, result["failures"])

    def test_callback_unknown_object_value_never_formats_or_accesses_getters(self):
        touched = []
        class Unknown:
            def __str__(self):
                touched.append("str")
                print(ACCOUNT)
                return ACCOUNT
            def __repr__(self):
                touched.append("repr")
                return ACCOUNT
        capture = profile.CallbackCapture(profile.PrivateRedactor(b"isolated callback"))
        result = capture.returned(SimpleNamespace(account_id=ACCOUNT, status=Unknown()))
        self.assertTrue(result["items"][0]["capture_errors"])
        self.assertEqual(touched, [])
        self.assertNotIn(ACCOUNT, json.dumps(result))

    def test_callback_unknown_class_dir_and_attribute_hooks_are_not_called(self):
        touched = []
        class Unknown:
            def __dir__(self):
                touched.append("dir")
                print(ACCOUNT)
                return ["status"]
            def __getattribute__(self, name):
                touched.append("getattr")
                print(ACCOUNT, file=sys.stderr)
                raise ValueError("unsafe getter")
        capture = profile.CallbackCapture(profile.PrivateRedactor(b"isolated callback"))
        with self.assertRaises(loader.ContractViolation):
            capture.returned(Unknown())
        self.assertEqual(touched, [])

    def test_callback_simple_namespace_subclass_descriptor_is_rejected(self):
        touched = []
        class Derived(SimpleNamespace):
            @property
            def status(self):
                touched.append(True)
                print(ACCOUNT)
                return 0
        capture = profile.CallbackCapture(profile.PrivateRedactor(b"isolated callback"))
        with self.assertRaises(loader.ContractViolation):
            capture.returned(Derived(account_id=ACCOUNT))
        self.assertEqual(touched, [])

    def test_two_callback_threads_interleave_without_stream_mutation_or_getter_execution(self):
        sdk, getter_calls, captures, failures, stream_facts = FixtureSDK(), [], [], [], []
        boundary, a_done, a_holds = threading.Barrier(2, timeout=1.0), threading.Event(), threading.Event()
        original_consume = profile.consume
        original_capture, original_call = profile.CallbackCapture.returned, loader.ReadOnlyGate.callback_call
        output, errors = io.StringIO(), io.StringIO()
        class Dangerous:
            @property
            def status(self):
                getter_calls.append(threading.current_thread().name)
                print(ACCOUNT)
                print("PRIVATE_CONCURRENT_CALLBACK_PATH", file=sys.stderr)
                return 0
        def interleaved_capture(capture, value, *args, **kwargs):
            name = threading.current_thread().name
            if name == "HARNESS_CALLBACK_A":
                captures.append("A owns capture lock")
                a_holds.set()
                boundary.wait()
            elif name == "HARNESS_CALLBACK_B":
                captures.append("B owns capture lock")
                if not a_done.wait(1.0):
                    failures.append("A did not exit before B capture")
            return original_capture(capture, value, *args, **kwargs)
        def interleaved_call(gate, operation):
            name = threading.current_thread().name
            if name == "HARNESS_CALLBACK_B":
                captures.append("B entered while A holds capture lock")
                boundary.wait()  # B is inside the handler before A can finish.
            return original_call(gate, operation)
        def callback_thread():
            try:
                stream_facts.append((sys.stdout is output, sys.stderr is errors))
                sdk.trader._callback.on_account_status(Dangerous())
                stream_facts.append((sys.stdout is output, sys.stderr is errors))
            except BaseException:
                failures.append("callback thread exception")
            finally:
                if threading.current_thread().name == "HARNESS_CALLBACK_A":
                    a_done.set()
        def after_complete(directory, selection, deadline=None):
            journal = profile.Journal(directory)
            self.assertTrue(journal.path("COMPLETE").exists())
            threads = [threading.Thread(target=callback_thread, name="HARNESS_CALLBACK_" + name) for name in ("A", "B")]
            threads[0].start()
            observed_a = a_holds.wait(1.0)
            threads[1].start()
            for thread in threads:
                thread.join(2.0)
            self.assertTrue(observed_a)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertIs(sys.stdout, output)
            self.assertIs(sys.stderr, errors)
            return original_consume(directory, selection, deadline)
        before_stdout, before_stderr = sys.stdout, sys.stderr
        with tempfile.TemporaryDirectory(prefix="positionx_harness_fake_") as root:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), mock.patch.object(
                    profile, "consume", new=after_complete), mock.patch.object(
                    profile.CallbackCapture, "returned", new=interleaved_capture), mock.patch.object(
                    loader.ReadOnlyGate, "callback_call", new=interleaved_call):
                directory, result = supervisor.run_fixture(root, selection_fixture(root), ACCOUNT, lambda: sdk)
            self.assertIs(sys.stdout, before_stdout)
            self.assertIs(sys.stderr, before_stderr)
            self.assertEqual(failures, [])
            self.assertEqual(getter_calls, [])
            self.assertEqual(stream_facts, [(True, True)] * 4)
            self.assertEqual(captures, ["A owns capture lock", "B entered while A holds capture lock", "B owns capture lock"])
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["reason"], "callback_closure_failure")
            self.assertEqual(result["checks"]["all_failure_sources"], "FAIL")
            journal = profile.Journal(directory)
            self.assert_payload_binding(journal)
            text = output.getvalue() + errors.getvalue() + "".join(p.read_text(encoding="utf-8") for p in directory.iterdir())
            self.assertNotIn(ACCOUNT, text)
            self.assertNotIn("PRIVATE_CONCURRENT_CALLBACK_PATH", text)


if __name__ == "__main__":
    unittest.main()
