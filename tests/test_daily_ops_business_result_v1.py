from __future__ import annotations

from contextlib import ExitStack
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.ops_runtime import extract_json

spec = importlib.util.spec_from_file_location(
    "daily_ops_business_result_test", ROOT / "scripts/run_daily_ops_orchestrator_v1.py"
)
orch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(orch)

TASK_FIELDS = (
    ("HEALTH_PREOPEN", "ready"),
    ("BROKER_RECONCILE_PREOPEN", "ok"),
    ("BROKER_RECONCILE_PRECLOSE", "ok"),
    ("BROKER_RECONCILE_FINAL", "ok"),
)


class DailyOpsBusinessResultTests(unittest.TestCase):
    def setUp(self):
        # Imports only define functions. Forbid runtime I/O unless a test
        # replaces it with an in-memory implementation.
        safety = ExitStack()
        self.addCleanup(safety.close)
        for name in ("run_process", "RunStore", "TaskLock", "qmt_is_trading_day"):
            safety.enter_context(patch.object(
                orch, name, side_effect=AssertionError(f"Real {name} is forbidden")
            ))
        safety.enter_context(patch(
            "core.ops_runtime.subprocess.run",
            side_effect=AssertionError("Real subprocesses are forbidden"),
        ))

    def result(self, payload, rc=0):
        return {
            "returncode": rc, "parsed_json": payload, "stdout": "fake stdout",
            "argv": ["fake-command"], "log_path": None,
        }

    def run_generic(self, task_id, payload, rc=0):
        with patch.object(orch, "run_step", return_value=self.result(payload, rc)):
            return orch.generic({}, {"steps": [["fake-command"]]}, {"task_id": task_id})

    def test_true_field_succeeds_with_or_without_pass_status(self):
        for task_id, field in TASK_FIELDS:
            for extra in ({}, {"status": "PASS"}, {"status": "PASS_BUSINESS_RESULT"}):
                with self.subTest(task=task_id, extra=extra):
                    status, _ = self.run_generic(task_id, {field: True, **extra})
                    self.assertEqual(status, "SUCCESS")

    def test_false_field_fails_even_with_pass_status_and_zero_rc(self):
        for task_id, field in TASK_FIELDS:
            for extra in ({}, {"status": "PASS"}):
                with self.subTest(task=task_id, extra=extra):
                    status, steps = self.run_generic(task_id, {field: False, **extra})
                    self.assertEqual(status, "FAILED")
                    self.assertEqual(steps[-1]["error_summary"], f"BUSINESS_RESULT_FALSE: {field}")

    def test_explicit_failure_status_overrides_true_field(self):
        for task_id, field in TASK_FIELDS:
            for value in ("FAIL", "ERROR_X", "BLOCKED_X", "WAITING_X", "PARTIAL_X",
                          "DEGRADED_X", "NOT_READY", "INCOMPLETE_X", "RED", " fail_x "):
                with self.subTest(task=task_id, status=value):
                    status, steps = self.run_generic(task_id, {field: True, "status": value})
                    self.assertEqual(status, "FAILED")
                    self.assertIn("BUSINESS_STATUS_NON_SUCCESS", steps[-1]["error_summary"])

    def test_missing_required_field_fails(self):
        for task_id, field in TASK_FIELDS:
            for payload in ({}, {"status": "PASS"}, {"unrelated": True}):
                with self.subTest(task=task_id, payload=payload):
                    status, steps = self.run_generic(task_id, payload)
                    self.assertEqual(status, "FAILED")
                    self.assertIn(f"MISSING_FIELD: {field}", steps[-1]["error_summary"])

    def test_required_field_rejects_truthy_and_other_non_boolean_values(self):
        for task_id, field in TASK_FIELDS:
            for value in (None, 0, 1, "true", "false", [], {}, [True]):
                with self.subTest(task=task_id, value=value):
                    status, steps = self.run_generic(task_id, {field: value})
                    self.assertEqual(status, "FAILED")
                    self.assertIn("INVALID_FIELD", steps[-1]["error_summary"])

    def test_non_object_results_fail_closed(self):
        for task_id, _ in TASK_FIELDS:
            for payload in (None, [], False, "PASS", 1):
                with self.subTest(task=task_id, payload=payload):
                    status, steps = self.run_generic(task_id, payload)
                    self.assertEqual(status, "FAILED")
                    self.assertIn("BUSINESS_RESULT_INVALID", steps[-1]["error_summary"])

    def test_invalid_optional_status_fails_closed(self):
        for task_id, field in TASK_FIELDS:
            for value in (None, False, 1, [], {}, "", " "):
                with self.subTest(task=task_id, value=value):
                    status, steps = self.run_generic(task_id, {field: True, "status": value})
                    self.assertEqual(status, "FAILED")
                    self.assertIn("BUSINESS_STATUS_INVALID", steps[-1]["error_summary"])

    def test_nonzero_process_rc_overrides_success_payload(self):
        for task_id, field in TASK_FIELDS:
            with self.subTest(task=task_id):
                status, steps = self.run_generic(task_id, {field: True, "status": "PASS"}, rc=2)
                self.assertEqual(status, "FAILED")
                self.assertEqual(steps[-1]["error_summary"], "SUBPROCESS_EXIT_NONZERO: rc=2")

    def test_failed_stdout_parsing_cannot_become_success(self):
        for task_id, field in TASK_FIELDS:
            for stdout in ("", "not JSON", '{"broken":',
                           json.dumps({field: True}) + "\ntrailing text"):
                with self.subTest(task=task_id, stdout=stdout):
                    payload = extract_json(stdout)
                    self.assertIsNone(payload)
                    status, _ = self.run_generic(task_id, payload)
                    self.assertEqual(status, "FAILED")

    def test_parsed_readiness_failure_propagates(self):
        payload = extract_json('diagnostic log\n{"ready": false, "broker": false}')
        status, _ = self.run_generic("HEALTH_PREOPEN", payload)
        self.assertEqual(status, "FAILED")

    def test_preopen_does_not_require_open_session_or_live_execution(self):
        status, _ = self.run_generic("HEALTH_PREOPEN", {
            "ready": True, "session_open": False, "execution_mode": "SAFE_PREVIEW",
        })
        self.assertEqual(status, "SUCCESS")

    def test_other_task_contracts_remain_compatible(self):
        cases = (
            ("G4_OPEN_CHECK", {"status": "PASS"}, "SUCCESS"),
            ("G4_OPEN_CHECK", {"status": "FAIL"}, "FAILED"),
            ("PIT_PREOPEN", None, "SUCCESS"),
            ("PIT_POSTCLOSE", None, "SUCCESS"),
            ("DAILY_REPORT", {"overall_status": "RED"}, "SUCCESS"),
            ("PILOT_READINESS", {"ready": False, "status": "PILOT_LIVE_READINESS_IN_PROGRESS"}, "SUCCESS"),
        )
        for task_id, payload, expected in cases:
            with self.subTest(task=task_id):
                status, _ = self.run_generic(task_id, payload)
                self.assertEqual(status, expected)

    def test_failure_stops_remaining_steps(self):
        with patch.object(orch, "run_step", return_value=self.result({"ready": False})) as run_step:
            status, steps = orch.generic({}, {"steps": [["first"], ["second"]]}, {
                "task_id": "HEALTH_PREOPEN",
            })
        self.assertEqual(status, "FAILED")
        self.assertEqual(len(steps), 1)
        run_step.assert_called_once()

    def test_failure_is_recorded_and_blocks_existing_dependency(self):
        records = []
        store = MagicMock()
        store.new.side_effect = lambda task_id, date, source: (
            Path("unused-run.json"), {"task_id": task_id, "run_id": "fake-run"}
        )
        store.successful.side_effect = lambda date, task_id: any(
            row["task_id"] == task_id and row["status"] == "SUCCESS" for row in records
        )

        def finish(path, obj, status, **updates):
            obj.update(updates, status=status)
            records.append(obj)
            return obj

        store.finish.side_effect = finish
        registry = {
            "calendar": {"market": "SH"},
            "ops_storage": {"run_root": "unused-runs", "lock_root": "unused-locks"},
            "tasks": {
                task_id: {
                    "calendar_scope": "TRADING_DAY", "exactly_once": True,
                    "handler": "GENERIC", "steps": [["fake-command"]],
                    "dependencies": [] if task_id == "HEALTH_PREOPEN" else ["HEALTH_PREOPEN"],
                }
                for task_id in ("HEALTH_PREOPEN", "BROKER_RECONCILE_PREOPEN")
            },
        }
        payload = {"ready": False, "broker": False}
        with ExitStack() as mocks:
            mocks.enter_context(patch.object(orch, "load_registry", return_value=(registry, Path("unused-registry.json"))))
            mocks.enter_context(patch.object(orch, "RunStore", return_value=store))
            mocks.enter_context(patch.object(orch, "TaskLock", return_value=MagicMock()))
            mocks.enter_context(patch.object(orch, "qmt_is_trading_day", return_value=True))
            mocks.enter_context(patch.object(orch, "sha256_file", return_value="fake-hash"))
            run_step = mocks.enter_context(patch.object(orch, "run_step", return_value=self.result(payload)))
            health = orch.execute_task("HEALTH_PREOPEN", "unused-project", "unused-data", "20261016", "test")
            broker = orch.execute_task("BROKER_RECONCILE_PREOPEN", "unused-project", "unused-data", "20261016", "test")
        self.assertEqual(health["status"], "FAILED")
        self.assertEqual(health["exit_code"], 1)
        self.assertEqual(health["parsed_summary"], payload)
        self.assertEqual(health["error_summary"], "BUSINESS_RESULT_FALSE: ready")
        self.assertEqual(broker["status"], "BLOCKED")
        self.assertEqual(broker["dependency_state"], {"HEALTH_PREOPEN": False})
        run_step.assert_called_once()


if __name__ == "__main__":
    unittest.main()
