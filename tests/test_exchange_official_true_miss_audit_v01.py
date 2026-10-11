
import unittest,importlib.util,tempfile
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("m",ROOT/"scripts"/"audit_exchange_official_true_misses_v01.py")
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class T(unittest.TestCase):
    def test_domain(self):
        self.assertEqual(m.exchange("600000.SH"),"SH");self.assertEqual(m.exchange("000001.SZ"),"SZ");self.assertIsNone(m.exchange("920106.BJ"))
    def test_title(self): self.assertEqual(m.norm_title("关于《重大合同》的公告"),"重大合同的公告")
    def test_match(self): self.assertEqual(m.best_match("关于重大合同的公告",[{"title":"关于重大合同的公告"}])["ratio"],1.0)
    def test_filter(self):
        df=pd.DataFrame({"announcement_id":["a","b"],"canonical_symbol":["600000.SH","000001.SZ"],"event_date":["2026-01-01","2026-01-02"],
                         "title":["A","B"],"query_completed":[True,True],"recovered_by_blank_exact_date":[False,True]})
        p=tempfile.NamedTemporaryFile(suffix=".csv",delete=False).name;df.to_csv(p,index=False)
        self.assertEqual(len(m.load_true_misses(p)),1)
if __name__=="__main__":unittest.main()
