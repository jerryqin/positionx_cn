"""Saved-response provenance only; in-memory DuckDB, no network or real assets."""
import unittest
import duckdb
import pandas as pd
from scripts.prepare_daily_data import merge_benchmark


class SavedBenchmarkObservationTests(unittest.TestCase):
    def test_response_observation_stays_separate_from_research_assembly(self):
        original='2026-10-28T18:04:32.472966+08:00'
        assembled='2026-10-28T19:15:16.790489+08:00'
        frame=pd.DataFrame([dict(order_book_id='000905.XSHG',date=pd.Timestamp('2026-10-28'),
            open=10.,high=11.,low=9.,close=10.5,prev_close=10.)])
        with duckdb.connect(':memory:',config={'temp_directory':''}) as con:
            con.execute("ATTACH ':memory:' AS delta")
            con.register('native',frame)
            con.execute('CREATE TABLE delta.benchmark AS SELECT * FROM native')
            meta=merge_benchmark(con,dict(source='RQDATA',dates=['20261028'],
                database_sha256='SYNTHETIC_SOURCE_HASH',observed_at=assembled,
                benchmark_observed_at=original))
            actual=con.execute('SELECT observed_at,close FROM benchmark').fetchone()
        self.assertEqual(actual,(original,10.5))
        self.assertEqual(meta['observed_at'],original)
        self.assertEqual(meta['date_bindings']['20261028']['observed_at'],original)

if __name__=='__main__':unittest.main()
