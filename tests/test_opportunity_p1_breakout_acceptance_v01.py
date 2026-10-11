
import unittest,importlib.util
from pathlib import Path
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("m",ROOT/"scripts"/"run_opportunity_p1_breakout_acceptance_v01.py")
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_domain(self):
        self.assertTrue(m.is_target_symbol("600000.SH"))
        self.assertTrue(m.is_target_symbol("000001.SZ"))
        self.assertFalse(m.is_target_symbol("920106.BJ"))
        self.assertFalse(m.is_target_symbol("900901.SH"))
        self.assertFalse(m.is_target_symbol("200706.SZ"))

    def test_d2_signal_reconstruction(self):
        cfg={"frozen_d2_parent":{"compression_rank_max":.25,"breakout_margin_min_exclusive":0.0,"range_release_ratio_min_exclusive":1.0}}
        df=pd.DataFrame({"trade_date":["2026-01-01"]*3,"symbol":["a","b","c"],
            "compression_ratio":[.2,.3,.2],"breakout_margin":[.01,.01,-.01],"range_release_ratio":[1.2,1.2,1.2]})
        self.assertEqual(m.reconstruct_d2_signal(df,cfg).tolist(),[True,False,False])

    def test_acceptance(self):
        f=pd.DataFrame({"open":[101,102,103],"high":[103,104,106],"low":[99,100,102],
                        "close":[102,103,105],"pre_close":[100,102,103]})
        p=m.normalized_future_path(f)
        self.assertTrue(m.classify_acceptance(p,.98)["accepted_3d"])

    def test_rejection(self):
        p=pd.DataFrame({"rel_close":[1.01,.97,1.02],"rel_low":[.99,.96,1.0],"rel_high":[1.02,1.01,1.03]})
        self.assertTrue(m.classify_acceptance(p,1.0)["rejected_3d"])

    def test_label(self):
        p=pd.DataFrame({"rel_high":[1.05,1.26],"rel_low":[.98,.95]})
        self.assertEqual(m.trend_label(p,1.0,.25,2,-.10)["label"],1)
        p2=pd.DataFrame({"rel_high":[1.05,1.26],"rel_low":[.85,.95]})
        self.assertEqual(m.trend_label(p2,1.0,.25,2,-.10)["label"],0)

if __name__=="__main__":unittest.main()
