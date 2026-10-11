import unittest,ast
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"scripts/audit_g5_deep_full_ticket_policy_v04.py"
class T(unittest.TestCase):
    def test_parse(self):ast.parse(SCRIPT.read_text(encoding="utf-8"))
    def test_capital_3m(self):self.assertIn("TOTAL_CAPITAL=3_000_000",SCRIPT.read_text(encoding="utf-8"))
    def test_no_prorata(self):
        s=SCRIPT.read_text(encoding="utf-8")
        self.assertIn("NO_FULL_TICKET_CAPACITY",s)
        self.assertNotIn("scale=alloc_total/desired_total",s)
    def test_symbol_asc(self):self.assertIn('mode=="SYMBOL_ASC"',SCRIPT.read_text(encoding="utf-8"))
    def test_random_100(self):
        s=SCRIPT.read_text(encoding="utf-8")
        self.assertIn("RANDOM_SEEDS=100",s);self.assertIn("RANDOM_PASS_FRACTION=0.90",s)
    def test_no_backfill(self):
        s=SCRIPT.read_text(encoding="utf-8")
        self.assertIn("chosen_rows=ordered[:k]",s)
        self.assertIn('skips["LOT_INFEASIBLE"]',s)
    def test_no_refresh_or_broker(self):
        s=SCRIPT.read_text(encoding="utf-8").lower()
        self.assertNotIn("download_history_data",s);self.assertNotIn("xttrader",s);self.assertNotIn("positionx.db",s)
if __name__=="__main__":unittest.main()
