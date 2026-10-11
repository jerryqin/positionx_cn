
from pathlib import Path
import unittest, yaml
ROOT=Path(__file__).resolve().parents[1]
CFG=ROOT/"config/opportunity/full_market_ret120_generalization_v0_1.yaml"

class T(unittest.TestCase):
    def test_current_cn_root_panel_path(self):
        c=yaml.safe_load(CFG.read_text(encoding="utf-8"))
        self.assertEqual(
            c["source"]["frozen_ret120_panel"],
            r"D:\positionx_cn\data\research\edge_discovery\trend_persistence_scan_csi500_panel_v0221.parquet"
        )
    def test_frozen_panel_hash_unchanged(self):
        c=yaml.safe_load(CFG.read_text(encoding="utf-8"))
        self.assertEqual(
            c["source"]["expected_frozen_ret120_panel_sha256"],
            "66b358a54c33866dd03aae187c1d60379a0317df3c15fe3d453babb430b338f1"
        )
if __name__=="__main__":
    unittest.main()
