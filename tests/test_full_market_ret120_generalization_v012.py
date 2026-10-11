
import unittest, importlib.util, inspect
from pathlib import Path
import pandas as pd
import yaml

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location(
    "m", ROOT/"scripts"/"run_full_market_ret120_generalization_v01.py"
)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_spearman_no_scipy_perfect_positive(self):
        a=pd.Series([10,20,30,40,50])
        b=pd.Series([1,2,3,4,5])
        self.assertAlmostEqual(m.spearman_corr_no_scipy(a,b),1.0)

    def test_spearman_no_scipy_perfect_negative(self):
        a=pd.Series([10,20,30,40,50])
        b=pd.Series([5,4,3,2,1])
        self.assertAlmostEqual(m.spearman_corr_no_scipy(a,b),-1.0)

    def test_spearman_handles_ties(self):
        a=pd.Series([1,1,2,3])
        b=pd.Series([5,5,6,7])
        self.assertAlmostEqual(m.spearman_corr_no_scipy(a,b),1.0)

    def test_bridge_has_no_scipy_spearman_call(self):
        src=inspect.getsource(m.bridge)
        self.assertNotIn('method="spearman"',src)
        self.assertIn("spearman_corr_no_scipy",src)

    def test_current_cn_root_panel_path(self):
        c=yaml.safe_load(
            (ROOT/"config/opportunity/full_market_ret120_generalization_v0_1.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            c["source"]["frozen_ret120_panel"],
            r"D:\positionx_cn\data\research\edge_discovery\trend_persistence_scan_csi500_panel_v0221.parquet"
        )

if __name__=="__main__":
    unittest.main()
