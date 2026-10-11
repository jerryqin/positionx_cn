import unittest, importlib.util
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('m',ROOT/'scripts'/'run_full_market_52wh_generalization_v01.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_trim5(self):
        self.assertAlmostEqual(m.trim_mean_5(list(range(100))),49.5)
    def test_year_gate(self):
        self.assertTrue(m.year_gate(2,3,2));self.assertFalse(m.year_gate(1,3,2));self.assertTrue(m.year_gate(1,1,2))
    def test_strong(self):
        h={'horizon_pass':True,'pos_adjusted_trim5_positive':True,'pos_neg_date_equal_spread_positive':True}
        self.assertEqual(m.decision_from_horizons(h,h,{}),'STRONG_52WH_CANDIDATE')
    def test_promising(self):
        a={'horizon_pass':True,'pos_adjusted_trim5_positive':True,'pos_neg_date_equal_spread_positive':True}
        b={'horizon_pass':False,'pos_adjusted_trim5_positive':False,'pos_neg_date_equal_spread_positive':False}
        self.assertEqual(m.decision_from_horizons(a,b,{}),'PROMISING_HORIZON_CANDIDATE')
    def test_fast_reject(self):
        a={'horizon_pass':False,'pos_adjusted_trim5_positive':False,'pos_neg_date_equal_spread_positive':True}
        b={'horizon_pass':False,'pos_adjusted_trim5_positive':False,'pos_neg_date_equal_spread_positive':True}
        self.assertEqual(m.decision_from_horizons(a,b,{}),'FAST_REJECT')
if __name__=='__main__':unittest.main()
