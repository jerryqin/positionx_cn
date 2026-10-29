"""Isolated tests; no provider initialization, network, production paths or QMT."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from core import p0_rq_input as rq
from scripts import collect_postclose_candidate as candidate


class CandidateTests(unittest.TestCase):
    def test_supplier_failure_has_exact_stage_before_any_identity_or_price_call(self):
        from types import SimpleNamespace
        problem=TimeoutError('SAFE_TEST_FAILURE')
        out=self.root/'reports'/'candidate'
        (self.root/'source_metadata.json').write_text('{}',encoding='utf-8')
        with patch.object(candidate,'ROOT',self.root), \
                patch.object(candidate.rq,'DailyInput',return_value=SimpleNamespace(meta={'asof':'2026-10-27'})), \
                patch.object(candidate.local,'sessions',return_value=['20261028']), \
                patch.object(candidate.old,'ready_receipt',side_effect=problem), \
                patch.object(candidate,'capture_identity',side_effect=AssertionError('IDENTITY_NOT_ALLOWED')):
            with self.assertRaises(TimeoutError):
                candidate.run('20261028',self.base,out,provider=object(),scope='CORE_MODELS_V1')
        self.assertEqual(candidate.SOURCE_STEP,{'method':'supplier_ready','date':'20261028'})

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / 'base.json'
        self.base.write_text('{}', encoding='utf-8')
        self.out = self.root / 'out'

    def complete(self):
        self.out.mkdir()
        (self.out / 'increment.duckdb').write_bytes(b'ISOLATED_TEST_ONLY')
        value = dict(target_date='20261027', status='PASS_CANDIDATE_NOT_PRODUCTION',
                     base_manifest_sha256=rq.sha(self.base),
                     database_sha256=rq.sha(self.out / 'increment.duckdb'))
        (self.out / 'manifest.json').write_text(json.dumps(value), encoding='utf-8')
        return value

    def test_verified_retry_does_not_initialize_or_download(self):
        self.complete()
        with patch.object(candidate, 'ROOT', self.root.parent), \
             patch.object(candidate.old, 'init_provider', side_effect=AssertionError('NETWORK')):
            # The pure reuse gate runs before any provider is needed.
            actual = candidate.verified_existing(self.out, '20261027', self.base)
        self.assertEqual(actual['status'], 'PASS_CANDIDATE_NOT_PRODUCTION')

    def test_failed_attempt_is_retained(self):
        self.out.mkdir()
        with self.assertRaisesRegex(ValueError, 'INCOMPLETE_ATTEMPT_RETAINED'):
            candidate.verified_existing(self.out, '20261027', self.base)
        self.assertTrue(self.out.exists())

    def test_tampered_candidate_or_wrong_date_is_rejected(self):
        self.complete()
        with self.assertRaisesRegex(ValueError, 'BINDING_FAILED'):
            candidate.verified_existing(self.out, '20261026', self.base)
        (self.out / 'increment.duckdb').write_bytes(b'CHANGED')
        with self.assertRaisesRegex(ValueError, 'BINDING_FAILED'):
            candidate.verified_existing(self.out, '20261027', self.base)

    def test_output_outside_reports_rejected_before_provider(self):
        with patch.object(candidate.old, 'init_provider', side_effect=AssertionError('NETWORK')):
            with self.assertRaisesRegex(ValueError, 'REPOSITORY_REPORTS'):
                candidate.run('20261027', self.base, self.root / 'forbidden')

    def test_source_timeout_is_not_retried_and_identifies_date_and_method(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        method = Mock(side_effect=TimeoutError('SENSITIVE_NOT_LOGGED'))
        with self.assertRaises(TimeoutError):
            candidate.fetch(SimpleNamespace(get_price=method), 'get_price', ['000001.XSHE'],
                            start_date='20261026', end_date='20261027')
        self.assertEqual(method.call_count, 1)
        self.assertEqual(candidate.SOURCE_STEP, dict(method='get_price', start='20261026',
                                                    end='20261027', batch_size=1))

    def test_partial_status_requires_each_session_and_respects_listing_date(self):
        identity = pd.DataFrame({'order_book_id': ['000001.XSHE', '000002.XSHE'],
                                 'listed_date': ['1991-01-01', '2026-10-27']})
        retained = pd.DataFrame({'order_book_id': ['000001.XSHE', '000002.XSHE'],
                                 'date': ['2026-10-26', '2026-10-27']})
        self.assertEqual(candidate.completed_capture_ids(retained, identity, ['20261026', '20261027']),
                         {'000002.XSHE'})

    def test_status_retry_preserves_real_times_but_rejects_fact_conflicts(self):
        previous = pd.DataFrame({'order_book_id': ['000001.XSHE'], 'date': ['2026-10-26'],
                                 'status_suspended': [False], 'price_tick': [0.01],
                                 'status_available_at': ['2026-10-27T16:00:00+08:00']})
        retry = previous.copy()
        retry['status_available_at'] = '2026-10-27T16:01:00+08:00'
        candidate.validate_status_overlap(previous, retry)
        self.assertEqual(previous.iloc[0].status_available_at, '2026-10-27T16:00:00+08:00')
        for field, value in [('status_suspended', True), ('price_tick', 0.02)]:
            changed = retry.copy()
            changed[field] = value
            with self.assertRaisesRegex(rq.InputBlocked, 'CAPTURE_STATUS_KEY_CONFLICT:' + field):
                candidate.validate_status_overlap(previous, changed)

    def test_status_duplicate_keys_are_not_silently_ignored(self):
        rows = pd.DataFrame({'order_book_id': ['000001.XSHE'] * 2,
                             'date': ['2026-10-27'] * 2,
                             'status_suspended': [False, True], 'price_tick': [0.01, 0.01]})
        with self.assertRaisesRegex(rq.InputBlocked, 'CAPTURE_STATUS_DUPLICATE_KEY'):
            candidate.validate_status_overlap(pd.DataFrame(), rows)


if __name__ == '__main__':
    unittest.main()
