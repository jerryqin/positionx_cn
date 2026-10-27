"""New data-contract/admission checks only; no existing settlement or 41-test rerun."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest

import pandas as pd

from core import p0_rq_input as rq
from scripts import postclose_data_admission_v1 as admission
from scripts import run_postclose_local_candidate as local


class DataAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        guard = local.OfflineBoundary(self.root)
        sys.meta_path.insert(0, guard)
        self.addCleanup(sys.meta_path.remove, guard)
        self.identity = pd.DataFrame({'order_book_id': ['000001.XSHE', '000002.XSHE'],
                                      'symbol': ['ONE', 'TWO'], 'listed_date': ['2000-01-01'] * 2,
                                      'special_type': ['Normal'] * 2, 'round_lot': [100, 100]})
        self.bars = pd.DataFrame({'order_book_id': self.identity.order_book_id,
            'date': pd.Timestamp('2026-10-26'), 'open': 10., 'high': 10., 'low': 10., 'close': 10.,
            'prev_close': 10., 'volume': [100., 0.], 'total_turnover': [1000., 0.],
            'num_trades': [2., 0.], 'limit_up': 11., 'limit_down': 9.})
        self.status = pd.DataFrame({'order_book_id': self.identity.order_book_id,
            'date': pd.Timestamp('2026-10-26'), 'status_suspended': [False, True], 'price_tick': .01,
            'status_available_at': '2026-10-26T16:00:00+08:00', 'tick_available_at': '2026-10-26T16:00:00+08:00'})
        self.meta = {'asof': '2026-10-26', 'business_date': '2026-10-26', 'raw_adjustment': 'none', 'volume_unit': 'FIXTURE_NATIVE_UNIT'}

    def audit(self):
        return admission.audit_stock_records(self.bars, self.status, self.identity, '20261026', self.meta, '2026-10-26T17:00:00+08:00')

    def gates(self):
        return {'STOCK_DATA_READY': admission.state(True, strategy_inputs={k: admission.state(True) for k in ('deep', 'ret120')}),
                'PIT_READY': admission.state(True), 'BENCHMARK_READY': admission.state(False),
                'FUNNEL_STRUCTURE_READY': admission.state(False)}

    def plan(self):
        path = self.root / 'plan.csv'
        path.write_text('symbol,signal_date,data_kind\n000001.SZ,20261027,ACTUAL_QUALIFIED_POSTCLOSE\n', encoding='utf-8')
        expected = {'strategy': 'deep', 'signal_date': '20261027', 'next_session': '20261028',
                    'input_version': 'CONTRACT_TEST_ONLY_20261027', 'input_manifest_sha256': 'a' * 64,
                    'strategy_fingerprint': 'b' * 64, 'plan_sha256': rq.sha(path),
                    'data_available_at': '2026-10-27T16:00:00+08:00', 'next_event_at': '2026-10-28T09:30:00+08:00'}
        plan = {k: v for k, v in expected.items() if k not in ('strategy', 'data_available_at', 'next_event_at')}
        plan.update(provenance_mode='ACTUAL_QUALIFIED_POSTCLOSE', research_state='FORWARD_CANDIDATE',
                    inherited_positions=[], parent_replay=None, forward_acceptance=False,
                    generated_at='2026-10-27T16:01:00+08:00', frozen_at='2026-10-27T16:02:00+08:00')
        return path, plan, expected

    def test_raw_quality_retains_native_units_and_nontrading_truth(self):
        result = self.audit()
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['zero_trade_rows'], 1)
        self.assertEqual(result['volume_unit_retained'], 'FIXTURE_NATIVE_UNIT')
        self.assertEqual(result['executions_created'], 0)

    def test_exact_date_never_falls_back(self):
        self.meta['asof'] = '2026-10-23'
        with self.assertRaisesRegex(ValueError, 'EXACT_DATE'):
            self.audit()

    def test_unknown_status_and_conflicting_trade_are_rejected(self):
        self.status['status_suspended'] = self.status.status_suspended.astype(object)
        self.status.loc[0, 'status_suspended'] = None
        with self.assertRaisesRegex(RuntimeError, 'STATUS_UNKNOWN'):
            self.audit()
        self.status.loc[0, 'status_suspended'] = True
        with self.assertRaisesRegex(RuntimeError, 'SUSPENSION_TRADE_CONFLICT'):
            self.audit()

    def test_old_status_timestamp_is_not_current_identity_evidence(self):
        self.status['status_available_at'] = '2026-10-23T16:00:00+08:00'
        with self.assertRaisesRegex(ValueError, 'EXACT_DAY_TIME_INVALID'):
            self.audit()

    def test_unit_and_integer_lot_are_explicit(self):
        self.meta['volume_unit'] = ''
        with self.assertRaisesRegex(ValueError, 'UNIT_BINDING'):
            self.audit()
        self.meta['volume_unit'] = 'FIXTURE_NATIVE_UNIT'
        self.identity['round_lot'] = 100.5
        with self.assertRaisesRegex(ValueError, 'MINIMUM_LOT'):
            self.audit()

    def test_missing_benchmark_funnel_and_deep_do_not_block_ret120(self):
        gates = self.gates()
        p = admission.compute_permissions(gates)
        self.assertTrue(p['deep_independent_computation'])
        self.assertTrue(p['ret120_independent_computation'])
        self.assertFalse(p['formal_ledger_advance'])
        gates['STOCK_DATA_READY']['strategy_inputs']['deep']['ready'] = False
        p = admission.compute_permissions(gates)
        self.assertFalse(p['deep_independent_computation'])
        self.assertTrue(p['ret120_independent_computation'])

    def test_candidate_admission_is_not_forward_acceptance_or_ledger_write(self):
        path, plan, expected = self.plan()
        result = admission.forward_start_decision(self.gates(), plan, expected, '2026-10-27T16:03:00+08:00', path)
        self.assertTrue(result['eligible'])
        self.assertFalse(result['formal_forward_acceptance'])
        self.assertEqual(result['ledger_action'], 'NOT_CREATED_OR_ADVANCED')
        self.assertEqual(list(self.root.glob('*.duckdb')), [])

    def test_not_forward_and_inherited_positions_are_permanently_rejected(self):
        path, plan, expected = self.plan()
        for field, value in [('research_state', 'SYNTHETIC_SAMPLE_NOT_FORWARD'),
                             ('provenance_mode', 'ISOLATED_MODEL_REPLAY'),
                             ('inherited_positions', [{'symbol': 'OLD'}]), ('parent_replay', 'old_23_run')]:
            bad = copy.deepcopy(plan);bad[field] = value
            result = admission.forward_start_decision(self.gates(), bad, expected, '2026-10-27T16:03:00+08:00', path)
            self.assertFalse(result['eligible'], field)
        path.write_text('symbol,signal_date,data_kind\n000001.SZ,20261023,MODEL_RESEARCH_ONLY\n', encoding='utf-8')
        plan['plan_sha256'] = expected['plan_sha256'] = rq.sha(path)
        result = admission.forward_start_decision(self.gates(), plan, expected, '2026-10-27T16:03:00+08:00', path)
        self.assertIn('PLAN_CONTENT_IS_NOT_ACTUAL_TARGET_SIGNAL', result['reasons'])

    def test_frozen_input_strategy_file_and_event_bindings(self):
        path, plan, expected = self.plan()
        for key in ('input_manifest_sha256', 'strategy_fingerprint', 'next_session', 'input_version'):
            bad = dict(plan, **{key: 'CHANGED'})
            result = admission.forward_start_decision(self.gates(), bad, expected, '2026-10-27T16:03:00+08:00', path)
            self.assertFalse(result['eligible'], key)
        result = admission.forward_start_decision(self.gates(), plan, expected, '2026-10-28T09:30:00+08:00', path)
        self.assertFalse(result['eligible'])
        path.write_text('ALTERED', encoding='utf-8')
        result = admission.forward_start_decision(self.gates(), plan, expected, '2026-10-27T16:03:00+08:00', path)
        self.assertIn('FROZEN_PLAN_FILE_HASH_UNVERIFIABLE', result['reasons'])

    def test_missing_inputs_never_creates_a_forward_date(self):
        gates = self.gates();gates['PIT_READY'] = admission.state(False, ['MISSING_ACTUAL_PIT'])
        decision = admission.forward_start_decision(gates, None, {}, '2026-10-26T12:00:00+08:00')
        self.assertFalse(decision['eligible'])
        self.assertIsNone(decision['first_forward_signal_date'])
        self.assertEqual(decision['ledger_action'], 'NOT_CREATED_OR_ADVANCED')


if __name__ == '__main__':
    unittest.main()
