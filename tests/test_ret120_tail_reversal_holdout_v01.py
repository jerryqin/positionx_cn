
import unittest,importlib.util
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sp=importlib.util.spec_from_file_location("m",ROOT/"scripts"/"run_ret120_tail_reversal_holdout_v01.py")
m=importlib.util.module_from_spec(sp);sp.loader.exec_module(m)
class T(unittest.TestCase):
    def test_support_gate(self):
        R={
          "EX_CSI1800_HOLDOUT":{"H20":{"spread":-.02,"monotonicity":-.8},"H60":{"spread":-.03,"monotonicity":-.4}},
          "CSI1800_HOLDOUT":{"H20":{"spread":-.005,"monotonicity":-.1},"H60":{"spread":.01,"monotonicity":.7}}
        }
        self.assertEqual(m.gate(R)["status"],"HOLDOUT_SUPPORTS_TAIL_REVERSAL_HYPOTHESIS")
    def test_no_support_if_h60_flips(self):
        R={
          "EX_CSI1800_HOLDOUT":{"H20":{"spread":-.02,"monotonicity":-.8},"H60":{"spread":.01,"monotonicity":.2}},
          "CSI1800_HOLDOUT":{"H20":{"spread":-.005,"monotonicity":-.1},"H60":{"spread":.02,"monotonicity":.7}}
        }
        self.assertEqual(m.gate(R)["status"],"HOLDOUT_DOES_NOT_SUPPORT_TAIL_REVERSAL_HYPOTHESIS")
if __name__=="__main__":unittest.main()
