import unittest,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from research.g4_isolated_preview_adapter import *
class T(unittest.TestCase):
    def test_incremental_target_empty_symbol(self):
        self.assertAlmostEqual(incremental_target_weight(0,1_000_000,50_000),0.05)
    def test_incremental_target_existing_external_position(self):
        self.assertAlmostEqual(incremental_target_weight(200_000,1_000_000,50_000),0.25)
    def test_invalid_asset(self):
        with self.assertRaises(ValueError):incremental_target_weight(0,0,50_000)
    def test_preview_evidence_full(self):
        e=preview_evidence({"status":"ORDER_PREVIEW","notional":49_000},50_000,60_000)
        self.assertTrue(e["pass"]);self.assertAlmostEqual(e["implementation_fidelity"],0.98)
    def test_preview_cannot_exceed_requested(self):
        e=preview_evidence({"status":"ORDER_PREVIEW","notional":50_100},50_000,60_000)
        self.assertFalse(e["pass"])
    def test_requested_cannot_exceed_shadow(self):
        e=preview_evidence({"status":"ORDER_PREVIEW","notional":20_000},30_000,20_000)
        self.assertFalse(e["pass"])
    def test_blocked_is_no_submit(self):
        e=preview_evidence({"status":"BLOCKED","notional":0},50_000,60_000)
        self.assertTrue(e["checks"]["no_submit_status"])
if __name__=="__main__":unittest.main()
