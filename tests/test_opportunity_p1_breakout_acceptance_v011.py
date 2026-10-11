
import unittest,importlib.util
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("m",ROOT/"scripts"/"run_opportunity_p1_breakout_acceptance_v01.py")
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_domain(self):
        self.assertTrue(m.is_target_symbol("600000.SH"));self.assertTrue(m.is_target_symbol("000001.SZ"))
        self.assertFalse(m.is_target_symbol("920106.BJ"));self.assertFalse(m.is_target_symbol("900901.SH"));self.assertFalse(m.is_target_symbol("200706.SZ"))
    def test_cross_section_rank_not_raw_threshold(self):
        df=pd.DataFrame({"trade_date":["2026-01-01"]*4,"symbol":["a","b","c","d"],
          "compression_ratio":[.6,.8,1.0,1.2],"breakout_margin":[.1]*4,"range_release_ratio":[1.2]*4})
        cfg={"frozen_d2_parent":{"compression_rank_max":.25,"breakout_margin_min_exclusive":0.0,"range_release_ratio_min_exclusive":1.0}}
        rank,setup,sig=m.d2_signal_with_rank_method(df,cfg,"average")
        self.assertEqual(setup.tolist(),[True,False,False,False]);self.assertEqual(sig.tolist(),[True,False,False,False])
    def test_accept_reject(self):
        p=pd.DataFrame({"rel_close":[1.01,1.02,1.03],"rel_low":[.99,1.0,1.01],"rel_high":[1.02,1.03,1.04]})
        self.assertTrue(m.classify_acceptance(p,.98)["accepted_3d"])
        p2=pd.DataFrame({"rel_close":[1.01,.97,1.02],"rel_low":[.99,.96,1.0],"rel_high":[1.02,1.01,1.03]})
        self.assertTrue(m.classify_acceptance(p2,1.0)["rejected_3d"])
    def test_label(self):
        p=pd.DataFrame({"rel_high":[1.05,1.26],"rel_low":[.98,.95]})
        self.assertEqual(m.trend_label(p,1.0,.25,2,-.10)["label"],1)
    def test_expected_from_summary(self):
        s={"primary":{"all_valid_rows":10,"compression_setup_count":3,"breakout_signal_count":1,
          "by_year":{"2025":{"valid_rows":10,"compression_setup_count":3,"breakout_signal_count":1}}}}
        z=m.frozen_primary_expected(s);self.assertEqual(z["breakout_signal_count"],1)

if __name__=="__main__":unittest.main()
