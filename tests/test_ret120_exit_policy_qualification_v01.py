
import unittest
import pandas as pd
import numpy as np
from research import ret120_exit_policy_qualification as e

class T(unittest.TestCase):
    def test_synth_adjust_dividend_scale(self):
        x=pd.DataFrame({'trade_date':pd.to_datetime(['2026-01-01','2026-01-02']), 'open':[10,9], 'high':[10,9], 'low':[10,9], 'close':[10,9], 'pre_close':[10,9], 'suspend_flag':[0,0]})
        y=e.synth_adjust(x)
        self.assertTrue((y['adj_close']>0).all())

    def test_gate_duplicate_conflict(self):
        x=pd.DataFrame([
          {'recommendation_id':'r','_gate_run':'a','fire_state':'FIRE_MANUAL','actual_open':10,'suggested_shares':100,'suggested_notional':1000,'open_valid_low':9.9,'open_valid_high':10.1,'stop_raw':9.7,'lane':'MOMENTUM','symbol':'A','signal_date':'2026-10-08','fire_date':'2026-10-09'},
          {'recommendation_id':'r','_gate_run':'b','fire_state':'FIRE_MANUAL','actual_open':10.1,'suggested_shares':100,'suggested_notional':1010,'open_valid_low':9.9,'open_valid_high':10.1,'stop_raw':9.7,'lane':'MOMENTUM','symbol':'A','signal_date':'2026-10-08','fire_date':'2026-10-09'}])
        with self.assertRaises(RuntimeError): e.canonicalize_gate_rows(x)

    def _ctx(self):
        dates=pd.to_datetime(['2026-01-01','2026-01-02','2026-01-05','2026-01-06','2026-01-07'])
        g=pd.DataFrame({'symbol':['A']*5,'trade_date':dates,'open':[100,100,99,97,96], 'high':[101]*5,'low':[99,99,98,96,95], 'close':[100,100,98,97,96], 'pre_close':[100,100,100,98,97], 'suspend_flag':[0]*5})
        g=e.synth_adjust(g)
        ev=pd.Series({'symbol':'A','signal_date':'2026-01-01','fire_date':'2026-01-02','stop_raw':98.5,'actual_open':100.0})
        return e.event_context(ev,g,dates),dates

    def test_structural_close_not_wick(self):
        ctx,dates=self._ctx()
        z=e.first_structural_exit(ctx,dates,pd.Timestamp('2026-01-07'))
        self.assertEqual(str(z['trigger_date'].date()),'2026-01-05')
        self.assertEqual(str(z['exit_date'].date()),'2026-01-06')

    def test_hybrid_structural_before_horizon(self):
        ctx,dates=self._ctx()
        ev=pd.Series({'recommendation_id':'r','signal_date':'2026-01-01','fire_date':'2026-01-02','symbol':'A','lane':'MOMENTUM','universe_tier':'CSI500'})
        p={'policy_id':'X','type':'HYBRID','horizon_sessions':3,'winner_candidate':True}
        z=e.evaluate_policy(ev,p,ctx,dates,pd.Timestamp('2026-01-07'))
        self.assertEqual(z['exit_reason'],'STRUCTURAL')
        self.assertEqual(z['exit_date'],'2026-01-06')

    def test_same_day_horizon_wins(self):
        dates=pd.to_datetime(['2026-01-01','2026-01-02','2026-01-05'])
        g=pd.DataFrame({'symbol':['A']*3,'trade_date':dates,'open':[100,100,98], 'high':[101]*3,'low':[99,99,97], 'close':[100,100,98], 'pre_close':[100,100,100], 'suspend_flag':[0]*3})
        g=e.synth_adjust(g)
        ctx=e.event_context(pd.Series({'symbol':'A','signal_date':'2026-01-01','fire_date':'2026-01-02','stop_raw':98.5,'actual_open':100}),g,dates)
        ev=pd.Series({'recommendation_id':'r','signal_date':'2026-01-01','fire_date':'2026-01-02','symbol':'A','lane':'MOMENTUM','universe_tier':'CSI500'})
        p={'policy_id':'X','type':'HYBRID','horizon_sessions':1,'winner_candidate':True}
        z=e.evaluate_policy(ev,p,ctx,dates,pd.Timestamp('2026-01-05'))
        self.assertEqual(z['exit_reason'],'FIXED_HORIZON')
        self.assertEqual(z['exit_date'],'2026-01-05')

    def test_metrics(self):
        x=pd.DataFrame({'status':['CLOSED','CLOSED'],'gross_return':[.1,-.05],'net_return_20bp':[.098,-.052],'net_return_50bp':[.095,-.055],'holding_sessions':[20,10],'structural_exit':[False,True]})
        m=e.metrics(x); self.assertEqual(m['completed_n'],2); self.assertAlmostEqual(m['structural_exit_fraction'],.5)

if __name__=='__main__': unittest.main()
