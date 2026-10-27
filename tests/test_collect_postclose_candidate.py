"""Isolated tests; no provider initialization, network, production paths or QMT."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core import p0_rq_input as rq
from scripts import collect_postclose_candidate as candidate


class CandidateTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
