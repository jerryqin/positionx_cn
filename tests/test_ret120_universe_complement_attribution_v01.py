
import unittest,importlib.util
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("m",ROOT/"scripts"/"run_ret120_universe_complement_attribution_v01.py")
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_diagnose_inverse(self):
        ex={f"H{h}":{"q5_minus_q1":-.01} for h in (20,60,120)}
        self.assertEqual(m.diagnose(ex),"COMPLEMENT_INVERSE")

    def test_diagnose_mostly_inverse(self):
        ex={"H20":{"q5_minus_q1":-.01},"H60":{"q5_minus_q1":-.02},"H120":{"q5_minus_q1":.01}}
        self.assertEqual(m.diagnose(ex),"COMPLEMENT_MOSTLY_INVERSE")

    def test_reproduction_gate(self):
        results={
          "CSI500_CURRENT":{"H20":{"q5_minus_q1":1.0,"monotonicity":.8,"positive_folds":1},
                            "H60":{"q5_minus_q1":2.0,"monotonicity":.9,"positive_folds":3},
                            "H120":{"q5_minus_q1":3.0,"monotonicity":1.0,"positive_folds":3}},
          "FULL_MARKET_CURRENT":{"H20":{"q5_minus_q1":-1.0,"monotonicity":-1.0,"positive_folds":0},
                                 "H60":{"q5_minus_q1":-2.0,"monotonicity":-.3,"positive_folds":1},
                                 "H120":{"q5_minus_q1":-3.0,"monotonicity":-.3,"positive_folds":1}},
        }
        c={"v04_reproduction_reference":{"numeric_tolerance":1e-12,
             "CSI500_CURRENT":{"H20":{"q5_minus_q1":1.0,"monotonicity":.8,"positive_folds":1},
                               "H60":{"q5_minus_q1":2.0,"monotonicity":.9,"positive_folds":3},
                               "H120":{"q5_minus_q1":3.0,"monotonicity":1.0,"positive_folds":3}},
             "FULL_MARKET_CURRENT":{"H20":{"q5_minus_q1":-1.0,"monotonicity":-1.0,"positive_folds":0},
                                    "H60":{"q5_minus_q1":-2.0,"monotonicity":-.3,"positive_folds":1},
                                    "H120":{"q5_minus_q1":-3.0,"monotonicity":-.3,"positive_folds":1}}}}
        self.assertTrue(m.reproduce_v04(results,c)["pass"])

if __name__=="__main__":
    unittest.main()
