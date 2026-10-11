
import unittest, importlib.util
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]

def load(name,file):
    spec=importlib.util.spec_from_file_location(name,ROOT/"scripts"/file)
    m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

i=load("i","freeze_e1_equity_identity_v02.py")
q=load("q","verify_e1_backfill_qmt_v02.py")

class T(unittest.TestCase):
    def test_identity_domain(self):
        pol={"exclude_b_share_prefixes":["20","90"],"include_bse_prefixes":["43","83","87","88","92"],"bse_suffix":"BJ"}
        self.assertEqual(i.classify_equity("920106.SH",set(),pol)[0],"920106.BJ")
        self.assertEqual(i.classify_equity("200706.SZ",set(),pol)[0],None)
        self.assertEqual(i.classify_equity("000001.SZ",set(),pol)[0],"000001.SZ")
    def test_qmt_symbol(self):
        self.assertEqual(q.canonical_qmt_symbol(pd.Series({"symbol":"920106.SH"})),"920106.BJ")
        self.assertEqual(q.canonical_qmt_symbol(pd.Series({"symbol":"200706.SZ"})),None)
    def test_parse_time(self):
        self.assertEqual(str(q.parse_time_col("20250102").date()),"2025-01-02")

if __name__=="__main__":
    unittest.main()
