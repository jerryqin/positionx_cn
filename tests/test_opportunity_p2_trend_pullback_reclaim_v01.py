
import unittest, importlib.util
from pathlib import Path
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("m",ROOT/"scripts"/"run_opportunity_p2_trend_pullback_reclaim_v01.py")
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_domain(self):
        self.assertTrue(m.is_target_symbol("600000.SH"))
        self.assertTrue(m.is_target_symbol("000001.SZ"))
        self.assertFalse(m.is_target_symbol("920106.BJ"))
        self.assertFalse(m.is_target_symbol("900901.SH"))
        self.assertFalse(m.is_target_symbol("200706.SZ"))

    def test_synthetic_adjust(self):
        df=pd.DataFrame({
            "trade_date":pd.to_datetime(["2026-01-01","2026-01-02"]),
            "open":[10,10.5],"high":[10.5,11],"low":[9.8,10.3],
            "close":[10,11],"pre_close":[10,10]
        })
        z=m.synthetic_adjust(df)
        self.assertAlmostEqual(z.iloc[0]["adj_close"],1.0)
        self.assertAlmostEqual(z.iloc[1]["adj_close"],1.1)

    def test_trend_label_success(self):
        g=pd.DataFrame({
            "adj_high":[1.05,1.26],
            "adj_low":[.98,.95]
        })
        r=m.trend_label(g,0,1.0,.25,2,-.10)
        self.assertEqual(r["label"],1)

    def test_trend_label_floor_fail(self):
        g=pd.DataFrame({
            "adj_high":[1.05,1.26],
            "adj_low":[.85,.95]
        })
        r=m.trend_label(g,0,1.0,.25,2,-.10)
        self.assertEqual(r["label"],0)

    def test_first_pullback_prior5_logic(self):
        c=pd.Series([10,10.1,10.2,10.3,10.4,10.5,9.9])
        ma=pd.Series([9.5]*7)
        above=c>=ma
        prior5=above.shift(1).rolling(5,min_periods=5).sum().eq(5)
        self.assertTrue(bool(prior5.iloc[-1]))

if __name__=="__main__":
    unittest.main()
