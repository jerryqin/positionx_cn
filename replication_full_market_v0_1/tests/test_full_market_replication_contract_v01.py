import unittest,yaml
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
FULL='D:\\positionx_data\\research\\opportunity_engine\\full_market_universe_v0_1\\full_market_tradable_universe_v0_1.parquet'
class T(unittest.TestCase):
 def test_p2_p3_p4_use_fullmarket(self):
  for n in ['p2_trend_pullback_reclaim_v0_1.yaml','p3_rs_ignition_v0_1.yaml','p4_overnight_repricing_acceptance_v0_1.yaml']:
   c=yaml.safe_load((ROOT/'config/opportunity'/n).read_text(encoding='utf-8')); self.assertEqual(c['source']['universe_panel'],FULL)
 def test_no_parameter_scan_stays_true(self):
  names=['trend_ignition_v0_1.yaml','d1_absorption_v0_1.yaml','d1_capitulation_recovery_v0_1.yaml','d2_compression_breakout_v0_1.yaml','d3_market_breadth_ignition_v0_1.yaml','p1_breakout_acceptance_v0_1.yaml','p2_trend_pullback_reclaim_v0_1.yaml','p3_rs_ignition_v0_1.yaml','p4_overnight_repricing_acceptance_v0_1.yaml']
  for n in names:
   c=yaml.safe_load((ROOT/'config/opportunity'/n).read_text(encoding='utf-8'))
   mech=c.get('mechanism',{})
   if 'no_parameter_scan' in mech: self.assertTrue(mech['no_parameter_scan'],n)
if __name__=='__main__': unittest.main()
