"""Offline fake and isolated tests. No third-party or PositionX runtime imports."""

import importlib.abc
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import time
import traceback
from unittest import mock
from types import SimpleNamespace


class DenyRealSDK(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "xtquant" or fullname.startswith("xtquant."):
            raise ImportError("Real SDK imports are forbidden in offline tests")
        return None


# Also installed when the spawned worker imports this fake module.
if any(name == "xtquant" or name.startswith("xtquant.") for name in sys.modules):
    raise RuntimeError("Offline test process already loaded a real SDK")
sys.meta_path.insert(0, DenyRealSDK())

from tools.full_qmt_readonly_probe_v01.evidence import (Journal, Redactor, Capture, CallbackBuffer,
                                                       PAYLOAD_FILES, clock, new_run_directory)
from tools.full_qmt_readonly_probe_v01.supervisor import (
    aggregate, configuration, judge, run_offline_probe, consume_completed, cleanup_worker,
)
from tools.full_qmt_readonly_probe_v01.worker import run_worker


FAKE_ACCOUNT = "FAKE_ACCOUNT_987654321"


class BrokenPublicField:
    @property
    def stock_code(self):
        raise ValueError(FAKE_ACCOUNT)


class FakeCallbackBase:
    pass


class FakeSDK:
    offline_fake = True
    version = "fake-only-0.1.3"
    fake_account_id = FAKE_ACCOUNT
    CallbackBase = FakeCallbackBase
    AccountType = SimpleNamespace

    @property
    def TraderType(self):
        return FakeTrader

    def __init__(self, scenario):
        self.scenario = scenario

    def Trader(self, path, session_id):
        if path != "FAKE_USERDATA_NOT_A_FILESYSTEM_PATH":
            raise AssertionError("A filesystem userdata path is forbidden")
        if self.scenario == "construct_exception":
            raise ValueError(FAKE_ACCOUNT)
        if self.scenario == "invalid_trader":
            return None
        return FakeTrader(self.scenario)

    def Account(self, account_id, account_type):
        if self.scenario == "account_none":
            return None
        if self.scenario == "invalid_account":
            return 123
        if self.scenario == "wrong_account":
            account_id = "FAKE_WRONG_ACCOUNT"
        if self.scenario == "empty_account_identity":
            account_id = ""
        return SimpleNamespace(account_id=account_id, account_type=account_type)


class FakeTrader:
    # Deliberately provides no submit/cancel methods.
    def __init__(self, scenario):
        self.scenario = scenario
        self.callback = None

    def register_callback(self, callback):
        self.callback = callback

    def start(self):
        if self.scenario == "start_exception":
            raise RuntimeError(FAKE_ACCOUNT)

    def connect(self):
        if self.scenario != "no_callback":
            self.callback.on_connected()
            self.callback.on_account_status(SimpleNamespace(account_id=FAKE_ACCOUNT, status=0))
        if self.scenario == "callback_overflow":
            for _ in range(100):
                self.callback.on_stock_order(self.order())
        if self.scenario == "callback_capture_failure":
            self.callback.on_stock_order(BrokenPublicField())
        if self.scenario == "callback_error":
            self.callback.on_order_error(SimpleNamespace(account_id=FAKE_ACCOUNT, order_id=1234,
                                                         error_id=99, error_msg=FAKE_ACCOUNT))
        if self.scenario == "disconnected":
            self.callback.on_disconnected()
        if self.scenario in {"connect_nonzero", "primary_and_stop_failure", "primary_and_stop_block"}:
            return -1
        if self.scenario == "connect_bool":
            return False
        return 0

    def subscribe(self, account):
        return -2 if self.scenario == "subscribe_nonzero" else 0

    @staticmethod
    def asset():
        return SimpleNamespace(account_id=FAKE_ACCOUNT, cash=1000.0, frozen_cash=10.0,
                               market_value=2000.0, total_asset=3010.0)

    @staticmethod
    def position():
        return SimpleNamespace(account_id=FAKE_ACCOUNT, stock_code="600000.SH", volume=100,
                               can_use_volume=100, open_price=12.0, avg_price=13.0,
                               market_value=2000.0)

    @staticmethod
    def order():
        return SimpleNamespace(account_id=FAKE_ACCOUNT, stock_code="600000.SH", order_id=1234,
                               order_type=23, order_volume=100, price=20.0, traded_volume=100,
                               order_status=56, order_time=1792371600,
                               order_remark=FAKE_ACCOUNT, status_msg=FAKE_ACCOUNT)

    @staticmethod
    def trade():
        return SimpleNamespace(account_id=FAKE_ACCOUNT, stock_code="600000.SH", order_id=1234,
                               traded_id="FAKE_TRADE_1", order_type=23, traded_price=20.0,
                               traded_volume=100, traded_amount=2000.0, traded_time=1792371600)

    def query_stock_asset(self, account):
        if self.scenario == "worker_crash":
            os._exit(17)
        if self.scenario == "finite_return":
            time.sleep(0.04)
        if self.scenario == "stdout_leak":
            print(FAKE_ACCOUNT)
            print("C:\\Users\\" + FAKE_ACCOUNT, file=sys.stderr)
            try:
                raise ValueError(FAKE_ACCOUNT)
            except ValueError:
                traceback.print_exc()
        if self.scenario == "native_output":
            os.write(1, (FAKE_ACCOUNT + "\n").encode())
            os.write(2, (FAKE_ACCOUNT + "\n").encode())
        if self.scenario == "query_block":
            threading.Event().wait()
        if self.scenario == "query_exception":
            raise ValueError(FAKE_ACCOUNT)
        if self.scenario in {"query_none", "none_account"}:
            return None
        if self.scenario == "unexpected_shape":
            return 12
        value = self.asset()
        if self.scenario == "return_field":
            setattr(value, "return", 987654321)
        if self.scenario == "sensitive_class":
            secret_type = type("PRIVATE_ALPHA_ACCOUNT", (SimpleNamespace,), {})
            value = secret_type(**vars(value))
        if self.scenario == "unknown_numeric":
            value.fund_id = 987654321
            value.acct_id = 987654321
            value.investor_id = 987654321
        if self.scenario == "wrong_type":
            value.cash = True
        return value

    def query_stock_positions(self, account):
        if self.scenario == "query_none":
            return None
        if self.scenario == "query_empty":
            return []
        value = self.position()
        if self.scenario == "missing_field":
            del value.avg_price
        return [value]

    def query_stock_orders(self, account):
        if self.scenario == "query_none":
            return None
        return [] if self.scenario == "query_empty" else [self.order()]

    def query_stock_trades(self, account):
        if self.scenario == "query_none":
            return None
        if self.scenario == "query_empty":
            return []
        value = self.trade()
        if self.scenario == "empty_id":
            value.traded_id = ""
        return [value]

    def stop(self):
        if self.scenario == "stop_callback":
            self.callback.on_disconnected()
        if self.scenario in {"late_callback", "late_callback_write_failure", "late_channel_failure"}:
            # Non-daemon timer keeps the child alive until a post-seal callback.
            def callback():
                original = Journal.append
                def disk_failure(journal, name, row):
                    if name == "worker_events.jsonl":
                        raise OSError(FAKE_ACCOUNT)
                    return original(journal, name, row)
                if self.scenario in {"late_callback_write_failure", "late_channel_failure"}:
                    Journal.append = disk_failure
                if self.scenario == "late_channel_failure":
                    os.close(1)
                    os.close(2)
                try:
                    self.callback.on_disconnected()
                finally:
                    Journal.append = original
            timer = threading.Timer(0.05, callback)
            timer.daemon = False
            timer.start()
        if self.scenario in {"stop_block", "primary_and_stop_block"}:
            threading.Event().wait()
        if self.scenario in {"stop_exception", "primary_and_stop_failure"}:
            raise RuntimeError(FAKE_ACCOUNT)


class ProbeTests(unittest.TestCase):
    def run_scenario(self, scenario="normal", overrides=None):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            directory, summary = run_offline_probe(root, scenario, overrides)
            records = {}
            texts = []
            for path in directory.iterdir():
                content = path.read_text(encoding="utf-8")
                texts.append(content)
                records[path.name] = ([json.loads(line) for line in content.splitlines()]
                                      if path.suffix == ".jsonl" else json.loads(content))
            return summary, records, "\n".join(texts)

    def test_normal_lifecycle_and_fake_only_scope(self):
        summary, records, _ = self.run_scenario()
        self.assertEqual(summary["status"], "PASS")
        self.assertEqual(summary["scope"], "OFFLINE_FAKE_ONLY")
        self.assertEqual(summary["live_validation"], "NOT_COVERED")
        self.assertEqual(summary["qmt_client_health"], "NOT_COVERED")
        self.assertEqual(summary["implementation_gate"], "NOT_COVERED")
        self.assertEqual(summary["submit"], "NOT_COVERED")
        self.assertEqual(summary["cancel"], "NOT_COVERED")
        self.assertTrue(summary["process"]["exited"])
        self.assertFalse(summary["process"]["forced_termination"])
        self.assertEqual(records["worker_result.json"]["runtime"]["sdk_kind"], "offline_fake")

    def test_exact_read_only_call_sequence_and_step_fields(self):
        _, records, _ = self.run_scenario()
        ends = [row for row in records["steps.jsonl"] if row["phase"] == "end"]
        self.assertEqual([row["name"] for row in ends], [
            "construct", "register", "start", "connect", "construct_account", "subscribe",
            "query.account", "query.positions", "query.orders", "query.trades", "stop"])
        for row in ends:
            for name in ("timestamp_utc", "timestamp_cn", "raw_return", "raw_type", "error"):
                self.assertIn(name, row)
            self.assertGreaterEqual(row["elapsed_ms"], 0)
        self.assertEqual(ends[-1]["raw_type"], "builtins.NoneType")
        self.assertTrue(ends[-1]["returned"])

    def test_connect_nonzero_rc_zero_process_is_not_business_success(self):
        summary, records, _ = self.run_scenario("connect_nonzero")
        self.assertEqual(summary["process"]["exitcode"], 0)
        self.assertEqual(summary["status"], "FAIL")
        self.assertEqual(summary["checks"]["connect"], "FAIL")
        self.assertTrue(records["worker_result.json"]["stop_returned"])

    def test_subscribe_nonzero(self):
        summary, _, _ = self.run_scenario("subscribe_nonzero")
        self.assertEqual(summary["status"], "FAIL")
        self.assertEqual(summary["checks"]["subscribe"], "FAIL")

    def test_bool_is_not_integer_connect_success(self):
        summary, _, _ = self.run_scenario("connect_bool")
        self.assertEqual(summary["checks"]["connect"], "FAIL")

    def test_none_retains_unknown_and_does_not_become_empty(self):
        summary, records, _ = self.run_scenario("query_none")
        self.assertEqual(summary["status"], "NOT_COVERED")
        for domain in summary["domains"].values():
            self.assertEqual(domain["raw_query"], "NOT_COVERED")
            self.assertEqual(domain["schema_coverage"], "NOT_COVERED")
        self.assertTrue(all(row["raw"]["kind"] == "none" for row in records["snapshots.jsonl"]))

    def test_empty_raw_success_is_not_schema_coverage(self):
        summary, records, _ = self.run_scenario("query_empty")
        self.assertEqual(summary["status"], "NOT_COVERED")
        for name in ("positions", "orders", "trades"):
            self.assertEqual(summary["domains"][name]["raw_query"], "PASS")
            self.assertEqual(summary["domains"][name]["schema_coverage"], "NOT_COVERED")
        self.assertEqual(records["snapshots.jsonl"][1]["raw"]["items"], [])

    def test_samples_preserve_raw_fields_types_and_distinct_prices(self):
        summary, records, _ = self.run_scenario()
        raw = records["snapshots.jsonl"][1]["raw"]["items"][0]["public_fields"]
        self.assertEqual(raw["volume"], {"raw_type": "builtins.int", "value": 100})
        normalized = summary["domains"]["positions"]["items"][0]["normalized"]
        self.assertEqual(normalized["open_price"], 12.0)
        self.assertEqual(normalized["avg_price"], 13.0)
        self.assertTrue(all(d["schema_coverage"] == "PASS" for d in summary["domains"].values()))

    def test_query_exception_preserves_primary_and_cleans_up(self):
        summary, records, _ = self.run_scenario("query_exception")
        facts = records["worker_result.json"]
        self.assertEqual(summary["status"], "FAIL")
        self.assertEqual(facts["primary_failure"]["error_type"], "builtins.ValueError")
        self.assertIsNone(facts["cleanup_failure"])
        self.assertTrue(facts["stop_returned"])

    def test_worker_query_block_is_terminated_only_as_own_child(self):
        summary, _, _ = self.run_scenario("query_block", {"step_timeout_seconds": 0.2})
        self.assertEqual(summary["status"], "FAIL")
        process = summary["process"]
        self.assertTrue(process["exited"])
        self.assertTrue(process["forced_termination"])
        self.assertEqual(process["timeout"]["name"], "query.account")
        self.assertFalse(process["timeout"]["returned"])
        self.assertEqual(summary["checks"]["evidence_parse"], "FAIL")

    def test_stop_exception_does_not_invent_primary_failure(self):
        summary, records, _ = self.run_scenario("stop_exception")
        facts = records["worker_result.json"]
        self.assertEqual(summary["status"], "FAIL")
        self.assertIsNone(facts["primary_failure"])
        self.assertEqual(facts["cleanup_failure"]["error_type"], "builtins.RuntimeError")

    def test_primary_and_cleanup_failure_both_survive(self):
        summary, records, _ = self.run_scenario("primary_and_stop_failure")
        facts = records["worker_result.json"]
        self.assertTrue(facts["primary_failure"]["error_type"].endswith("WorkflowPreconditionError"))
        self.assertEqual(facts["cleanup_failure"]["error_type"], "builtins.RuntimeError")
        self.assertEqual({f["kind"] for f in summary["failures"]},
                         {"primary_failure", "cleanup_failure"})

    def test_callback_capture(self):
        summary, records, _ = self.run_scenario()
        callbacks = records["callbacks.jsonl"]
        self.assertEqual([row["event"] for row in callbacks], ["connected", "account_status"])
        self.assertEqual([row["sequence"] for row in callbacks], [1, 2])
        self.assertIn("thread_id", callbacks[0])
        self.assertEqual(summary["checks"]["callback_capture"], "PASS")
        self.assertEqual(summary["callback_event_coverage"]["connected"], "PASS")
        self.assertEqual(summary["callback_event_coverage"]["trade"], "NOT_COVERED")

    def test_callback_overflow_fails_instead_of_silent_drop(self):
        summary, records, _ = self.run_scenario("callback_overflow", {"callback_capacity": 2})
        self.assertEqual(summary["status"], "FAIL")
        self.assertGreater(records["worker_result.json"]["callbacks"]["overflow_count"], 0)
        self.assertEqual(summary["checks"]["callback_capture"], "FAIL")

    def test_callback_capture_failure(self):
        summary, records, _ = self.run_scenario("callback_capture_failure")
        self.assertEqual(summary["status"], "FAIL")
        self.assertEqual(records["worker_result.json"]["callbacks"]["capture_failure_count"], 1)
        self.assertEqual(summary["checks"]["callback_capture"], "FAIL")

    def test_redaction_in_fields_callbacks_errors_and_evidence(self):
        for scenario in ("normal", "primary_and_stop_failure", "callback_capture_failure"):
            with self.subTest(scenario=scenario):
                _, _, text = self.run_scenario(scenario)
                self.assertNotIn(FAKE_ACCOUNT, text)
                self.assertNotIn("FAKE_TRADE_1", text)
                self.assertNotIn('"key":', text)
                self.assertIn("redacted:", text)

    def test_worker_never_produces_verdict_or_normalized_view(self):
        _, records, _ = self.run_scenario()
        self.assertNotIn("status", records["worker_result.json"])
        for row in records["snapshots.jsonl"]:
            self.assertNotIn("normalized", row)
            self.assertNotIn("schema_coverage", row)

    def test_missing_field_is_schema_failure_without_raw_query_failure(self):
        summary, _, _ = self.run_scenario("missing_field")
        domain = summary["domains"]["positions"]
        self.assertEqual(domain["raw_query"], "PASS")
        self.assertEqual(domain["schema_coverage"], "FAIL")
        self.assertEqual(domain["items"][0]["missing_fields"], ["avg_price"])

    def test_wrong_type_is_not_silently_coerced(self):
        summary, _, _ = self.run_scenario("wrong_type")
        self.assertEqual(summary["domains"]["account"]["items"][0]["invalid_fields"], ["cash"])
        self.assertEqual(summary["status"], "FAIL")

    def test_unexpected_query_shape(self):
        summary, _, _ = self.run_scenario("unexpected_shape")
        self.assertEqual(summary["domains"]["account"]["raw_query"], "FAIL")

    def test_none_account_does_not_pass_using_other_domain_samples(self):
        summary, _, _ = self.run_scenario("none_account")
        self.assertEqual(summary["domains"]["account"]["raw_query"], "NOT_COVERED")
        self.assertEqual(summary["status"], "NOT_COVERED")

    def test_stop_block_cannot_claim_cleanup(self):
        summary, _, _ = self.run_scenario("stop_block", {"step_timeout_seconds": 0.2})
        self.assertEqual(summary["status"], "FAIL")
        self.assertEqual(summary["process"]["timeout"]["name"], "stop")

    def test_primary_survives_even_if_stop_never_returns(self):
        summary, _, _ = self.run_scenario("primary_and_stop_block", {"step_timeout_seconds": 0.2})
        self.assertEqual(summary["status"], "FAIL")
        kinds = {failure["kind"] for failure in summary["failures"]}
        self.assertIn("primary_failure", kinds)
        self.assertIn("cleanup_failure", kinds)
        self.assertIn("supervisor_timeout", kinds)

    def test_construct_and_partial_start_failure(self):
        for scenario, expected_stop in (("construct_exception", False), ("start_exception", True)):
            with self.subTest(scenario=scenario):
                summary, records, _ = self.run_scenario(scenario)
                self.assertEqual(summary["status"], "FAIL")
                self.assertEqual(records["worker_result.json"]["stop_attempted"], expected_stop)

    def test_disconnect_fails_and_no_callback_is_not_event_coverage(self):
        summary, _, _ = self.run_scenario("disconnected")
        self.assertEqual(summary["status"], "FAIL")
        summary, _, _ = self.run_scenario("no_callback")
        self.assertTrue(all(status == "NOT_COVERED" for status in summary["callback_event_coverage"].values()))

    def test_missing_and_malformed_evidence_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            directory = new_run_directory(root)
            journal = Journal(directory)
            process = {"exitcode": 0, "exited": True, "timeout": None, "forced_termination": False}
            self.assertEqual(judge(journal, process)["status"], "FAIL")
            journal.path("worker_result.json").write_text("{broken", encoding="utf-8")
            self.assertEqual(judge(journal, process)["status"], "FAIL")

    def test_snapshot_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            directory, summary = run_offline_probe(root)
            journal = Journal(directory)
            rows = journal.rows("snapshots.jsonl")
            rows[0]["step_id"] = -1
            journal.path("snapshots.jsonl").write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            self.assertEqual(judge(journal, summary["process"])["status"], "FAIL")

    def test_wrong_evidence_container_types_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            directory, summary = run_offline_probe(root)
            journal = Journal(directory)
            worker_path = journal.path("worker_result.json")
            original = worker_path.read_text(encoding="utf-8")
            for value in ([], None, 1, "bad"):
                with self.subTest(value=value):
                    worker_path.write_text(json.dumps(value), encoding="utf-8")
                    self.assertEqual(judge(journal, summary["process"])["status"], "FAIL")
            worker_path.write_text(original, encoding="utf-8")
            journal.path("callbacks.jsonl").write_text('42\n"bad"\n', encoding="utf-8")
            self.assertEqual(judge(journal, summary["process"])["status"], "FAIL")

    def test_hash_manifest_matches_all_final_artifacts(self):
        import hashlib
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            directory, _ = run_offline_probe(root)
            hashes = Journal(directory).read("hashes.json")
            self.assertEqual(set(hashes["files"]), PAYLOAD_FILES)
            for name, expected in hashes["files"].items():
                self.assertEqual(hashlib.sha256((directory / name).read_bytes()).hexdigest(), expected)

    def test_root_and_configuration_guards(self):
        with self.assertRaises(ValueError):
            new_run_directory("relative")
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal = Journal(new_run_directory(root))
            with self.assertRaises(ValueError):
                journal.write("../outside.json", {})
            with self.assertRaises(ValueError):
                run_offline_probe(root, "real")
        for overrides in ({"callback_capacity": 0}, {"step_timeout_seconds": float("nan")},
                          {"session_id": True}, {"unknown": 1}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                configuration(overrides)

    def test_aggregate_never_defaults_missing_or_unknown_to_success(self):
        self.assertEqual(aggregate([]), "NOT_COVERED")
        self.assertEqual(aggregate(["PASS", "NOT_COVERED"]), "NOT_COVERED")
        self.assertEqual(aggregate(["FAIL", "NOT_COVERED"]), "FAIL")
        self.assertEqual(aggregate(["PASS", "unknown"]), "FAIL")

    def test_real_sdk_has_not_been_imported(self):
        self.assertFalse(any(name == "xtquant" or name.startswith("xtquant.") for name in sys.modules))


class HardeningTests(unittest.TestCase):
    def fixture(self, root):
        directory, result = run_offline_probe(root)
        self.assertEqual(result["status"], "PASS", result)
        return Journal(directory), result

    def write_json(self, journal, name, value):
        # Intentional corruption of this test's own temporary evidence only.
        journal.path(name).write_bytes((json.dumps(value, ensure_ascii=True) + "\n").encode("utf-8"))

    def write_rows(self, journal, name, rows):
        journal.path(name).write_bytes(b"".join((json.dumps(row) + "\n").encode() for row in rows))

    def write_bound_worker(self, journal, worker):
        # Keep the channel binding valid so a semantic test cannot pass merely
        # because its intentional worker mutation also broke the report digest.
        self.write_json(journal, "worker_result.json", worker)
        rows = journal.rows("channel_events.jsonl")
        digest = hashlib.sha256(json.dumps(worker, sort_keys=True, ensure_ascii=True,
                                          allow_nan=False).encode("utf-8")).hexdigest()
        for row in rows:
            if row["event"] == "report":
                row["report_sha256"] = digest
        self.write_rows(journal, "channel_events.jsonl", rows)

    def assert_channel_consistent(self, journal, process):
        from tools.full_qmt_readonly_probe_v01.supervisor import channel_facts
        counts, ok = channel_facts(journal, journal.read("worker_result.json"), process)
        self.assertTrue(ok)
        self.assertEqual(counts, process["output_counts"])

    def add_failure_event(self, journal):
        rows = journal.rows("worker_events.jsonl")
        error = Redactor().error(ValueError(FAKE_ACCOUNT))
        stamp = {key: rows[-1][key] for key in ("timestamp_utc", "timestamp_cn", "monotonic_ns")}
        rows.append({**stamp, "run_id": journal.directory.name, "event": "primary_failure", "facts": error})
        self.write_rows(journal, "worker_events.jsonl", rows)
        return error

    def test_failure_event_otherwise_pass_cannot_pass(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, result = self.fixture(root)
            self.add_failure_event(journal)
            decision = judge(journal, result["process"])
            self.assertEqual(decision["status"], "FAIL")
            self.assertEqual(decision["checks"]["all_failure_sources"], "FAIL")
            self.assertEqual(decision["checks"]["source_consistency"], "FAIL")
            self.assertEqual(decision["checks"]["evidence_parse"], "PASS")
            self.assertTrue(decision["failures"])
            self.assert_channel_consistent(journal, result["process"])

    def test_result_failure_without_event_is_conflict(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, result = self.fixture(root)
            worker = journal.read("worker_result.json")
            worker["cleanup_failure"] = Redactor().error(RuntimeError(FAKE_ACCOUNT))
            self.write_bound_worker(journal, worker)
            decision = judge(journal, result["process"])
            self.assertEqual(decision["status"], "FAIL")
            self.assertEqual(decision["checks"]["source_consistency"], "FAIL")
            self.assertEqual(decision["checks"]["evidence_parse"], "PASS")
            self.assertTrue(any(row["kind"] == "cleanup_failure" for row in decision["failures"]))
            self.assert_channel_consistent(journal, result["process"])

    def test_different_event_and_result_errors_are_conflict(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, result = self.fixture(root)
            self.add_failure_event(journal)
            worker = journal.read("worker_result.json")
            worker["primary_failure"] = Redactor().error(TypeError("different"))
            self.write_bound_worker(journal, worker)
            decision = judge(journal, result["process"])
            self.assertEqual(decision["checks"]["source_consistency"], "FAIL")
            self.assertEqual(decision["checks"]["evidence_parse"], "PASS")
            self.assert_channel_consistent(journal, result["process"])

    def test_finite_late_return_between_polls_fails_postcheck(self):
        real_sleep = time.sleep
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            # Parent polls slowly; the separate child's 40ms call finishes between polls.
            with mock.patch("tools.full_qmt_readonly_probe_v01.supervisor.time.sleep",
                            side_effect=lambda seconds: real_sleep(0.5)):
                directory, result = run_offline_probe(root, "finite_return", {"step_timeout_seconds": 0.02})
            self.assertEqual(result["status"], "FAIL", result)
            self.assertIsNone(result["process"]["timeout"])
            self.assertEqual(result["checks"]["deadlines"], "FAIL")
            ends = [r for r in Journal(directory).rows("steps.jsonl") if r["phase"] == "end"]
            self.assertGreater(next(r for r in ends if r["name"] == "query.account")["elapsed_ms"], 20)

    def test_total_deadline_is_rechecked_after_exit(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, result = self.fixture(root)
            process = dict(result["process"])
            process["finished"] = dict(process["finished"])
            process["finished"]["monotonic_ns"] = process["started"]["monotonic_ns"] + 11_000_000_000
            self.assertEqual(judge(journal, process)["checks"]["deadlines"], "FAIL")

    def test_manifest_missing_corrupt_and_config_mismatch(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, result = self.fixture(root)
            path = journal.path("manifest.json")
            original = path.read_bytes()
            for variant in (None, b"{broken", original.replace(b'"session_id": 1019', b'"session_id": 1020')):
                with self.subTest(variant=variant is None):
                    if variant is None:
                        path.unlink()
                    else:
                        path.write_bytes(variant)
                    decision = judge(journal, result["process"])
                    self.assertEqual(decision["status"], "FAIL")
                    self.assertEqual(consume_completed(journal)["completion"], "INCOMPLETE")
                    path.write_bytes(original)

    def test_hashes_write_failure_leaves_only_candidate(self):
        original = Journal.write
        def fail_hashes(journal, name, value):
            if name == "hashes.json":
                raise OSError(FAKE_ACCOUNT)
            return original(journal, name, value)
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            with mock.patch.object(Journal, "write", new=fail_hashes):
                directory, result = run_offline_probe(root)
            journal = Journal(directory)
            self.assertEqual(result["completion"], "INCOMPLETE")
            self.assertTrue(journal.path("summary.candidate.json").exists())
            self.assertEqual(journal.read("summary.candidate.json")["publication"], "CANDIDATE")
            self.assertFalse(journal.path("COMPLETE").exists())
            self.assertEqual(consume_completed(journal)["status"], "FAIL")

    def test_complete_publication_failure_cannot_publish_pass(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            with mock.patch.object(Journal, "complete", side_effect=OSError(FAKE_ACCOUNT)):
                directory, result = run_offline_probe(root)
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["completion"], "INCOMPLETE")
            self.assertTrue(Journal(directory).path("hashes.json").exists())
            self.assertEqual(consume_completed(Journal(directory))["status"], "FAIL")

    def test_every_required_artifact_is_mandatory(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, _ = self.fixture(root)
            for name in PAYLOAD_FILES | {"hashes.json", "COMPLETE"}:
                with self.subTest(name=name):
                    path = journal.path(name)
                    data = path.read_bytes()
                    path.unlink()
                    self.assertEqual(consume_completed(journal)["completion"], "INCOMPLETE")
                    path.write_bytes(data)

    def test_step_reorder_and_unknown_extra_step(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, result = self.fixture(root)
            original = journal.rows("steps.jsonl")
            reordered = original[2:4] + original[:2] + original[4:]
            self.write_rows(journal, "steps.jsonl", reordered)
            self.assertEqual(judge(journal, result["process"])["status"], "FAIL")
            unknown = [dict(row) for row in original]
            unknown[-2]["name"] = unknown[-1]["name"] = "extra_unknown_step"
            self.write_rows(journal, "steps.jsonl", unknown)
            self.assertEqual(judge(journal, result["process"])["status"], "FAIL")

    def test_callback_missing_fields_and_truncated_jsonl(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, result = self.fixture(root)
            self.write_rows(journal, "callbacks.jsonl", [{"event": "connected"}, {"event": "account_status"}])
            self.assertEqual(judge(journal, result["process"])["status"], "FAIL")
            path = journal.path("steps.jsonl")
            path.write_bytes(path.read_bytes().rstrip(b"\n"))
            self.assertEqual(judge(journal, result["process"])["status"], "FAIL")

    def test_sdk_write_and_stop_errors_are_three_distinct_facts(self):
        original = Journal.append
        def fail_query_error_record(journal, name, row):
            if name == "steps.jsonl" and row.get("name") == "query.account" and row.get("phase") == "end":
                raise OSError("C:\\Users\\" + FAKE_ACCOUNT)
            return original(journal, name, row)
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal = Journal(new_run_directory(root))
            journal.initialize_streams()
            with mock.patch.object(Journal, "append", new=fail_query_error_record), mock.patch.object(
                    FakeTrader, "stop", side_effect=RuntimeError(FAKE_ACCOUNT)):
                facts = run_worker(FakeSDK("query_exception"), journal.directory, configuration())
            self.assertEqual(facts["primary_failure"]["error_type"], "builtins.ValueError")
            self.assertEqual(facts["cleanup_failure"]["error_type"], "builtins.RuntimeError")
            self.assertGreater(facts["evidence_failure_count"], 0)
            self.assertTrue(any(e["error_type"] == "builtins.OSError" for e in facts["evidence_failures"]))
            self.assertNotIn(FAKE_ACCOUNT, json.dumps(facts))

    def test_invalid_handles_none_account_and_empty_id(self):
        for scenario in ("invalid_trader", "account_none", "invalid_account", "empty_id"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
                _, result = run_offline_probe(root, scenario)
                self.assertEqual(result["status"], "FAIL", result)

    def test_unknown_numeric_sensitive_fields_are_not_plaintext(self):
        summary, records, text = ProbeTests.run_scenario(self, "unknown_numeric")
        fields = records["snapshots.jsonl"][0]["raw"]["items"][0]["public_fields"]
        unknown = [field for name, field in fields.items() if name.startswith("redacted_field_")]
        self.assertEqual(len(unknown), 3)
        for field in unknown:
            self.assertTrue(field["value"].startswith("redacted:"))
            self.assertEqual(field["raw_type"], "builtins.int")
        self.assertNotIn("987654321", text)
        self.assertEqual(summary["status"], "PASS")

    def test_stdout_stderr_traceback_and_native_output_do_not_leak(self):
        import contextlib
        import io
        for scenario in ("stdout_leak", "native_output", "query_exception"):
            with self.subTest(scenario=scenario):
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    result, _, files = ProbeTests.run_scenario(self, scenario)
                self.assertNotIn(FAKE_ACCOUNT, files + stdout.getvalue() + stderr.getvalue())
                self.assertNotIn("C:\\Users\\", files)
                if scenario == "native_output":
                    self.assertEqual(result["status"], "FAIL")

    def test_worker_crash_is_not_pass(self):
        result, _, _ = ProbeTests.run_scenario(self, "worker_crash")
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["process"]["exitcode"], 17)

    def test_terminate_survival_is_unconfirmed_without_kill_escalation(self):
        class Survivor:
            returncode = None
            def __init__(self):
                self.terminations = 0
            def poll(self):
                return None
            def terminate(self):
                self.terminations += 1
            def wait(self, timeout):
                import subprocess
                raise subprocess.TimeoutExpired("fake", timeout)
            def kill(self):
                raise AssertionError("Windows kill is not an escalation")
        process = Survivor()
        facts = cleanup_worker(process, wait_seconds=0.001)
        self.assertFalse(facts["exited"])
        self.assertIsNone(facts["exitcode"])
        self.assertEqual(process.terminations, 1)
        self.assertTrue(facts["cleanup_errors"])
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, result = self.fixture(root)
            process_facts = {**result["process"], **facts}
            self.assertEqual(judge(journal, process_facts)["status"], "FAIL")

    def test_unconfirmed_exit_refuses_complete_publication(self):
        from tools.full_qmt_readonly_probe_v01.supervisor import publish
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, result = self.fixture(root)
            for name in ("COMPLETE", "hashes.json", "summary.candidate.json"):
                journal.path(name).unlink()
            rows = journal.rows("process_checks.jsonl")
            rows[0]["exited"], rows[0]["exitcode"] = False, None
            self.write_rows(journal, "process_checks.jsonl", rows)
            decision = judge(journal, rows[0])
            final = publish(journal, decision, journal.read("manifest.json"))
            self.assertEqual(final["completion"], "INCOMPLETE")
            self.assertFalse(journal.path("COMPLETE").exists())

    def test_callback_raw_field_type_conflict_fails(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, result = self.fixture(root)
            rows = journal.rows("callbacks.jsonl")
            rows[1]["raw"]["items"][0]["public_fields"]["status"]["value"] = {"unexpected": 1}
            self.write_rows(journal, "callbacks.jsonl", rows)
            self.assertEqual(judge(journal, result["process"])["status"], "FAIL")

    def test_terminate_exception_is_recorded(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.terminate.side_effect = OSError(FAKE_ACCOUNT)
        process.wait.side_effect = TimeoutError(FAKE_ACCOUNT)
        process.returncode = None
        facts = cleanup_worker(process, wait_seconds=0.001)
        self.assertEqual([e["stage"] for e in facts["cleanup_errors"]], ["terminate", "wait"])
        self.assertFalse(facts["exited"])
        self.assertNotIn(FAKE_ACCOUNT, json.dumps(facts))

    def test_hash_inventory_missing_entry_even_with_rebound_marker(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, _ = self.fixture(root)
            hashes = journal.read("hashes.json")
            del hashes["files"]["callbacks.jsonl"]
            self.write_json(journal, "hashes.json", hashes)
            marker = journal.read("COMPLETE")
            marker["hashes_sha256"] = journal.digest("hashes.json")
            self.write_json(journal, "COMPLETE", marker)
            self.assertEqual(consume_completed(journal)["completion"], "INCOMPLETE")

    def test_stop_boundary_and_post_close_callback(self):
        result, records, _ = ProbeTests.run_scenario(self, "stop_callback")
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(any(row["event"] == "disconnected" and row["phase"] == "stopping"
                            for row in records["callbacks.jsonl"]))
        result, records, _ = ProbeTests.run_scenario(self, "late_callback")
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any(row["event"] == "late_callback_failure" for row in records["worker_events.jsonl"]))

    def test_late_callback_and_failed_event_write_still_blocks_pass(self):
        result, records, _ = ProbeTests.run_scenario(self, "late_callback_write_failure")
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["process"]["channel_ok"])
        self.assertGreater(result["process"]["output_counts"]["stderr_bytes"], 0)
        self.assertFalse(any(row["event"] == "late_callback_failure" for row in records["worker_events.jsonl"]))

    def test_late_callback_all_error_channels_lost_exits_nonzero(self):
        result, _, _ = ProbeTests.run_scenario(self, "late_channel_failure")
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["process"]["exitcode"], 94)
        self.assertTrue(result["process"]["exited"])

    def test_sensitive_exception_and_field_names_are_redacted(self):
        secret_error = type(FAKE_ACCOUNT, (Exception,), {})
        redactor = Redactor()
        error = redactor.error(secret_error("C:\\Users\\" + FAKE_ACCOUNT))
        raw = Capture(redactor).public(SimpleNamespace(**{FAKE_ACCOUNT: 987654321}))
        self.assertNotIn(FAKE_ACCOUNT, json.dumps({"error": error, "raw": raw}))
        self.assertNotIn("987654321", json.dumps(raw))

    def test_complete_marker_cannot_be_overwritten_or_appended_after_seal(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, result = self.fixture(root)
            before = journal.path("COMPLETE").read_bytes()
            with self.assertRaises(ValueError):
                journal.complete({})
            with self.assertRaises(ValueError):
                journal.append("steps.jsonl", {"bad": True})
            self.assertEqual(journal.path("COMPLETE").read_bytes(), before)
            self.assertEqual(consume_completed(journal)["status"], result["status"])

    def test_duplicate_json_keys_are_not_silently_accepted(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, result = self.fixture(root)
            journal.path("worker_result.json").write_bytes(b'{"primary_failure": {}, "primary_failure": null}\n')
            self.assertEqual(judge(journal, result["process"])["status"], "FAIL")

    def test_temporary_marker_is_not_completion(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, _ = self.fixture(root)
            journal.path("COMPLETE").rename(journal.path("COMPLETE.tmp"))
            self.assertEqual(consume_completed(journal)["completion"], "INCOMPLETE")

    def test_child_launch_is_isolated_and_arguments_have_no_account(self):
        import subprocess
        original = subprocess.Popen
        calls = []
        def audited(*args, **kwargs):
            calls.append(args[0])
            return original(*args, **kwargs)
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            with mock.patch("tools.full_qmt_readonly_probe_v01.supervisor.subprocess.Popen", side_effect=audited):
                _, result = run_offline_probe(root)
            self.assertEqual(result["status"], "PASS")
        self.assertTrue({"-I", "-S", "-B"}.issubset(calls[0]))
        self.assertNotIn(FAKE_ACCOUNT, str(calls[0]))


class FinalHardeningTests(unittest.TestCase):
    def fixture(self, root):
        directory, result = run_offline_probe(root)
        self.assertEqual(result["status"], "PASS", result)
        return Journal(directory), result

    write_json = HardeningTests.write_json
    write_rows = HardeningTests.write_rows
    write_bound_worker = HardeningTests.write_bound_worker
    assert_channel_consistent = HardeningTests.assert_channel_consistent

    def test_return_field_name_is_not_a_scalar_return_exemption(self):
        result, records, text = ProbeTests.run_scenario(self, "return_field")
        self.assertEqual(result["status"], "PASS")
        self.assertNotIn("987654321", text)
        fields = records["snapshots.jsonl"][0]["raw"]["items"][0]["public_fields"]
        self.assertNotIn("return", fields)
        unknown = [field for name, field in fields.items() if name.startswith("redacted_field_")]
        self.assertEqual(len(unknown), 1)
        self.assertTrue(unknown[0]["value"].startswith("redacted:"))

    def test_sensitive_query_class_name_never_reaches_evidence(self):
        result, records, text = ProbeTests.run_scenario(self, "sensitive_class")
        self.assertEqual(result["status"], "PASS")
        self.assertNotIn("PRIVATE_ALPHA_ACCOUNT", text)
        raw = records["snapshots.jsonl"][0]["raw"]
        self.assertTrue(raw["raw_type"].startswith("type:"))
        self.assertTrue(raw["items"][0]["raw_type"].startswith("type:"))

    def test_unknown_labels_value_types_and_scalars_are_private(self):
        secret_type = type("PRIVATE_ALPHA_CLASS", (), {})
        capture = Capture(Redactor())
        raw = capture.public(SimpleNamespace(PRIVATE_ALPHA_FIELD=secret_type(), investor_id=987654321))
        scalar = capture.returned(987654321)
        text = json.dumps({"raw": raw, "scalar": scalar})
        for secret in ("PRIVATE_ALPHA_CLASS", "PRIVATE_ALPHA_FIELD", "investor_id", "987654321"):
            self.assertNotIn(secret, text)
        self.assertTrue(scalar["value"].startswith("redacted:"))

    def delayed_stage(self, stage, after=False):
        from tools.full_qmt_readonly_probe_v01 import supervisor
        supervisor_stages = {"verify": "_verify_package", "consume": "consume_completed"}
        original = getattr(supervisor, supervisor_stages[stage]) if stage in supervisor_stages else getattr(Journal, stage)
        delayed = False
        def operation(*args, **kwargs):
            nonlocal delayed
            should_delay = not delayed and (stage != "write" or args[1] == "summary.candidate.json")
            if should_delay:
                delayed = True
                if after:
                    result = original(*args, **kwargs)
                    time.sleep(1.6)
                    return result
                time.sleep(1.6)
            return original(*args, **kwargs)
        target = supervisor if stage in supervisor_stages else Journal
        attribute = supervisor_stages.get(stage, stage)
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            started = time.monotonic()
            with mock.patch.object(target, attribute, new=operation):
                directory, result = run_offline_probe(root, overrides={"total_timeout_seconds": 1.5})
            self.assertTrue(delayed)
            self.assertGreater(time.monotonic() - started, 1.5)
            self.assertEqual(result["status"], "FAIL", result)
            self.assertEqual(result["completion"], "INCOMPLETE")
            journal = Journal(directory)
            self.assertTrue(journal.rows("process_checks.jsonl")[0]["exited"])
            self.assertFalse(journal.path("COMPLETE").exists())
            self.assertEqual(consume_completed(journal)["status"], "FAIL")

    def test_worker_timely_but_summary_publish_exceeds_run_deadline(self):
        self.delayed_stage("write")

    def test_worker_timely_but_hashing_exceeds_run_deadline(self):
        self.delayed_stage("hashes")

    def test_complete_creation_delayed_beyond_deadline(self):
        self.delayed_stage("complete")

    def test_marker_revoked_if_complete_returns_late(self):
        self.delayed_stage("complete", after=True)

    def test_final_consumer_verification_is_inside_run_deadline(self):
        self.delayed_stage("verify", after=True)

    def test_post_marker_consume_does_not_receive_a_fresh_run_budget(self):
        self.delayed_stage("consume", after=True)

    def test_published_marker_corruption_cannot_return_pass(self):
        original = Journal.complete
        def corrupt_marker(journal, marker):
            original(journal, marker)
            journal.path("COMPLETE").write_bytes(b"{broken\n")
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            with mock.patch.object(Journal, "complete", new=corrupt_marker):
                directory, result = run_offline_probe(root)
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["completion"], "INCOMPLETE")
            self.assertEqual(consume_completed(Journal(directory))["status"], "FAIL")

    def test_initialization_uses_same_run_budget(self):
        from tools.full_qmt_readonly_probe_v01 import supervisor
        original = supervisor.build_manifest
        def delayed(*args, **kwargs):
            time.sleep(0.06)
            return original(*args, **kwargs)
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            with mock.patch.object(supervisor, "build_manifest", new=delayed), mock.patch.object(
                    supervisor.subprocess, "Popen") as launch:
                directory, result = run_offline_probe(root, overrides={"total_timeout_seconds": 0.04})
            launch.assert_not_called()
            self.assertEqual(result["completion"], "INCOMPLETE")
            self.assertFalse((directory / "COMPLETE").exists())

    def test_worker_refuses_to_begin_sdk_lifecycle_with_expired_run_deadline(self):
        from tools.full_qmt_readonly_probe_v01.evidence import RunDeadline
        config = configuration({"total_timeout_seconds": 1.0})
        started = clock()
        started["monotonic_ns"] -= 2_000_000_000
        timing = RunDeadline(started, config["total_timeout_seconds"]).timing
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal = Journal(new_run_directory(root))
            journal.initialize_streams()
            with mock.patch.object(FakeSDK, "Trader", side_effect=AssertionError("SDK call after deadline")) as construct:
                facts = run_worker(FakeSDK("normal"), journal.directory, config, run_timing=timing)
            construct.assert_not_called()
            self.assertIsNotNone(facts["primary_failure"])
            self.assertFalse(facts["stop_attempted"])

    def test_raw_callback_capture_errors_beat_zero_worker_counters(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, result = self.fixture(root)
            callbacks = journal.rows("callbacks.jsonl")
            attempts = journal.rows("callback_attempts.jsonl")
            error = Redactor().error(ValueError("fake capture error"))
            callbacks[1]["raw"]["items"][0]["capture_errors"] = [error]
            attempts[1]["raw"] = callbacks[1]["raw"]
            self.write_rows(journal, "callbacks.jsonl", callbacks)
            self.write_rows(journal, "callback_attempts.jsonl", attempts)
            self.assertEqual(journal.read("worker_result.json")["callbacks"]["capture_failure_count"], 0)
            from tools.full_qmt_readonly_probe_v01.supervisor import validate_callback_raw
            with self.assertRaisesRegex(ValueError, "Raw callback capture failure"):
                validate_callback_raw("account_status", callbacks[1]["raw"],
                                      journal.read("worker_result.json")["expected_account_token"])
            decision = judge(journal, result["process"])
            self.assertEqual(decision["status"], "FAIL")
            self.assertEqual(decision["checks"]["evidence_parse"], "FAIL")
            self.assert_channel_consistent(journal, result["process"])

    def test_raw_stderr_beats_channel_ok_true(self):
        from tools.full_qmt_readonly_probe_v01.supervisor import channel_facts
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, result = self.fixture(root)
            rows = journal.rows("channel_events.jsonl")
            index = next(i for i, row in enumerate(rows) if row["event"] == "closed" and row["stream"] == "stderr")
            injected = {key: value for key, value in rows[index].items() if key != "stream"}
            injected.update(event="stderr_bytes", count=17)
            rows.insert(index, injected)
            for index, row in enumerate(rows, 1):
                row["sequence"] = index
            self.write_rows(journal, "channel_events.jsonl", rows)
            counts, raw_ok = channel_facts(journal, journal.read("worker_result.json"), result["process"])
            self.assertEqual(counts, {"noise_bytes": 0, "stderr_bytes": 17, "invalid_packets": 0})
            self.assertFalse(raw_ok)
            process = {**result["process"], "channel_ok": True,
                       "output_counts": {"noise_bytes": 0, "stderr_bytes": 17, "invalid_packets": 0}}
            self.assertEqual(judge(journal, process)["status"], "FAIL")

    def test_output_summary_without_matching_raw_observation_fails(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, result = self.fixture(root)
            process = {**result["process"], "output_counts": {"noise_bytes": 0, "stderr_bytes": 1, "invalid_packets": 0}}
            self.assertEqual(judge(journal, process)["status"], "FAIL")

    def test_callback_summary_without_raw_counts_fails(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, result = self.fixture(root)
            worker = journal.read("worker_result.json")
            worker["callbacks"]["observed_count"] += 1
            self.write_bound_worker(journal, worker)
            decision = judge(journal, result["process"])
            self.assertEqual(decision["status"], "FAIL")
            self.assertEqual(decision["checks"]["callback_capture"], "FAIL")
            self.assertEqual(decision["checks"]["evidence_parse"], "PASS")
            self.assert_channel_consistent(journal, result["process"])

    def test_callback_sequence_gap_is_not_accepted_as_increasing(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, result = self.fixture(root)
            for name in ("callbacks.jsonl", "callback_attempts.jsonl"):
                rows = journal.rows(name)
                rows[1]["sequence"] = 3
                self.write_rows(journal, name, rows)
            self.assertEqual(judge(journal, result["process"])["status"], "FAIL")

    def test_callback_close_summary_requires_raw_close_event(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, result = self.fixture(root)
            rows = [row for row in journal.rows("worker_events.jsonl") if row["event"] != "callback_closed"]
            self.write_rows(journal, "worker_events.jsonl", rows)
            self.assertEqual(judge(journal, result["process"])["status"], "FAIL")

    def test_corrupt_steps_recovers_primary_cleanup_and_evidence_from_result(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            directory, result = run_offline_probe(root, "primary_and_stop_failure")
            journal = Journal(directory)
            worker = journal.read("worker_result.json")
            evidence_error = Redactor().error(OSError("fake failed record"))
            worker.update(evidence_failure=evidence_error, evidence_failure_count=1,
                          evidence_failures=[evidence_error])
            self.write_bound_worker(journal, worker)
            journal.path("steps.jsonl").write_bytes(b"{broken\n")
            journal.path("worker_events.jsonl").write_bytes(b"{broken\n")
            decision = judge(journal, result["process"])
            self.assertEqual(decision["status"], "FAIL")
            recovered = {row["kind"]: row for row in decision["failures"] if row.get("origin") == "worker_result"}
            self.assertEqual(set(recovered), {"primary_failure", "cleanup_failure", "evidence_failure"})
            self.assertEqual(recovered["primary_failure"]["evidence"], worker["primary_failure"])
            self.assertEqual(recovered["cleanup_failure"]["evidence"], worker["cleanup_failure"])

    def test_correct_class_with_wrong_or_empty_account_identity_fails_before_subscribe(self):
        for scenario in ("wrong_account", "empty_account_identity"):
            with self.subTest(scenario=scenario):
                result, records, _ = ProbeTests.run_scenario(self, scenario)
                self.assertEqual(result["status"], "FAIL")
                self.assertTrue(records["worker_result.json"]["stop_returned"])
                self.assertFalse(any(row.get("domain") for row in records["snapshots.jsonl"]))

    def test_subscribe_binding_raw_identity_conflict_is_independently_rejected(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, result = self.fixture(root)
            events = journal.rows("worker_events.jsonl")
            binding = next(row for row in events if row["event"] == "subscribe_binding")
            binding["facts"]["identity"]["public_fields"]["account_id"]["value"] = Redactor().token("wrong")
            self.write_rows(journal, "worker_events.jsonl", events)
            self.assertEqual(judge(journal, result["process"])["status"], "FAIL")

    def test_complete_is_package_completion_even_when_business_fails(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            directory, result = run_offline_probe(root, "query_exception")
            self.assertEqual(result["completion"], "COMPLETE")
            self.assertEqual(result["status"], "FAIL")
            journal = Journal(directory)
            self.assertNotIn("status", journal.read("COMPLETE"))
            consumed = consume_completed(journal)
            self.assertEqual(consumed["completion"], "COMPLETE")
            self.assertEqual(consumed["status"], "FAIL")

    def test_required_inventory_has_independent_protocol_oracle(self):
        self.assertEqual(PAYLOAD_FILES, {
            "manifest.json", "steps.jsonl", "snapshots.jsonl", "callbacks.jsonl", "callback_attempts.jsonl",
            "channel_events.jsonl", "worker_events.jsonl", "worker_result.json", "process_checks.jsonl",
            "normalized.jsonl", "summary.candidate.json",
        })

    def test_persisted_late_verification_receipt_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, _ = self.fixture(root)
            marker = journal.read("COMPLETE")
            marker["verification_finished"]["monotonic_ns"] = marker["run_timing"]["deadline_ns"] + 1
            self.write_json(journal, "COMPLETE", marker)
            self.assertEqual(consume_completed(journal)["completion"], "INCOMPLETE")

    def test_consumer_rejects_raw_error_even_with_fresh_hashes_and_pass_candidate(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, result = self.fixture(root)
            callbacks = journal.rows("callbacks.jsonl")
            attempts = journal.rows("callback_attempts.jsonl")
            callbacks[1]["raw"]["items"][0]["capture_errors"] = [Redactor().error(ValueError("fake raw error"))]
            attempts[1]["raw"] = callbacks[1]["raw"]
            self.write_rows(journal, "callbacks.jsonl", callbacks)
            self.write_rows(journal, "callback_attempts.jsonl", attempts)
            self.assertEqual(journal.read("summary.candidate.json")["decision"]["status"], "PASS")
            hashes = journal.read("hashes.json")
            hashes["files"] = journal.hashes()
            self.write_json(journal, "hashes.json", hashes)
            marker = journal.read("COMPLETE")
            marker["hashes_sha256"] = journal.digest("hashes.json")
            self.write_json(journal, "COMPLETE", marker)
            self.assertEqual(consume_completed(journal)["status"], "FAIL")

    def test_callback_payload_shape_status_and_binding_are_checked(self):
        from tools.full_qmt_readonly_probe_v01.supervisor import validate_callback_raw
        capture = Capture(Redactor())
        expected = capture.redactor.token(FAKE_ACCOUNT)
        for value in (None, SimpleNamespace(account_id=FAKE_ACCOUNT),
                      SimpleNamespace(account_id="wrong", status=0),
                      SimpleNamespace(account_id=FAKE_ACCOUNT, status=True)):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                validate_callback_raw("account_status", capture.returned(value), expected)
        with self.assertRaises(ValueError):
            validate_callback_raw("order", capture.returned(SimpleNamespace(account_id=FAKE_ACCOUNT)), expected)

    def test_error_callback_with_clean_worker_summary_cannot_pass(self):
        result, records, _ = ProbeTests.run_scenario(self, "callback_error")
        self.assertIsNone(records["worker_result.json"]["primary_failure"])
        self.assertEqual(records["worker_result.json"]["callbacks"]["capture_failure_count"], 0)
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any(row["kind"] == "callback_failure" for row in result["failures"]))

    def test_supervisor_cleanup_exception_type_is_also_private(self):
        secret_error = type("PRIVATE_ALPHA_ERROR", (Exception,), {})
        process = mock.Mock()
        process.poll.return_value = None
        process.terminate.side_effect = secret_error("PRIVATE_ALPHA_PATH")
        process.wait.side_effect = secret_error("PRIVATE_ALPHA_PATH")
        facts = cleanup_worker(process, wait_seconds=0)
        text = json.dumps(facts)
        self.assertNotIn("PRIVATE_ALPHA_ERROR", text)
        self.assertNotIn("PRIVATE_ALPHA_PATH", text)
        self.assertTrue(all(row["error_type"].startswith("type:") for row in facts["cleanup_errors"]))

    def test_channel_report_digest_binds_worker_result(self):
        from tools.full_qmt_readonly_probe_v01.supervisor import channel_facts
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, result = self.fixture(root)
            worker = journal.read("worker_result.json")
            worker["suppressed_output_characters"] += 1
            with self.assertRaises(ValueError):
                channel_facts(journal, worker, result["process"])


class ClosurePatchTests(unittest.TestCase):
    fixture = FinalHardeningTests.fixture
    write_json = HardeningTests.write_json
    write_rows = HardeningTests.write_rows
    assert_channel_consistent = HardeningTests.assert_channel_consistent

    def rehash_package(self, journal):
        # Rebind the bytes independently of business judgment. The existing PASS
        # candidate must not hide semantic corruption behind a stale hash failure.
        hashes = journal.read("hashes.json")
        hashes["files"] = {name: hashlib.sha256(journal.path(name).read_bytes()).hexdigest()
                           for name in PAYLOAD_FILES}
        self.write_json(journal, "hashes.json", hashes)
        marker = journal.read("COMPLETE")
        marker["hashes_sha256"] = hashlib.sha256(journal.path("hashes.json").read_bytes()).hexdigest()
        self.write_json(journal, "COMPLETE", marker)

    def reject_process_mutation(self, mutate):
        from tools.full_qmt_readonly_probe_v01.supervisor import validate_process_facts
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, result = self.fixture(root)
            process = journal.rows("process_checks.jsonl")[0]
            mutate(process)
            with self.assertRaises(ValueError):
                validate_process_facts(process)
            decision = judge(journal, process)
            self.assertEqual(decision["status"], "FAIL")
            self.assertEqual(decision["checks"]["process_schema"], "FAIL")
            self.assertEqual(decision["checks"]["worker_exit"], "FAIL")
            self.assert_channel_consistent(journal, result["process"])
            self.write_rows(journal, "process_checks.jsonl", [process])
            self.rehash_package(journal)
            self.assertEqual(journal.read("summary.candidate.json")["decision"]["status"], "PASS")
            consumed = consume_completed(journal)
            self.assertEqual(consumed["status"], "FAIL")
            self.assertEqual(consumed["completion"], "INCOMPLETE")

    def test_process_missing_cleanup_errors(self):
        self.reject_process_mutation(lambda row: row.pop("cleanup_errors"))

    def test_process_cleanup_errors_null(self):
        self.reject_process_mutation(lambda row: row.update(cleanup_errors=None))

    def test_process_cleanup_errors_false(self):
        self.reject_process_mutation(lambda row: row.update(cleanup_errors=False))

    def test_process_cleanup_errors_string(self):
        self.reject_process_mutation(lambda row: row.update(cleanup_errors=""))

    def test_process_missing_controller_error(self):
        self.reject_process_mutation(lambda row: row.pop("controller_error"))

    def test_process_all_decision_fields_are_required(self):
        from tools.full_qmt_readonly_probe_v01.supervisor import validate_process_facts
        required = {"started", "finished", "pid", "timeout", "controller_error", "exited", "exitcode",
                    "forced_termination", "cleanup_errors", "channel_ok", "output_counts"}
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            _, result = self.fixture(root)
            for name in required:
                process = dict(result["process"])
                del process[name]
                with self.subTest(name=name), self.assertRaises(ValueError):
                    validate_process_facts(process)

    def test_process_exact_types_and_nested_schemas(self):
        from tools.full_qmt_readonly_probe_v01.supervisor import validate_process_facts
        mutations = [
            ("exited", 1), ("forced_termination", 0), ("channel_ok", 1),
            ("exitcode", False), ("pid", True), ("pid", None),
            ("cleanup_errors", {}), ("cleanup_errors", [None]),
            ("cleanup_errors", [{"stage": "wait"}]),
            ("cleanup_errors", [{"stage": "wait", "error_type": False}]),
            ("controller_error", False), ("controller_error", ""), ("controller_error", {}),
            ("timeout", False), ("timeout", {}),
            ("output_counts", None), ("output_counts", {}),
            ("output_counts", {"noise_bytes": False, "stderr_bytes": 0, "invalid_packets": 0}),
            ("started", None), ("finished", False),
        ]
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, result = self.fixture(root)
            for key, value in mutations:
                with self.subTest(key=key, value=value):
                    process = {**result["process"], key: value}
                    # judge must never crash or turn false-ish wrong types into success.
                    decision = judge(journal, process)
                    self.assertEqual(decision["checks"]["process_schema"], "FAIL")
                    self.assertEqual(decision["checks"]["worker_exit"], "FAIL")
                    self.assertEqual(decision["status"], "FAIL")
                    self.assertIsNotNone(decision["evidence_error"])

    def test_process_valid_but_nonempty_errors_block_business_pass(self):
        from tools.full_qmt_readonly_probe_v01.supervisor import validate_process_facts
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, result = self.fixture(root)
            for field, value in (
                ("cleanup_errors", [{"stage": "wait", "error_type": "builtins.TimeoutError"}]),
                ("controller_error", {"reason": "supervision_failed", "error_type": "builtins.OSError"}),
            ):
                with self.subTest(field=field):
                    process = {**result["process"], field: value}
                    validate_process_facts(process)
                    decision = judge(journal, process)
                    self.assertEqual(decision["checks"]["process_schema"], "PASS")
                    self.assertEqual(decision["checks"]["evidence_parse"], "PASS")
                    self.assertEqual(decision["checks"]["worker_exit"], "FAIL")
                    self.assertEqual(decision["status"], "FAIL")

    def assert_timeline_rejected(self, journal, process, message):
        from tools.full_qmt_readonly_probe_v01.supervisor import validate_callback_timeline
        self.assert_channel_consistent(journal, process)
        with self.assertRaisesRegex(ValueError, message):
            validate_callback_timeline(journal.rows("steps.jsonl"), journal.rows("worker_events.jsonl"),
                                       journal.rows("callbacks.jsonl"), journal.rows("callback_attempts.jsonl"),
                                       journal.read("worker_result.json"))
        decision = judge(journal, process)
        self.assertEqual(decision["checks"]["process_schema"], "PASS")
        self.assertEqual(decision["checks"]["callback_timeline"], "FAIL")
        self.assertEqual(decision["status"], "FAIL")
        self.rehash_package(journal)
        self.assertEqual(consume_completed(journal)["completion"], "INCOMPLETE")

    def test_connect_disconnect_forged_stopping_phase(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, result = self.fixture(root)
            connect = [row for row in journal.rows("steps.jsonl") if row["name"] == "connect"]
            for name in ("callbacks.jsonl", "callback_attempts.jsonl"):
                rows = journal.rows(name)
                self.assertLessEqual(connect[0]["monotonic_ns"], rows[0]["monotonic_ns"])
                self.assertLessEqual(rows[0]["monotonic_ns"], connect[1]["monotonic_ns"])
                rows[0].update(event="disconnected", phase="stopping")
                self.write_rows(journal, name, rows)
            self.assert_timeline_rejected(journal, result["process"], "Callback phase/timeline conflict")
            self.assertEqual(journal.read("summary.candidate.json")["decision"]["status"], "PASS")

    def test_stop_disconnect_forged_running_phase(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            directory, result = run_offline_probe(root, "stop_callback")
            self.assertEqual(result["status"], "PASS")
            journal = Journal(directory)
            for name in ("callbacks.jsonl", "callback_attempts.jsonl"):
                rows = journal.rows(name)
                next(row for row in rows if row["event"] == "disconnected")["phase"] = "running"
                self.write_rows(journal, name, rows)
            self.assert_timeline_rejected(journal, result["process"], "Callback phase/timeline conflict")

    def test_stop_begin_cannot_precede_active_connect(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, result = self.fixture(root)
            connect_end = next(row for row in journal.rows("steps.jsonl")
                               if row["name"] == "connect" and row["phase"] == "end")
            events = journal.rows("worker_events.jsonl")
            next(row for row in events if row["event"] == "stop_begin")["monotonic_ns"] = connect_end["monotonic_ns"] - 1
            self.write_rows(journal, "worker_events.jsonl", events)
            self.assert_timeline_rejected(journal, result["process"], "Stop began before active lifecycle completed")

    def test_missing_stop_boundary_is_incomplete(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, result = self.fixture(root)
            self.write_rows(journal, "worker_events.jsonl",
                            [row for row in journal.rows("worker_events.jsonl") if row["event"] != "stop_begin"])
            self.assert_timeline_rejected(journal, result["process"], "Missing stop boundaries")

    def test_stop_end_cannot_precede_stop_return(self):
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, result = self.fixture(root)
            stop_end = next(row for row in journal.rows("steps.jsonl") if row["name"] == "stop" and row["phase"] == "end")
            events = journal.rows("worker_events.jsonl")
            next(row for row in events if row["event"] == "stop_end")["monotonic_ns"] = stop_end["monotonic_ns"] - 1
            self.write_rows(journal, "worker_events.jsonl", events)
            self.assert_timeline_rejected(journal, result["process"], "Stop boundary ordering conflict")

    def test_recorded_shutdown_boundaries_cover_stop_callbacks_and_failure(self):
        for scenario in ("stop_callback", "stop_exception"):
            with self.subTest(scenario=scenario):
                result, records, _ = ProbeTests.run_scenario(self, scenario)
                self.assertEqual(result["checks"]["callback_timeline"], "PASS")
                events = records["worker_events.jsonl"]
                begin = next(row["monotonic_ns"] for row in events if row["event"] == "stop_begin")
                end = next(row["monotonic_ns"] for row in events if row["event"] == "stop_end")
                stop = [row for row in records["steps.jsonl"] if row["name"] == "stop"]
                self.assertLessEqual(begin, stop[0]["monotonic_ns"])
                self.assertLessEqual(stop[1]["monotonic_ns"], end)
                if scenario == "stop_callback":
                    disconnect = next(row for row in records["callbacks.jsonl"] if row["event"] == "disconnected")
                    self.assertLessEqual(begin, disconnect["monotonic_ns"])
                    self.assertLessEqual(disconnect["monotonic_ns"], end)
                    self.assertEqual(result["checks"]["no_observed_disconnect"], "PASS")
                else:
                    self.assertEqual(result["status"], "FAIL")
                    self.assertIsNotNone(records["worker_result.json"]["cleanup_failure"])

    def test_stop_boundary_write_failure_does_not_skip_sdk_cleanup(self):
        original = Journal.append
        for event in ("stop_begin", "stop_end"):
            def fail_boundary(journal, name, row):
                if name == "worker_events.jsonl" and row.get("event") == event:
                    raise OSError(FAKE_ACCOUNT)
                return original(journal, name, row)
            with self.subTest(event=event), tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
                journal = Journal(new_run_directory(root))
                journal.initialize_streams()
                with mock.patch.object(Journal, "append", new=fail_boundary):
                    facts = run_worker(FakeSDK("normal"), journal.directory, configuration())
                self.assertTrue(facts["stop_returned"])
                self.assertIsNone(facts["primary_failure"])
                self.assertGreater(facts["evidence_failure_count"], 0)

    def noisy_exception(self, calls):
        class PRIVATE_SECRET_EXCEPTION(Exception):
            def __str__(self):
                calls.append("str")
                print(FAKE_ACCOUNT)
                print("PRIVATE_SECRET_PATH", file=sys.stderr)
                return FAKE_ACCOUNT

            def __repr__(self):
                calls.append("repr")
                print(FAKE_ACCOUNT)
                print("PRIVATE_SECRET_PATH", file=sys.stderr)
                return FAKE_ACCOUNT
        return PRIVATE_SECRET_EXCEPTION(FAKE_ACCOUNT)

    def assert_no_parent_leak(self, calls, stdout, stderr, facts):
        self.assertEqual(calls, [])
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        text = json.dumps(facts)
        for secret in (FAKE_ACCOUNT, "PRIVATE_SECRET_PATH", "PRIVATE_SECRET_EXCEPTION"):
            self.assertNotIn(secret, text)

    def test_supervisor_cleanup_never_formats_unknown_exception(self):
        calls, stdout, stderr = [], io.StringIO(), io.StringIO()
        error = self.noisy_exception(calls)
        process = mock.Mock()
        process.poll.return_value = None
        process.terminate.side_effect = error
        process.wait.side_effect = error
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            facts = cleanup_worker(process, wait_seconds=0)
        self.assertEqual([row["stage"] for row in facts["cleanup_errors"]], ["terminate", "wait"])
        self.assertTrue(all(row["error_type"].startswith("type:") for row in facts["cleanup_errors"]))
        self.assert_no_parent_leak(calls, stdout, stderr, facts)

    def test_supervisor_judge_never_formats_unknown_exception(self):
        calls, stdout, stderr = [], io.StringIO(), io.StringIO()
        error = self.noisy_exception(calls)
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            journal, result = self.fixture(root)
            with mock.patch.object(journal, "read", side_effect=error), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                decision = judge(journal, result["process"])
            self.assertEqual(decision["status"], "FAIL")
            self.assertTrue(decision["evidence_error"]["error_type"].startswith("type:"))
            self.assert_no_parent_leak(calls, stdout, stderr, decision)

    def test_supervisor_launch_error_never_formats_unknown_exception(self):
        calls, stdout, stderr = [], io.StringIO(), io.StringIO()
        error = self.noisy_exception(calls)
        with tempfile.TemporaryDirectory(prefix="positionx_fake_probe_") as root:
            with mock.patch("tools.full_qmt_readonly_probe_v01.supervisor.subprocess.Popen", side_effect=error), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                directory, result = run_offline_probe(root)
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["completion"], "INCOMPLETE")
            facts = Journal(directory).rows("process_checks.jsonl")[0]
            self.assertTrue(facts["controller_error"]["error_type"].startswith("type:"))
            self.assert_no_parent_leak(calls, stdout, stderr, {"result": result, "process": facts})


if __name__ == "__main__":
    unittest.main()
