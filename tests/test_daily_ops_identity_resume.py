"""Direct identity-entry integration checks with fake transport and owned files.

No network, credentials, Task Scheduler, production writes, or old-suite runs.
"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import duckdb
import pandas as pd

from core import p0_rq_input as rq, rqdata_identity as identity_core
from scripts import collect_postclose_candidate as candidate
from scripts import prepare_daily_data as prep
from scripts import run_data_only_daily as daily


DAY = '20261028'


def identity_frame():
    ids = [f'{600000 + n:06}.XSHG' for n in range(3000)]
    return pd.DataFrame(dict(order_book_id=ids, symbol='Fake identity',
        listed_date='1991-01-01', de_listed_date='0000-00-00', type='CS',
        special_type='Normal', status='Active', round_lot=100))


def captured(output, *, altered=False):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    proof = output/'checkpoint.json'
    proof.write_text(json.dumps(dict(schema='FAKE_COMPLETE_IDENTITY', business_date=DAY)), encoding='utf-8')
    frame = identity_frame()
    if altered:
        frame.loc[0, 'symbol'] = 'Different observed value'
    frame.attrs['identity_capture'] = dict(manifest=str(proof), manifest_sha256=rq.sha(proof),
        business_date=DAY, target_list_sha256='fake_same_day_target',
        observed_range=dict(first='2026-10-28T08:19:57+08:00', last='2026-10-28T08:19:59+08:00'),
        atomic_snapshot=False, rows=len(frame))
    return frame


class Provider:
    source_version = 'RQDATA_HTTPS_V1'

    def __init__(self, *, fail_price=False):
        self.ids = identity_frame().order_book_id.tolist()
        self.calls = []
        self.fail_price = fail_price

    def all_instruments(self, **kwargs):
        raise AssertionError('FORBIDDEN_FULL_IDENTITY_CALL')

    def is_data_ready(self, **kwargs):
        self.calls.append('ready')
        return pd.DataFrame([dict(market='cn', category='stock_daybar', ready=True,
                                  latest_date='2026-10-28')])

    def index_components(self, code, date, return_create_tm):
        self.calls.append('members')
        start, count = {'000300.XSHG': (0, 300), '000905.XSHG': (300, 500),
                        '000852.XSHG': (800, 1000)}[code]
        return self.ids[start:start+count], pd.Timestamp('2026-10-27T18:00:00+08:00')

    def get_price(self, ids, **kwargs):
        self.calls.append('price')
        if self.fail_price:
            raise ValueError('FAKE_PRICE_FAILURE_AFTER_IDENTITY')
        ids = [ids] if isinstance(ids, str) else ids
        frame = pd.DataFrame(dict(order_book_id=ids, date=pd.Timestamp('2026-10-28'),
            open=10., high=11., low=9., close=10.5, prev_close=10., volume=1000.,
            total_turnover=10500., limit_up=11., limit_down=9., num_trades=10.))
        return frame[['order_book_id', 'date', *kwargs['fields']]]

    def is_suspended(self, ids, **kwargs):
        self.calls.append('suspended')
        return pd.DataFrame(False, index=[pd.Timestamp('2026-10-28')], columns=ids)

    def get_tick_size(self, ids):
        self.calls.append('tick')
        return pd.Series(.01, index=ids)

    def get_ex_factor(self, ids, **kwargs):
        self.calls.append('factor')
        return pd.DataFrame(columns=['order_book_id', 'ex_date', 'ex_cum_factor'])


class IdentityEntryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_legacy_batch_missing_same_day_target_blocks_before_members_and_output(self):
        provider = Mock()
        output = self.root/'reports/new_preopen'
        with patch.object(prep, 'ROOT', self.root), patch.object(identity_core, 'ROOT', self.root), \
                patch.object(prep, 'capture_identity', identity_core.capture_identity_batches), \
                patch.object(rq, 'clock', return_value=pd.Timestamp('2026-10-28T08:20:00+08:00')):
            with self.assertRaisesRegex(rq.InputBlocked, 'SAME_DAY_QUALIFIED_TARGET_MISSING'):
                prep.preopen(DAY, output, provider)
        provider.call.assert_not_called()
        provider.index_components.assert_not_called()
        self.assertFalse(output.exists())

    def test_preopen_identity_precedes_members_and_preserves_observation_range(self):
        provider = Provider()
        output = self.root/'reports/preopen'
        def obtain(p, day, folder, **kwargs):
            self.assertIs(p, provider)
            self.assertEqual(day, DAY)
            self.assertEqual(kwargs['deadline'], pd.Timestamp('2026-10-28T09:20:00+08:00'))
            provider.calls.append('identity')
            return captured(folder)
        with patch.object(prep, 'ROOT', self.root), patch.object(prep, 'capture_identity', side_effect=obtain), \
                patch.object(rq, 'clock', return_value=pd.Timestamp('2026-10-28T08:20:01+08:00')):
            result = prep.preopen(DAY, output, provider)
        self.assertEqual(provider.calls, ['identity', 'members', 'members', 'members'])
        proof = next(output.glob('pit/manifests/*_preopen.json'))
        manifest = json.loads(proof.read_text(encoding='utf-8'))
        self.assertEqual(result['status'], 'GREEN_PIT')
        self.assertEqual(manifest['identity_capture'], result['identity_capture'])
        self.assertEqual(manifest['identity_observed_range'], result['identity_capture']['observed_range'])
        self.assertFalse(manifest['identity_capture']['atomic_snapshot'])
        self.assertNotEqual(manifest['observed_at'], manifest['identity_observed_range']['last'])

    def test_identity_crossing_deadline_never_requests_members(self):
        provider = Provider()
        output = self.root/'reports/missed'
        with patch.object(prep, 'ROOT', self.root), \
                patch.object(prep, 'capture_identity', side_effect=lambda p,d,folder,**k: captured(folder)), \
                patch.object(rq, 'clock', side_effect=[pd.Timestamp('2026-10-28T09:19:59+08:00'),
                                                     pd.Timestamp('2026-10-28T09:20:00+08:00')]):
            with self.assertRaisesRegex(ValueError, 'IDENTITY_CAPTURE_FINISHED_AFTER_0920'):
                prep.preopen(DAY, output, provider)
        self.assertEqual(provider.calls, [])
        self.assertFalse((output/'pit').exists())

    def candidate_context(self):
        base = self.root/'base.json'
        if not base.exists():
            base.write_text('{}', encoding='utf-8')
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(candidate, 'ROOT', self.root))
        stack.enter_context(patch.object(rq, 'DailyInput', return_value=SimpleNamespace(
            meta=dict(asof='2026-10-27', volume_unit='SHARES'))))
        stack.enter_context(patch.object(rq, 'profile', return_value={}))
        stack.enter_context(patch.object(rq, 'clock', return_value=pd.Timestamp('2026-10-28T16:10:00+08:00')))
        stack.enter_context(patch.object(candidate.local, 'sessions', return_value=['20261027', DAY]))
        return stack, base

    def test_actual_candidate_binds_identity_checkpoint_and_reuse_checks_hash(self):
        output = self.root/'reports/stock'
        provider = Provider()
        def obtain(p, day, folder, **kwargs):
            self.assertEqual(kwargs['deadline'], pd.Timestamp('2026-10-28T17:15:00+08:00'))
            return captured(folder)
        stack, base = self.candidate_context()
        with stack, patch.object(candidate, 'capture_identity', side_effect=obtain) as identity:
            result = candidate.run(DAY, base, output, provider)
            self.assertEqual(identity.call_count, 1)
            calls = list(provider.calls)
            reused = candidate.run(DAY, base, output, provider)
            self.assertTrue(reused['reused_without_download'])
            self.assertEqual(identity.call_count, 1)
            self.assertEqual(provider.calls, calls)
            proof = Path(result['identity_capture']['manifest'])
            self.assertEqual(rq.sha(proof), result['identity_capture']['manifest_sha256'])
            self.assertEqual(result['identity_observed_range'], result['identity_capture']['observed_range'])
            with duckdb.connect(str(output/'increment.duckdb'), read_only=True) as c:
                self.assertEqual(c.execute('SELECT count(*) FROM identity_observed_today').fetchone()[0], 3000)
            proof.write_text('{}', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'EXISTING_IDENTITY_CAPTURE_BINDING_FAILED'):
                candidate.run(DAY, base, output, provider)

    def test_retained_identity_conflict_is_not_silently_overwritten(self):
        output = self.root/'reports/conflict'
        stack, base = self.candidate_context()
        with stack:
            with patch.object(candidate, 'capture_identity', side_effect=lambda p,d,folder,**k: captured(folder)):
                with self.assertRaisesRegex(ValueError, 'FAKE_PRICE_FAILURE_AFTER_IDENTITY'):
                    candidate.run(DAY, base, output, Provider(fail_price=True))
            with patch.object(candidate, 'capture_identity', side_effect=lambda p,d,folder,**k: captured(folder, altered=True)):
                with self.assertRaisesRegex(ValueError, 'IDENTITY_CACHED_CAPTURE_CONFLICT'):
                    candidate.run(DAY, base, output, Provider())
        with duckdb.connect(str(output/'increment.duckdb'), read_only=True) as c:
            value = c.execute("SELECT symbol FROM identity_observed_today WHERE order_book_id='600000.XSHG'").fetchone()[0]
        self.assertEqual(value, 'Fake identity')
        self.assertFalse((output/'manifest.json').exists())

    def test_transport_detail_is_safe_and_not_a_permanent_contract_latch(self):
        error_type = type('HTTPSTransportError', (RuntimeError,), {'__module__': 'core.rqdata_https'})
        with patch.object(daily, 'OPS', self.root/'ops'), patch.object(daily, 'result_files'), \
                patch.object(rq, 'clock', return_value=pd.Timestamp('2026-10-28T16:10:00+08:00')), \
                patch.object(daily, 'run', side_effect=error_type('HTTPS_CONNECT_ConnectionResetError')), \
                patch.object(daily.msvcrt, 'locking'), patch('sys.argv', ['entry', '--phase', 'postclose']), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(daily.main(), 3)
        failure = json.loads((self.root/'ops/20261028_postclose_last_error.json').read_text(encoding='utf-8'))
        self.assertEqual(failure['error'], 'HTTPS_CONNECT_ConnectionResetError')
        self.assertFalse(failure['nonretryable'])


if __name__ == '__main__':
    unittest.main()
