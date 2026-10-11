
import unittest, importlib.util
from pathlib import Path
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("m",ROOT/"scripts"/"run_ret120_current_vintage_paired_universe_v04.py")
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class FakeScanner:
    @staticmethod
    def assign_quintiles(v):
        out=pd.Series(index=v.index,dtype="object")
        good=v.dropna()
        if len(good)<5:return out
        q=pd.qcut(good.rank(method="first"),5,labels=["Q1","Q2","Q3","Q4","Q5"])
        out.loc[q.index]=q.astype(str)
        return out

class T(unittest.TestCase):
    def test_symbol_ascending_tie_break(self):
        mem=pd.DataFrame({"signal_date":["2024-01-02"]*10,
                          "symbol":[f"{i:06d}.SZ" for i in range(9,-1,-1)]})
        ret=mem.rename(columns={"signal_date":"trade_date"}).copy()
        ret["ret120"]=1.0
        c={"signal":{"daily_feature_coverage_min_fraction":.8,
                     "daily_feature_coverage_min_absolute":5}}
        z,a=m.bucket_one(mem,ret,FakeScanner,c)
        self.assertEqual(z[z["bucket"]=="Q5"]["symbol"].tolist(),["000008.SZ","000009.SZ"])

    def test_compact_result(self):
        r={"full_sample":{"20":{"q5_minus_q1":.1,"monotonicity":.8,"qmeans":{}},
                          "60":{"q5_minus_q1":.2,"monotonicity":.9,"qmeans":{}},
                          "120":{"q5_minus_q1":.3,"monotonicity":1.0,"qmeans":{}}},
           "fast_reject":{"positive_folds_by_horizon":{"20":2,"60":3,"120":3},
                          "fast_reject_candidate":True}}
        z=m.compact_result(r)
        self.assertTrue(z["candidate"])
        self.assertEqual(z["H60"]["monotonicity"],.9)

if __name__=="__main__":
    unittest.main()
