
import unittest,importlib.util
from pathlib import Path
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("m",ROOT/"scripts"/"run_full_market_ret120_qmt_v02.py")
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_ret120_exact_shift_120(self):
        s=pd.Series(range(1,130),dtype=float)
        r=s/s.shift(120)-1
        self.assertAlmostEqual(r.iloc[120],121.0/1.0-1.0)

    def test_qcut_symbol_asc_rank_first(self):
        z=pd.DataFrame({"signal_date":["2024-01-02"]*10,
                        "symbol":[f"{i:06d}.SZ" for i in range(10)],
                        "ret120":[1.0]*10})
        q=m.assign_quintiles(z)
        self.assertEqual(q[q["bucket"]=="Q5"]["symbol"].tolist(),["000008.SZ","000009.SZ"])

    def test_normalize_qmt_yyyymmdd_index(self):
        df=pd.DataFrame({"close":[10.0,11.0]},index=["20240102","20240103"])
        z=m.normalize_qmt_frame("600000.SH",df)
        self.assertEqual(z["trade_date"].dt.strftime("%Y%m%d").tolist(),["20240102","20240103"])

    def test_monotonicity(self):
        self.assertEqual(m.monotonicity({"Q1":1,"Q2":2,"Q3":3,"Q4":4,"Q5":5}),1.0)

    def test_verdict_candidate(self):
        cfg={"frozen_fast_reject_gate":{"at_least_2_horizons_monotonicity_ge":.5,
                                        "each_horizon_positive_folds_min":2}}
        ms=[{"q5_minus_q1":.01,"monotonicity":.5,"positive_folds":2},
            {"q5_minus_q1":.02,"monotonicity":.75,"positive_folds":3},
            {"q5_minus_q1":.03,"monotonicity":1.0,"positive_folds":2}]
        self.assertEqual(m.verdict(ms,cfg)[0],"CANDIDATE")

if __name__=="__main__":
    unittest.main()
