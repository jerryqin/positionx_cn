
import unittest, importlib.util
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
sp=importlib.util.spec_from_file_location('runner',ROOT/'scripts'/'run_ret120_universe_prior_forward_v10.py')
runner=importlib.util.module_from_spec(sp);sp.loader.exec_module(runner)
rp=importlib.util.spec_from_file_location('rules',ROOT/'research'/'ret120_universe_prior.py')
rules=importlib.util.module_from_spec(rp);rp.loader.exec_module(rules)

class T(unittest.TestCase):
    def test_norm_date(self):
        self.assertEqual(runner.norm_date('20260930'),'20260930')
        self.assertEqual(runner.norm_date(20260930),'20260930')
        self.assertEqual(runner.norm_date(pd.Timestamp('2026-09-30')),'20260930')

    def test_series_for_field_centric(self):
        raw={'close':pd.DataFrame([[1.0,2.0]],index=['000001.SZ'],columns=['20260929','20260930'])}
        s=runner.series_for(raw,'close','000001.SZ')
        self.assertEqual(list(s.values),[1.0,2.0])

    def test_series_for_symbol_centric(self):
        raw={'000001.SZ':pd.DataFrame({'close':[1.0,2.0]},index=['20260929','20260930'])}
        s=runner.series_for(raw,'close','000001.SZ')
        self.assertEqual(list(s.values),[1.0,2.0])

    def test_ret120_requires_t_and_121(self):
        dates=pd.bdate_range(end='2026-09-30',periods=121)
        df=pd.DataFrame({'trade_date':dates,'symbol':'000001.SZ','close':range(100,221)})
        z=runner.compute_ret120(df,'2026-09-30')
        self.assertEqual(len(z),1)
        stale=df[df.trade_date<pd.Timestamp('2026-09-30')]
        self.assertEqual(len(runner.compute_ret120(stale,'2026-09-30')),0)


    def test_refresh_chunk_size_is_small_and_capture_no_auto_refresh(self):
        c=runner.cfg()
        self.assertLessEqual(int(c['qmt_history']['refresh_chunk_size']),100)
        self.assertFalse(bool(c['qmt_history']['capture_auto_refresh']))

    def test_rules_unchanged(self):
        self.assertTrue(rules.classify_ret120_prior('CSI300','Q5')['momentum_candidate'])
        self.assertTrue(rules.classify_ret120_prior('EX_CSI1800','Q5')['momentum_penalty'])
        self.assertTrue(rules.classify_ret120_prior('EX_CSI1800','Q1')['mean_reversion_candidate'])

if __name__=='__main__': unittest.main()
