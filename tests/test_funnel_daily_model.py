"""Isolated Funnel model tests; all bars synthetic, no external calls."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from research import funnel_daily_model as model


class FunnelModelTests(unittest.TestCase):
    def setUp(self):
        self.dates = ['20261026', '20261027', '20261028', '20261029']
        self.plan = {'symbol': '688001.SH', 'status': 'PREPARE_MANUAL', 'lane': 'MOMENTUM',
            'stop_raw': 9., 'open_valid_low': 9.5, 'open_valid_high': 10.5,
            'minimum_order_quantity': 200, 'quantity_step': 1, 'adv20_amount': 100_000_000.}
        self.binding = {'version': 'SYNTHETIC', 'model_research_authorized': True}
        self.identity = {'688001.SH': {'special_type': 'Normal'}}

    def bar(self, open_=10., close=10., tradable=True, **extra):
        return dict(open=open_ if tradable else None, close=close if tradable else None,
                    high=max(open_, close), low=min(open_, close), tradable=tradable,
                    up_stop_price=11., down_stop_price=8., price_tick=.01, factor=1.,
                    price_tick_rule={'minimum_order_quantity': 200, 'quantity_step': 1}, **extra)

    def run_day(self, root, day, *, parent=None, plans=None, bar=None, affected=False):
        frame = pd.DataFrame({'corporate_action_affected': [affected]}, index=[day])
        kwargs = dict(day=day, market_dates=self.dates, output_db=root/(day+'.duckdb'),
            histories={'688001.SH': frame}, plans=plans or [], identities=self.identity,
            input_binding=self.binding, plan_frozen_at=pd.Timestamp(day).tz_localize('Asia/Shanghai').replace(hour=16))
        if parent:
            kwargs.update(parent_db=root/(parent+'.duckdb'), parent_day=parent,
                parent_frozen_at=pd.Timestamp(parent).tz_localize('Asia/Shanghai').replace(hour=16))
        with patch.object(model.rq, 'execution_bar', return_value=bar or self.bar()):
            return model.run_session(**kwargs)

    def test_entries_stop_next_open_and_exact_one_way_25bp_costs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initial = self.run_day(root, '20261026', plans=[self.plan])
            self.assertEqual(initial['stage_status']['entries'], 'CALCULATED_ZERO')
            entered = self.run_day(root, '20261027', parent='20261026', bar=self.bar(close=8.9))
            self.assertEqual(len(entered['entries']), 1)
            self.assertEqual(len(entered['exits']), 0)
            self.assertEqual(entered['exit_plans'][0]['trigger_date'], '20261027')
            entry = entered['entries'][0]
            self.assertEqual(entry['entry_fee'], float(model.cost(entry['entry_notional'])))
            self.assertEqual(model.cost('2.00'), model.money('.005'))
            exited = self.run_day(root, '20261028', parent='20261027', bar=self.bar(open_=8.5))
            self.assertEqual(len(exited['exits']), 1)
            self.assertEqual(exited['positions'], [])
            self.assertEqual(exited['exits'][0]['exit_price'], 8.5)
            self.assertEqual(exited['exits'][0]['exit_fee'],
                             float(model.cost(model.money(8.5*entry['shares']))))
            self.assertEqual(entry['shares'], 2500)
            self.assertEqual(entry['entry_fee'], 62.50)
            self.assertEqual(exited['exits'][0]['exit_fee'], 53.13)
            self.assertEqual(exited['exits'][0]['net_pnl'], -3865.63)
            self.assertEqual(exited['settlement']['cash'], 996134.37)
            self.assertEqual(exited['settlement']['equity'], 996134.37)
            with model._read(root/'20261028.duckdb') as con:
                self.assertEqual(con.execute('SELECT count(*) FROM forward_trades').fetchone()[0], 1)
                self.assertEqual(con.execute('SELECT count(*) FROM forward_positions').fetchone()[0], 0)
                self.assertEqual(con.execute('SELECT count(*) FROM forward_equity').fetchone()[0], 3)
            self.assertFalse(exited['forward_acceptance'])
            self.assertFalse(exited['state']['actual_model_forward'])

    def test_suspension_defers_exit_and_corporate_action_equivalence_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.run_day(root, '20261026', plans=[self.plan])
            self.run_day(root, '20261027', parent='20261026', bar=self.bar(close=8.9))
            delayed = self.run_day(root, '20261028', parent='20261027', bar=self.bar(tradable=False))
            self.assertEqual(len(delayed['positions']), 1)
            self.assertEqual(delayed['delayed'][0]['reason'], 'NO_REAL_TRADE_OR_SUSPENDED')
            with self.assertRaisesRegex(ValueError, 'BLOCKED_CORPORATE_ACTION_EQUIVALENCE'):
                self.run_day(root, '20261029', parent='20261028', affected=True)

    def test_idempotent_same_input_and_parent_timing_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.run_day(root, '20261026', plans=[self.plan])
            sha = model.rq.sha(root/'20261026.duckdb')
            repeated = self.run_day(root, '20261026', plans=[self.plan])
            self.assertTrue(repeated['idempotent_reuse'])
            self.assertEqual(model.rq.sha(root/'20261026.duckdb'), sha)
            with self.assertRaisesRegex(ValueError, 'FUNNEL_ADJACENT_PARENT_REQUIRED'):
                self.run_day(root, '20261028', parent='20261026')
            with patch.object(model.rq, 'execution_bar', return_value=self.bar()):
                with self.assertRaisesRegex(ValueError, 'FUNNEL_PLAN_NOT_FROZEN_BEFORE_OPEN'):
                    model.run_session(day='20261027', market_dates=self.dates, output_db=root/'late.duckdb',
                        histories={}, plans=[], identities=self.identity, input_binding=self.binding,
                        parent_db=root/'20261026.duckdb', parent_day='20261026',
                        parent_frozen_at='2026-10-27T09:30:00+08:00', plan_frozen_at='2026-10-27T16:00:00+08:00')

    def test_actual_forward_requires_real_window_and_rejects_historical_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.run_day(root, '20261028')
            self.binding['actual_model_forward'] = True
            with patch.object(model.rq, 'clock', return_value=pd.Timestamp('2026-10-28T16:00:00+08:00')):
                with self.assertRaisesRegex(ValueError, 'REAL_APPROVED_DAY_WINDOW'):
                    self.run_day(root, '20261029', parent='20261028')
            with patch.object(model.rq, 'clock', return_value=pd.Timestamp('2026-10-29T16:00:00+08:00')):
                with self.assertRaisesRegex(ValueError, 'FORWARD_CANNOT_INHERIT_HISTORICAL_MODEL'):
                    self.run_day(root, '20261029', parent='20261028')
                actual = self.run_day(root, '20261029')
            self.assertTrue(actual['forward_acceptance'])
            self.assertTrue(actual['state']['actual_model_forward'])
            self.assertEqual(actual['mode'], 'ACTUAL_MODEL_FORWARD')


if __name__ == '__main__':
    unittest.main()
