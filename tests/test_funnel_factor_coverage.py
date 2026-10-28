"""Actual SQL coverage binding on in-memory DuckDB; no network or production."""
import unittest
import duckdb
from scripts.prepare_daily_data import core_factor_coverage


class FunnelFactorCoverageTests(unittest.TestCase):
    def test_exact_current_capture_binding_and_incomplete_sets(self):
        capture = dict(target_date='20261029', dates=['20261029'],
                       factor_capture_coverage={'order_book_ids': ['OLD_INHERITED']})
        binding = dict(capture_manifest_sha256='owned_manifest', increment_database_sha256='owned_db')
        with duckdb.connect(':memory:', config={'temp_directory': ''}) as con:
            con.execute('CREATE SCHEMA delta')
            con.execute("CREATE TABLE delta.identity_observed_today AS SELECT * FROM (VALUES ('600000.XSHG'),('000001.XSHE')) t(order_book_id)")
            con.execute('CREATE TABLE delta.factor_capture_coverage AS SELECT * FROM delta.identity_observed_today')
            result = core_factor_coverage(con, capture, binding)
            self.assertEqual(result['order_book_ids'], ['000001.XSHE', '600000.XSHG'])
            self.assertEqual(result['dates'], ['20261029'])
            self.assertEqual(result['business_date'], '2026-10-29')
            self.assertEqual(result['capture_manifest_sha256'], 'owned_manifest')
            self.assertEqual(result['increment_database_sha256'], 'owned_db')
            self.assertEqual(result['status'], 'COMPLETE')
            for sql in ["DELETE FROM delta.factor_capture_coverage WHERE order_book_id='000001.XSHE'",
                        "INSERT INTO delta.factor_capture_coverage VALUES ('UNQUALIFIED_EXTRA')",
                        "INSERT INTO delta.factor_capture_coverage SELECT * FROM delta.identity_observed_today LIMIT 1"]:
                con.execute('DELETE FROM delta.factor_capture_coverage')
                con.execute('INSERT INTO delta.factor_capture_coverage SELECT * FROM delta.identity_observed_today')
                con.execute(sql)
                with self.assertRaisesRegex(ValueError, 'INCOMPLETE_OR_CONFLICT'):
                    core_factor_coverage(con, capture, binding)
            with self.assertRaisesRegex(ValueError, 'EXACT_DAY_REQUIRED'):
                core_factor_coverage(con, dict(capture, dates=['20261028','20261029']), binding)


if __name__ == '__main__':
    unittest.main()
