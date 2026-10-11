import unittest, ast
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"scripts/audit_g5_dynamic_capital_frontier_v03.py"
class T(unittest.TestCase):
    def test_parse(self): ast.parse(SCRIPT.read_text(encoding="utf-8"))
    def test_grid_frozen(self):
        s=SCRIPT.read_text(encoding="utf-8")
        self.assertIn("CAPITAL_GRID=[4_000_000,5_000_000,6_000_000,8_000_000,10_000_000,12_000_000,15_000_000,20_000_000]",s)
    def test_stop_first_pass(self):
        s=SCRIPT.read_text(encoding="utf-8")
        self.assertIn('if row["full_pass"]:',s); self.assertIn("break",s)
    def test_no_threshold_relaxation(self):
        s=SCRIPT.read_text(encoding="utf-8")
        self.assertIn("BASE_POLICY.fidelity_min",s)
        self.assertIn("BASE_POLICY.max_abs_cagr_drift",s)
        self.assertIn("BASE_POLICY.max_abs_mdd_drift",s)
        self.assertIn("BASE_POLICY.max_abs_sharpe_drift",s)
    def test_no_refresh(self):
        s=SCRIPT.read_text(encoding="utf-8").lower()
        self.assertNotIn("download_history_data",s)
        self.assertNotIn("refresh_history",s)
    def test_no_broker(self):
        s=SCRIPT.read_text(encoding="utf-8").lower()
        self.assertNotIn("xttrader",s)
        self.assertNotIn("positionx.db",s)
if __name__=="__main__":unittest.main()
