"""Focused offline input tests. Only disposable DuckDB/Parquet in this stage.

No SDK imports, services, production DBs or Scheduler. A meta-path guard fails
the tests if xtquant or rqdatac is imported accidentally.
"""
import ast
import copy
import hashlib
import importlib.abc
import importlib.util
import json
from pathlib import Path
import sys
import symtable
import tempfile
import types
import unittest
from unittest.mock import patch

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('p0_rq_input_under_test', ROOT / 'core/p0_rq_input.py')
rq = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rq)


class DenySDK(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('xtquant', 'rqdatac'):
            raise AssertionError('REAL_SDK_FORBIDDEN')


class InputsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.guard = DenySDK()
        sys.meta_path.insert(0, cls.guard)

    @classmethod
    def tearDownClass(cls):
        sys.meta_path.remove(cls.guard)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'daily.duckdb'
        with duckdb.connect(str(self.db)) as c:
            c.execute('''CREATE TABLE raw_daily AS SELECT '000001.XSHE' AS order_book_id,
                DATE '2026-10-16' AS date, 10.0::DOUBLE AS open, 11.0::DOUBLE AS high,
                9.0::DOUBLE AS low, 10.0::DOUBLE AS close, 12300.0::DOUBLE AS volume,
                123000.0::DOUBLE AS total_turnover, 20.0::DOUBLE AS num_trades,
                'FAKE_VENDOR_V1' AS source_version''')
            c.execute("INSERT INTO raw_daily SELECT order_book_id,DATE '2026-10-19',20,21,19,20,23400,468000,30,source_version FROM raw_daily")
            c.execute('ALTER TABLE raw_daily ADD COLUMN prev_close DOUBLE DEFAULT 10')
            c.execute("CREATE TABLE factors AS SELECT '000001.XSHE' AS order_book_id,DATE '2026-10-16' AS ex_date,2.0::DOUBLE AS ex_cum_factor")
            c.execute("INSERT INTO factors VALUES ('000001.XSHE','2026-10-19',4),('000001.XSHE','2026-10-21',100)")
            c.execute("CREATE TABLE suspension AS SELECT '000001.XSHE' AS order_book_id,DATE '2026-10-16' AS date,true AS is_suspended")
            c.execute("CREATE TABLE security_identity AS SELECT '000001.XSHE' AS order_book_id")
        self.meta = {'source': 'RQDATA', 'asof': '2026-10-19', 'database': 'daily.duckdb',
                     'database_sha256': rq.sha(self.db), 'raw_adjustment': 'none',
                     'volume_unit': 'FAKE_VENDOR_SHARES', 'availability': '2026-10-20T13:45:09+08:00',
                     'version': 'FAKE_ONLY_V1'}
        self.config = {'strategy_contracts': {}, 'no_fill_semantics_accepted': False}
        self.seal()

    def seal(self):
        self.meta['database_sha256'] = rq.sha(self.db)
        (self.root / 'source_metadata.json').write_text(json.dumps(self.meta), encoding='utf-8')
        names = ['source_metadata.json', 'daily.duckdb']
        manifest = {'files': [{'path': n, 'sha256': rq.sha(self.root / n)} for n in names]}
        (self.root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        (self.root / 'COMPLETE.json').write_text(json.dumps({'manifest_sha256': rq.sha(self.root / 'manifest.json')}), encoding='utf-8')

    def reader(self, **kw):
        return rq.DailyInput(self.root / 'manifest.json', self.config,
                            now=kw.get('now', '2026-10-20T14:00:00+08:00'))

    def accept_fake_contract(self, adjustment):
        evidence = self.root / 'fake_contract.txt'
        evidence.write_text('Controlled fake fixture only; not production approval.', encoding='utf-8')
        self.config['strategy_contracts'][adjustment] = {
            'accepted': True, 'evidence_file': str(evidence), 'evidence_sha256': rq.sha(evidence),
            'representation': 'RQ_PRE' if adjustment == 'front' else 'RET120_FROZEN_CLOSE_PRE_CLOSE',
            'execution_price_basis_accepted': True, 'suspension_semantics_accepted': True}

    def test_raw_values_unit_suspension_and_lineage_preserved(self):
        f = self.reader().histories(['000001.SZ'], '20261019', 2)['000001.SZ']
        self.assertEqual(f.volume.tolist(), [12300, 23400])
        self.assertEqual(f.close.tolist(), [10, 20])
        self.assertEqual(f.is_suspended.tolist(), [True, False])
        self.assertEqual(f.attrs['source'], 'RQDATA')
        self.assertEqual(f.attrs['adjustment'], 'none')
        self.assertTrue({'unit', 'asof', 'availability', 'version'} <= f.attrs.keys())

    def test_rq_pre_post_are_distinct_and_ignore_future_factor(self):
        r = self.reader()
        pre = r.histories(['000001.SZ'], '20261019', 2, 'RQ_PRE')['000001.SZ']
        post = r.histories(['000001.SZ'], '20261019', 2, 'RQ_POST')['000001.SZ']
        self.assertEqual(pre.close.tolist(), [5, 20])
        self.assertEqual(post.close.tolist(), [20, 80])
        self.assertEqual(post.volume.tolist(), [12300, 23400])

    def test_front_and_back_ratio_block_without_contract(self):
        for name in ['front', 'back_ratio']:
            with self.assertRaisesRegex(rq.InputBlocked, 'AFFINE_ACTION_CONVERSION_REQUIRED' if name == 'front' else 'STRATEGY_CONTRACT_UNVERIFIED'):
                self.reader().histories(['000001.SZ'], '20261019', 2, name)
        self.assertEqual(len(self.reader().histories(['000001.SZ'], '20261019', 2)), 1)

    def test_accepted_fake_contract_preserves_actual_representation(self):
        self.accept_fake_contract('back_ratio')
        f = self.reader().histories(['000001.SZ'], '20261019', 2, 'back_ratio')['000001.SZ']
        self.assertEqual(f.attrs['adjustment'], 'RET120_FROZEN_CLOSE_PRE_CLOSE')
        self.assertEqual(f.attrs['requested_adjustment'], 'back_ratio')
        self.accept_fake_contract('front')
        with self.assertRaisesRegex(rq.InputBlocked, 'AFFINE_ACTION_CONVERSION_REQUIRED'):
            self.reader().require_contract('front')

    def test_unverified_unused_volume_is_retained_without_strategy_gate(self):
        self.accept_fake_contract('back_ratio')
        self.config['strategy_contracts']['back_ratio']['unit_accepted'] = False
        f = self.reader().histories(['000001.SZ'], '20261019', 2, 'back_ratio')['000001.SZ']
        self.assertEqual(f.volume.tolist(), [12300, 23400])
        self.config['strategy_contracts']['back_ratio']['execution_price_basis_accepted'] = False
        with self.assertRaisesRegex(rq.InputBlocked, 'STRATEGY_CONTRACT_UNVERIFIED'):
            self.reader().require_contract('back_ratio')

    def test_missing_contract_evidence_blocks(self):
        self.accept_fake_contract('back_ratio')
        Path(self.config['strategy_contracts']['back_ratio']['evidence_file']).write_text('changed')
        with self.assertRaisesRegex(rq.InputBlocked, 'STRATEGY_CONTRACT_UNVERIFIED'):
            self.reader().require_contract('back_ratio')

    def test_stale_business_date_rejected(self):
        with self.assertRaisesRegex(rq.InputBlocked, 'STALE_OR_WRONG_BUSINESS_DATE'):
            self.reader().histories(['000001.SZ'], '20261020', 2)

    def test_weekend_rejected(self):
        with self.assertRaisesRegex(rq.InputBlocked, 'NOT_APPROVED_EXCHANGE_SESSION'):
            self.reader().histories(['000001.SZ'], '20261018', 2)

    def test_not_yet_available_rejected(self):
        with self.assertRaisesRegex(rq.InputBlocked, 'NOT_AVAILABLE_AT_DECISION'):
            self.reader(now='2026-10-19T16:00:00+08:00')
        self.meta['availability'] = '2026-10-19T12:00:00+08:00'
        self.seal()
        with self.assertRaisesRegex(rq.InputBlocked, 'COMPLETE_CLOSE_NOT_YET_AVAILABLE'):
            self.reader(now='2026-10-19T14:00:00+08:00')

    def test_missing_symbol_is_not_success(self):
        with self.assertRaisesRegex(rq.InputBlocked, 'EXACT_T_BAR_MISSING'):
            self.reader().histories(['600000.SH'], '20261019', 2)

    def test_insufficient_history_rejected(self):
        with self.assertRaisesRegex(rq.InputBlocked, 'REQUIRED_HISTORY_NOT_MET'):
            self.reader().histories(['000001.SZ'], '20261019', 120, min_bars=120)

    def test_suspended_filled_price_cannot_enter_unverified_no_fill_prior(self):
        with self.assertRaisesRegex(rq.InputBlocked, 'NO_FILL_SEMANTICS_UNVERIFIED'):
            self.reader().histories(['000001.SZ'], '20261019', 2, no_fill=True)
        self.config['no_fill_semantics_accepted'] = True
        with duckdb.connect(str(self.db)) as c:
            c.execute("UPDATE raw_daily SET volume=0,num_trades=0 WHERE date=DATE '2026-10-19'")
        self.seal()
        # The old assertion retained a positive padded zero-trade close. The
        # requested real-observation contract must reject that exact case.
        with self.assertRaisesRegex(rq.InputBlocked, 'REQUIRED_HISTORY_NOT_MET'):
            self.reader().histories(['000001.SZ'], '20261019', 2, no_fill=True)

    def test_affine_cash_split_rights_combined_and_effective_date(self):
        f = pd.DataFrame({'date': pd.to_datetime(['2026-10-12', '2026-10-13']),
                          'open': [100., 80.], 'high': [101., 81.],
                          'low': [99., 79.], 'close': [100., 80.]})
        a = pd.DataFrame([dict(ex_date='2026-10-13', cash_per_share=1.5,
            share_multiplier=1.4, rights_per_share=.2, rights_price=5.,
            available_at='2026-09-29T23:59:59+08:00', observed_at='2026-10-20T13:00:00+08:00')])
        out = rq.affine_front(f, a, '2026-10-19', '2026-10-20T14:00:00+08:00')
        self.assertAlmostEqual(out.iloc[0].close, (100 - 1.5 + .2 * 5) / 1.6)
        self.assertEqual(out.iloc[1].close, 80.)
        self.assertEqual(f.iloc[0].close, 100.)
        a['observed_at'] = '2026-10-21T13:00:00+08:00'
        with self.assertRaisesRegex(rq.InputBlocked, 'ACTION_NOT_AVAILABLE'):
            rq.affine_front(f, a, '2026-10-19', '2026-10-20T14:00:00+08:00')

    def test_front_reader_requires_complete_action_coverage_and_caps_scope(self):
        self.accept_fake_contract('front')
        self.config['strategy_contracts']['front']['representation'] = 'QMT_FRONT_AFFINE'
        with self.assertRaisesRegex(rq.InputBlocked, 'ACTION_COVERAGE_MISSING'):
            self.reader().histories(['000001.SZ'], '20261019', 2, 'front')
        self.meta.update(corporate_action_coverage_complete=True,
            corporate_action_coverage_start='2026-10-16', corporate_action_coverage_end='2026-10-19',
            corporate_action_symbols=['000001.XSHE'])
        self.seal()
        self.assertEqual(self.reader().histories(['000001.SZ'], '20261019', 2, 'front')['000001.SZ'].close.tolist(), [10., 20.])
        with self.assertRaisesRegex(rq.InputBlocked, 'DEEP_FRONT_SCOPE_LIMIT_120'):
            self.reader().histories(['000001.SZ'], '20261019', 121, 'front', 121)

    def test_frozen_bad_factor_inside_121_is_not_hidden(self):
        f = pd.DataFrame({'close': 10., 'open': 10., 'raw_close': 10., 'raw_open': 10.,
                          'prev_close': 10., 'volume': 100., 'num_trades': 2.,
                          'bar_available_at': '2026-10-20T13:00:00+08:00'},
                         index=pd.bdate_range(end='2026-10-19', periods=122).strftime('%Y%m%d'))
        f.iloc[5, f.columns.get_loc('prev_close')] = 0.
        f = rq.frozen_ret120_prices(f)
        f.attrs['source'] = 'RQDATA'
        self.assertTrue(rq.feature_histories({'000001.SZ': f}, '2026-10-20T14:00:00+08:00')['000001.SZ'].empty)
        executable = self.execution_frame()
        executable['factor_valid'] = False
        with self.assertRaisesRegex(rq.InputBlocked, 'EXECUTION_RESEARCH_FACTOR_INVALID'):
            rq.execution_bar(executable, '20261019', '2026-10-20T14:00:00+08:00')

    def execution_frame(self):
        f = self.reader().histories(['000001.SZ'], '20261019', 2, 'RQ_POST')['000001.SZ']
        f['status_suspended'] = [True, False]
        f['status_available_at'] = '2026-10-20T13:00:00+08:00'
        f['price_tick'] = .01
        f['tick_available_at'] = '2026-10-19T16:00:00+08:00'
        f['limit_up'] = 22.
        f['limit_down'] = 18.
        f['prev_close'] = 20.
        return f

    def test_execution_uses_raw_price_limit_tick_and_date(self):
        f = self.execution_frame()
        b = rq.execution_bar(f, '20261019', '2026-10-20T14:00:00+08:00')
        self.assertEqual(b['open'], 20.)
        self.assertEqual(f.loc['20261019', 'open'], 80.)
        self.assertEqual(b['price_tick'], .01)
        self.assertTrue(b['tradable'])
        f['tick_available_at'] = '2026-10-20T13:00:00+08:00'
        with self.assertRaisesRegex(rq.InputBlocked, 'EXACT_DATE_TICK_MISSING'):
            rq.execution_bar(f, '20261019', '2026-10-20T14:00:00+08:00')

    def test_suspension_and_zero_trades_never_execute_positive_ohlc(self):
        f = self.execution_frame()
        f.loc['20261019', 'status_suspended'] = True
        self.assertIsNone(rq.execution_bar(f, '20261019', '2026-10-20T14:00:00+08:00')['close'])
        f.loc['20261019', 'status_suspended'] = False
        f.loc['20261019', 'num_trades'] = 0.
        self.assertIsNone(rq.execution_bar(f, '20261019', '2026-10-20T14:00:00+08:00')['open'])

    def test_future_status_and_bar_times_cannot_execute(self):
        f = self.execution_frame()
        f['status_available_at'] = '2026-10-21T13:00:00+08:00'
        with self.assertRaisesRegex(rq.InputBlocked, 'EXECUTION_EVIDENCE_NOT_AVAILABLE'):
            rq.execution_bar(f, '20261019', '2026-10-20T14:00:00+08:00')
        f['status_available_at'] = '2026-10-20T13:00:00+08:00'
        f['bar_available_at'] = '2026-10-21T13:00:00+08:00'
        with self.assertRaisesRegex(rq.InputBlocked, 'EXECUTION_EVIDENCE_NOT_AVAILABLE'):
            rq.execution_bar(f, '20261019', '2026-10-20T14:00:00+08:00')

    def test_only_real_observations_count_toward_121(self):
        f = self.execution_frame()
        out = rq.feature_histories({'000001.SZ': f}, '2026-10-20T14:00:00+08:00')
        self.assertEqual(out['000001.SZ'].index.tolist(), ['20261019'])
        self.assertEqual(out['000001.SZ'].attrs['source'], 'RQDATA')

    def load_script(self, name):
        spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_close_readiness_has_no_intraday_query_and_requires_supplier(self):
        producer = self.load_script('collect_p0_rq_close_v01')
        denied = types.SimpleNamespace(is_data_ready=lambda **kw: self.fail('INTRADAY_QUERY'))
        out = producer.ready_receipt(denied, '20261020', '2026-10-20T15:00:00+08:00')
        self.assertEqual(out['status'], 'PENDING')
        provider = types.SimpleNamespace(is_data_ready=lambda **kw: pd.DataFrame(
            [dict(market='cn', category='stock_daybar', ready=False, latest_date=pd.Timestamp('2026-10-19'))]))
        self.assertEqual(producer.ready_receipt(provider, '20261020', '2026-10-20T16:00:00+08:00')['status'], 'PENDING')
        with self.assertRaisesRegex(producer.rq.InputBlocked, 'ONLY_ACTUAL_CURRENT_DAY'):
            producer.ready_receipt(denied, '20261021', '2026-10-20T16:00:00+08:00')

    def test_local_action_binding_empty_window_and_missing_factor_event(self):
        producer = self.load_script('collect_p0_rq_close_v01')
        catalog = self.root / 'fake_actions.duckdb'
        with duckdb.connect(str(catalog)) as src:
            src.execute('CREATE SCHEMA rqdata_a2')
            for name, field in [('dividend', 'ex_dividend_date'), ('split', 'ex_dividend_date'),
                                ('allotment', 'ex_right_date'), ('dividend_info', 'ex_dividend_date')]:
                src.execute(f'CREATE TABLE rqdata_a2.{name}(order_book_id VARCHAR, {field} DATE)')
        before = rq.sha(catalog)
        with duckdb.connect(':memory:') as c:
            c.execute("CREATE TABLE membership_reference AS SELECT '000905' AS index_code, lpad(i::VARCHAR,6,'0')||'.XSHE' AS order_book_id FROM range(1,501) t(i)")
            c.execute("CREATE TABLE raw_daily AS SELECT order_book_id, DATE '2026-10-19'-j::INTEGER AS date FROM membership_reference CROSS JOIN range(120) t(j)")
            c.execute('CREATE TABLE factors(order_book_id VARCHAR,ex_date DATE)')
            c.execute("CREATE TABLE corporate_actions AS SELECT 'STALE' AS order_book_id")
            good = producer.bind_local_actions(c, '20261019', '2026-10-20T15:00:00+08:00', catalog)
            self.assertTrue(good['corporate_action_coverage_complete'])
            self.assertEqual(good['corporate_action_events'], 0)
            self.assertNotIn('corporate_actions', {r[0] for r in c.execute('SHOW TABLES').fetchall()})
            c.execute("INSERT INTO factors VALUES ('000001.XSHE', DATE '2026-10-13')")
            missing = producer.bind_local_actions(c, '20261019', '2026-10-20T15:00:00+08:00', catalog)
            self.assertFalse(missing['corporate_action_coverage_complete'])
            self.assertEqual(missing['corporate_action_missing'], [dict(order_book_id='000001.XSHE', ex_date='2026-10-13')])
            captured = pd.DataFrame([dict(order_book_id='000001.XSHE',ex_date=pd.Timestamp('2026-10-13'),
                cash_per_share=.5,share_multiplier=1.,rights_per_share=0.,rights_price=0.,
                available_at='2026-10-12T23:59:59+08:00',observed_at='2026-10-20T14:00:00+08:00')])
            c.register('saved_capture', captured)
            c.execute('CREATE TABLE captured_actions AS SELECT * FROM saved_capture')
            bound = producer.bind_local_actions(c, '20261019', '2026-10-20T15:00:00+08:00', catalog)
            self.assertTrue(bound['corporate_action_coverage_complete'])
            self.assertEqual(c.execute('SELECT cash_per_share FROM corporate_actions').fetchone()[0], .5)
            c.execute('INSERT INTO captured_actions SELECT order_book_id,ex_date,.6,share_multiplier,rights_per_share,rights_price,available_at,observed_at FROM captured_actions')
            conflict = producer.bind_local_actions(c, '20261019', '2026-10-20T15:00:00+08:00', catalog)
            self.assertFalse(conflict['corporate_action_coverage_complete'])
            self.assertEqual(conflict['corporate_action_reason'], 'LOCAL_AND_CAPTURED_ACTION_TERMS_CONFLICT')
        self.assertEqual(rq.sha(catalog), before)

    def test_current_action_capture_exact_two_ids_and_dates(self):
        producer = self.load_script('collect_p0_rq_close_v01')
        calls = []
        ids = ['000783.XSHE', '002812.XSHE']
        def fetch(kind, symbols, **kw):
            calls.append((kind, symbols, kw))
            if kind != 'dividend':
                return None
            return pd.DataFrame([dict(order_book_id=s, round_lot=10., dividend_cash_before_tax=.5,
                declaration_announcement_date=pd.Timestamp('2026-10-19'),
                ex_dividend_date=pd.Timestamp('2026-10-20')) for s in symbols])
        provider = types.SimpleNamespace(**{kind: (lambda ids, _kind=kind, **kw: fetch(_kind[4:], ids, **kw))
            for kind in ('get_dividend','get_split','get_allotment')})
        with patch.object(producer.rq, 'clock', return_value=pd.Timestamp('2026-10-20T16:00:00+08:00')):
            producer.capture_action_terms(provider, ids, '2026-10-19', '2026-10-20', self.root)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(c[1] == ids and c[2] == dict(start_date='2026-10-19',end_date='2026-10-20') for c in calls))
        actions = pd.read_parquet(self.root / 'captured_actions.parquet')
        self.assertEqual(actions.cash_per_share.tolist(), [.05, .05])
        self.assertTrue(actions.observed_at.eq('2026-10-20T16:00:00+08:00').all())
        self.assertTrue(pd.to_datetime(actions.ex_date).eq(pd.Timestamp('2026-10-20')).all())

    def test_seal_saved_capture_preserves_new_factor_creation_time(self):
        producer = self.load_script('collect_p0_rq_close_v01')
        self.meta['adjustments'] = {}
        with duckdb.connect(str(self.db)) as c:
            c.execute('ALTER TABLE raw_daily ADD COLUMN limit_up DOUBLE')
            c.execute('ALTER TABLE raw_daily ADD COLUMN limit_down DOUBLE')
            c.execute('''CREATE TABLE factor_raw_versions(order_book_id VARCHAR, ex_date DATE,
                ex_factor DOUBLE, ex_cum_factor DOUBLE, announcement_date DATE,
                ex_end_date DATE, source_version VARCHAR)''')
            c.execute("DELETE FROM factors WHERE ex_date>DATE '2026-10-19'")
        self.seal()
        original_hash = rq.sha(self.db)
        work = self.root / 'offline_saved_capture'
        work.mkdir()
        ready = dict(status='PASS', business_date='2026-10-20', available_at='2026-10-20T15:52:00+08:00')
        for name in ('supplier_ready.json', 'supplier_ready_after.json'):
            producer.write_json(work / name, ready)
        bars = pd.DataFrame([dict(order_book_id='000001.XSHE', date=pd.Timestamp('2026-10-20'),
            open=20., high=21., low=19., close=20., volume=100., total_turnover=2000.,
            prev_close=20., limit_up=22., limit_down=18., num_trades=10., source_version='SAVED_FAKE')])
        status = pd.DataFrame([dict(order_book_id='000001.XSHE', date=pd.Timestamp('2026-10-20'),
            status_suspended=False, status_available_at=ready['available_at'], price_tick=.01,
            tick_available_at=ready['available_at'])])
        created = 1791808164  # actual vendor representation retained as raw integer
        factor = pd.DataFrame([dict(order_book_id='000001.XSHE', ex_date=pd.Timestamp('2026-10-20'),
            ex_factor=1.1, ex_cum_factor=4.4, announcement_date=pd.Timestamp('2026-10-19'),
            ex_end_date=pd.Timestamp('2026-10-21'), rice_create_tm=created)])
        with patch.object(producer.rq, 'clock', return_value=pd.Timestamp('2026-10-20T15:55:00+08:00')):
            result = producer.seal_capture(self.reader(), '20261020', work, bars, status,
                pd.DataFrame({'order_book_id': ['000001.XSHE']}), [factor], ready)
        self.assertEqual(result['status'], 'PASS')
        with duckdb.connect(str(work / 'daily.duckdb'), read_only=True) as c:
            observed = c.execute('SELECT rice_create_tm FROM factor_raw_versions').df().iloc[0, 0]
            self.assertEqual(observed, created)
        self.assertEqual(rq.sha(self.db), original_hash)
        self.assertTrue((work / 'identity_observed_today.parquet').is_file())
        producer.rq.DailyInput(work / 'manifest.json', {'enabled': False}, now='2026-10-20T16:00:00+08:00')

    def test_close_increment_two_successive_fake_days_and_sealed_readback(self):
        producer = self.load_script('collect_p0_rq_close_v01')
        self.meta['adjustments'] = {}
        with duckdb.connect(str(self.db)) as c:
            for col in ['limit_up', 'limit_down']:
                c.execute('ALTER TABLE raw_daily ADD COLUMN ' + col + ' DOUBLE')
            c.execute('UPDATE raw_daily SET prev_close=20,limit_up=22,limit_down=18')
            c.execute("DELETE FROM factors WHERE ex_date > DATE '2026-10-19'")
        self.seal()
        ids = [f'{i:06}.XSHE' for i in range(1, 3002)]
        class Provider:
            day = pd.Timestamp('2026-10-20')
            def all_instruments(self, **kw):
                return pd.DataFrame({'order_book_id': ids})
            def is_data_ready(self, **kw):
                return pd.DataFrame([dict(market='cn', category='stock_daybar', ready=True, latest_date=self.day)])
            def get_price(self, batch, **kw):
                self.assert_exact(kw)
                x = pd.DataFrame({'order_book_id': batch, 'date': self.day,
                    'open': 20., 'high': 21., 'low': 19., 'close': 20., 'volume': 100.,
                    'total_turnover': 2000., 'prev_close': 20., 'limit_up': 22.,
                    'limit_down': 18., 'num_trades': 2.})
                x.loc[x.order_book_id.eq('000016.XSHE'), ['volume', 'total_turnover', 'num_trades']] = 0.
                return x
            def assert_exact(self, kw):
                assert kw['start_date'] == self.day.date() == kw['end_date']
                assert kw['adjust_type'] == 'none'
            def is_suspended(self, batch, **kw):
                return pd.DataFrame({ob: [ob == '000016.XSHE'] for ob in batch}, index=[self.day])
            def get_tick_size(self, batch):
                return pd.Series(.01, index=batch)
            def get_ex_factor(self, *args, **kw):
                return None
        provider = Provider()
        manifest = self.root / 'manifest.json'
        previous_hash = rq.sha(self.db)
        for day in ['2026-10-20', '2026-10-21']:
            provider.day = pd.Timestamp(day)
            work = self.root / day
            with patch.object(producer.rq, 'clock', return_value=pd.Timestamp(day + 'T16:00:00+08:00')):
                result = producer.collect(provider, manifest, day, work)
                self.assertEqual(result['status'], 'PASS')
                sealed = producer.rq.DailyInput(work / 'manifest.json', {'enabled': False})
                self.assertEqual(sealed.meta['business_date'], day)
                self.assertEqual(sealed.meta['quality_status'], 'PASS')
                frame = sealed.histories(['000001.SZ'], day, 3)['000001.SZ']
                self.assertTrue(producer.rq.execution_bar(frame, day)['tradable'])
                halted = sealed.histories(['000016.SZ'], day, 1)['000016.SZ']
                self.assertFalse(producer.rq.execution_bar(halted, day)['tradable'])
            manifest = work / 'manifest.json'
        self.assertEqual(rq.sha(self.db), previous_hash)
        # The production publisher is never called by this fake test.

    def test_ret_runner_raw_entry_deferred_exit_and_unchanged_economic_pnl(self):
        runner = self.load_script('run_ret120_csi500_forward')
        f = self.execution_frame()
        provider = types.SimpleNamespace(get_instrument_detail=lambda *a, **kw:
            {'InstrumentName': 'FAKE', 'OpenDate': '19910101', 'InstrumentStatus': 0,
             'UpStopPrice': 22., 'PriceTick': .01})
        with patch.object(runner.rq_input, 'enabled', return_value=True), patch.object(
                runner.rq_input, 'clock', return_value=pd.Timestamp('2026-10-20T14:00:00+08:00')):
            with runner.ResearchStore(self.root / 'isolated_runner.duckdb') as store:
                runner.ensure_ret120_tables(store)
                state = runner.initial_state(runner.POLICY, {})
                store.con.execute('''INSERT INTO ret120_forward_signals
                    (strategy_id,symbol,signal_date,ret120,quintile,status)
                    VALUES (?,'000001.SZ','20261016',.1,'Q5','PENDING_ENTRY')''', [runner.POLICY.strategy_id])
                diag = runner.process_entries(store, provider, {'000001.SZ': f}, '20261019', ['20261016', '20261019'], state)
                self.assertEqual(diag['entered'], 1)
                self.assertEqual(store.con.execute('SELECT entry_open FROM ret120_forward_signals').fetchone()[0], 20.)
                raw, adjusted = store.con.execute("SELECT raw_price,research_price FROM ret120_price_basis WHERE ledger='BASE20'").fetchone()
                self.assertEqual((raw, adjusted), (20., 80.))
                calendar = pd.bdate_range('2026-10-16', periods=122).strftime('%Y%m%d').tolist()
                target, resume = calendar[120:122]
                last = f.iloc[[-1]].copy()
                last.index = [target]
                last['status_suspended'] = True
                last['volume'] = last['num_trades'] = 0.
                last['tick_available_at'] = pd.Timestamp(target).strftime('%Y-%m-%d') + 'T16:00:00+08:00'
                with patch.object(runner.rq_input, 'clock', return_value=pd.Timestamp(target).tz_localize(rq.CST) + pd.Timedelta(hours=17)):
                    diag = runner.process_exits_and_mark(store, {'000001.SZ': last}, target, calendar, state)
                self.assertEqual(diag['deferred_due'], 1)
                self.assertEqual(store.con.execute('SELECT count(*) FROM forward_trades').fetchone()[0], 0)
                last.index = [resume]
                last['status_suspended'] = False
                last['is_suspended'] = False
                last['volume'], last['num_trades'] = 100., 2.
                last['raw_close'], last['close'], last['research_factor'] = 11., 88., 8.
                last['tick_available_at'] = pd.Timestamp(resume).strftime('%Y-%m-%d') + 'T16:00:00+08:00'
                with patch.object(runner.rq_input, 'clock', return_value=pd.Timestamp(resume).tz_localize(rq.CST) + pd.Timedelta(hours=17)):
                    diag = runner.process_exits_and_mark(store, {'000001.SZ': last}, resume, calendar, state)
                self.assertEqual(diag['exited'], 1)
                notional, pnl = store.con.execute("SELECT entry_notional,net_pnl FROM forward_trades WHERE ledger='BASE20'").fetchone()
                self.assertAlmostEqual(pnl, notional * ((88 / 80) * (1 - .001) - 1 - .001))
                self.assertEqual(store.con.execute("SELECT raw_price FROM ret120_price_basis WHERE ledger='BASE20' AND phase='EXIT'").fetchone()[0], 11.)

    def test_optional_prior_coverage_does_not_fabricate_short_history(self):
        out = self.reader().histories(['000001.SZ', '600000.SH'], '20261019', 121,
                                      min_bars=121, allow_incomplete=True)
        self.assertEqual(out, {})

    def test_hash_change_rejected(self):
        (self.root / 'source_metadata.json').write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(rq.InputBlocked, 'FILE_HASH_MISMATCH'):
            self.reader()

    def test_no_complete_is_not_consumable(self):
        (self.root / 'COMPLETE.json').unlink()
        with self.assertRaises(FileNotFoundError):
            self.reader()

    def test_invalid_prices_not_silently_filtered(self):
        with duckdb.connect(str(self.db)) as c:
            c.execute('UPDATE raw_daily SET high=1')
        self.seal()
        with self.assertRaisesRegex(rq.InputBlocked, 'INVALID_OHLC_RANGE'):
            self.reader().histories(['000001.SZ'], '20261019', 2)

    def test_same_asset_repeated_reads_do_not_modify_bytes(self):
        before = rq.sha(self.db)
        one = self.reader().histories(['000001.SZ'], '20261019', 2)['000001.SZ']
        two = self.reader().histories(['000001.SZ'], '20261019', 2)['000001.SZ']
        pd.testing.assert_frame_equal(one, two)
        self.assertEqual(rq.sha(self.db), before)

    def test_default_profile_disabled(self):
        self.assertIs(rq.profile()['enabled'], False)
        self.assertIsNone(rq.open_inputs('20261019'))

    def test_four_prior_threshold_and_formula_functions_unchanged(self):
        baseline = Path(r'D:\TEMP\positionx_fullqmt_cutover_20261020\candidate\scripts\run_ret120_universe_prior_forward_v10.py')
        staged = ROOT / 'scripts/run_ret120_universe_prior_forward_v10.py'
        def nodes(path):
            return {n.name: ast.dump(n, include_attributes=False) for n in ast.parse(path.read_text(encoding='utf-8')).body
                    if isinstance(n, ast.FunctionDef)}
        old, new = nodes(baseline), nodes(staged)
        for name in ['compute_ret120', 'tier_coverage', 'completeness', 'load_scanner']:
            self.assertEqual(old[name], new[name], name)

    def membership(self):
        rows = []
        for code, lo, count in [('000300.SH', 1, 300), ('000905.SH', 1001, 500), ('000852.SH', 3001, 1000)]:
            for i in range(lo, lo + count):
                rows.append({'snapshot_date': '20261022', 'effective_date': '2026-10-22',
                             'observed_at': '2026-10-22T08:20:00+08:00',
                             'provider_created_at': '2026-10-21T19:20:00+08:00',
                             'index_code': code, 'symbol': f'{i:06}.SZ', 'source': 'RQDATA_COMPONENTS_PROVIDER'})
        return pd.DataFrame(rows)

    def test_real_time_preopen_membership_accepted_as_observation_only(self):
        sets = rq.validate_preopen_membership(self.membership(), '20261022', '2026-10-22T16:00:00+08:00')
        self.assertEqual(len(sets['000905.SH']), 500)

    def test_midday_capture_cannot_become_preopen_PIT(self):
        f = self.membership()
        f['observed_at'] = '2026-10-22T13:38:00+08:00'
        with self.assertRaisesRegex(rq.InputBlocked, 'PIT_ASOF_INVALID'):
            rq.validate_preopen_membership(f, '20261022', '2026-10-22T16:00:00+08:00')

    def test_future_provider_availability_rejected(self):
        f = self.membership()
        f['provider_created_at'] = '2026-10-22T08:21:00+08:00'
        with self.assertRaisesRegex(rq.InputBlocked, 'PIT_ASOF_INVALID'):
            rq.validate_preopen_membership(f, '20261022', '2026-10-22T16:00:00+08:00')

    def test_wrong_effective_day_rejected(self):
        f = self.membership()
        f['effective_date'] = '2026-10-21'
        with self.assertRaisesRegex(rq.InputBlocked, 'PIT_ASOF_INVALID'):
            rq.validate_preopen_membership(f, '20261022', '2026-10-22T16:00:00+08:00')

    def test_duplicate_member_rejected(self):
        f = self.membership()
        f = pd.concat([f, f.iloc[:1]], ignore_index=True)
        with self.assertRaisesRegex(rq.InputBlocked, 'PIT_ASOF_INVALID'):
            rq.validate_preopen_membership(f, '20261022', '2026-10-22T16:00:00+08:00')

    def test_fake_live_capture_records_actual_return_time(self):
        f = self.membership()
        class Provider:
            def index_components(self, code, date, return_create_tm):
                rows = f.loc[f.index_code.eq(rq.qmt_id(code)), 'symbol']
                return [rq.rq_id(s) for s in rows], pd.Timestamp('2026-10-21 19:20:00')
        with patch.object(rq, 'clock', return_value=pd.Timestamp('2026-10-22T08:20:01+08:00')):
            out = rq.membership_frame(Provider(), '2026-10-22T08:20:00+08:00', 'FAKE_CAPTURE')
        self.assertTrue(out.observed_at.eq('2026-10-22T08:20:01+08:00').all())
        self.assertTrue(out.provider_created_at.eq('2026-10-21T19:20:00+08:00').all())

    def test_late_capture_does_not_query_provider(self):
        provider = types.SimpleNamespace(index_components=lambda *a, **kw: self.fail('LATE_QUERY'))
        with patch.object(rq, 'clock', return_value=pd.Timestamp('2026-10-22T10:00:00+08:00')):
            with self.assertRaisesRegex(rq.InputBlocked, 'PREOPEN_CAPTURE_TIME_INVALID'):
                rq.membership_frame(provider, '2026-10-22T10:00:00+08:00', 'FAKE')

    def function(self, filename, name, env):
        tree = ast.parse((ROOT / 'scripts' / filename).read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
        future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
        module = ast.fix_missing_locations(ast.Module(body=[future, node], type_ignores=[]))
        exec(compile(module, filename, 'exec'), env)
        return env[name]

    def test_deep_read_call_uses_120_and_preserves_lineage_without_download(self):
        calls = []
        f = self.reader().histories(['000001.SZ'], '20261019', 2)['000001.SZ']
        fake = types.SimpleNamespace(enabled=lambda: True,
            strategy_histories=lambda *args: (calls.append(args), {'000001.SZ': f})[1])
        sentinel = object()
        fun = self.function('run_deep_oversold_csi500_forward.py', 'load_histories',
                            {'rq_input': fake, 'xtdata': sentinel,
                             'POLICY': types.SimpleNamespace(dividend_type='front'),
                             'normalize_history': lambda frame: frame})
        fake_runtime = types.SimpleNamespace(get_xtdata=lambda: sentinel)
        with patch.dict(sys.modules, {'core.qmt_runtime': fake_runtime}):
            result = fun(['000001.SZ'], bars=120, refresh=True)
        self.assertEqual(calls, [(sentinel, ['000001.SZ'], 120, 'front', 120)])
        self.assertEqual(result['000001.SZ'].attrs['source'], 'RQDATA')

    def test_ret_refresh_checks_sealed_input_instead_of_native_download(self):
        calls = []
        obj = types.SimpleNamespace(require_contract=lambda value: calls.append(('contract', value)))
        fake = types.SimpleNamespace(enabled=lambda: True,
                                     open_inputs=lambda target: (calls.append(('asof', target)), obj)[1])
        fun = self.function('run_ret120_csi500_forward.py', 'refresh_history',
                            {'rq_input': fake, 'POLICY': types.SimpleNamespace(dividend_type='back_ratio')})
        fun(object(), ['000001.SZ'], '20261022')
        self.assertEqual(calls, [('asof', '20261022'), ('contract', 'back_ratio')])

    def test_prior_reads_rq_and_refresh_cannot_start_qmt_download(self):
        calls = []
        fake = types.SimpleNamespace(enabled=lambda: True,
                                     prior_close=lambda *args: calls.append(args))
        env = {'rq_input': fake}
        read = self.function('run_ret120_universe_prior_forward_v10.py', 'local_back_ratio_close', env)
        refresh = self.function('run_ret120_universe_prior_forward_v10.py', 'refresh_history', env)
        read(['000001.SZ'], '2026-10-22', {})
        self.assertEqual(calls, [(['000001.SZ'], '2026-10-22')])
        with self.assertRaisesRegex(RuntimeError, 'NO_QMT_DOWNLOAD'):
            refresh(['000001.SZ'], '2026-10-22', {})

    def test_pit_manifest_uses_module_binding_in_both_phases(self):
        path = ROOT / 'scripts/daily_pit_intake_v01.py'
        table = symtable.symtable(path.read_text(encoding='utf-8'), str(path), 'exec')
        run = next(child for child in table.get_children() if child.get_name() == 'run')
        symbol = run.lookup('rq_input')
        self.assertTrue(symbol.is_global())
        self.assertFalse(symbol.is_local())


if __name__ == '__main__':
    unittest.main()
