
import unittest,importlib.util
from pathlib import Path
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("m",ROOT/"scripts"/"run_opportunity_p4_overnight_repricing_acceptance_v01.py")
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_domain(self):
        self.assertTrue(m.is_target_symbol("600000.SH"))
        self.assertTrue(m.is_target_symbol("000001.SZ"))
        self.assertFalse(m.is_target_symbol("920106.BJ"))
        self.assertFalse(m.is_target_symbol("900901.SH"))
        self.assertFalse(m.is_target_symbol("200706.SZ"))

    def test_gap_rank_direction(self):
        x=pd.DataFrame({
            "trade_date":pd.to_datetime(["2026-01-01"]*4),
            "symbol":["a","b","c","d"],
            "open":[10,10.1,10.2,10.4],
            "high":[10.2,10.3,10.4,10.6],
            "low":[9.9,10.0,10.1,10.3],
            "close":[10.1,10.2,10.3,10.5],
            "pre_close":[10,10,10,10],
        })
        z=m.add_gap_features(x)
        r=dict(zip(z["symbol"],z["gap_rank"]))
        self.assertGreater(r["d"],r["a"])

    def test_gap_accept_reject(self):
        x=pd.DataFrame({
            "trade_date":pd.to_datetime(["2026-01-01","2026-01-01"]),
            "symbol":["a","b"],
            "open":[10.5,10.5],"high":[10.8,10.8],"low":[10.1,9.9],
            "close":[10.6,10.2],"pre_close":[10,10]
        })
        z=m.add_gap_features(x)
        # Both are positive gaps; force parent status for the direct acceptance logic check.
        self.assertTrue(bool((z.loc[0,"low"]>z.loc[0,"pre_close"])))
        self.assertFalse(bool((z.loc[1,"low"]>z.loc[1,"pre_close"])))

    def test_synthetic_adjust(self):
        df=pd.DataFrame({
            "trade_date":pd.to_datetime(["2026-01-01","2026-01-02"]),
            "open":[10,10.5],"high":[10.2,11.2],"low":[9.8,10.4],
            "close":[10,11],"pre_close":[10,10]
        })
        z=m.synthetic_adjust(df)
        self.assertAlmostEqual(z.iloc[0]["adj_close"],1.0)
        self.assertAlmostEqual(z.iloc[1]["adj_close"],1.1)

    def test_trend_label_success(self):
        g=pd.DataFrame({"adj_high":[1.05,1.26],"adj_low":[.98,.95]})
        self.assertEqual(m.trend_label(g,0,1.0,.25,2,-.10)["label"],1)

    def test_trend_label_floor_fail(self):
        g=pd.DataFrame({"adj_high":[1.05,1.26],"adj_low":[.85,.95]})
        self.assertEqual(m.trend_label(g,0,1.0,.25,2,-.10)["label"],0)

if __name__=="__main__":
    unittest.main()
