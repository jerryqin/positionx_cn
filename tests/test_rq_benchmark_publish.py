"""Offline publication regression, using only auto-cleaned disposable databases."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import duckdb
import pandas as pd

from core import p0_rq_input as rq
from scripts import prepare_daily_data as prep
from scripts import run_postclose_local_candidate as local


class BenchmarkPublicationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.base = self.root/'base'
        self.src = self.root/'reports/capture'
        self.out = self.root/'reports/TEMP/published'
        self.base.mkdir()
        self.src.mkdir(parents=True)
        with duckdb.connect(str(self.base/'daily.duckdb')) as c:
            c.execute("CREATE TABLE raw_daily AS SELECT '000001.XSHE' AS order_book_id, DATE '2026-10-23' AS date")
            c.execute('CREATE TABLE execution_status AS SELECT *, false AS status_suspended FROM raw_daily')
            c.execute('CREATE TABLE suspension AS SELECT order_book_id,date,status_suspended AS is_suspended FROM execution_status WHERE false')
            c.execute('CREATE TABLE security_identity AS SELECT order_book_id FROM raw_daily')
            c.execute('CREATE TABLE factors(order_book_id VARCHAR,ex_date DATE,ex_cum_factor DOUBLE)')
        meta = dict(source='RQDATA', asof='2026-10-23', availability='2026-10-23T17:00:00+08:00',
                    ingested_at='2026-10-23T16:00:00+08:00', available_at='2026-10-23T17:00:00+08:00',
                    base_manifest_sha256='TEST_OLDER_ANCESTOR_NOT_DIRECT_PARENT',
                    counts=dict(raw_rows=999, symbols=999, appended_rows=999, business_date_symbols=999),
                    version='TEST_BASE', database='daily.duckdb', database_sha256=rq.sha(self.base/'daily.duckdb'),
                    raw_adjustment='none', volume_unit='TEST_ONLY')
        local.write_json(self.base/'source_metadata.json', meta)
        local.write_json(self.base/'manifest.json', dict(files=[dict(path=n,sha256=rq.sha(self.base/n))
                         for n in ('daily.duckdb','source_metadata.json')]))
        local.write_json(self.base/'COMPLETE.json', dict(manifest_sha256=rq.sha(self.base/'manifest.json')))
        with duckdb.connect(str(self.src/'increment.duckdb')) as c:
            c.execute("CREATE TABLE raw_daily AS SELECT '000001.XSHE' AS order_book_id, d AS date FROM (VALUES (DATE '2026-10-26'),(DATE '2026-10-27')) t(d)")
            c.execute('CREATE TABLE execution_status AS SELECT *, false AS status_suspended FROM raw_daily')
            c.execute('CREATE TABLE identity_observed_today AS SELECT DISTINCT order_book_id FROM raw_daily')
            c.execute("""CREATE TABLE benchmark AS SELECT '000905.XSHG' AS order_book_id, date,
                6750.0::DOUBLE AS open,6800.0::DOUBLE AS high,6500.0::DOUBLE AS low,
                CASE WHEN day(date)=26 THEN 6712.7274 ELSE 6511.4183 END::DOUBLE AS close,
                6800.0::DOUBLE AS prev_close FROM raw_daily""")
        self.capture = dict(target_date='20261027', source='RQDATA', status='PASS_CANDIDATE_NOT_PRODUCTION',
                            base_manifest=str(self.base/'manifest.json'), base_manifest_sha256=rq.sha(self.base/'manifest.json'),
                            observed_at='2026-10-27T16:44:27+08:00', dates=['20261026','20261027'],factor_events=0,
                            before={'available_at':'2026-10-27T16:40:00+08:00'},after={'available_at':'2026-10-27T16:44:27+08:00'})
        self.seal_capture()
        for p in (patch.object(prep, 'ROOT', self.root), patch.object(rq, 'profile', return_value={}),
                  patch.object(prep.close.old, 'bind_local_actions', return_value={}),
                  patch.object(prep.close, 'bounded_provider', side_effect=AssertionError('NETWORK'))):
            p.start()
            self.addCleanup(p.stop)

    def seal_capture(self):
        self.capture['database_sha256'] = rq.sha(self.src/'increment.duckdb')
        local.write_json(self.src/'manifest.json', self.capture)

    def merge(self):
        return prep.merge_candidate(self.src, output=self.out)

    def test_publication_real_reader_and_repeated_merge_preserve_raw_points(self):
        before = rq.sha(self.src/'increment.duckdb')
        reader = self.merge()
        rows = reader.benchmark_history('000905.SH', ['20261026','20261027'])
        self.assertEqual(rows.close.tolist(), [6712.7274,6511.4183])
        self.assertEqual(rows.order_book_id.tolist(), ['000905.XSHG']*2)
        self.assertEqual(rows.attrs['price_unit'], 'INDEX_POINTS')
        digest = rq.sha(reader.db)
        self.assertEqual(rq.sha(self.merge().db), digest)
        self.assertEqual(rq.sha(self.src/'increment.duckdb'), before)
        with self.assertRaisesRegex(rq.InputBlocked, 'EXACT_ROWS'):
            reader.benchmark_history('000905.SH', ['20261023'])

    def test_new_version_counts_and_times_do_not_inherit_base_facts(self):
        now = pd.Timestamp('2026-10-28T12:00:00+08:00')
        with patch.object(rq, 'clock', return_value=now):
            reader = self.merge()
        self.assertEqual(reader.meta['counts'], dict(raw_rows=3,symbols=1,appended_rows=2,business_date_symbols=1))
        for name in ('availability','available_at','ingested_at'):
            self.assertEqual(reader.meta[name], now.isoformat())
        self.assertEqual(reader.meta['base_version_metadata']['counts']['raw_rows'], 999)
        self.assertEqual(reader.meta['base_version_metadata']['ingested_at'], '2026-10-23T16:00:00+08:00')
        self.assertEqual(reader.meta['base_manifest_sha256'], self.capture['base_manifest_sha256'])
        self.assertEqual(reader.meta['base_version_metadata']['base_manifest_sha256'],
                         'TEST_OLDER_ANCESTOR_NOT_DIRECT_PARENT')

    def test_bad_benchmark_rolls_back_and_never_seals_partial_stock_rows(self):
        with duckdb.connect(str(self.src/'increment.duckdb')) as c:
            c.execute('UPDATE benchmark SET close=-1')
        self.seal_capture()
        with self.assertRaisesRegex(ValueError, 'BENCHMARK_EXACT_NATIVE_ROWS_INVALID'):
            self.merge()
        self.assertFalse((self.out/'COMPLETE.json').exists())
        with local.read_db(self.out/'daily.duckdb') as c:
            self.assertEqual(c.execute('SELECT count(*) FROM raw_daily').fetchone()[0], 1)
        with self.assertRaises(FileNotFoundError):
            rq.DailyInput(self.out/'manifest.json', {})

    def test_complete_reuse_rejects_another_capture_binding(self):
        self.merge()
        self.capture['observed_at'] = '2026-10-27T17:00:00+08:00'
        self.seal_capture()
        with self.assertRaisesRegex(ValueError, 'MERGE_EXISTING_BINDING_MISMATCH'):
            self.merge()

    def test_benchmark_append_is_idempotent_and_conflict_is_rejected(self):
        with duckdb.connect() as c:
            c.execute("ATTACH '"+str(self.src/'increment.duckdb').replace("'","''")+"' AS delta (READ_ONLY)")
            prep.merge_benchmark(c, self.capture)
            prep.merge_benchmark(c, self.capture)
            self.assertEqual(c.execute('SELECT count(*) FROM benchmark').fetchone()[0], 2)
            c.execute('UPDATE benchmark SET close=close+1')
            with self.assertRaisesRegex(ValueError, 'BENCHMARK_CONFLICT'):
                prep.merge_benchmark(c, self.capture)

    def test_unknown_historical_source_is_not_relabelled(self):
        with duckdb.connect() as c:
            c.execute("ATTACH '"+str(self.src/'increment.duckdb').replace("'","''")+"' AS delta (READ_ONLY)")
            c.execute("CREATE TABLE benchmark AS SELECT * REPLACE(DATE '2026-10-23' AS date) FROM delta.benchmark LIMIT 1")
            prep.merge_benchmark(c, self.capture)
            self.assertEqual(c.execute("SELECT close,source,price_unit FROM benchmark WHERE date='2026-10-23'").fetchone(),
                             (6712.7274,None,None))

    def test_unknown_overlapping_source_and_duplicate_keys_block(self):
        with duckdb.connect() as c:
            c.execute("ATTACH '"+str(self.src/'increment.duckdb').replace("'","''")+"' AS delta (READ_ONLY)")
            c.execute('CREATE TABLE benchmark AS SELECT * FROM delta.benchmark')
            with self.assertRaisesRegex(ValueError, 'BENCHMARK_CONFLICT'):
                prep.merge_benchmark(c, self.capture)
            c.execute('INSERT INTO benchmark BY NAME SELECT * FROM delta.benchmark')
            with self.assertRaisesRegex(ValueError, 'BENCHMARK_DUPLICATE_KEY'):
                prep.merge_benchmark(c, self.capture)

    def test_seal_readback_failure_revokes_complete(self):
        output = self.root/'reports/seal_failure'
        with patch('scripts.postclose_data_admission_v1.audit_stock_asset', return_value={}), \
             patch.object(rq.DailyInput, 'benchmark_history', side_effect=ValueError('READBACK_FAILED')):
            with self.assertRaisesRegex(ValueError, 'READBACK_FAILED'):
                prep.seal('20261027', self.src, output)
        self.assertFalse((output/'TEMP/published/COMPLETE.json').exists())
        self.assertFalse((output/'publication.json').exists())

    def test_daily_append_retains_prior_capture_binding_for_comparison(self):
        db = self.root/'nextday.duckdb'
        increment = self.root/'next_increment.duckdb'
        with duckdb.connect(str(increment)) as c:
            c.execute("ATTACH '"+str(self.src/'increment.duckdb').replace("'","''")+"' AS old (READ_ONLY)")
            c.execute("CREATE TABLE benchmark AS SELECT * REPLACE(DATE '2026-10-28' AS date) FROM old.benchmark LIMIT 1")
        next_capture = dict(self.capture, dates=['20261028'], observed_at='2026-10-28T16:00:00+08:00',
                            database_sha256=rq.sha(increment))
        with duckdb.connect(str(db)) as c:
            c.execute("ATTACH '"+str(self.src/'increment.duckdb').replace("'","''")+"' AS delta (READ_ONLY)")
            binding = prep.merge_benchmark(c, self.capture)
            c.execute('DETACH delta')
            c.execute("ATTACH '"+str(increment).replace("'","''")+"' AS delta (READ_ONLY)")
            binding = prep.merge_benchmark(c, next_capture, binding)
        reader = object.__new__(rq.DailyInput)
        reader.db = db
        reader.meta = dict(asof='2026-10-28', version='TEST_ONLY', benchmark=binding)
        rows = reader.benchmark_history('000905.SH', ['20261027','20261028'])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows.source_database_sha256.tolist(),
                         [self.capture['database_sha256'], next_capture['database_sha256']])


if __name__ == '__main__':
    unittest.main()
