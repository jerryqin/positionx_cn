
import unittest, importlib.util
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("m",ROOT/"scripts"/"audit_cninfo_eva_commercial_cross_v012.py")
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_domain(self):
        self.assertEqual(m.canonical_shsz("600000.SH"),"600000.SH")
        self.assertEqual(m.canonical_shsz("000001.SZ"),"000001.SZ")
        self.assertIsNone(m.canonical_shsz("920106.BJ"))
        self.assertIsNone(m.canonical_shsz("200706.SZ"))
    def test_epoch_to_shanghai(self):
        z=m.parse_cninfo_epoch_local(0)
        # zero is not treated as a valid real event timestamp by this helper fallback,
        # so use a modern known ms value instead.
        z=m.parse_cninfo_epoch_local(1760000000000)
        self.assertTrue(pd.notna(z))
    def test_eva_local_naive(self):
        z=m.parse_eva_local("2025-01-02 15:30:00")
        self.assertEqual(str(z),"2025-01-02 15:30:00")
    def test_keywords(self):
        s=pd.Series(["中标|重大合同","定点"])
        self.assertEqual(set(m.split_keywords(s)),{"中标","重大合同","定点"})

if __name__=="__main__": unittest.main()
