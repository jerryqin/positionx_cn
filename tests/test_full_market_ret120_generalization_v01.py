
import unittest, importlib.util
from pathlib import Path
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("m",ROOT/"scripts"/"run_full_market_ret120_generalization_v01.py")
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_quintiles_symbol_asc_first(self):
        rows=[]
        for i in range(10):
            rows.append({"signal_date":"2024-01-02","symbol":f"{i:06d}.SZ","ret120":float(i)})
        z=m.assign_quintiles(pd.DataFrame(rows))
        self.assertEqual(z.loc[z["symbol"]=="000000.SZ","bucket"].iloc[0],"Q1")
        self.assertEqual(z.loc[z["symbol"]=="000009.SZ","bucket"].iloc[0],"Q5")

    def test_quintile_ties_are_deterministic(self):
        rows=[{"signal_date":"2024-01-02","symbol":f"{i:06d}.SZ","ret120":1.0} for i in range(10)]
        z=m.assign_quintiles(pd.DataFrame(rows))
        q5=z[z["bucket"]=="Q5"]["symbol"].tolist()
        self.assertEqual(q5,["000008.SZ","000009.SZ"])

    def test_monotonicity(self):
        self.assertEqual(m.monotonicity({"Q1":1,"Q2":2,"Q3":3,"Q4":4,"Q5":5}),1.0)
        self.assertEqual(m.monotonicity({"Q1":1,"Q2":0,"Q3":3,"Q4":2,"Q5":5}),0.5)

    def test_fold_resolution(self):
        cfg={"frozen_folds":{"source":"x","F1":["2020-08-03","2022-10-17"],
                             "F2":["2022-10-18","2024-09-19"],
                             "F3":["2024-09-20","2026-09-24"]}}
        self.assertEqual(m.fold_for_date("2024-09-19",cfg),"F2")
        self.assertEqual(m.fold_for_date("2024-09-20",cfg),"F3")

    def test_verdict_candidate(self):
        cfg={"frozen_fast_reject_gate":{"at_least_2_horizons_monotonicity_ge":.5,
                "each_horizon_positive_folds_min":2}}
        ms=[{"q5_minus_q1":.01,"monotonicity":.5,"positive_folds":2},
            {"q5_minus_q1":.02,"monotonicity":.75,"positive_folds":3},
            {"q5_minus_q1":.03,"monotonicity":1.0,"positive_folds":2}]
        v,c=m.verdict(ms,cfg)
        self.assertEqual(v,"CANDIDATE")
        self.assertTrue(all(c.values()))

    def test_verdict_reject_one_negative_horizon(self):
        cfg={"frozen_fast_reject_gate":{"at_least_2_horizons_monotonicity_ge":.5,
                "each_horizon_positive_folds_min":2}}
        ms=[{"q5_minus_q1":-.01,"monotonicity":.5,"positive_folds":2},
            {"q5_minus_q1":.02,"monotonicity":.75,"positive_folds":3},
            {"q5_minus_q1":.03,"monotonicity":1.0,"positive_folds":2}]
        self.assertEqual(m.verdict(ms,cfg)[0],"REJECT")

if __name__=="__main__":
    unittest.main()
