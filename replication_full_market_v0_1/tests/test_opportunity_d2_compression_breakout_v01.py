import sys, unittest
from pathlib import Path
import numpy as np, pandas as pd
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from opportunity.d2_compression_breakout import add_compression_breakout_features, add_forward_labels_from_t1, mark_setup_and_signal, evaluate_d2

class T(unittest.TestCase):
    def bars(self,n=180):
        close=np.linspace(10,13,n)*(1+0.002*np.sin(np.arange(n)))
        return pd.DataFrame({'symbol':['A.SH']*n,'trade_date':pd.date_range('2025-01-01',periods=n,freq='D').strftime('%Y%m%d'),'open':close*.995,'high':close*1.01,'low':close*.99,'close':close,'pre_close':np.r_[close[0],close[:-1]],'volume':100.0})
    def test_features_use_prior_windows(self):
        z=add_compression_breakout_features(self.bars())
        self.assertIn('compression_ratio',z.columns); self.assertIn('breakout_margin',z.columns); self.assertIn('range_release_ratio',z.columns)
    def test_signal_requires_setup_breakout_release(self):
        df=pd.DataFrame({'trade_date':['20250101']*4,'compression_ratio':[.5,.7,.9,1.1],'breakout_margin':[.01,.01,.01,.01],'range_release_ratio':[1.2,1.2,1.2,1.2]})
        z=mark_setup_and_signal(df,.25)
        self.assertEqual(int(z['d2_setup'].sum()),1); self.assertEqual(int(z['d2_signal'].sum()),1)
    def test_no_signal_without_breakout(self):
        df=pd.DataFrame({'trade_date':['20250101']*4,'compression_ratio':[.5,.7,.9,1.1],'breakout_margin':[-.01,.01,.01,.01],'range_release_ratio':[1.2,1.2,1.2,1.2]})
        z=mark_setup_and_signal(df,.25); self.assertFalse(bool(z.iloc[0]['d2_signal']))
    def test_labels_from_t1_open(self):
        z=add_compression_breakout_features(self.bars()); z=add_forward_labels_from_t1(z); i=40
        entry=float(z.iloc[i+1]['adj_open']); max_close=float(z.iloc[i+1:i+1+60]['adj_close'].max()); self.assertAlmostEqual(float(z.iloc[i]['D2_TREND25_60_mfe']),max_close/entry-1)
    def test_eval_baseline_setup(self):
        rows=[]
        for y in range(2020,2026):
            for i in range(10):
                rows.append({'trade_date':f'{y}01{i+1:02d}','d2_setup':True,'d2_signal':i<3,'D2_TREND25_60_label':1 if i<2 else 0,'D2_TREND25_60_mfe':.3,'D2_TREND25_60_mae_before_hit':-.05,'D2_TREND25_60_time_to_target':20.})
        r=evaluate_d2(pd.DataFrame(rows),'D2_TREND25_60_label',1,1.0,0.0,1)
        self.assertEqual(r['compression_setup_count'],60); self.assertEqual(r['breakout_signal_count'],18); self.assertGreater(r['breakout_signal_rate'],r['compression_setup_rate'])
    def test_duckdb_reserved_aliases_are_explicitly_quoted_in_runner(self):
        script=(ROOT/'scripts/run_opportunity_d2_compression_breakout_v01.py').read_text(encoding='utf-8')
        for fragment in [
            'cast(m.open as double) as "open"',
            'cast(m.high as double) as "high"',
            'cast(m.low as double) as "low"',
            'cast(m.close as double) as "close"',
            'cast(m.pre_close as double) as "pre_close"',
            'cast(m.volume as double) as "volume"',
        ]:
            self.assertIn(fragment, script)
        self.assertNotIn('cast(m.close as double) close', script)
if __name__=='__main__': unittest.main()
