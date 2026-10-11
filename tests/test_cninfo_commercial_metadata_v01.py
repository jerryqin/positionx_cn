
import unittest, importlib.util
from pathlib import Path
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("m",ROOT/"scripts"/"acquire_cninfo_commercial_metadata_v01.py")
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_month_windows(self):
        z=m.month_windows("2026-01-15","2026-03-02")
        self.assertEqual(str(z[0][0]),"2026-01-15")
        self.assertEqual(str(z[-1][1]),"2026-03-02")
        self.assertEqual(len(z),3)

    def test_canonical_symbol(self):
        self.assertEqual(m.canonical_symbol("600000"),"600000.SH")
        self.assertEqual(m.canonical_symbol("000001"),"000001.SZ")
        self.assertEqual(m.canonical_symbol("920106"),"920106.BJ")
        self.assertIsNone(m.canonical_symbol("200706"))

    def test_title_group(self):
        cfg={"title_patterns":{
            "HARD_CONFIRMATION":["中标","量产"],
            "SOFT_INTENT":["框架协议"]
        }}
        self.assertEqual(m.title_group("关于收到中标通知书的公告",cfg),"HARD_CONFIRMATION")
        self.assertEqual(m.title_group("战略框架协议公告",cfg),"SOFT_INTENT")

    def test_event_time_ms(self):
        x=m.parse_event_time(1760000000000)
        self.assertTrue(pd.notna(x))

if __name__=="__main__":
    unittest.main()
