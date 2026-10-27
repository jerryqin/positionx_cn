"""Local frozen-feature input checks only; no network, Broker or production writes."""
import unittest
import pandas as pd
from scripts.qualify_postclose_funnel import features_for, verify_unit
from scripts.run_ret120_execution_funnel_v01 import C


class FunnelInputTests(unittest.TestCase):
    def frame(self):
        dates=pd.bdate_range(end='2026-10-27',periods=70)
        return pd.DataFrame(dict(trade_date=dates,open=10.,high=11.,low=9.,close=10.,
                                 pre_close=10.,amount=2000000.,volume=200000.))

    def test_frozen_structure_adv20_and_gate_execute(self):
        f=features_for(self.frame(),'20261027',C()['structure_proxy'])
        self.assertTrue(f['history_ok'])
        self.assertEqual(f['adv20_amount'],2000000.)
        self.assertIn('entry_condition_state',f)

    def test_stale_or_missing_window_fails(self):
        for frame in (self.frame().iloc[:-1],self.frame().tail(64)):
            with self.assertRaisesRegex(ValueError,'WINDOW_INVALID'):
                features_for(frame,'20261027',C()['structure_proxy'])

    def test_missing_currency_evidence_does_not_qualify(self):
        self.assertFalse(verify_unit(None))


if __name__=='__main__':unittest.main()
