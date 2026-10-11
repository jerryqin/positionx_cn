
import unittest,importlib.util
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
sp=importlib.util.spec_from_file_location('m',ROOT/'scripts'/'run_ret120_causal_regime_detector_v01.py')
m=importlib.util.module_from_spec(sp);sp.loader.exec_module(m)
class FakeScanner:
    @staticmethod
    def monotonicity_score(q):
        vals=[q[x] for x in ['Q1','Q2','Q3','Q4','Q5']]
        return 1.0 if vals==sorted(vals) else (-1.0 if vals==sorted(vals,reverse=True) else 0.0)
class T(unittest.TestCase):
    def test_rolling_evidence(self):
        rows=[]
        for i in range(25):
            rows.append({'signal_date':pd.Timestamp('2024-01-01')+pd.Timedelta(days=i),'exit_date':pd.Timestamp('2024-02-01')+pd.Timedelta(days=i),'horizon':20,'Q1':0,'Q2':1,'Q3':2,'Q4':3,'Q5':4,'spread':4,'mono':1})
        z=m.rolling_evidence(pd.DataFrame(rows),FakeScanner,20)
        self.assertTrue(pd.isna(z.iloc[18]['roll_spread']));self.assertEqual(z.iloc[19]['roll_spread'],4)
    def test_state_logic(self):
        evid=pd.DataFrame([{'exit_date':pd.Timestamp('2024-01-02'),'horizon':20,'roll_spread':-1.0,'roll_mono':-1.0,'evidence_count':20},{'exit_date':pd.Timestamp('2024-01-02'),'horizon':60,'roll_spread':-2.0,'roll_mono':-1.0,'evidence_count':20}])
        z=m.state_daily(evid,[pd.Timestamp('2024-01-02')],{'detector':{'rolling_matured_cohorts':20}});self.assertEqual(z.iloc[0]['state'],'REVERSAL')
    def test_state_is_asof_only(self):
        evid=pd.DataFrame([{'exit_date':pd.Timestamp('2024-01-03'),'horizon':20,'roll_spread':1.0,'roll_mono':1.0,'evidence_count':20},{'exit_date':pd.Timestamp('2024-01-03'),'horizon':60,'roll_spread':1.0,'roll_mono':1.0,'evidence_count':20}])
        z=m.state_daily(evid,[pd.Timestamp('2024-01-02'),pd.Timestamp('2024-01-03')],{'detector':{'rolling_matured_cohorts':20}})
        self.assertEqual(z.iloc[0]['state'],'UNAVAILABLE');self.assertEqual(z.iloc[1]['state'],'PERSISTENCE')
if __name__=='__main__':unittest.main()
