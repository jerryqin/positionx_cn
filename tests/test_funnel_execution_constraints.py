"""Funnel quantity/ranking checks; synthetic inputs, no API or production writes."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd

from research import ret120_execution_funnel as engine
from scripts import qualify_postclose_funnel as qualify
from scripts import run_ret120_execution_funnel_v01 as funnel


class FunnelQuantityTests(unittest.TestCase):
    def setUp(self):
        self.risk = funnel.C()['risk_preview']

    def test_star_minimum_200_and_step_1_preserve_257_share_risk_budget_result(self):
        result = engine.preview_size(10., 9., 100_000_000., 103_160., 'MOMENTUM', self.risk,
                                     minimum_order_quantity=200, quantity_step=1)
        self.assertEqual(result['preview_shares'], 257)
        self.assertEqual(result['minimum_order_quantity'], 200)
        self.assertEqual(result['quantity_step'], 1)
        self.assertEqual(result['sizing_limit'], 'RISK_BUDGET')
        self.assertLessEqual(result['preview_shares'] * (10. - 9.), result['risk_budget_amount'])
        self.assertLessEqual(result['preview_weight'], self.risk['momentum_max_weight'])

    def test_insufficient_minimum_is_a_normal_skip_and_never_rounded_up(self):
        result = engine.preview_size(10., 9., 100_000_000., 60_000., 'MOMENTUM', self.risk,
                                     minimum_order_quantity=200, quantity_step=1)
        self.assertEqual(result['risk_budget_amount'], 150.)
        self.assertEqual(result['preview_shares'], 0)
        self.assertEqual(result['sizing_status'], 'SKIP_MIN_ORDER_RISK')

    def test_main_board_explicit_constraints_preserve_existing_budget_and_quantity(self):
        old = engine.preview_size(10., 9., 100_000_000., 103_160., 'MOMENTUM', self.risk)
        new = engine.preview_size(10., 9., 100_000_000., 103_160., 'MOMENTUM', self.risk,
                                  minimum_order_quantity=100, quantity_step=100)
        self.assertEqual({key: new[key] for key in old}, old)
        self.assertEqual(new['preview_shares'], 200)
        with self.assertRaisesRegex(ValueError, 'EXPLICIT_EXECUTION_QUANTITY_CONSTRAINTS_REQUIRED'):
            engine.preview_size(10., 9., 100_000_000., 103_160., 'MOMENTUM', self.risk,
                                minimum_order_quantity=200)


class FunnelSealedPreviewTests(unittest.TestCase):
    def inputs(self):
        symbols = ['688001.SH', '688002.SH', '688003.SH']
        candidates = pd.DataFrame([{'symbol': symbol, 'momentum_candidate': True,
            'mean_reversion_candidate': False, 'momentum_confidence': 'STRONG',
            'ret120': .1, 'bucket': 'Q5', 'universe_tier': 'CSI500'} for symbol in symbols])
        features = pd.DataFrame([{'symbol': symbol, 'setup_state': 'CONFIRMED_RETEST_T',
            'trend_recovered': True, 'raw_close': 10., 'stop_raw': 9.,
            'open_valid_low': 9.5, 'open_valid_high': 10.5,
            'entry_condition_state': 'IN_ZONE_AT_T_CLOSE', 'risk_distance_pct': .1,
            'adv20_amount': 100_000_000.} for symbol in symbols])
        identity = pd.DataFrame([{'order_book_id': symbol.replace('.SH', '.XSHG'),
            'symbol': 'NATIVE_NAME', 'special_type': special, 'board_type': 'KSH'}
            for symbol, special in zip(symbols, ['Other', 'Normal', 'StarST'])])
        return symbols, candidates, features, identity

    def test_unknown_stays_ranked_known_st_is_not_silently_filtered_and_no_future_fill(self):
        symbols, candidates, features, identity = self.inputs()
        def rule(row, day):
            self.assertEqual(row['symbol'], 'NATIVE_NAME')
            self.assertIn(row['qmt_symbol'], symbols)
            self.assertEqual(day, '20261028')
            return {'minimum_order_quantity': 200, 'quantity_step': 1, 'price_tick': .01,
                    'source': 'EXCHANGE_RULE_DERIVED', 'rule_id': 'FAKE_RULE'}
        with patch.object(funnel, 'load_engine', return_value=engine):
            panel, summary = qualify.build_execution_preview(candidates, features, identity,
                {symbol: False for symbol in symbols}, '20261028', 103_160., funnel.C(), rule_resolver=rule)
        self.assertEqual(panel.symbol.tolist(), symbols)
        self.assertEqual(panel.lane_rank.tolist(), [1, 2, 3])
        self.assertEqual(panel.iloc[0].classification, 'UNKNOWN')
        self.assertEqual(panel.iloc[0].preview_decision, 'BLOCKED_UNKNOWN_SECURITY_CLASSIFICATION')
        self.assertTrue(pd.isna(panel.iloc[0].preview_shares))
        self.assertEqual(panel.iloc[1].preview_shares, 257)
        self.assertEqual(panel.iloc[2].preview_shares, 257)
        self.assertTrue(panel.future_open_price.isna().all())
        self.assertTrue(panel.future_entry_units.isna().all())
        self.assertEqual(summary['unknown_symbols'], [symbols[0]])
        self.assertEqual(summary['full_model_status'], 'FUNNEL_FULL_MODEL_BLOCKED_EXIT_POLICY')
        self.assertEqual(summary['status'], 'PARTIAL_EXECUTION_PREVIEW')

    def test_rule_failure_and_unknown_suspension_are_local_blocks_not_missing_candidates(self):
        symbols, candidates, features, identity = self.inputs()
        identity['special_type'] = 'Normal'
        def rule(row, day):
            if row['qmt_symbol'] == symbols[0]:
                raise ValueError('RULE_DATE_OUT_OF_RANGE')
            return {'minimum_order_quantity': 200, 'quantity_step': 1, 'price_tick': .01}
        with patch.object(funnel, 'load_engine', return_value=engine):
            panel, summary = qualify.build_execution_preview(candidates, features, identity,
                {symbols[2]: False}, '20261028', 60_000., funnel.C(), rule_resolver=rule)
        self.assertEqual(len(panel), 3)
        self.assertEqual(panel.lane_rank.tolist(), [1, 2, 3])
        self.assertEqual(panel.preview_decision.tolist(), ['BLOCKED_TRADING_RULE_EVIDENCE',
            'BLOCKED_EXACT_DAY_SUSPENSION_UNKNOWN', 'SKIP_MIN_ORDER_RISK'])
        self.assertEqual(panel.iloc[0].rule_error, 'RULE_DATE_OUT_OF_RANGE')
        self.assertEqual(summary['minimum_order_skips'][0]['symbol'], symbols[2])

    def test_preview_refuses_missing_sealed_source_before_catalog_or_network_access(self):
        source = SimpleNamespace(db=Path('UNREAD_DATABASE'), meta={'business_date': '2026-10-28'})
        with patch.object(qualify.rq, 'sha', return_value='FIXTURE'), \
                patch.object(qualify.local, 'read_db', side_effect=AssertionError('DATABASE_MUST_NOT_OPEN')), \
                patch.object(funnel, 'load_market', side_effect=AssertionError('OLD_CATALOG_MUST_NOT_OPEN')):
            with self.assertRaisesRegex(ValueError, 'FUNNEL_EXECUTION_PREVIEW_REQUIRES_SEALED_SOURCE'):
                qualify.run(source.db, '20261028', qualify.ROOT / 'reports/UNWRITTEN_FUNNEL_TEST',
                            source=source, execution_preview=True)

    def test_no_candidates_is_a_calculated_empty_preview(self):
        _, candidates, features, identity = self.inputs()
        with patch.object(funnel, 'load_engine', return_value=engine):
            panel, summary = qualify.build_execution_preview(candidates.iloc[:0], features.iloc[:0],
                identity, {}, '20261028', 1_000_000., funnel.C(), rule_resolver=lambda *args: {})
        self.assertTrue(panel.empty)
        self.assertTrue(panel.loc[panel.status.isin(['PREPARE_MANUAL', 'READY', 'WATCH'])].empty)
        self.assertEqual(summary['status'], 'PASS_EXECUTION_PREVIEW_NO_CANDIDATES')


if __name__ == '__main__':
    unittest.main()
