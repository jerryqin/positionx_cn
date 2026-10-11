import unittest
from pathlib import Path


class ForwardOpsHardeningAssetsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.runner = (root/"scripts/run_deep_oversold_csi500_forward.py").read_text(encoding="utf-8")
        cls.status = (root/"scripts/status_deep_oversold_forward_task.ps1").read_text(encoding="utf-8")

    def test_status_reads_v11_output_directory(self):
        self.assertIn("forward_deep_oversold_csi500_v1_1", self.status)
        self.assertNotIn("forward_deep_oversold_csi500_v1\\state.json", self.status)

    def test_benchmark_history_is_explicitly_refreshed(self):
        self.assertIn(
            "load_histories([POLICY.benchmark], bars=120, refresh=True)",
            self.runner,
        )

    def test_current_universe_histories_are_refreshed(self):
        self.assertIn(
            "histories = load_histories(symbols, bars=120, refresh=True)",
            self.runner,
        )

    def test_missed_forward_session_fails_closed(self):
        self.assertIn("BLOCKED_MISSED_FORWARD_SESSION", self.runner)
        self.assertIn("BLOCKED_FORWARD_SESSION_NOT_TODAY", self.runner)

    def test_session_commit_is_atomic(self):
        self.assertIn('store.con.execute("BEGIN TRANSACTION")', self.runner)
        self.assertIn('store.con.execute("COMMIT")', self.runner)
        self.assertIn('store.con.execute("ROLLBACK")', self.runner)


if __name__ == "__main__":
    unittest.main()
