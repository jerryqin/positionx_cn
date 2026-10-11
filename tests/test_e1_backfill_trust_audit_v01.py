
import unittest, importlib.util
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("m",ROOT/"scripts"/"audit_e1_backfill_trust_v01.py")
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_suffix(self):
        self.assertEqual(m.suffix_of("430001.BJ"),"BJ")
        self.assertEqual(m.suffix_of("sh.600000"),"SH")
        self.assertEqual(m.suffix_of("SZ000001"),"SZ")
    def test_deterministic_rank(self):
        self.assertEqual(m.deterministic_symbol_rank("000001.SZ"),m.deterministic_symbol_rank("000001.SZ"))
    def test_sample_dates(self):
        df=pd.DataFrame({
            "symbol":["A"]*10,
            "trade_date":pd.date_range("2025-01-01",periods=10),
            "open":range(10),"high":range(10),"low":range(10),"close":range(10),"pre_close":range(10)
        })
        z=m.sample_dates(df,["A"],5)
        self.assertGreaterEqual(len(z),4)
        self.assertLessEqual(len(z),5)

if __name__=="__main__":
    unittest.main()
