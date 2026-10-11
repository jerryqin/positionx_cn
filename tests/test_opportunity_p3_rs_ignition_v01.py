
import unittest,importlib.util
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("m",ROOT/"scripts"/"run_opportunity_p3_rs_ignition_v01.py")
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class T(unittest.TestCase):
    def test_domain(self):
        self.assertTrue(m.is_target_symbol("600000.SH"));self.assertTrue(m.is_target_symbol("000001.SZ"))
        self.assertFalse(m.is_target_symbol("920106.BJ"));self.assertFalse(m.is_target_symbol("900901.SH"));self.assertFalse(m.is_target_symbol("200706.SZ"))
    def test_synthetic_adjust_and_ret20(self):
        n=25
        closes=[10.0+i*.1 for i in range(n)]
        df=pd.DataFrame({"trade_date":pd.date_range("2026-01-01",periods=n,freq="D"),
            "open":closes,"high":[x+.2 for x in closes],"low":[x-.2 for x in closes],
            "close":closes,"pre_close":[10.0]+closes[:-1]})
        z=m.synthetic_adjust(df);self.assertTrue(pd.notna(z.iloc[20]["ret20"]))
    def test_rank_direction(self):
        x=pd.DataFrame({"trade_date":pd.to_datetime(["2026-01-01"]*4),
                        "symbol":["a","b","c","d"],"ret20":[-.1,0,.1,.2]})
        z=m.add_rs_ranks(x);r=dict(zip(z["symbol"],z["rs20_rank"]))
        self.assertGreater(r["d"],r["a"])
    def test_label_success(self):
        g=pd.DataFrame({"adj_high":[1.05,1.26],"adj_low":[.98,.95]})
        self.assertEqual(m.trend_label(g,0,1.0,.25,2,-.10)["label"],1)
    def test_label_floor_fail(self):
        g=pd.DataFrame({"adj_high":[1.05,1.26],"adj_low":[.85,.95]})
        self.assertEqual(m.trend_label(g,0,1.0,.25,2,-.10)["label"],0)
if __name__=="__main__":unittest.main()
