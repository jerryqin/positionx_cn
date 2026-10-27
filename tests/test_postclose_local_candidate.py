"""Offline fake-ledger checks. All writes are temporary and cleaned normally."""
import importlib
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd

from scripts import run_postclose_local_candidate as local
from scripts import run_deep_oversold_csi500_forward as deep
from scripts import run_ret120_csi500_forward as ret
from research.research_store import ResearchStore


class LocalBoundaryTests(unittest.TestCase):
    def test_sdk_import_rejected_and_deep_import_is_lazy(self):
        guard = local.OfflineBoundary(local.ROOT / 'reports' / 'fake_only')
        sys.meta_path.insert(0, guard)
        self.addCleanup(sys.meta_path.remove, guard)
        with self.assertRaisesRegex(RuntimeError, 'OFFLINE_BOUNDARY:import:xtquant'):
            importlib.import_module('xtquant')
        self.assertNotIn('core.qmt_runtime', sys.modules)
        importlib.reload(deep)
        self.assertNotIn('core.qmt_runtime', sys.modules)

    def test_network_process_and_outside_writes_rejected(self):
        guard = local.OfflineBoundary(local.ROOT / 'reports' / 'fake_only')
        for event in ('socket.connect', 'socket.getaddrinfo', 'subprocess.Popen'):
            with self.assertRaisesRegex(RuntimeError, 'OFFLINE_BOUNDARY'):
                guard.audit(event, ())
        with self.assertRaisesRegex(RuntimeError, 'write_outside_output'):
            guard.check_write(local.ROOT / 'data' / 'production.db')
        guard.check_write(guard.output / 'model.duckdb')

    def test_output_and_duplicate_or_gap_fail_closed(self):
        with self.assertRaisesRegex(ValueError, 'OUTPUT_MUST_BE_NEW'):
            local.output_path(local.ROOT / 'data' / 'unsafe')
        for cursor in ('20261021', '20261023', '20250930'):
            with self.assertRaisesRegex(ValueError, 'MODEL_LEDGER_GAP'):
                local.require_next_session({'last_processed_date': cursor}, ['20261021', '20261022', '20261023'], '20261023')
        local.require_next_session({'last_processed_date': '20261022'}, ['20261022', '20261023'], '20261023')

    def test_unknown_status_is_not_zero(self):
        with self.assertRaisesRegex(ValueError, 'STATUS_MISSING'):
            local.require_details({'000001.SZ': {'InstrumentStatus': None}}, ['000001.SZ'])


class FakeModelLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'fake.duckdb'

    def test_deep_local_entry_lots_cost_and_s20_exit_without_sdk(self):
        dates = [d.strftime('%Y%m%d') for d in pd.bdate_range('2026-08-03', periods=21)]
        target = dates[1]
        histories = {'000001.SZ': pd.DataFrame({'open': [10.0], 'close': [10.5], 'high': [10.6], 'low': [9.9]}, index=[target])}
        details = {'000001.SZ': {'UpStopPrice': 11.0, 'InstrumentStatus': 0, 'PriceTick': .01, 'MinLimitOrderVolume': 100}}
        with ResearchStore(self.path) as store:
            state = deep.initial_state(deep.POLICY)
            store.con.execute("INSERT INTO forward_signals(strategy_id,symbol,signal_date,ret20,status,reason) VALUES (?,?,?,?,?,?)",
                              [deep.POLICY.strategy_id, '000001.SZ', dates[0], -.3, 'PENDING_ENTRY', 'FAKE_ONLY'])
            with patch.object(deep, 'details', side_effect=AssertionError('SDK accessor used')):
                deep.process_entries(store, histories, target, state, instrument_details=details, market_sessions=dates)
            p = deep.get_positions(store, 'BASE20')[0]
            self.assertEqual(p['shares'], 1000)
            self.assertEqual(p['entry_fee'], 10.0)
            histories['000001.SZ'] = pd.DataFrame({'close': [11.0]}, index=[dates[20]])
            deep.process_exits_and_mark(store, histories, dates[20], state, market_sessions=dates)
            self.assertEqual(deep.get_positions(store, 'BASE20'), [])
            self.assertAlmostEqual(store.con.execute("SELECT net_pnl FROM forward_trades WHERE ledger='BASE20'").fetchone()[0], 979.0)

    def test_ret120_raw_entry_adjusted_units_and_s120_defer_then_exit(self):
        dates = [d.strftime('%Y%m%d') for d in pd.bdate_range('2026-01-05', periods=122)]
        def history(day, suspended=False):
            t = pd.Timestamp(day).strftime('%Y-%m-%d') + 'T16:00:00+08:00'
            f = pd.DataFrame({'open': [5.0], 'close': [5.5], 'high': [5.6], 'low': [4.9],
                              'raw_open': [10.0], 'raw_close': [11.0], 'raw_high': [11.2], 'raw_low': [9.8],
                              'research_factor': [.5], 'factor_valid': [True], 'prev_close': [10.0],
                              'limit_up': [12.0], 'limit_down': [8.0], 'price_tick': [.01],
                              'status_suspended': [suspended], 'volume': [1000.0], 'num_trades': [10.0],
                              'status_available_at': [t], 'bar_available_at': [t], 'tick_available_at': [t]}, index=[day])
            f.attrs.update(source='RQDATA', version='FAKE_ONLY')
            return {'000001.SZ': f}
        with patch.object(ret.rq_input, 'enabled', return_value=True), ResearchStore(self.path) as store:
            ret.ensure_ret120_tables(store)
            state = ret.initial_state(ret.POLICY, {})
            store.con.execute('''INSERT INTO ret120_forward_signals(strategy_id,symbol,signal_date,ret120,quintile,status)
                VALUES (?,?,?,?,?,?)''', [ret.POLICY.strategy_id, '000001.SZ', dates[0], .2, 'Q5', 'PENDING_ENTRY'])
            details = {'000001.SZ': {'InstrumentName': 'FAKE', 'OpenDate': '20000101', 'InstrumentStatus': 0}}
            with patch.object(ret, 'details', side_effect=AssertionError('SDK accessor used')):
                diag = ret.process_entries(store, None, history(dates[1]), dates[1], dates, state, instrument_details=details)
            self.assertEqual(diag['entered'], 1)
            self.assertAlmostEqual(ret.get_positions(store, 'BASE20')[0]['shares'], 2000.0)
            proof = store.con.execute("SELECT raw_price,research_price,factor FROM ret120_price_basis WHERE ledger='BASE20'").fetchone()
            self.assertEqual(proof, (10.0, 5.0, .5))
            diag = ret.process_exits_and_mark(store, history(dates[120], suspended=True), dates[120], dates, state)
            self.assertEqual(diag, {'exited': 0, 'deferred_due': 1})
            diag = ret.process_exits_and_mark(store, history(dates[121]), dates[121], dates, state)
            self.assertEqual(diag['exited'], 1)
            self.assertEqual(ret.get_positions(store, 'BASE20'), [])

    def test_copy_policy_failure_preserves_source(self):
        from core import p0_rq_input as rq
        with ResearchStore(self.path) as store:
            store.save_state(deep.POLICY.strategy_id, deep.POLICY.fingerprint(), {'last_processed_date': '20261022'})
        before = rq.sha(self.path)
        dest = self.path.parent / 'never_created.duckdb'
        with self.assertRaisesRegex(ValueError, 'POLICY_IDENTITY_MISMATCH'):
            local.copy_ledger(self.path, dest, deep.POLICY.strategy_id, 'WRONG', ['20261022', '20261023'], '20261023')
        self.assertEqual(before, rq.sha(self.path))
        self.assertFalse(dest.exists())

    def test_funnel_failure_preserves_independent_core_results(self):
        from core import p0_rq_input as rq
        root = Path(self.temp.name)
        for name in ('manifest.json', 'COMPLETE.json', 'source_metadata.json', 'pit.parquet', 'pit.json', 'source.duckdb'):
            (root / name).write_text('FAKE_ONLY', encoding='utf-8')
        source = SimpleNamespace(root=root, db=root / 'source.duckdb',
                                 meta={'quality_status': 'PASS', 'engineering_only': False},
                                 lineage=lambda adjustment: {'source': 'FAKE_ONLY'})
        profile = {'segment_db': str(source.db), 'calendar': str(local.ROOT / 'config/ops/cn_exchange_calendar_2026.json')}
        pit = {'path': str(root / 'pit.parquet'), 'sha256': rq.sha(root / 'pit.parquet'),
               'manifest': str(root / 'pit.json'), 'manifest_sha256': rq.sha(root / 'pit.json')}
        curve = pd.DataFrame({'trade_date': ['20261023'], 'equity': [1e6], 'cash': [1e6], 'gross_market_value': [0.0]})
        calls = []
        def model(kind, *args):
            calls.append(kind)
            return {'status': 'MODEL_REPLAY_COMPLETE'}, curve
        with patch.object(local, 'output_path', return_value=root / 'output'), \
                patch.object(local.OfflineBoundary, 'install'), \
                patch.object(local, 'read_json', return_value=profile), \
                patch.object(local, 'sessions', return_value=['20261022', '20261023']), \
                patch.object(rq, 'open_inputs', return_value=source), \
                patch.object(local, 'membership', return_value=({}, pit)), \
                patch.object(local, 'local_details', return_value={}), \
                patch.object(local, 'historical_runs', return_value={'runs': []}), \
                patch.object(local, 'run_model', side_effect=model), \
                patch.object(local, 'run_funnel', side_effect=ValueError('FAKE_FUNNEL_GAP')):
            result = local.run('20261023', root / 'output')
        self.assertEqual(calls, ['deep', 'ret120'])
        self.assertEqual(result['strategies']['ret120']['status'], 'MODEL_REPLAY_COMPLETE')
        self.assertEqual(result['strategies']['funnel']['status'], 'BLOCKED')
        self.assertEqual(result['portfolio']['latest']['portfolio_equity'], 1e6)
        self.assertIsNone(result['real_account']['positions'])


if __name__ == '__main__':
    unittest.main()
