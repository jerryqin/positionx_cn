
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

    def test_pdna_scalar_safe(self):
        self.assertIsNone(m.code6(pd.NA))
        self.assertEqual(m.clean_title(pd.NA),"")
        self.assertIsNone(m.canonical_shsz(pd.NA))

    def test_normalize_with_pdna(self):
        df=pd.DataFrame({
            "announcement_id":["a","b"],
            "symbol":["600000.SH",pd.NA],
            "title":["公告",pd.NA],
            "event_time":["2025-01-01",pd.NA]
        })
        z=m.normalize(df,"X")
        self.assertEqual(z.loc[0,"canonical_symbol"],"600000.SH")
        self.assertTrue(pd.isna(z.loc[1,"canonical_symbol"]))
        self.assertEqual(z.loc[1,"title"],"")

    def test_unique(self):
        df=pd.DataFrame({"announcement_id":["a","a","b"],"event_time":pd.to_datetime(["2025-01-02","2025-01-01","2025-01-03"])})
        z=m.unique_ann(df)
        self.assertEqual(len(z),2)

if __name__=="__main__":
    unittest.main()
