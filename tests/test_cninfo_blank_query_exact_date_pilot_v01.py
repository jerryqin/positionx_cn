
import unittest, importlib.util
from pathlib import Path
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("m",ROOT/"scripts"/"run_cninfo_blank_query_exact_date_pilot_v01.py")
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_domain(self):
        self.assertEqual(m.canonical_shsz("600000.SH"),"600000.SH")
        self.assertEqual(m.canonical_shsz("000001.SZ"),"000001.SZ")
        self.assertIsNone(m.canonical_shsz("920106.BJ"))
        self.assertIsNone(m.canonical_shsz("200706.SZ"))

    def test_exchange(self):
        self.assertEqual(m.exchange_params("600000.SH"),("sse","sh"))
        self.assertEqual(m.exchange_params("000001.SZ"),("szse","sz"))

    def test_date(self):
        self.assertEqual(m.parse_local_date("2026-01-05 15:30:00"),"2026-01-05")

    def test_rank_stable(self):
        self.assertEqual(m.deterministic_rank("abc"),m.deterministic_rank("abc"))

if __name__=="__main__":
    unittest.main()
