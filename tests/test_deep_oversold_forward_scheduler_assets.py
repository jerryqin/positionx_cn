import unittest
from pathlib import Path


class DeepOversoldForwardSchedulerAssetsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.wrapper = (
            cls.root / "scripts" / "run_deep_oversold_forward_scheduled.ps1"
        ).read_text(encoding="utf-8")
        cls.installer = (
            cls.root / "scripts" / "install_deep_oversold_forward_task.ps1"
        ).read_text(encoding="utf-8")

    def test_wrapper_calls_only_frozen_forward_runner(self):
        self.assertIn("run_deep_oversold_csi500_forward.py", self.wrapper)
        self.assertNotIn("core\\", self.wrapper.lower())
        self.assertNotIn("main.py", self.wrapper)

    def test_wrapper_has_bounded_retries(self):
        self.assertIn("[int]$MaxAttempts = 4", self.wrapper)
        self.assertIn("[int]$RetrySeconds = 900", self.wrapper)

    def test_task_is_weekdays_after_close(self):
        self.assertIn("Monday,Tuesday,Wednesday,Thursday,Friday", self.installer)
        self.assertIn('[string]$At = "15:15"', self.installer)

    def test_task_is_interactive_user_session(self):
        self.assertIn("-LogonType Interactive", self.installer)

    def test_multiple_instances_are_ignored(self):
        self.assertIn("-MultipleInstances IgnoreNew", self.installer)


if __name__ == "__main__":
    unittest.main()
