
import unittest, importlib.util
from pathlib import Path
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location(
    "m", ROOT/"scripts"/"run_full_market_52wh_generalization_v01.py"
)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_trim5(self):
        self.assertAlmostEqual(m.trim_mean_5(list(range(100))),49.5)

    def test_year_gate(self):
        self.assertTrue(m.year_gate(2,3,2))
        self.assertFalse(m.year_gate(1,3,2))
        self.assertTrue(m.year_gate(1,1,2))

    def test_bool_mask_null_is_false(self):
        x=pd.Series([True, False, None, pd.NA])
        self.assertEqual(m.bool_mask(x).tolist(),[True,False,False,False])

    def test_complete_filter_with_na(self):
        df=pd.DataFrame({
            "exclude_reason":[None,None,None,"X"],
            "complete_h120_cohort":[True,None,False,True],
        })
        eligible=df[df["exclude_reason"].isna()]
        complete=eligible[m.bool_mask(eligible["complete_h120_cohort"])]
        self.assertEqual(len(complete),1)

    def test_decision_strong(self):
        h={"horizon_pass":True,"pos_adjusted_trim5_positive":True,
           "pos_neg_date_equal_spread_positive":True}
        self.assertEqual(m.decision_from_horizons(h,h,{"gate":{}}),
                         "STRONG_52WH_CANDIDATE")

    def test_decision_promising(self):
        a={"horizon_pass":True,"pos_adjusted_trim5_positive":True,
           "pos_neg_date_equal_spread_positive":True}
        b={"horizon_pass":False,"pos_adjusted_trim5_positive":False,
           "pos_neg_date_equal_spread_positive":False}
        self.assertEqual(m.decision_from_horizons(a,b,{"gate":{}}),
                         "PROMISING_HORIZON_CANDIDATE")

    def test_decision_fast_reject(self):
        a={"horizon_pass":False,"pos_adjusted_trim5_positive":False,
           "pos_neg_date_equal_spread_positive":True}
        b={"horizon_pass":False,"pos_adjusted_trim5_positive":False,
           "pos_neg_date_equal_spread_positive":True}
        self.assertEqual(m.decision_from_horizons(a,b,{"gate":{}}),
                         "FAST_REJECT")

    def test_source_contains_null_safe_sql(self):
        src=(ROOT/"scripts"/"run_full_market_52wh_generalization_v01.py").read_text(encoding="utf-8")
        self.assertIn("COALESCE(e.complete_h120_cohort,FALSE)=FALSE",src)
        self.assertIn("COALESCE(",src)
        self.assertIn("complete=eligible[bool_mask(",src)

if __name__=="__main__":
    unittest.main()
