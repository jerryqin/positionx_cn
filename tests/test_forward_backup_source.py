import unittest
from pathlib import Path

class ForwardBackupSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.mod = (root/'research/forward_backup.py').read_text(encoding='utf-8')
        cls.wrap = (root/'scripts/run_deep_oversold_forward_scheduled.ps1').read_text(encoding='utf-8')
    def test_backup_checkpoints_and_hashes(self):
        self.assertIn('con.execute("CHECKPOINT")', self.mod)
        self.assertIn('sha256_file', self.mod)
    def test_backup_is_staged_then_renamed(self):
        self.assertIn('.staging', self.mod)
        self.assertIn('staging.rename(final_dir)', self.mod)
    def test_wrapper_requires_backup_after_forward_success(self):
        self.assertIn('FORWARD_SUCCESS', self.wrap)
        self.assertIn('--only-if-session-today', self.wrap)
        self.assertIn('SUCCESS forward=0 backup=0', self.wrap)
    def test_restore_contract_is_matched_snapshot(self):
        self.assertIn('never mix backup keys', self.mod)
if __name__ == '__main__': unittest.main()
