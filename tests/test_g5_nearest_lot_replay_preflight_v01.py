from pathlib import Path
import ast, unittest
ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"scripts/g5_nearest_lot_replay_preflight_v01.py"
class T(unittest.TestCase):
    def test_parse(self): ast.parse(SCRIPT.read_text(encoding="utf-8"))
    def test_read_only_no_qmt(self):
        s=SCRIPT.read_text(encoding="utf-8").lower()
        self.assertNotIn("xtquant",s); self.assertNotIn("xttrader",s)
    def test_no_production_sqlite(self):
        s=SCRIPT.read_text(encoding="utf-8").lower()
        self.assertNotIn("positionx.db",s)
    def test_no_network(self):
        s=SCRIPT.read_text(encoding="utf-8").lower()
        self.assertNotIn("requests.",s); self.assertNotIn("urllib",s)
if __name__=="__main__": unittest.main()
