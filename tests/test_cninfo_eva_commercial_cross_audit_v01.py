
import unittest, importlib.util
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("m",ROOT/"scripts"/"audit_cninfo_eva_commercial_cross_v01.py")
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_domain(self):
        self.assertEqual(m.canonical_shsz("600000.SH"),"600000.SH")
        self.assertEqual(m.canonical_shsz("000001.SZ"),"000001.SZ")
        self.assertIsNone(m.canonical_shsz("920106.BJ"))
        self.assertIsNone(m.canonical_shsz("200706.SZ"))
    def test_title(self):
        self.assertEqual(m.clean_title("<em>中标</em> 公告"),"中标公告")
    def test_unique(self):
        df=pd.DataFrame({"announcement_id":["a","a","b"],"event_time":pd.to_datetime(["2025-01-02","2025-01-01","2025-01-03"])})
        z=m.unique_ann(df)
        self.assertEqual(len(z),2)
    def test_ratio(self):
        self.assertEqual(m.ratio(8,10),0.8)
        self.assertIsNone(m.ratio(0,0))

if __name__=="__main__": unittest.main()
