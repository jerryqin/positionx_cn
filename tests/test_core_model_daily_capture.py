"""Daily CORE capture/qualification in owned files; all external calls are fake."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import duckdb
import pandas as pd

from core import p0_rq_input as rq
from scripts import collect_postclose_candidate as capture
from scripts import prepare_daily_data as prep
from scripts import postclose_data_admission_v1 as admission


DAY = '20261029'
NOW = pd.Timestamp('2026-10-29T16:10:00+08:00')
OBSERVED = '2026-10-29T16:00:00+08:00'
IDS = ['600000.XSHG', '600001.XSHG']


def identity(ids):
    return pd.DataFrame(dict(order_book_id=ids, type='CS', exchange='XSHG',
        board_type='MainBoard', symbol='Owned fixture', listed_date='1991-01-01',
        de_listed_date='0000-00-00', special_type='Normal', round_lot=100))


def daily_rows(ids):
    bars = pd.DataFrame(dict(order_book_id=ids, date=pd.Timestamp(DAY),
        open=10., high=11., low=9., close=10.5, prev_close=10., volume=1000.,
        total_turnover=10500., limit_up=11., limit_down=9., num_trades=10.,
        source_version='OWNED_TEST_ONLY'))
    status = pd.DataFrame(dict(order_book_id=ids, date=pd.Timestamp(DAY),
        status_suspended=False, status_available_at=OBSERVED,
        price_tick=None, tick_available_at=None))
    return bars, status


def proof(row, day, **kwargs):
    return dict(price_tick=.01, identity={key: row[key] for key in
        ('order_book_id', 'type', 'exchange', 'board_type')},
        source='EXCHANGE_RULE_DERIVED', rule_id='OWNED_RULE', version='V1',
        effective_from='2026-07-06', source_sha256='owned source binding',
        acquired_at='2026-10-28T20:00:00+08:00')


class CoreModelDailyCaptureTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='positionx_core_daily_test_')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        (self.root / 'reports').mkdir()
        for patcher in (patch.object(prep, 'ROOT', self.root),
                        patch.object(capture, 'ROOT', self.root),
                        patch.object(rq, 'clock', return_value=NOW),
                        patch.object(rq, 'profile', return_value={'enabled': False}),
                        patch('core.exchange_rule_evidence.resolve_rule', side_effect=proof),
                        patch('core.exchange_rule_evidence.verify_rule_evidence',
                              side_effect=lambda evidence, *args, **kw: evidence)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_tables(self, path, tables):
        with duckdb.connect(str(path), config={'temp_directory': ''}) as con:
            for name, frame in tables.items():
                con.register('owned_frame', frame)
                con.execute('CREATE TABLE ' + name + ' AS SELECT * FROM owned_frame')

    def source(self):
        root = self.root / 'reports/source'
        root.mkdir()
        bars, status = daily_rows(IDS)
        db = root / 'daily.duckdb'
        self.write_tables(db, dict(raw_daily=bars, execution_status=status,
                                  identity_observed_today=identity(IDS)))
        meta = dict(source='RQDATA', database='daily.duckdb', database_sha256=rq.sha(db),
            version='asof_' + DAY + '_' + rq.sha(db)[:12], asof='2026-10-29',
            business_date='2026-10-29', availability=OBSERVED, volume_unit='SHARES',
            raw_adjustment='none', qualification_scope='DAILY_SIGNALS_V1',
            quality_status='DAILY_SIGNALS_PASS_V1', engineering_only=True,
            full_p0_pass=False, execution_qualified=False)
        capture.local.write_json(root / 'source_metadata.json', meta)
        for name in ('supplier_ready.json', 'supplier_ready_after.json'):
            capture.local.write_json(root / name, dict(status='PASS',
                business_date='2026-10-29', available_at=OBSERVED))
        files = ['daily.duckdb', 'source_metadata.json', 'supplier_ready.json', 'supplier_ready_after.json']
        capture.local.write_json(root / 'manifest.json', dict(version=meta['version'],
            files=[dict(path=name, sha256=rq.sha(root / name)) for name in files]))
        capture.local.write_json(root / 'COMPLETE.json', dict(manifest_sha256=rq.sha(root / 'manifest.json')))
        return root

    def test_new_day_resume_only_qualifies_incoming_execution_intersection(self):
        ids = [f'{600000 + number:06d}.XSHG' for number in range(3000)]
        native = identity(ids)
        native.attrs['identity_capture'] = dict(business_date=DAY)
        bars, status = daily_rows(ids)
        out = self.root / 'reports/resume'
        out.mkdir()
        base = self.root / 'reports/base'
        base.mkdir()
        (base / 'manifest.json').write_text('owned parent', encoding='utf-8')
        capture.local.write_json(base / 'source_metadata.json', {'qualification_scope': 'P0_COMPLETE'})
        capture.local.write_json(out / 'capture.json', dict(target_date=DAY,
            base_manifest_sha256=rq.sha(base / 'manifest.json'), qualification_scope='CORE_MODELS_V1',
            execution_symbols=['600000.SH', '600001.SH']))
        self.write_tables(out / 'increment.duckdb', dict(identity_observed_today=native,
            raw_daily=bars, execution_status=status.iloc[:1],
            factor_capture_coverage=pd.DataFrame({'order_book_id': ids})))
        class StopAfterInspectedRules(Exception):
            pass
        class Provider:
            calls = []
            def is_suspended(self, requested, **kwargs):
                self.calls.append((list(requested), kwargs))
                return pd.DataFrame(False, index=pd.DatetimeIndex([DAY]), columns=requested)
        provider = Provider()
        def inspect(frame, native, selected, day):
            self.assertEqual(day, DAY)
            self.assertEqual(set(selected), {'600001.SH'})
            self.assertNotIn('600000.XSHG', set(frame.order_book_id))
            self.assertTrue(frame.price_tick.isna().all())
            raise StopAfterInspectedRules()
        with patch.object(rq, 'require_trading_date'), \
             patch.object(rq, 'DailyInput', return_value=SimpleNamespace(meta={'asof': '2026-10-28'})), \
             patch.object(capture.local, 'sessions', return_value=['20261028', DAY]), \
             patch.object(capture.old, 'ready_receipt', return_value={'status': 'PASS'}), \
             patch.object(capture, 'capture_identity', return_value=native), \
             patch.object(capture, 'apply_execution_rules', side_effect=inspect):
            with self.assertRaises(StopAfterInspectedRules):
                capture.run(DAY, base / 'manifest.json', out, provider,
                    scope='CORE_MODELS_V1', execution_symbols=['600000.SH', '600001.SH'])
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(provider.calls[0][0]), 2999)
        self.assertFalse((out / 'manifest.json').exists())

    def test_complete_candidate_rejects_changed_execution_set_without_calls(self):
        out = self.root / 'reports/complete'
        out.mkdir()
        (out / 'manifest.json').write_text('{}', encoding='utf-8')
        with patch.object(capture, 'verified_existing', return_value={
                'execution_qualified_symbols': ['600000.SH']}), \
             patch.object(capture, 'bounded_provider') as factory:
            with self.assertRaisesRegex(ValueError, 'RESUME_BINDING_FAILED_EXECUTION_SET_CHANGED'):
                capture.run(DAY, self.root / 'parent.json', out,
                    scope='CORE_MODELS_V1', execution_symbols=['600001.SH'])
        factory.assert_not_called()

    def test_rule_adaptation_keeps_original_nulls_and_input_frame(self):
        _, status = daily_rows(IDS)
        before = status.copy(deep=True)
        result, bindings = capture.apply_execution_rules(status, identity(IDS), ['600000.SH'], DAY)
        pd.testing.assert_frame_equal(status, before)
        self.assertTrue(result.price_tick.isna().all())
        self.assertTrue(result.tick_available_at.isna().all())
        self.assertEqual(result.iloc[0].rule_price_tick, .01)
        self.assertTrue(pd.isna(result.iloc[1].rule_price_tick))
        self.assertEqual(set(bindings), {'600000.SH'})

    def test_empty_execution_set_is_explicit_and_does_not_claim_pool_p0(self):
        source = self.source()
        reader = prep.qualify_core_models(source / 'manifest.json', self.root / 'reports/empty', [])
        audit = admission.audit_core_model_asset(reader, DAY, execution_symbols=[])
        self.assertEqual((audit['execution_count'], audit['execution_symbols']), (0, []))
        self.assertFalse(audit['full_p0_pass'])
        self.assertEqual(audit['quality']['raw_tick_unknown_rows'], 2)

    def test_qualification_reuses_identical_binding_and_preserves_parent(self):
        source = self.source()
        before = {p.name: rq.sha(p) for p in source.iterdir()}
        out = self.root / 'reports/qualified'
        reader = prep.qualify_core_models(source / 'manifest.json', out, ['600000.SH'])
        hashes = {p.name: rq.sha(p) for p in out.iterdir()}
        again = prep.qualify_core_models(source / 'manifest.json', out, ['600000.SH'])
        self.assertEqual(reader.meta['version'], again.meta['version'])
        self.assertEqual(hashes, {p.name: rq.sha(p) for p in out.iterdir()})
        self.assertEqual(before, {p.name: rq.sha(p) for p in source.iterdir()})
        self.assertEqual(admission.audit_core_model_asset(reader, DAY)['execution_count'], 1)
        with self.assertRaisesRegex(ValueError, 'CORE_QUALIFICATION_BINDING_CHANGED'):
            prep.qualify_core_models(source / 'manifest.json', out, ['600001.SH'])

    def test_failed_qualification_has_no_complete_marker_or_parent_changes(self):
        source = self.source()
        before = {p.name: rq.sha(p) for p in source.iterdir()}
        out = self.root / 'reports/failed'
        with patch.object(admission, 'audit_core_model_asset', side_effect=ValueError('OWNED_AUDIT_FAILURE')):
            with self.assertRaisesRegex(ValueError, 'OWNED_AUDIT_FAILURE'):
                prep.qualify_core_models(source / 'manifest.json', out, ['600000.SH'])
        self.assertFalse((out / 'COMPLETE.json').exists())
        self.assertTrue((out / 'daily.duckdb').exists())
        self.assertEqual(before, {p.name: rq.sha(p) for p in source.iterdir()})


if __name__ == '__main__':
    unittest.main()
