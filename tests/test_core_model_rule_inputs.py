"""Execution evidence branch checks; in-memory frames and verifier fake only."""
import json
import unittest
from unittest.mock import patch
import pandas as pd
from core import p0_rq_input as rq

class ExecutionRuleInputs(unittest.TestCase):
    def frame(self):
        identity=dict(order_book_id='600000.XSHG',exchange='XSHG',type='CS',board_type='MainBoard')
        row=dict(order_book_id='600000.XSHG',status_suspended=False,research_factor=2.328/2.33,
            status_available_at='2026-10-28T18:00:00+08:00',bar_available_at='2026-10-28T19:00:00+08:00',
            price_tick=None,tick_available_at=None,tick_source='EXCHANGE_RULE_DERIVED',rule_price_tick=.01,
            tick_rule_evidence=json.dumps(dict(identity=identity,evidence={'rule_id':'FAKE_RULE'})),
            volume=1000,num_trades=10,is_suspended=False,open=2.328,close=2.328,
            raw_open=2.33,raw_high=2.4,raw_low=2.3,raw_close=2.33,limit_up=2.56,limit_down=2.1,prev_close=2.33)
        frame=pd.DataFrame([row],index=['20261028'])
        frame.attrs.update(source='RQDATA',version='FAKE_CORE',qualification_scope='CORE_MODELS_V1')
        return frame
    def test_rule_branch_keeps_raw_unknown_and_research_price(self):
        frame=self.frame()
        with patch('core.exchange_rule_evidence.verify_rule_evidence',return_value={'price_tick':.01,'source':'EXCHANGE_RULE_DERIVED'}) as verify:
            result=rq.execution_bar(frame,'20261028',pd.Timestamp('2026-10-28T20:00:00+08:00'))
        self.assertEqual(result['price_tick'],.01)
        self.assertEqual(result['price_tick_source'],'EXCHANGE_RULE_DERIVED')
        self.assertIsNone(frame.iloc[0].price_tick)
        self.assertEqual(frame.iloc[0].open,2.328)
        self.assertEqual(result['open'],2.33)
        verify.assert_called_once()
    def test_source_or_native_conflict_is_not_silently_overridden(self):
        with patch('core.exchange_rule_evidence.verify_rule_evidence',return_value={'price_tick':.01}):
            frame=self.frame();frame.loc['20261028','price_tick']=.02
            with self.assertRaisesRegex(rq.InputBlocked,'RQ_AND_RULE_TICK_CONFLICT'):rq.execution_bar(frame,'20261028')
            frame=self.frame();frame.loc['20261028','tick_source']='UNAPPROVED_SOURCE'
            with self.assertRaisesRegex(rq.InputBlocked,'TICK_SOURCE_UNKNOWN'):rq.execution_bar(frame,'20261028')
    def test_rule_identity_or_signal_scope_cannot_escape_gate(self):
        frame=self.frame()
        payload=json.loads(frame.iloc[0].tick_rule_evidence);payload['identity']['order_book_id']='600001.XSHG'
        frame.loc['20261028','tick_rule_evidence']=json.dumps(payload)
        with self.assertRaisesRegex(rq.InputBlocked,'RULE_SECURITY_CONFLICT'):rq.execution_bar(frame,'20261028')
        frame=self.frame();frame.attrs['qualification_scope']='DAILY_SIGNALS_V1'
        with self.assertRaisesRegex(rq.InputBlocked,'DAILY_SIGNALS_SCOPE_FORBIDDEN'):rq.execution_bar(frame,'20261028')
    def test_original_rq_same_day_evidence_requirement_is_preserved(self):
        frame=self.frame();frame.loc['20261028','tick_source']=None
        frame.loc['20261028','price_tick']=.01
        frame.loc['20261028','tick_available_at']='2026-10-27T18:00:00+08:00'
        with self.assertRaisesRegex(rq.InputBlocked,'EXACT_DATE_TICK_MISSING'):rq.execution_bar(frame,'20261028')

if __name__=='__main__':unittest.main()
