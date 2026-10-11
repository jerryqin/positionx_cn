
import unittest,importlib.util
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sp=importlib.util.spec_from_file_location("m",ROOT/"scripts"/"run_ret120_index_ladder_attribution_v01.py")
m=importlib.util.module_from_spec(sp);sp.loader.exec_module(m)
class T(unittest.TestCase):
    def test_localized(self):
        R={}
        for u in ["CSI300_CURRENT","CSI500_CURRENT","CSI1000_CURRENT"]:
            R[u]={"H20":{"spread":.01},"H60":{"spread":.02},"H120":{"spread":.03}}
        R["EX_CSI1800_CURRENT"]={"H20":{"spread":-.01},"H60":{"spread":-.02},"H120":{"spread":-.03}}
        self.assertEqual(m.verdict(R),"INVERSION_LOCALIZED_BELOW_CSI1800")
    def test_tail_mostly(self):
        R={}
        for u in ["CSI300_CURRENT","CSI500_CURRENT","CSI1000_CURRENT"]:
            R[u]={"H20":{"spread":-.01},"H60":{"spread":.02},"H120":{"spread":.03}}
        R["EX_CSI1800_CURRENT"]={"H20":{"spread":-.01},"H60":{"spread":-.02},"H120":{"spread":.03}}
        self.assertEqual(m.verdict(R),"TAIL_MOSTLY_INVERSE")
if __name__=="__main__":unittest.main()
