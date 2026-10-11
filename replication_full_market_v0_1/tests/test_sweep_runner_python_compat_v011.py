
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
RUNNER=ROOT/"scripts"/"run_full_market_replication_sweep_v01.py"

class TestSweepRunnerCompat(unittest.TestCase):
    def test_no_nested_fstring_log_path(self):
        src=RUNNER.read_text(encoding="utf-8")
        self.assertNotIn("see {logs/f", src)
        self.assertIn("log_path=logs/f", src)
        self.assertIn("failed; see {log_path}", src)

    def test_runner_compiles(self):
        src=RUNNER.read_text(encoding="utf-8")
        compile(src, str(RUNNER), "exec")

if __name__=="__main__":
    unittest.main()
