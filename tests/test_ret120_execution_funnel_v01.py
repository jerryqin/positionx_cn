
import unittest,json, importlib.util
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
sp=importlib.util.spec_from_file_location('e',ROOT/'research/ret120_execution_funnel.py');e=importlib.util.module_from_spec(sp);sp.loader.exec_module(e)
P={'lookback':20,'breakout_buffer':0.003,'volume_lookback':20,'min_volume_ratio':1.10,'retest_tolerance':0.01,'retest_upper':0.005,'max_retest_sessions':5,'invalidation':0.015}
class T(unittest.TestCase):
    def test_sizing(self):
        r={'board_lot':100,'adv20_participation_max':.01,'momentum_risk_fraction_nav':.0025,'mean_reversion_risk_fraction_nav':.0015,'momentum_max_weight':.05,'mean_reversion_max_weight':.03}
        z=e.preview_size(50,47.5,100_000_000,2_000_000,'MOMENTUM',r)
        self.assertEqual(z['preview_shares']%100,0);self.assertGreaterEqual(z['preview_shares'],100);self.assertLessEqual(z['preview_weight'],.05+1e-9)
    def test_rank_does_not_use_ret120(self):
        x=pd.DataFrame({'symbol':['B','A'],'setup_state':['WATCH','WATCH'],'risk_distance_pct':[.05,.05],'adv20_amount':[1e8,1e8],'momentum_confidence':['STRONG','STRONG'],'ret120':[9,-9]})
        z=e.rank_panel(x,'MOMENTUM');self.assertEqual(z.iloc[0]['symbol'],'A')
    def test_no_history(self):
        d=pd.bdate_range('2026-01-01',periods=10);x=pd.DataFrame({'trade_date':d,'open':10,'high':10.1,'low':9.9,'close':10,'pre_close':10,'amount':1e8})
        self.assertEqual(e.breakout_retest_features(x,P)['setup_state'],'NO_HISTORY')

    def test_entry_condition_diagnostic(self):
        a=e.entry_condition_state(10.0,9.9,10.05)
        self.assertEqual(a['entry_condition_state'],'IN_ZONE_AT_T_CLOSE')
        b=e.entry_condition_state(10.2,9.9,10.05)
        self.assertEqual(b['entry_condition_state'],'ABOVE_ZONE_WAIT_PULLBACK')
        self.assertGreater(b['close_vs_open_zone_pct'],0)
        c=e.entry_condition_state(9.8,9.9,10.05)
        self.assertEqual(c['entry_condition_state'],'BELOW_ZONE_WAIT_RECOVERY')


    def test_prepare_entry_condition_counts_json_safe(self):
        x=pd.DataFrame({
            'status':['PREPARE_MANUAL','PREPARE_MANUAL','PREPARE_MANUAL'],
            'lane':['MOMENTUM','MOMENTUM','MEAN_REVERSION'],
            'entry_condition_state':['IN_ZONE_AT_T_CLOSE','ABOVE_ZONE_WAIT_PULLBACK','IN_ZONE_AT_T_CLOSE'],
        })
        d=x[x['status'].eq('PREPARE_MANUAL')].groupby(
            ['lane','entry_condition_state']
        ).size().unstack(fill_value=0).to_dict(orient='index')
        raw=json.dumps(d)
        self.assertIn('MOMENTUM', raw)
        self.assertIsInstance(d['MOMENTUM'], dict)

if __name__=='__main__':unittest.main()
