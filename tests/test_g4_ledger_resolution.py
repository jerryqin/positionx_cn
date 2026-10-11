import unittest,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from research.g4_ledger_resolution import choose_base_ledger,LedgerResolutionError

class T(unittest.TestCase):
    def test_current_deep_names(self):
        self.assertEqual(choose_base_ledger({"BASE20":3,"SHADOW50":3},3),"BASE20")
    def test_zero_positions_no_ledger(self):
        self.assertIsNone(choose_base_ledger({},0))
    def test_zero_positions_shadow_only(self):
        self.assertIsNone(choose_base_ledger({"SHADOW50":0},0))
    def test_ambiguous_base_fails(self):
        with self.assertRaises(LedgerResolutionError):
            choose_base_ledger({"BASE20":3,"BASE_ALT":3},3)
    def test_count_mismatch_fails(self):
        with self.assertRaises(LedgerResolutionError):
            choose_base_ledger({"BASE20":2,"SHADOW50":2},3)
    def test_shadow_never_selected(self):
        with self.assertRaises(LedgerResolutionError):
            choose_base_ledger({"SHADOW50":3},3)

if __name__=="__main__":unittest.main()
